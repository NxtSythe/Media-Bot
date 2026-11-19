"""/media and /debug commands for the media manager."""
from __future__ import annotations

import io
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import style
from core.perms import ADMIN_PERMS, AdminCog
from core.texts import KIND_NAME, PLATFORM_NAME, related_label
from core.utils import chunk_lines, jdate, now_ts
from core.registry import RegistrationError, valid_minecraft

log = logging.getLogger("media_admin")

KIND_CHOICES = [
    app_commands.Choice(name="Streamer", value="streamer"),
    app_commands.Choice(name="Content Creator", value="creator"),
    app_commands.Choice(name="Both (Streamer and Content Creator)", value="both"),
]
ITEM_CHOICES = [
    app_commands.Choice(name="Stream", value="stream"),
    app_commands.Choice(name="Video", value="video"),
]
ADMIN = ADMIN_PERMS


def _embeds(lines: list[str], title: str, maker=style.info) -> list[discord.Embed]:
    embeds = [maker(b) for b in chunk_lines(lines)] or [maker("-")]
    embeds[0].title = title
    return embeds[:10]


class MediaAdmin(AdminCog, commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    media = app_commands.Group(name="media", description="Manage media members",
                               default_permissions=ADMIN, guild_only=True)
    debug = app_commands.Group(name="debug", description="Troubleshooting tools",
                               default_permissions=ADMIN, guild_only=True)

    # ---------------------------------------------------------------- add
    @media.command(name="add", description="Link a YouTube or Aparat channel to a media member")
    @app_commands.describe(member="The media member", link="Channel, live or video link",
                           kind="What this media member does", minecraft="Their Minecraft username (optional)")
    @app_commands.choices(kind=KIND_CHOICES)
    async def add(self, interaction: discord.Interaction, member: discord.Member, link: str,
                  kind: app_commands.Choice[str], minecraft: Optional[str] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            info, platform, row_id, created = await self.bot.register_channel(
                member.id, link, kind.value, minecraft.strip() if minecraft else None, allow_unverified=True)
        except RegistrationError as e:
            msg = {
                "bad_link": "Only YouTube or Aparat links are supported.",
                "not_found": f"Couldn't find the channel: {e.detail}",
                "taken": f"This channel is already linked to <@{e.other_id}>. Remove it first with `/media unlink`.",
                "bad_minecraft": "That Minecraft username isn't valid (3-20 characters: letters, numbers, _ or .).",
            }.get(e.code, str(e))
            return await interaction.followup.send(embed=style.error(msg, title="Couldn't add channel"),
                                                   ephemeral=True)
        except Exception as e:
            log.exception("register failed")
            return await interaction.followup.send(embed=style.error(f"Unexpected error: `{e}`"), ephemeral=True)

        if not created:
            return await interaction.followup.send(embed=style.info(
                f"**{info['name']}** was already linked to {member.mention}. Type updated to **{kind.name}**."),
                ephemeral=True)
        await interaction.followup.send(embed=style.success(
            f"[{info['name']}]({info['url']}) ({PLATFORM_NAME[platform]}) is now linked to {member.mention}.\n"
            f"Type: **{kind.name}** | Channel ID: `{row_id}`\n"
            f"New streams and videos from now on will be tracked.", title="Channel added"), ephemeral=True)

    # ---------------------------------------------------------------- minecraft
    @media.command(name="set-minecraft", description="Set a media member's Minecraft username")
    async def set_minecraft(self, interaction: discord.Interaction, member: discord.Member, username: str):
        username = username.strip()
        if not valid_minecraft(username):
            return await interaction.response.send_message(embed=style.error(
                "That Minecraft username isn't valid (3-20 characters: letters, numbers, _ or .)."), ephemeral=True)
        if not await self.db.get_member(member.id):
            return await interaction.response.send_message(
                embed=style.error("This user is not in the media list."), ephemeral=True)
        await self.db.set_minecraft(member.id, username)
        await interaction.response.send_message(
            embed=style.success(f"{member.mention}'s Minecraft username is now **{username}**."), ephemeral=True)

    @media.command(name="remind-minecraft",
                   description="DM every media member who hasn't given their Minecraft username yet")
    async def remind_minecraft(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        sent, failed = await self.bot.get_cog("Onboarding").ask_missing_minecraft(include_asked=True)
        maker = style.success if not failed else style.pending
        text = f"Sent a DM to **{len(sent)}** member(s)."
        if failed:
            text += "\nCouldn't DM (DMs closed): " + ", ".join(f"<@{i}>" for i in failed)
        if not sent and not failed:
            text = "Everyone already has a Minecraft username."
        await interaction.followup.send(embed=maker(text), ephemeral=True)

    # ---------------------------------------------------------------- remove / unlink
    @media.command(name="remove", description="Remove a media member and all their channels")
    async def remove(self, interaction: discord.Interaction, member: discord.User):
        if not await self.db.get_member(member.id):
            return await interaction.response.send_message(
                embed=style.error("This user is not in the media list."), ephemeral=True)
        await self.db.execute("DELETE FROM channels WHERE discord_id = ?", (member.id,))
        await self.db.execute("DELETE FROM members WHERE discord_id = ?", (member.id,))
        await interaction.response.send_message(
            embed=style.success(f"{member.mention} and all their channels were removed."), ephemeral=True)

    @media.command(name="unlink", description="Unlink one channel (get the ID from /media list)")
    @app_commands.describe(code="Channel ID")
    async def unlink(self, interaction: discord.Interaction, code: int):
        c = await self.db.one("SELECT * FROM channels WHERE id = ?", (code,))
        if not c:
            return await interaction.response.send_message(
                embed=style.error("No channel with this ID."), ephemeral=True)
        await self.db.execute("DELETE FROM channels WHERE id = ?", (code,))
        await interaction.response.send_message(
            embed=style.success(f"**{c['name']}** was unlinked from <@{c['discord_id']}>."), ephemeral=True)

    # ---------------------------------------------------------------- list
    @media.command(name="list", description="List all media members and their channels")
    async def list_(self, interaction: discord.Interaction):
        members = await self.db.members()
        if not members:
            return await interaction.response.send_message(
                embed=style.info("No media members yet. Start with `/media add`."), ephemeral=True)
        lines = []
        for m in members:
            mc = f"`{m['mc_username']}`" if m.get("mc_username") else "*not set*"
            lines.append(f"**<@{m['discord_id']}>** - {KIND_NAME.get(m['kind'], m['kind'])} | Minecraft: {mc}")
            chans = await self.db.channels(discord_id=m["discord_id"])
            if not chans:
                lines.append("> No channel linked")
            for c in chans:
                lines.append(f"> `{c['id']}` {PLATFORM_NAME[c['platform']]}: [{c['name']}]({c['url']})")
            lines.append("")
        await interaction.response.send_message(
            embeds=_embeds(lines, f"Media members ({len(members)})"), ephemeral=True)

    # ---------------------------------------------------------------- pending / set-related
    @media.command(name="pending", description="Streams and videos not yet confirmed as server-related or not")
    async def pending(self, interaction: discord.Interaction):
        since = now_ts() - 60 * 86400
        streams = await self.db.all(
            "SELECT * FROM streams WHERE server_related IS NULL AND status = 'ended' AND started_at >= ? "
            "ORDER BY started_at", (since,))
        videos = await self.db.all(
            "SELECT * FROM videos WHERE server_related IS NULL AND published_at >= ? ORDER BY published_at", (since,))
        if not streams and not videos:
            return await interaction.response.send_message(
                embed=style.success("Nothing is waiting for confirmation."), ephemeral=True)
        dm = {0: "", 1: " | DM sent", 2: " | DM failed"}
        lines = []
        for s in streams:
            lines.append(f"Stream `{s['id']}` <@{s['discord_id']}> - [{(s['title'] or '-')[:45]}]({s['url']}) "
                         f"| {jdate(s['started_at'])}{dm.get(s['dm_sent'], '')}")
        for v in videos:
            lines.append(f"Video `{v['id']}` <@{v['discord_id']}> - [{(v['title'] or '-')[:45]}]({v['url']}) "
                         f"| {jdate(v['published_at'])}{dm.get(v['dm_sent'], '')}")
        embeds = _embeds(lines, "Awaiting confirmation", style.pending)
        embeds[-1].set_footer(text="Use /media set-related to mark them yourself.")
        await interaction.response.send_message(embeds=embeds, ephemeral=True)

    @media.command(name="set-related", description="Mark a stream/video as server-related or not")
    @app_commands.describe(item="Stream or video", code="ID (shown under history messages or in /media pending)",
                           related="Was it about the server?")
    @app_commands.choices(item=ITEM_CHOICES)
    async def set_related(self, interaction: discord.Interaction, item: app_commands.Choice[str], code: int,
                          related: bool):
        table = "streams" if item.value == "stream" else "videos"
        row = await self.db.one(f"SELECT * FROM {table} WHERE id = ?", (code,))
        if not row:
            return await interaction.response.send_message(
                embed=style.error(f"No {item.value} with this ID."), ephemeral=True)
        await self.db.update(table, code, server_related=1 if related else 0)
        await self.bot.refresh_announcement(item.value, code)
        maker = style.success if related else style.error
        await interaction.response.send_message(
            embed=maker(f"{item.name} `{code}` ({row['title']}) is now: **{related_label(1 if related else 0)}**"),
            ephemeral=True)

    # ---------------------------------------------------------------- debug
    @debug.command(name="aparat", description="Test every Aparat endpoint for a username from this server")
    @app_commands.describe(username="Aparat username (the part after aparat.com/)")
    async def debug_aparat(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        username = username.strip().strip("/").split("/")[-1] if "aparat.com" not in username else \
            username.split("aparat.com/")[-1].split("/")[0]
        lines = await self.bot.aparat.diagnose(username)
        report = "\n".join(lines)
        file = discord.File(io.BytesIO(report.encode("utf-8")), filename=f"aparat-debug-{username}.txt")
        await interaction.followup.send(
            embed=style.info("Send this file to the developer if Aparat tracking isn't working.",
                             title=f"Aparat diagnostics: {username}"),
            file=file, ephemeral=True)


async def setup(bot):
    await bot.add_cog(MediaAdmin(bot))
