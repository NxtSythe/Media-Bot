"""/stats - see and edit a media member's activity (admins only)."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Optional

import discord
import jdatetime
from discord import app_commands
from discord.ext import commands

from core import style
from core.activity import evaluate_member, member_lines
from core.history_parse import en
from core.perms import AdminCog, NO_ACCESS, is_admin
from core.registry import valid_minecraft
from core.texts import KIND_NAME, PLATFORM_NAME, related_label
from core.utils import (chunk_lines, current_jalali_month, fmt_duration, jalali, jalali_month_range, jdate, jtime,
                        month_label, now_ts, previous_jalali_month)
from core import utils

log = logging.getLogger("stats")


def display_name(bot, discord_id: int) -> str:
    g = bot.guild
    m = g.get_member(discord_id) if g else None
    if m:
        return m.display_name
    u = bot.get_user(discord_id)
    return u.name if u else str(discord_id)


# ------------------------------------------------------------------ input parsing
def parse_date(text: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"\s*(\d{4})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*", en(text))
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    try:
        jdatetime.date(y, mo, d)
    except ValueError:
        return None
    return y, mo, d


def parse_time(text: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*", en(text or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        return None
    return int(m.group(1)), int(m.group(2) or 0)


def parse_minutes(text: str) -> int | None:
    """'120' minutes, '2:30' (h:mm for streams / m:ss handled by caller) -> seconds."""
    s = en(text or "").strip()
    if re.fullmatch(r"\d+", s):
        return int(s) * 60
    m = re.fullmatch(r"(\d+):(\d{1,2})", s)
    return (int(m.group(1)) * 60 + int(m.group(2))) * 60 if m else None


def parse_video_length(text: str) -> int | None:
    """'8' minutes or '8:30' (m:ss) -> seconds."""
    s = en(text or "").strip()
    if re.fullmatch(r"\d+", s):
        return int(s) * 60
    m = re.fullmatch(r"(\d+):(\d{1,2})", s)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def to_ts(date: tuple[int, int, int], time: tuple[int, int]) -> int:
    g = jdatetime.datetime(*date, *time).togregorian()
    return int(datetime(g.year, g.month, g.day, g.hour, g.minute, tzinfo=utils.TZ).timestamp())


def jdate_input(ts: int) -> str:
    j = jalali(ts)
    return f"{j.year}/{j.month:02d}/{j.day:02d}"


RELATED_OPTIONS = [("Server-related (counts)", "1"), ("Not server-related (doesn't count)", "0")]


def related_select(default: int | None = 1) -> discord.ui.Select:
    return discord.ui.Select(min_values=1, max_values=1, required=True, options=[
        discord.SelectOption(label=l, value=v, default=(str(default) == v)) for l, v in RELATED_OPTIONS])


# ------------------------------------------------------------------ modals
class StreamModal(discord.ui.Modal):
    def __init__(self, panel: "StatsPanel", stream: dict | None = None):
        super().__init__(title="Edit stream" if stream else "Add stream", timeout=900)
        self.panel, self.stream = panel, stream
        now = now_ts()
        start = stream["started_at"] if stream else now
        dur = ((stream["ended_at"] or start) - start) // 60 if stream else 120
        self.date = discord.ui.TextInput(default=jdate_input(start), max_length=10)
        self.time = discord.ui.TextInput(default=jtime(start), max_length=5)
        self.length = discord.ui.TextInput(default=str(dur), max_length=6)
        self.peak = discord.ui.TextInput(required=False, max_length=7,
                                         default=str(stream["peak_viewers"]) if stream and stream["peak_viewers"] else None)
        self.related = related_select(stream["server_related"] if stream else 1)
        self.add_item(discord.ui.Label(text="Date (Jalali)", description="Example: 1405/08/27", component=self.date))
        self.add_item(discord.ui.Label(text="Start time", description="24-hour, e.g. 20:30", component=self.time))
        self.add_item(discord.ui.Label(text="Length in minutes", description="e.g. 135 (or 2:15)",
                                       component=self.length))
        self.add_item(discord.ui.Label(text="Peak viewers (optional)", component=self.peak))
        self.add_item(discord.ui.Label(text="Counts for activity?", component=self.related))

    async def on_submit(self, interaction: discord.Interaction):
        date, time, secs = parse_date(self.date.value), parse_time(self.time.value), parse_minutes(self.length.value)
        if not date or not time or not secs:
            return await interaction.response.send_message(embed=style.error(
                "Check the values: date like 1405/08/27, time like 20:30, length in minutes."), ephemeral=True)
        peak = int(en(self.peak.value)) if en(self.peak.value).strip().isdigit() else None
        start = to_ts(date, time)
        related = int(self.related.values[0])
        db, bot = self.panel.bot.db, self.panel.bot
        if self.stream:
            await db.update("streams", self.stream["id"], started_at=start, ended_at=start + secs,
                            last_live_at=start + secs, peak_viewers=peak, server_related=related)
            await bot.announce_stream(self.stream["id"], create=False)
            note = f"Stream `{self.stream['id']}` updated."
        else:
            ch = await self.panel.first_channel()
            sid = await db.insert(
                "INSERT INTO streams(discord_id, channel_row, channel_name, platform, item_key, title, status, started_at, "
                "ended_at, last_live_at, peak_viewers, server_related, dm_sent, created_at, manual) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (self.panel.member_id, ch["id"] if ch else None, ch["name"] if ch else self.panel.name(),
                 ch["platform"] if ch else "manual", f"manual:{uuid.uuid4().hex[:12]}", "Added by admin", "ended",
                 start, start + secs, start + secs, peak, related, 1, now_ts()))
            note = f"Stream `{sid}` added."
        await self.panel.after_edit(interaction, note, month_of=start)


class VideoModal(discord.ui.Modal):
    def __init__(self, panel: "StatsPanel", video: dict | None = None):
        super().__init__(title="Edit video" if video else "Add video", timeout=900)
        self.panel, self.video = panel, video
        pub = video["published_at"] if video else now_ts()
        length = video["duration_sec"] if video else None
        self.date = discord.ui.TextInput(default=jdate_input(pub), max_length=10)
        self.title_in = discord.ui.TextInput(default=(video["title"] if video else None), max_length=200)
        self.length = discord.ui.TextInput(max_length=7, default=(f"{length // 60}:{length % 60:02d}" if length else None))
        self.link = discord.ui.TextInput(required=False, max_length=200, default=(video["url"] if video else None))
        self.related = related_select(video["server_related"] if video else 1)
        self.add_item(discord.ui.Label(text="Upload date (Jalali)", description="Example: 1405/08/27",
                                       component=self.date))
        self.add_item(discord.ui.Label(text="Title", component=self.title_in))
        self.add_item(discord.ui.Label(text="Length", description="Minutes, or m:ss like 8:30", component=self.length))
        self.add_item(discord.ui.Label(text="Link (optional)", component=self.link))
        self.add_item(discord.ui.Label(text="Counts for activity?", component=self.related))

    async def on_submit(self, interaction: discord.Interaction):
        date, secs = parse_date(self.date.value), parse_video_length(self.length.value)
        if not date or not secs:
            return await interaction.response.send_message(embed=style.error(
                "Check the values: date like 1405/08/27, length like 8 or 8:30."), ephemeral=True)
        pub = to_ts(date, (12, 0))
        related = int(self.related.values[0])
        db, bot = self.panel.bot.db, self.panel.bot
        if self.video:
            await db.update("videos", self.video["id"], published_at=pub, title=self.title_in.value,
                            duration_sec=secs, url=self.link.value or self.video["url"], server_related=related)
            await bot.announce_video(self.video["id"], create=False)
            note = f"Video `{self.video['id']}` updated."
        else:
            ch = await self.panel.first_channel()
            vid = await db.insert(
                "INSERT INTO videos(discord_id, channel_row, channel_name, platform, item_key, url, title, published_at, "
                "duration_sec, server_related, dm_sent, views_updated_at, manual) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (self.panel.member_id, ch["id"] if ch else None, ch["name"] if ch else self.panel.name(),
                 ch["platform"] if ch else "manual", f"manual:{uuid.uuid4().hex[:12]}", self.link.value or None,
                 self.title_in.value, pub, secs, related, 1, 2 ** 31 - 1))
            note = f"Video `{vid}` added."
        await self.panel.after_edit(interaction, note, month_of=pub)


class MinecraftEditModal(discord.ui.Modal):
    def __init__(self, panel: "StatsPanel", current: str | None):
        super().__init__(title="Minecraft username", timeout=900)
        self.panel = panel
        self.name = discord.ui.TextInput(min_length=3, max_length=20, default=current)
        self.add_item(discord.ui.Label(text="Minecraft username", component=self.name))

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name.value.strip()
        if not valid_minecraft(name):
            return await interaction.response.send_message(embed=style.error(
                "Not a valid Minecraft username (3-20 characters: letters, numbers, _ or .)."), ephemeral=True)
        await self.panel.bot.db.set_minecraft(self.panel.member_id, name)
        await self.panel.after_edit(interaction, f"Minecraft username set to `{name}`.")


# ------------------------------------------------------------------ item picker (edit / remove)
class PickView(discord.ui.View):
    def __init__(self, panel: "StatsPanel", kind: str, action: str, rows: list[dict]):
        super().__init__(timeout=600)
        self.panel, self.kind, self.action = panel, kind, action
        self.rows = {str(r["id"]): r for r in rows}
        opts = []
        for r in rows[-25:]:
            if kind == "stream":
                label = f"#{r['id']}  {jdate(r['started_at'])} {jtime(r['started_at'])}  " \
                        f"{fmt_duration((r['ended_at'] or r['started_at']) - r['started_at'])}"
            else:
                label = f"#{r['id']}  {jdate(r['published_at'])}  {(r['title'] or '-')}"
            opts.append(discord.SelectOption(label=label[:100], value=str(r["id"])))
        sel = discord.ui.Select(placeholder=f"Pick the {kind} to {action}", options=opts)
        sel.callback = self.picked
        self.sel = sel
        self.add_item(sel)

    async def picked(self, interaction: discord.Interaction):
        row = self.rows[self.sel.values[0]]
        if self.action == "edit":
            modal = StreamModal(self.panel, row) if self.kind == "stream" else VideoModal(self.panel, row)
            return await interaction.response.send_modal(modal)
        table = "streams" if self.kind == "stream" else "videos"
        db = self.panel.bot.db
        await db.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
        await db.execute("INSERT OR IGNORE INTO deleted_items(platform, item_key) VALUES(?, ?)",
                         (row["platform"], row["item_key"]))
        await interaction.response.edit_message(embed=style.success(
            f"{self.kind.capitalize()} `{row['id']}` removed."), view=None)
        await self.panel.refresh()


class KindView(discord.ui.View):
    def __init__(self, panel: "StatsPanel", current: str):
        super().__init__(timeout=600)
        self.panel = panel
        sel = discord.ui.Select(placeholder="Pick the type", options=[
            discord.SelectOption(label=v, value=k, default=k == current) for k, v in KIND_NAME.items()])
        sel.callback = self.picked
        self.sel = sel
        self.add_item(sel)

    async def picked(self, interaction: discord.Interaction):
        kind = self.sel.values[0]
        await self.panel.bot.db.execute("UPDATE members SET kind = ? WHERE discord_id = ?", (kind, self.panel.member_id))
        await interaction.response.edit_message(embed=style.success(f"Type set to **{KIND_NAME[kind]}**."), view=None)
        await self.panel.refresh()


# ------------------------------------------------------------------ the panel
class StatsPanel(discord.ui.View):
    def __init__(self, bot, origin: discord.Interaction, member_id: int, jy: int, jm: int):
        super().__init__(timeout=14 * 60)
        self.bot, self.origin, self.member_id, self.jy, self.jm = bot, origin, member_id, jy, jm

    def name(self) -> str:
        return display_name(self.bot, self.member_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.user):
            await interaction.response.send_message(embed=style.error(NO_ACCESS), ephemeral=True)
            return False
        return True

    async def first_channel(self) -> dict | None:
        chans = await self.bot.db.channels(discord_id=self.member_id)
        return chans[0] if chans else None

    async def month_rows(self):
        db = self.bot.db
        ms, me = jalali_month_range(self.jy, self.jm)
        streams = await db.all("SELECT * FROM streams WHERE discord_id = ? AND status = 'ended' "
                               "AND started_at >= ? AND started_at < ? ORDER BY started_at",
                               (self.member_id, ms - 7 * 86400, me))
        videos = await db.all("SELECT * FROM videos WHERE discord_id = ? AND published_at >= ? AND published_at < ? "
                              "ORDER BY published_at", (self.member_id, ms, me))
        return streams, videos, ms, me

    async def build(self) -> list[discord.Embed]:
        db = self.bot.db
        m = await db.get_member(self.member_id)
        if not m:
            return [style.error("This user is not in the media list anymore.")]
        streams, videos, ms, me = await self.month_rows()
        r = evaluate_member(m, streams, videos, self.jy, self.jm, self.bot.cfg)
        in_month = [s for s in streams if ms <= s["started_at"] < me]
        valid_s = [s for s in in_month if s["server_related"] == 1]
        live_time = sum((s["ended_at"] or s["started_at"]) - s["started_at"] for s in in_month)

        chans = await db.channels(discord_id=self.member_id)
        lines = [
            f"<@{self.member_id}> | Minecraft: {('`' + m['mc_username'] + '`') if m.get('mc_username') else '*not set*'}",
            f"Type: **{KIND_NAME.get(m['kind'], m['kind'])}**",
            "Channels: " + (", ".join(f"[{c['name']}]({c['url']}) ({PLATFORM_NAME.get(c['platform'], c['platform'])})"
                                      for c in chans) or "*none*"),
            "",
            f"**This month:** {len(in_month)} streams ({len(valid_s)} server-related), "
            f"total live time {fmt_duration(live_time)}, {len(videos)} videos",
        ]
        if r.skipped_new:
            lines.append("Added during this month, so the report doesn't review this month.")
        lines += member_lines(r, self.bot.cfg, only_failures=False)
        if in_month:
            lines.append("\n**Streams**")
            for s in in_month[-20:]:
                tag = " | manual" if s.get("manual") else ""
                lines.append(f"`{s['id']}` {jdate(s['started_at'])} {jtime(s['started_at'])} | "
                             f"{fmt_duration((s['ended_at'] or s['started_at']) - s['started_at'])} | "
                             f"peak {s['peak_viewers'] if s['peak_viewers'] is not None else '-'} | "
                             f"{related_label(s['server_related'])}{tag}")
        if videos:
            lines.append("\n**Videos**")
            for v in videos[-20:]:
                tag = " | manual" if v.get("manual") else ""
                title = (v["title"] or "-")[:40]
                title = f"[{title}]({v['url']})" if v.get("url") else title
                lines.append(f"`{v['id']}` {jdate(v['published_at'])} | {title} | "
                             f"{fmt_duration(v['duration_sec'], with_seconds=True)} | "
                             f"{related_label(v['server_related'])}{tag}")
        maker = style.pending if (r.skipped_new or not r.month_finished) else (style.error if r.failed else style.success)
        if r.weeks and not r.month_finished and r.stream_fail:
            maker = style.error
        embeds = [maker(b) for b in chunk_lines(lines)][:9]
        embeds[0].title = f"Stats - {self.name()} - {month_label(self.jy, self.jm)}"
        embeds[-1].set_footer(text="Buttons work for 14 minutes. Run /stats again after that.")
        return embeds

    async def refresh(self):
        try:
            await self.origin.edit_original_response(embeds=await self.build(), view=self)
        except discord.HTTPException as e:
            log.warning("stats refresh failed: %s", e)

    async def after_edit(self, interaction: discord.Interaction, note: str, month_of: int | None = None):
        if month_of:
            j = jalali(month_of)
            self.jy, self.jm = j.year, j.month
        await interaction.response.send_message(embed=style.success(note), ephemeral=True)
        await self.refresh()

    # ---- row 0: navigation
    @discord.ui.button(label="Previous month", style=discord.ButtonStyle.secondary, row=0)
    async def prev(self, interaction: discord.Interaction, _):
        self.jy, self.jm = previous_jalali_month(self.jy, self.jm)
        await interaction.response.edit_message(embeds=await self.build(), view=self)

    @discord.ui.button(label="Next month", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, _):
        self.jy, self.jm = (self.jy + 1, 1) if self.jm == 12 else (self.jy, self.jm + 1)
        await interaction.response.edit_message(embeds=await self.build(), view=self)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=0)
    async def reload(self, interaction: discord.Interaction, _):
        await interaction.response.edit_message(embeds=await self.build(), view=self)

    # ---- row 1: streams
    @discord.ui.button(label="Add stream", style=discord.ButtonStyle.success, row=1)
    async def add_stream(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(StreamModal(self))

    @discord.ui.button(label="Edit stream", style=discord.ButtonStyle.primary, row=1)
    async def edit_stream(self, interaction: discord.Interaction, _):
        await self._pick(interaction, "stream", "edit")

    @discord.ui.button(label="Remove stream", style=discord.ButtonStyle.danger, row=1)
    async def remove_stream(self, interaction: discord.Interaction, _):
        await self._pick(interaction, "stream", "remove")

    # ---- row 2: videos
    @discord.ui.button(label="Add video", style=discord.ButtonStyle.success, row=2)
    async def add_video(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(VideoModal(self))

    @discord.ui.button(label="Edit video", style=discord.ButtonStyle.primary, row=2)
    async def edit_video(self, interaction: discord.Interaction, _):
        await self._pick(interaction, "video", "edit")

    @discord.ui.button(label="Remove video", style=discord.ButtonStyle.danger, row=2)
    async def remove_video(self, interaction: discord.Interaction, _):
        await self._pick(interaction, "video", "remove")

    # ---- row 3: member
    @discord.ui.button(label="Set Minecraft username", style=discord.ButtonStyle.secondary, row=3)
    async def set_mc(self, interaction: discord.Interaction, _):
        m = await self.bot.db.get_member(self.member_id)
        await interaction.response.send_modal(MinecraftEditModal(self, (m or {}).get("mc_username")))

    @discord.ui.button(label="Change type", style=discord.ButtonStyle.secondary, row=3)
    async def change_kind(self, interaction: discord.Interaction, _):
        m = await self.bot.db.get_member(self.member_id)
        await interaction.response.send_message(embed=style.info("Pick the new type:"),
                                                view=KindView(self, (m or {}).get("kind", "both")), ephemeral=True)

    async def _pick(self, interaction: discord.Interaction, kind: str, action: str):
        streams, videos, ms, me = await self.month_rows()
        rows = [s for s in streams if ms <= s["started_at"] < me] if kind == "stream" else videos
        if not rows:
            return await interaction.response.send_message(
                embed=style.info(f"No {kind}s in {month_label(self.jy, self.jm)}."), ephemeral=True)
        await interaction.response.send_message(
            embed=style.info(f"Pick the {kind} to {action} ({month_label(self.jy, self.jm)}):"),
            view=PickView(self, kind, action, rows), ephemeral=True)


# ------------------------------------------------------------------ command
class Stats(AdminCog, commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def find_member(self, text: str) -> int | None:
        s = (text or "").strip()
        m = re.fullmatch(r"<@!?(\d+)>|(\d{15,25})", s)
        if m:
            did = int(m.group(1) or m.group(2))
            return did if await self.bot.db.get_member(did) else None
        low = s.lower()
        members = await self.bot.db.members()
        for mem in members:
            if (mem.get("mc_username") or "").lower() == low:
                return mem["discord_id"]
        for mem in members:
            if display_name(self.bot, mem["discord_id"]).lower() == low:
                return mem["discord_id"]
        for mem in members:
            if low in (mem.get("mc_username") or "").lower() or low in display_name(self.bot, mem["discord_id"]).lower():
                return mem["discord_id"]
        return None

    @app_commands.command(name="stats", description="See and edit a media member's stats")
    @app_commands.describe(media="Minecraft username or Discord name (pick from the list)",
                           month="Jalali month (1-12, default: this month)", year="Jalali year")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def stats(self, interaction: discord.Interaction, media: str,
                    month: Optional[app_commands.Range[int, 1, 12]] = None,
                    year: Optional[app_commands.Range[int, 1390, 1500]] = None):
        did = await self.find_member(media)
        if not did:
            return await interaction.response.send_message(embed=style.error(
                f"No media member matches `{media}`. Pick one from the list, or add them with `/media add`."),
                ephemeral=True)
        cy, cm = current_jalali_month()
        jy, jm = (year or cy, month) if month else (cy, cm)
        panel = StatsPanel(self.bot, interaction, did, jy, jm)
        await interaction.response.send_message(embeds=await panel.build(), view=panel, ephemeral=True)

    @stats.autocomplete("media")
    async def media_autocomplete(self, interaction: discord.Interaction, current: str):
        low = (current or "").lower()
        out = []
        for mem in await self.bot.db.members():
            mc = mem.get("mc_username") or "-"
            name = display_name(self.bot, mem["discord_id"])
            label = f"{mc} ({name})"
            if not low or low in label.lower():
                out.append(app_commands.Choice(name=label[:100], value=str(mem["discord_id"])))
            if len(out) >= 25:
                break
        return out


async def setup(bot):
    await bot.add_cog(Stats(bot))
