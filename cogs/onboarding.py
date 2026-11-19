"""New members: role `new`, info channel with rules + accept button, /setup commands."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from core import style
from core.perms import ADMIN_PERMS, AdminCog
from core.views import accept_view

log = logging.getLogger("onboarding")


class Onboarding(AdminCog, commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._info_checked = False

    # ---------------------------------------------------------------- events
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or member.guild.id != self.bot.cfg.guild_id:
            return
        role = member.guild.get_role(self.bot.cfg.role_new)
        if role is None:
            return await self.bot.log("The `new` role (roles.new in config.yml) was not found.", throttle_key="norole:new", level="error")
        try:
            await member.add_roles(role, reason="New member")
        except discord.Forbidden:
            await self.bot.log("Couldn't give the `new` role. The bot's role must be above `new`.",
                               throttle_key="forbid:new", level="error")

    @commands.Cog.listener()
    async def on_ready(self):
        if self._info_checked:
            return
        self._info_checked = True
        try:
            await self.ensure_info_message(force_new=False)
        except Exception:
            log.exception("could not ensure info message")
        try:
            sent, failed = await self.ask_missing_minecraft(include_asked=False)
            if sent or failed:
                text = f"Asked **{len(sent)}** media member(s) for their Minecraft username by DM."
                if failed:
                    text += ("\nCouldn't DM (DMs closed): " + ", ".join(f"<@{i}>" for i in failed)
                             + "\nSet theirs with `/media set-minecraft`.")
                await self.bot.log(text, level="info" if not failed else "warning")
        except Exception:
            log.exception("could not ask for minecraft usernames")

    async def ask_missing_minecraft(self, include_asked: bool) -> tuple[list[int], list[int]]:
        """DM members that have no Minecraft username. Without include_asked, each member is asked only once."""
        sql = "SELECT discord_id FROM members WHERE (mc_username IS NULL OR mc_username = '')"
        if not include_asked:
            sql += " AND COALESCE(mc_asked, 0) = 0"
        sent, failed = [], []
        guild = self.bot.guild
        for row in await self.bot.db.all(sql):
            did = row["discord_id"]
            if guild and guild.get_member(did) is None:
                continue  # left the server
            (sent if await self.bot.ask_minecraft(did) else failed).append(did)
            await asyncio.sleep(1.5)
        return sent, failed

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        """New channels are hidden from the `new` role automatically."""
        if channel.guild.id != self.bot.cfg.guild_id or channel.id == self.bot.cfg.ch_info:
            return
        role = channel.guild.get_role(self.bot.cfg.role_new)
        if role is None:
            return
        ow = channel.overwrites_for(role)
        if ow.view_channel is False:
            return
        ow.view_channel = False
        try:
            await channel.set_permissions(role, overwrite=ow, reason="Hide new channel from the new role")
        except discord.HTTPException:
            pass

    # ---------------------------------------------------------------- info message
    def info_embed(self) -> discord.Embed:
        cfg = self.bot.cfg
        e = discord.Embed(title=cfg.info_title, description=cfg.info_description[:4096], color=style.BLUE)
        e.set_footer(text=f"{cfg.server_name} Media Team")
        return e

    async def ensure_info_message(self, force_new: bool) -> str:
        cfg = self.bot.cfg
        ch = await self.bot.text_channel(cfg.ch_info)
        if ch is None:
            return "The info channel was not found."
        embed, view = self.info_embed(), accept_view(cfg.info_button)
        msg_id = await self.bot.db.get_kv("info_message_id")
        if msg_id and not force_new:
            try:
                msg = await ch.fetch_message(int(msg_id))
                await msg.edit(embed=embed, view=view)
                return "The rules message was updated."
            except discord.NotFound:
                pass
        elif msg_id and force_new:
            try:
                await ch.get_partial_message(int(msg_id)).delete()
            except discord.HTTPException:
                pass
        msg = await ch.send(embed=embed, view=view)
        await self.bot.db.set_kv("info_message_id", str(msg.id))
        return "The rules message was sent."

    # ---------------------------------------------------------------- /setup
    setup = app_commands.Group(name="setup", description="Bot setup",
                               default_permissions=ADMIN_PERMS, guild_only=True)

    @setup.command(name="info", description="Send or update the rules message in the info channel")
    @app_commands.describe(new_message="Delete the old message and send a new one?")
    async def setup_info(self, interaction: discord.Interaction, new_message: bool = False):
        await interaction.response.defer(ephemeral=True)
        result = await self.ensure_info_message(force_new=new_message)
        await interaction.followup.send(embed=(style.error if "not found" in result else style.success)(result), ephemeral=True)

    @setup.command(name="permissions", description="Let `new` see only the info channel and hide info from `media`")
    async def setup_permissions(self, interaction: discord.Interaction):
        cfg = self.bot.cfg
        guild = interaction.guild
        new_role, media_role = guild.get_role(cfg.role_new), guild.get_role(cfg.role_media)
        info = guild.get_channel(cfg.ch_info)
        if not new_role or not media_role or not info:
            return await interaction.response.send_message(embed=style.error(
                "The `new` role, `media` role or info channel was not found. Check config.yml."), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)

        changed, failed = 0, []

        async def apply(ch, target, **perms):
            nonlocal changed
            ow = ch.overwrites_for(target)
            if all(getattr(ow, k) == v for k, v in perms.items()):
                return
            for k, v in perms.items():
                setattr(ow, k, v)
            try:
                await ch.set_permissions(target, overwrite=ow, reason="/setup permissions")
                changed += 1
                await asyncio.sleep(0.3)
            except discord.HTTPException:
                failed.append(ch.mention if hasattr(ch, "mention") else ch.name)

        # categories first, then channels - keeps channels synced with their category
        ordered = sorted(guild.channels, key=lambda c: 0 if isinstance(c, discord.CategoryChannel) else 1)
        for ch in ordered:
            if ch.id == info.id:
                continue
            await apply(ch, new_role, view_channel=False)

        await apply(info, guild.default_role, view_channel=False)
        await apply(info, media_role, view_channel=False)
        await apply(info, new_role, view_channel=True, read_message_history=True,
                    send_messages=False, add_reactions=False, create_public_threads=False)

        text = f"Permissions updated ({changed} changes)."
        text += ("\n\nAdmins can still see everything because they have Administrator. "
                 "Channels you create later are hidden from `new` automatically.")
        if failed:
            text += f"\n\nThese channels were not changed (missing access): {', '.join(failed[:30])}"
            return await interaction.followup.send(embed=style.pending(text), ephemeral=True)
        await interaction.followup.send(embed=style.success(text), ephemeral=True)

    @setup.command(name="check", description="Check config.yml and the bot's permissions")
    async def setup_check(self, interaction: discord.Interaction):
        cfg = self.bot.cfg
        g = interaction.guild
        me = g.me
        lines, problems = [], 0

        def ok(cond, good, bad):
            nonlocal problems
            if not cond:
                problems += 1
            lines.append(f"[OK] {good}" if cond else f"[FAIL] {bad}")

        for key, cid in (("info", cfg.ch_info), ("live-history", cfg.ch_live_history),
                         ("video-history", cfg.ch_video_history), ("media-dt", cfg.ch_media_dt)):
            ch = g.get_channel(cid)
            if ch is None:
                ok(False, "", f"Channel `{key}` was not found")
                continue
            p = ch.permissions_for(me)
            ok(p.view_channel and p.send_messages and p.embed_links,
               f"Channel {key}: {ch.mention}", f"The bot can't send messages/embeds in {ch.mention} ({key})")
        if cfg.ch_log:
            ok(g.get_channel(cfg.ch_log) is not None, "Log channel", "Log channel was not found")

        for key, rid in (("new", cfg.role_new), ("media", cfg.role_media)):
            r = g.get_role(rid)
            if r is None:
                ok(False, "", f"Role `{key}` was not found")
            else:
                ok(me.top_role > r, f"Role {key}: {r.mention}",
                   f"The bot's role must be above {r.mention} (Server Settings > Roles)")
        ok(me.guild_permissions.manage_roles, "Manage Roles permission", "Missing the Manage Roles permission")
        ok(me.guild_permissions.manage_channels, "Manage Channels permission",
           "Missing the Manage Channels permission (needed for /setup permissions)")
        lines.append("[OK] YouTube: using the API key" if cfg.youtube_api_key
                     else "[OK] YouTube: no API key needed (using public YouTube pages)")
        lines.append(f"[OK] Aparat proxy: {cfg.aparat_proxy}" if cfg.aparat_proxy else "[OK] Aparat: direct connection")
        maker = style.success if not problems else style.error
        await interaction.response.send_message(embed=maker("\n".join(lines), title="Setup check"), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

async def setup(bot):
    await bot.add_cog(Onboarding(bot))
