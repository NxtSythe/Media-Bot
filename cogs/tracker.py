"""Background tracking of lives and uploads on YouTube and Aparat."""
from __future__ import annotations

import asyncio
import logging

from discord.ext import commands, tasks

from core.utils import now_ts
from platforms.aparat import AparatError

log = logging.getLogger("tracker")

UPCOMING_MAX_AGE = 45 * 86400


class Tracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.sem = asyncio.Semaphore(4)
        self.ap_sem = asyncio.Semaphore(2)   # be gentle with Aparat
        self._last_youtube = 0
        self._last_aparat = 0
        self._yt_retry: dict[str, int] = {}
        # Aparat health: back off when it's unreachable and send ONE log message instead of spam
        self.ap_down_since: int | None = None
        self.ap_fail_rounds = 0
        self.ap_next_try = 0
        self.ap_alerted = False
        self.ap_last_error = ""

    async def cog_load(self):
        self.poll.start()
        self.views_loop.start()

    async def cog_unload(self):
        self.poll.cancel()
        self.views_loop.cancel()

    # ================================================================ main loop
    @tasks.loop(seconds=30)
    async def poll(self):
        now, cfg, jobs = now_ts(), self.bot.cfg, []
        if now - self._last_youtube >= cfg.live_check_minutes * 60 - 5:
            self._last_youtube = now
            jobs.append(self.poll_youtube)
        if now - self._last_aparat >= cfg.aparat_live_check_seconds - 5 and now >= self.ap_next_try:
            self._last_aparat = now
            jobs.append(self.poll_aparat)
        for fn in jobs:
            try:
                await fn()
            except Exception as e:  # never let the loop die
                log.exception("poll error in %s", fn.__name__)
                await self.bot.log(f"Error while checking channels ({fn.__name__}): `{e}`",
                                   throttle_key=f"poll:{fn.__name__}", level="error")

    @poll.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    # ---------------------------------------------------------------- inserts
    async def _insert_stream(self, c: dict, platform: str, key: str, *, url, title, thumbnail, status,
                             started_at=None, ended_at=None, peak=None, related=None) -> int:
        now = now_ts()
        return await self.db.insert(
            "INSERT OR IGNORE INTO streams(discord_id, channel_row, channel_name, platform, item_key, url, title, "
            "thumbnail, status, started_at, ended_at, last_live_at, peak_viewers, server_related, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["discord_id"], c["id"], c["name"], platform, key, url, title, thumbnail, status,
             started_at, ended_at, now if status == "live" else None, peak, related, now),
        )

    async def _insert_video(self, c: dict, platform: str, key: str, *, url, title, thumbnail, published_at,
                            duration, views, related) -> int:
        now = now_ts()
        return await self.db.insert(
            "INSERT OR IGNORE INTO videos(discord_id, channel_row, channel_name, platform, item_key, url, title, "
            "thumbnail, published_at, duration_sec, views, server_related, views_updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["discord_id"], c["id"], c["name"], platform, key, url, title, thumbnail, published_at,
             duration, views, related, now),
        )

    async def _after_video(self, vid: int, related) -> None:
        if not vid:
            return
        await self.bot.announce_video(vid)
        if related is None:
            await self.bot.ask_confirmation("video", vid)

    async def _end_stream(self, s: dict, ended_at: int, **extra) -> None:
        await self.db.update("streams", s["id"], status="ended", ended_at=ended_at, **extra)
        await self.bot.announce_stream(s["id"])
        fresh = await self.db.one("SELECT * FROM streams WHERE id = ?", (s["id"],))
        if fresh and fresh["server_related"] is None and not fresh["dm_sent"]:
            await self.bot.ask_confirmation("stream", s["id"], live_now=False)

    # ================================================================ YouTube
    async def poll_youtube(self):
        yt = self.bot.youtube
        if not yt.enabled:
            return
        channels = await self.db.channels("youtube")

        async def discover(c):
            async with self.sem:
                ids = await yt.rss_ids(c["external_id"])
                live_id = await yt.live_page_id(c["external_id"])
            if live_id and live_id not in ids:
                ids.append(live_id)
            return c, ids

        results = await asyncio.gather(*(discover(c) for c in channels), return_exceptions=True)
        candidates: dict[str, dict] = {}
        for res in results:
            if isinstance(res, Exception):
                log.warning("youtube discover failed: %s", res)
                continue
            c, ids = res
            for vid in ids:
                if vid not in candidates and not await self.db.is_seen("youtube", vid):
                    candidates[vid] = c

        active = await self.db.all(
            "SELECT * FROM streams WHERE platform = 'youtube' AND status IN ('upcoming', 'live')")
        fetch = list(candidates) + [s["item_key"] for s in active]
        if not fetch:
            return
        details = await yt.videos(fetch)

        active_keys = {s["item_key"] for s in active}
        for vid, c in candidates.items():
            if vid not in details:  # couldn't check it right now - try again next round
                self._yt_retry[vid] = self._yt_retry.get(vid, 0) + 1
                if self._yt_retry[vid] < 30:
                    continue
            info = details.get(vid)
            if info and info["channel_id"] == c["external_id"] and vid not in active_keys:
                try:
                    await self._new_youtube_item(c, info)
                except Exception:
                    log.exception("new youtube item %s", vid)
            self._yt_retry.pop(vid, None)
            await self.db.mark_seen("youtube", [vid])

        for s in active:
            if s["item_key"] not in details:
                continue  # unknown right now, keep the current state
            try:
                await self._update_youtube_stream(s, details.get(s["item_key"]))
            except Exception:
                log.exception("update youtube stream %s", s["id"])

    async def _new_youtube_item(self, c: dict, info: dict):
        now = now_ts()
        related = self.bot.is_server_related(info["title"], info["description"])
        common = dict(url=info["url"], title=info["title"], thumbnail=info["thumbnail"])

        if info["is_stream"]:
            if info["state"] == "ended":
                # finished while the bot was offline - record it if it's newer than the channel link
                if (info["actual_start"] or 0) < c["added_at"]:
                    return
                sid = await self._insert_stream(c, "youtube", info["id"], status="ended",
                                                started_at=info["actual_start"], ended_at=info["actual_end"],
                                                related=related, **common)
                if sid:
                    await self.bot.announce_stream(sid)
                    if related is None:
                        await self.bot.ask_confirmation("stream", sid, live_now=False)
            elif info["state"] == "live":
                sid = await self._insert_stream(c, "youtube", info["id"], status="live",
                                                started_at=info["actual_start"] or now,
                                                peak=info["concurrent"], related=related, **common)
                if sid and related is None:
                    await self.bot.ask_confirmation("stream", sid, live_now=True)
            else:  # upcoming / scheduled
                await self._insert_stream(c, "youtube", info["id"], status="upcoming", related=related, **common)
            return

        # regular upload (or premiere)
        if (info["published_at"] or now) < c["added_at"] - 3600:
            return  # old video, not new
        vid = await self._insert_video(c, "youtube", info["id"], published_at=info["published_at"] or now,
                                       duration=info["duration"], views=info["views"], related=related, **common)
        await self._after_video(vid, related)

    async def _update_youtube_stream(self, s: dict, info: dict | None):
        now = now_ts()
        if info is None:  # deleted / made private
            if s["status"] == "live":
                await self._end_stream(s, s["last_live_at"] or now)
            else:
                await self.db.execute("DELETE FROM streams WHERE id = ?", (s["id"],))
            return

        fields = {}
        if info["title"] and info["title"] != s["title"]:
            fields["title"] = info["title"]
        if s["server_related"] is None and self.bot.is_server_related(info["title"], info["description"]):
            fields["server_related"] = 1

        if info["state"] == "upcoming":
            if now - s["created_at"] > UPCOMING_MAX_AGE:
                await self.db.execute("DELETE FROM streams WHERE id = ?", (s["id"],))
                return
            if fields:
                await self.db.update("streams", s["id"], **fields)
            return

        if info["state"] == "live":
            became_live = s["status"] == "upcoming"
            if became_live:
                fields.update(status="live", started_at=info["actual_start"] or now)
            if info["concurrent"] is not None:
                fields["peak_viewers"] = max(s["peak_viewers"] or 0, info["concurrent"])
            fields["last_live_at"] = now
            await self.db.update("streams", s["id"], **fields)
            if became_live and s["server_related"] is None and "server_related" not in fields:
                await self.bot.ask_confirmation("stream", s["id"], live_now=True)
            return

        # ended (or turned into a normal video)
        started = info["actual_start"] or s["started_at"] or s["created_at"]
        ended = info["actual_end"] or s["last_live_at"] or now
        await self._end_stream(s, ended, started_at=started, **fields)

    # ================================================================ Aparat
    async def poll_aparat(self):
        channels = await self.db.channels("aparat")
        if not channels:
            return
        active = {s["channel_row"]: s for s in await self.db.all(
            "SELECT * FROM streams WHERE platform = 'aparat' AND status = 'live'")}
        outcome = {"ok": 0, "down": 0}

        async def guarded(c, fn, what):
            try:
                await fn()
                outcome["ok"] += 1
            except AparatError as e:
                if e.kind == "unreachable":
                    outcome["down"] += 1
                    self.ap_last_error = str(e)
                else:
                    outcome["ok"] += 1  # Aparat answered; this channel just has a problem
                    await self.bot.log(
                        f"Aparat {what} check for `{c['external_id']}` failed: {e}\n"
                        f"Check the link with `/media list` or run `/debug aparat {c['external_id']}`.",
                        throttle_key=f"ap{what}:{c['id']}", every=24 * 3600)
            except Exception:
                log.exception("aparat %s %s", what, c["external_id"])

        async def run(c):
            async with self.ap_sem:
                await guarded(c, lambda: self._aparat_live(c, active.get(c["id"])), "live")
                if outcome["down"] and not outcome["ok"]:
                    return  # Aparat looks down - don't pile on more requests
                await guarded(c, lambda: self._aparat_videos(c), "video")
                await asyncio.sleep(0.5)

        await asyncio.gather(*(run(c) for c in channels))
        await self._aparat_health(outcome)

    async def _aparat_health(self, outcome: dict):
        now = now_ts()
        if outcome["down"] and not outcome["ok"]:
            # every request failed -> Aparat is unreachable from this server right now
            self.ap_fail_rounds += 1
            self.ap_down_since = self.ap_down_since or now
            backoff = min(30 * 60, 60 * 2 ** min(self.ap_fail_rounds - 1, 5))
            self.ap_next_try = now + backoff
            log.warning("Aparat unreachable (%s), next try in %ss", self.ap_last_error, backoff)
            if not self.ap_alerted and now - self.ap_down_since >= 30 * 60:
                self.ap_alerted = True
                await self.bot.log(
                    "Aparat hasn't been reachable from this server for 30 minutes, so Aparat streams and videos "
                    "can't be tracked right now. The bot keeps retrying on its own and will catch up when "
                    "Aparat is back.\n\n"
                    f"Last error: `{self.ap_last_error[:300]}`\n\n"
                    "If this keeps happening, Aparat is probably blocking this server's location. "
                    "Set `aparat: proxy:` in config.yml to an HTTP proxy on a server in Iran.",
                    throttle_key="aparat-down", every=12 * 3600, level="warning")
            return
        if outcome["ok"]:
            if self.ap_alerted:
                minutes = (now - (self.ap_down_since or now)) // 60
                await self.bot.log(f"Aparat is reachable again (it was down for about {minutes} minutes).",
                                   level="success")
            self.ap_down_since, self.ap_fail_rounds, self.ap_next_try, self.ap_alerted = None, 0, 0, False

    async def _aparat_live(self, c: dict, s: dict | None):
        now = now_ts()
        live = await self.bot.aparat.live_status(c["external_id"])
        session_key = str(live["session_start_raw"] or live["start_raw"] or "")

        if live["live"]:
            peak = max(x for x in (live["peak"], live["online"], (s or {}).get("peak_viewers"), 0) if x is not None)
            if s is None:
                started = now
                if live["session_start"] and 0 <= now - live["session_start"] <= 15 * 60:
                    started = live["session_start"]
                related = self.bot.is_server_related(live["title"], live["description"])
                sid = await self._insert_stream(c, "aparat", f"{c['external_id']}:{started}", status="live",
                                                url=live["url"], title=live["title"], thumbnail=live["thumbnail"],
                                                started_at=started, peak=peak or None, related=related)
                await self.db.execute("UPDATE channels SET last_session_key = ? WHERE id = ?", (session_key, c["id"]))
                if sid and related is None:
                    await self.bot.ask_confirmation("stream", sid, live_now=True)
            else:
                fields = dict(last_live_at=now, peak_viewers=peak or s["peak_viewers"])
                if live["title"] and live["title"] != s["title"]:
                    fields["title"] = live["title"]
                if s["server_related"] is None and self.bot.is_server_related(live["title"], live["description"]):
                    fields["server_related"] = 1
                await self.db.update("streams", s["id"], **fields)
            return

        # ---- not live right now
        if s is not None:
            last_seen = s["last_live_at"] or s["started_at"]
            end_reported = live["session_end"]
            if end_reported and not (s["started_at"] <= end_reported <= now + 60):
                end_reported = None
            if end_reported or now - last_seen >= self.bot.cfg.aparat_end_grace_minutes * 60:
                await self._end_stream(s, end_reported or last_seen)
                await self.db.execute("UPDATE channels SET last_session_key = ? WHERE id = ?",
                                      (session_key, c["id"]))
            return

        # ---- catch streams that started and ended between two checks
        st, en = live["session_start"], live["session_end"]
        if not (st and en and en > st and st >= c["added_at"] and en <= now + 60):
            return
        if session_key and session_key == (c["last_session_key"] or ""):
            return
        await self.db.execute("UPDATE channels SET last_session_key = ? WHERE id = ?", (session_key, c["id"]))
        overlap = await self.db.one(
            "SELECT id FROM streams WHERE platform = 'aparat' AND channel_row = ? AND started_at <= ? "
            "AND COALESCE(ended_at, last_live_at, started_at) >= ?", (c["id"], en + 600, st - 600))
        if overlap:
            return
        related = self.bot.is_server_related(live["title"], live["description"])
        sid = await self._insert_stream(c, "aparat", f"{c['external_id']}:{st}", status="ended",
                                        url=live["url"], title=live["title"], thumbnail=live["thumbnail"],
                                        started_at=st, ended_at=en, peak=live["peak"] or None, related=related)
        if sid:
            await self.bot.announce_stream(sid)
            if related is None:
                await self.bot.ask_confirmation("stream", sid, live_now=False)

    async def _aparat_videos(self, c: dict):
        now = now_ts()
        if now - (c["last_video_check"] or 0) < self.bot.cfg.video_check_minutes * 60:
            return
        await self.db.execute("UPDATE channels SET last_video_check = ? WHERE id = ?", (now, c["id"]))
        vids = await self.bot.aparat.user_videos(c["external_id"])
        if not c.get("baselined", 1):
            # channel restored from old messages: what it has now is old, just remember it
            await self.db.mark_seen("aparat", [v["uid"] for v in vids])
            await self.db.execute("UPDATE channels SET baselined = 1 WHERE id = ?", (c["id"],))
            return
        for v in reversed(vids):  # oldest first
            if await self.db.is_seen("aparat", v["uid"]):
                continue
            detail = await self.bot.aparat.video(v["uid"]) or {}
            info = {**v, **{k: val for k, val in detail.items() if val not in (None, "")}}
            related = self.bot.is_server_related(info["title"], info.get("description"))
            vid = await self._insert_video(c, "aparat", info["uid"], url=info["url"], title=info["title"],
                                           thumbnail=info.get("thumbnail"), published_at=now,
                                           duration=info.get("duration"), views=info.get("views"),
                                           related=related)
            await self.db.mark_seen("aparat", [info["uid"]])
            await self._after_video(vid, related)

    # ================================================================ daily view updates
    @tasks.loop(minutes=30)
    async def views_loop(self):
        try:
            await self.update_views()
        except Exception as e:
            log.exception("views update failed")
            await self.bot.log(f"Error while updating video views: `{e}`", throttle_key="views", level="error")

    @views_loop.before_loop
    async def _before_views(self):
        await self.bot.wait_until_ready()

    async def update_views(self):
        cfg = self.bot.cfg
        now = now_ts()
        sql = "SELECT * FROM videos WHERE views_updated_at <= ?"
        params: list = [now - int(cfg.views_update_hours * 3600)]
        if cfg.views_update_max_days > 0:
            sql += " AND published_at >= ?"
            params.append(now - cfg.views_update_max_days * 86400)
        rows = await self.db.all(sql, tuple(params))
        if not rows:
            return

        yt_rows = [r for r in rows if r["platform"] == "youtube"]
        details = {}
        if yt_rows:
            details = await self.bot.youtube.videos([r["item_key"] for r in yt_rows])

        for r in rows:
            fields = {"views_updated_at": now}
            if r["platform"] == "youtube":
                if r["item_key"] not in details:  # couldn't check now, retry in about an hour
                    fields["views_updated_at"] = now - int(cfg.views_update_hours * 3600) + 3600
                info = details.get(r["item_key"])
                if info:
                    fields.update(views=info["views"] if info["views"] is not None else r["views"],
                                  title=info["title"] or r["title"])
                    if r["server_related"] is None and self.bot.is_server_related(info["title"], info["description"]):
                        fields["server_related"] = 1
            else:
                if now < self.ap_next_try:
                    continue  # Aparat is down, try later
                async with self.ap_sem:
                    info = await self.bot.aparat.video(r["item_key"])
                if info:
                    fields.update(views=info["views"] if info["views"] is not None else r["views"],
                                  title=info["title"] or r["title"])
                    if r["server_related"] is None and self.bot.is_server_related(info["title"], info["description"]):
                        fields["server_related"] = 1
            await self.db.update("videos", r["id"], **fields)
            await self.bot.announce_video(r["id"], create=False)
            await asyncio.sleep(1.2)  # be gentle with Discord rate limits


async def setup(bot):
    await bot.add_cog(Tracker(bot))
