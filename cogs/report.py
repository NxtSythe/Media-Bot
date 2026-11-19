"""Monthly inactivity report in #media-dt (demotes are done by hand)."""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import style
from core.perms import AdminCog
from core.activity import evaluate_member, member_lines
from core.texts import KIND_NAME
from core.utils import (chunk_lines, current_jalali_month, jalali, jalali_month_range, month_label,
                        now_ts, previous_jalali_month, to_local)

log = logging.getLogger("report")


class Report(AdminCog, commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.auto_report.start()

    async def cog_unload(self):
        self.auto_report.cancel()

    # ---------------------------------------------------------------- data
    async def evaluate_all(self, jy: int, jm: int):
        db = self.bot.db
        m_start, m_end = jalali_month_range(jy, jm)
        # streams can start up to 6 days before the month (first week)
        lo, hi = m_start - 7 * 86400, m_end
        results = []
        for m in await db.members():
            if m["added_at"] >= m_end:
                continue
            streams = await db.all("SELECT * FROM streams WHERE discord_id = ? AND started_at >= ? AND started_at < ?",
                                   (m["discord_id"], lo, hi))
            videos = await db.all("SELECT * FROM videos WHERE discord_id = ? AND published_at >= ? AND published_at < ?",
                                  (m["discord_id"], m_start, m_end))
            results.append(evaluate_member(m, streams, videos, jy, jm, self.bot.cfg))
        return results

    async def build_embeds(self, jy: int, jm: int) -> list[discord.Embed]:
        cfg = self.bot.cfg
        results = await self.evaluate_all(jy, jm)
        guild = self.bot.guild
        _, m_end = jalali_month_range(jy, jm)
        finished = now_ts() >= m_end

        member_rows = await self.bot.db.members()
        all_members = {m["discord_id"] for m in member_rows}
        mc_names = {m["discord_id"]: m.get("mc_username") for m in member_rows}
        with_channels = {c["discord_id"] for c in await self.bot.db.channels()}

        left, failed, passed, new = [], [], [], []
        for r in results:
            if guild and guild.get_member(r.discord_id) is None:
                left.append(r)
            elif r.skipped_new:
                new.append(r)
            elif r.failed:
                failed.append(r)
            else:
                passed.append(r)

        sections: list[tuple] = []  # (maker, title, lines)

        # overview (blue)
        overview = [
            "**Rules**",
            f"Streamers: at least {cfg.min_streams_per_week} streams per week (Saturday to Friday), "
            f"each at least {cfg.min_stream_minutes} minutes.",
            f"Content creators: at least {cfg.min_videos_per_month} videos per month, "
            f"each at least {cfg.min_video_minutes} minutes.",
            "Only streams and videos confirmed as server-related are counted.",
            "",
            f"Need to be demoted: **{len(failed)}** | Met the requirements: **{len(passed)}**",
        ]
        if not finished:
            overview.append("\n**Preview:** this month isn't over yet, so only finished weeks are checked.")
        sections.append((style.info, f"Media Activity Report - {month_label(jy, jm)}", overview))

        # failed (red)
        lines = []
        for r in failed:
            mc = f" | Minecraft: `{mc_names[r.discord_id]}`" if mc_names.get(r.discord_id) else ""
            lines.append(f"**<@{r.discord_id}>** - {KIND_NAME.get(r.kind, r.kind)}{mc}")
            lines.extend(member_lines(r, cfg))
            lines.append("")
        sections.append((style.error, f"Need to be demoted ({len(failed)})", lines or ["Nobody. Everyone met the requirements."]))

        # passed (green)
        sections.append((style.success, f"Met the requirements ({len(passed)})",
                         [", ".join(f"<@{r.discord_id}>" for r in passed) or "-"]))

        # attention (yellow)
        att = []
        pend = [r for r in failed if r.pending]
        if pend:
            att.append("**Haven't answered the \"was it about the server?\" question**")
            att.append(", ".join(f"<@{r.discord_id}> ({r.pending})" for r in pend))
            att.append("Check them with `/media pending` and mark them with `/media set-related`.\n")
        if new:
            att.append("**Added this month (not reviewed)**")
            att.append(", ".join(f"<@{r.discord_id}>" for r in new) + "\n")
        if guild:
            media_role = guild.get_role(cfg.role_media)
            if media_role:
                no_channel = [m for m in media_role.members if not m.bot and
                              (m.id not in all_members or m.id not in with_channels)]
                if no_channel:
                    att.append("**Have the media role but no linked channel**")
                    att.append(", ".join(m.mention for m in no_channel[:60]))
                    att.append("Link their channels with `/media add`.\n")
        if left:
            att.append("**Left the server**")
            att.append(", ".join(f"<@{r.discord_id}>" for r in left))
            att.append("Remove them from the list with `/media remove`.")
        if att:
            sections.append((style.pending, "Needs attention", att))

        embeds = []
        for maker, title, sec_lines in sections:
            for k, block in enumerate(chunk_lines(sec_lines)):
                e = maker(block, title=title if k == 0 else None)
                embeds.append(e)
        embeds[-1].set_footer(text="The bot doesn't demote anyone. Demotions are up to you.")
        return embeds

    async def send_report(self, jy: int, jm: int) -> bool:
        ch = await self.bot.text_channel(self.bot.cfg.ch_media_dt)
        if ch is None:
            await self.bot.log("The media-dt channel was not found.", level="error")
            return False
        embeds = await self.build_embeds(jy, jm)
        for i in range(0, len(embeds), 10):
            await ch.send(embeds=embeds[i:i + 10], allowed_mentions=discord.AllowedMentions.none())
        return True

    # ---------------------------------------------------------------- auto
    @tasks.loop(minutes=20)
    async def auto_report(self):
        cfg = self.bot.cfg
        now = now_ts()
        j = jalali(now)
        if j.day < cfg.report_day or (j.day == cfg.report_day and to_local(now).hour < cfg.report_hour):
            return
        jy, jm = previous_jalali_month(j.year, j.month)
        key = f"report_sent:{jy}-{jm:02d}"
        if await self.bot.db.get_kv(key):
            return
        # don't report months from before the bot was set up
        first = await self.bot.db.one("SELECT MIN(added_at) AS t FROM members")
        if not first or not first["t"] or first["t"] >= jalali_month_range(jy, jm)[1]:
            await self.bot.db.set_kv(key, "skipped")
            return
        try:
            if await self.send_report(jy, jm):
                await self.bot.db.set_kv(key, str(now))
        except Exception:
            log.exception("auto report failed")

    @auto_report.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ---------------------------------------------------------------- command
    @app_commands.command(name="report", description="Media activity report (default: last month)")
    @app_commands.describe(year="Jalali year (e.g. 1405)", month="Jalali month (1-12)",
                           publish="Also post it in the media-dt channel?")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def report(self, interaction: discord.Interaction, month: Optional[app_commands.Range[int, 1, 12]] = None,
                     year: Optional[app_commands.Range[int, 1390, 1500]] = None, publish: bool = False):
        cy, cm = current_jalali_month()
        if month is None:
            jy, jm = previous_jalali_month(cy, cm)
        else:
            jm, jy = month, year or cy
        await interaction.response.defer(ephemeral=True, thinking=True)
        if publish:
            await self.send_report(jy, jm)
            return await interaction.followup.send(embed=style.success(
                f"The {month_label(jy, jm)} report was posted in <#{self.bot.cfg.ch_media_dt}>."), ephemeral=True)
        embeds = await self.build_embeds(jy, jm)
        await interaction.followup.send(embeds=embeds[:10], ephemeral=True)


async def setup(bot):
    await bot.add_cog(Report(bot))
