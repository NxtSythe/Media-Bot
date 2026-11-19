"""Rebuild the database from the bot's own old messages.

Runs automatically the first time the bot starts with an empty database,
and on demand with /import-history (safe to run again - nothing is doubled).
"""
from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from core import style
from core.history_parse import APARAT_LIVE_RE, parse_live_embed, parse_log_text, parse_video_embed
from core.perms import AdminCog, is_admin, NO_ACCESS
from core.registry import RegistrationError, valid_minecraft
from core.utils import now_ts

log = logging.getLogger("importer")


# ------------------------------------------------------------------ "send me your info" DM (English)
class InfoModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Your media info", timeout=900)
        self.link = discord.ui.TextInput(max_length=200, required=True,
                                         placeholder="https://www.aparat.com/... or https://www.youtube.com/@...")
        self.mc = discord.ui.TextInput(min_length=3, max_length=20, required=True, placeholder="Steve")
        self.kind = discord.ui.Select(min_values=1, max_values=1, required=True, options=[
            discord.SelectOption(label="Streamer", value="streamer"),
            discord.SelectOption(label="Content Creator", value="creator"),
            discord.SelectOption(label="Both", value="both"),
        ])
        self.add_item(discord.ui.Label(text="Your channel link (YouTube or Aparat)", component=self.link))
        self.add_item(discord.ui.Label(text="Your Minecraft username", component=self.mc))
        self.add_item(discord.ui.Label(text="What do you do?", component=self.kind))

    async def on_submit(self, interaction: discord.Interaction):
        bot = interaction.client
        await interaction.response.defer(thinking=True)
        try:
            info, platform, _, _ = await bot.register_channel(
                interaction.user.id, self.link.value, self.kind.values[0], self.mc.value.strip(),
                allow_unverified=True)
        except RegistrationError as e:
            msg = {"bad_link": "Only YouTube or Aparat links work.",
                   "not_found": "I couldn't find that channel. Check the link and click the button again.",
                   "taken": "That channel is already linked to someone else. Please tell an admin.",
                   "bad_minecraft": "That Minecraft username isn't valid (3-20 characters: letters, numbers, _ or .)."
                   }.get(e.code, "Something went wrong.")
            return await interaction.followup.send(embed=style.error(msg))
        await interaction.followup.send(embed=style.success(
            f"Thanks! Saved **{info['name']}** and Minecraft username **{self.mc.value.strip()}**.", title="Saved"))
        await bot.log(f"<@{interaction.user.id}> sent their info: [{info['name']}]({info['url']}), "
                      f"Minecraft `{self.mc.value.strip()}`", level="success")


class InfoButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lmc:info"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Send my info", style=discord.ButtonStyle.primary,
                                           custom_id="lmc:info"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(InfoModal())


class AskMissingButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lmc:askmissing"):
    """Admin button under the import summary: DM everyone whose info is missing."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="Ask them by DM", style=discord.ButtonStyle.primary,
                                           custom_id="lmc:askmissing"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            return await interaction.response.send_message(embed=style.error(NO_ACCESS), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        cog = interaction.client.get_cog("Importer")
        sent, failed = await cog.ask_missing()
        text = f"Sent a DM to **{len(sent)}** member(s)."
        if failed:
            text += "\nCouldn't DM (DMs closed): " + ", ".join(f"<@{i}>" for i in failed)
        await interaction.followup.send(embed=(style.success if not failed else style.pending)(text), ephemeral=True)


def _view(*items) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for i in items:
        v.add_item(i)
    return v


# ------------------------------------------------------------------ the cog
class Importer(AdminCog, commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self._started = False
        self.lock = asyncio.Lock()
        bot.add_dynamic_items(InfoButton, AskMissingButton)

    @commands.Cog.listener()
    async def on_ready(self):
        if self._started:
            return
        self._started = True
        empty = not any([await self.db.one("SELECT 1 FROM members LIMIT 1"),
                         await self.db.one("SELECT 1 FROM streams LIMIT 1"),
                         await self.db.one("SELECT 1 FROM videos LIMIT 1")])
        if not empty:
            return
        await self.bot.log("The database is empty, so I'm rebuilding it from my old messages. This can take a minute.",
                           level="info")
        try:
            summary = await self.run_import()
        except Exception as e:
            log.exception("import failed")
            return await self.bot.log(f"Rebuilding from old messages failed: `{e}`", level="error")
        ch = await self.bot.text_channel(self.bot.cfg.ch_log)
        if ch:
            embed, view = await self.summary_message(summary)
            await ch.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="import-history",
                          description="Rebuild streams, videos and members from the bot's old messages")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def import_history(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        summary = await self.run_import()
        embed, view = await self.summary_message(summary)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # -------------------------------------------------------------- reading
    async def _read(self, channel_id: int):
        ch = await self.bot.text_channel(channel_id)
        if ch is None:
            return
        async for msg in ch.history(limit=None, oldest_first=True):
            if msg.author.id == self.bot.user.id:
                yield msg

    async def run_import(self) -> dict:
        async with self.lock:
            return await self._run_import()

    async def _run_import(self) -> dict:
        db, cfg = self.db, self.bot.cfg
        deleted = {(r["platform"], r["item_key"]) for r in await db.all("SELECT * FROM deleted_items")}
        streams, videos, regs, mcs = [], [], {}, {}
        last_ts = 0

        async for msg in self._read(cfg.ch_live_history):
            ts = int(msg.created_at.timestamp())
            last_ts = max(last_ts, ts)
            for e in msg.embeds:
                d = parse_live_embed(e.to_dict(), ts)
                if d:
                    d.update(message_id=msg.id, message_channel=msg.channel.id)
                    streams.append(d)
        async for msg in self._read(cfg.ch_video_history):
            ts = int(msg.created_at.timestamp())
            last_ts = max(last_ts, ts)
            for e in msg.embeds:
                d = parse_video_embed(e.to_dict(), ts)
                if d:
                    d.update(message_id=msg.id, message_channel=msg.channel.id,
                             views_updated_at=int((msg.edited_at or msg.created_at).timestamp()))
                    videos.append(d)
        if cfg.ch_log:
            async for msg in self._read(cfg.ch_log):
                texts = [msg.content] + [e.description or "" for e in msg.embeds]
                for t in texts:
                    d = parse_log_text(t or "")
                    if not d:
                        continue
                    d["ts"] = int(msg.created_at.timestamp())
                    if d["type"] == "register":
                        regs[d["discord_id"]] = d
                    if d.get("minecraft") and valid_minecraft(d["minecraft"]):
                        mcs[d["discord_id"]] = d["minecraft"]

        last_ts = last_ts or now_ts()
        added = {"streams": 0, "videos": 0, "members": 0, "channels": 0}

        # ---- streams
        for s in streams:
            key = s["item_key"] or f"import:live:{s['message_id']}"
            if (s["platform"], key) in deleted:
                continue
            if await db.one("SELECT 1 FROM streams WHERE platform = ? AND item_key = ?", (s["platform"], key)):
                continue
            use_id = s["id"] if s["id"] and not await db.one("SELECT 1 FROM streams WHERE id = ?", (s["id"],)) else None
            cols = ("discord_id, channel_name, platform, item_key, url, title, thumbnail, status, started_at, ended_at, "
                    "last_live_at, peak_viewers, server_related, dm_sent, message_id, message_channel, created_at")
            vals = (s["discord_id"], s["channel_name"], s["platform"], key, s["url"], s["title"], s["thumbnail"],
                    "ended", s["started_at"], s["ended_at"], s["ended_at"], s["peak_viewers"], s["server_related"], 1,
                    s["message_id"], s["message_channel"], s["ended_at"])
            if use_id:
                cols, vals = "id, " + cols, (use_id, *vals)
            if await db.insert(f"INSERT OR IGNORE INTO streams({cols}) VALUES({','.join('?' * len(vals))})", vals):
                added["streams"] += 1
            if s["item_key"]:
                await db.mark_seen(s["platform"], [s["item_key"]])

        # ---- videos
        for v in videos:
            key = v["item_key"] or f"import:video:{v['message_id']}"
            if (v["platform"], key) in deleted:
                continue
            if await db.one("SELECT 1 FROM videos WHERE platform = ? AND item_key = ?", (v["platform"], key)):
                continue
            use_id = v["id"] if v["id"] and not await db.one("SELECT 1 FROM videos WHERE id = ?", (v["id"],)) else None
            cols = ("discord_id, channel_name, platform, item_key, url, title, thumbnail, published_at, duration_sec, "
                    "views, server_related, dm_sent, message_id, message_channel, views_updated_at")
            vals = (v["discord_id"], v["channel_name"], v["platform"], key, v["url"], v["title"], v["thumbnail"],
                    v["published_at"], v["duration_sec"], v["views"], v["server_related"], 1, v["message_id"],
                    v["message_channel"], v["views_updated_at"])
            if use_id:
                cols, vals = "id, " + cols, (use_id, *vals)
            if await db.insert(f"INSERT OR IGNORE INTO videos({cols}) VALUES({','.join('?' * len(vals))})", vals):
                added["videos"] += 1
            if v["item_key"]:
                await db.mark_seen(v["platform"], [v["item_key"]])

        # ---- members
        first_seen: dict[int, int] = {}
        has_s, has_v = set(), set()
        for s in streams:
            has_s.add(s["discord_id"])
            first_seen[s["discord_id"]] = min(first_seen.get(s["discord_id"], s["started_at"]), s["started_at"])
        for v in videos:
            has_v.add(v["discord_id"])
            first_seen[v["discord_id"]] = min(first_seen.get(v["discord_id"], v["published_at"]), v["published_at"])
        for did, r in regs.items():
            first_seen[did] = min(first_seen.get(did, r["ts"]), r["ts"])
        for did, since in first_seen.items():
            kind = (regs.get(did) or {}).get("kind") or (
                "both" if did in has_s and did in has_v else "streamer" if did in has_s else "creator")
            if not await db.get_member(did):
                await db.execute("INSERT INTO members(discord_id, kind, added_at) VALUES(?,?,?)", (did, kind, since))
                added["members"] += 1
            if mcs.get(did):
                m = await db.get_member(did)
                if not m.get("mc_username"):
                    await db.set_minecraft(did, mcs[did])

        # ---- channels (added_at = when the old data ends, so anything newer is picked up as new)
        wanted: dict[tuple, dict] = {}
        for did, r in regs.items():
            if r.get("channel_url"):
                wanted.setdefault(("url", r["channel_url"]), {"discord_id": did, "url": r["channel_url"],
                                                              "name": r.get("channel_name")})
        for s in streams:
            if s["platform"] == "aparat" and s.get("aparat_username"):
                wanted.setdefault(("aparat", s["aparat_username"].lower()), {
                    "discord_id": s["discord_id"], "url": f"https://www.aparat.com/{s['aparat_username']}",
                    "name": s["channel_name"]})
        yt_by_member: dict[int, str] = {}
        for item in [*streams, *videos]:
            if item["platform"] == "youtube" and item["item_key"]:
                yt_by_member.setdefault(item["discord_id"], item["url"])
        for did, url in yt_by_member.items():
            wanted.setdefault(("ytvideo", did), {"discord_id": did, "url": url, "name": None})
        aparat_video_members = {v["discord_id"]: v for v in videos if v["platform"] == "aparat"}
        for did, v in aparat_video_members.items():
            if not any(w["discord_id"] == did and "aparat" in w["url"] for w in wanted.values()):
                wanted.setdefault(("apvideo", did), {"discord_id": did, "url": v["url"], "name": v["channel_name"]})

        unresolved = []
        for w in wanted.values():
            info, platform = await self._resolve(w["url"])
            if not info:
                unresolved.append(w)
                continue
            if w.get("name") and info["name"] == info["external_id"]:
                info["name"] = w["name"]
            if await db.one("SELECT 1 FROM channels WHERE platform = ? AND external_id = ?",
                            (platform, info["external_id"])):
                continue
            await db.insert(
                "INSERT INTO channels(discord_id, platform, external_id, name, url, added_at, last_video_check, baselined) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (w["discord_id"], platform, info["external_id"], info["name"], info["url"], last_ts, 0,
                 0 if platform == "aparat" else 1))
            added["channels"] += 1

        await db.set_kv("import_done", str(now_ts()))
        return {"added": added, "found_streams": len(streams), "found_videos": len(videos),
                "unresolved": unresolved, "last_ts": last_ts}

    async def _resolve(self, url: str):
        from platforms.aparat import is_aparat_url
        from platforms.youtube import is_youtube_url
        client = platform = None
        if is_youtube_url(url):
            client, platform = self.bot.youtube, "youtube"
        elif is_aparat_url(url):
            client, platform = self.bot.aparat, "aparat"
            m = APARAT_LIVE_RE.search(url) or re.search(r"aparat\.com/(?!v/)([A-Za-z0-9_.\-]+)/?$", url)
            if m:  # channel/live link: no need to ask Aparat
                u = m.group(1)
                return {"external_id": u, "name": u, "url": f"https://www.aparat.com/{u}"}, platform
        if not client:
            return None, None
        try:
            return await client.resolve(url), platform
        except Exception:
            off = client.offline_resolve(url)
            return (off, platform) if off else (None, None)

    # -------------------------------------------------------------- missing info
    async def missing_members(self) -> list[int]:
        """Media-role members with no linked channel or no Minecraft username."""
        guild = self.bot.guild
        role = guild.get_role(self.bot.cfg.role_media) if guild else None
        if not role:
            return []
        with_channel = {c["discord_id"] for c in await self.db.channels()}
        mc = {m["discord_id"] for m in await self.db.members() if m.get("mc_username")}
        return [m.id for m in role.members if not m.bot and (m.id not in with_channel or m.id not in mc)]

    async def ask_missing(self) -> tuple[list[int], list[int]]:
        sent, failed = [], []
        for did in await self.missing_members():
            try:
                user = self.bot.get_user(did) or await self.bot.fetch_user(did)
                await user.send(embed=style.pending(
                    f"Hi **{self.bot.member_name(did)}**,\n"
                    f"We're rebuilding the {self.bot.cfg.server_name} media list. Please click the button below "
                    f"and send your channel link, your Minecraft username and what you do (streamer / content creator).",
                    title="Please send your media info"), view=_view(InfoButton()))
                sent.append(did)
            except discord.HTTPException:
                failed.append(did)
            await asyncio.sleep(1.5)
        return sent, failed

    async def summary_message(self, summary: dict):
        a = summary["added"]
        missing = await self.missing_members()
        lines = [
            f"Found **{summary['found_streams']}** stream and **{summary['found_videos']}** video messages.",
            f"Added: **{a['streams']}** streams, **{a['videos']}** videos, **{a['members']}** members, "
            f"**{a['channels']}** channels.",
            "",
            "Streams and videos from the time the bot was offline are not in these messages. "
            "Add them with `/stats`.",
        ]
        if summary["unresolved"]:
            lines.append("\nCouldn't work out these channels (link them with `/media add`):")
            lines += [f"> <@{w['discord_id']}>: {w['url']}" for w in summary["unresolved"][:15]]
        view = None
        if missing:
            lines.append(f"\n**{len(missing)}** media member(s) have no channel or no Minecraft username: "
                         + ", ".join(f"<@{i}>" for i in missing[:40]))
            lines.append("Click the button to ask them by DM.")
            view = _view(AskMissingButton())
        maker = style.success if not missing and not summary["unresolved"] else style.pending
        return maker("\n".join(lines), title="Database rebuilt from old messages"), view


async def setup(bot):
    await bot.add_cog(Importer(bot))
