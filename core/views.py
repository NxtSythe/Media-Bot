"""Persistent buttons (keep working after the bot restarts)."""
from __future__ import annotations

import logging
import re

import discord

from . import style, texts
from .registry import RegistrationError, valid_minecraft

log = logging.getLogger("views")

ACCEPT_ID = "lmc:accept"


# ------------------------------------------------------------------ rules section (Persian)
REG_ERRORS = {
    "bad_link": "لینک معتبر نیست. فقط لینک یوتیوب یا آپارات قبوله. دوباره روی دکمه بزن و لینک درست رو وارد کن.",
    "not_found": "نتونستم چنلت رو پیدا کنم. لینک رو چک کن و دوباره امتحان کن (بهتره لینک خود چنل رو بدی).",
    "taken": "این چنل قبلاً برای یه نفر دیگه ثبت شده. اگه فکر می‌کنی اشتباه شده به ادمین خبر بده.",
    "bad_minecraft": "یوزرنیم ماینکرفت معتبر نیست. باید ۳ تا ۲۰ کاراکتر باشه و فقط حروف انگلیسی، عدد، _ یا . داشته باشه.",
}
GENERIC_ERROR = "یه مشکلی پیش اومد. لطفاً به ادمین خبر بده."


async def _give_media_role(bot, member: discord.Member) -> bool:
    cfg = bot.cfg
    new_role = member.guild.get_role(cfg.role_new)
    media_role = member.guild.get_role(cfg.role_media)
    if media_role is None:
        await bot.log("The `media` role in config.yml was not found.", level="error")
        return False
    try:
        await member.add_roles(media_role, reason="Accepted media rules")
        if new_role and new_role in member.roles:
            await member.remove_roles(new_role, reason="Accepted media rules")
        return True
    except discord.Forbidden:
        await bot.log("Couldn't give/remove roles. The bot's role must be above `new` and `media` "
                      "and have the Manage Roles permission.", level="error")
        return False


class RegisterModal(discord.ui.Modal):
    """Opens when someone accepts the rules: channel link, Minecraft username, type."""

    def __init__(self, reg: dict):
        super().__init__(title=reg["title"][:45], timeout=900)
        self.link = discord.ui.TextInput(style=discord.TextStyle.short, max_length=200, required=True,
                                         placeholder="https://www.aparat.com/... / https://www.youtube.com/@...")
        self.mc = discord.ui.TextInput(style=discord.TextStyle.short, min_length=3, max_length=20, required=True,
                                       placeholder="Steve")
        self.kind = discord.ui.Select(min_values=1, max_values=1, required=True, options=[
            discord.SelectOption(label=reg["kind_streamer"][:100], value="streamer"),
            discord.SelectOption(label=reg["kind_creator"][:100], value="creator"),
            discord.SelectOption(label=reg["kind_both"][:100], value="both"),
        ])
        self.add_item(discord.ui.Label(text=reg["link_label"][:45], description=reg["link_description"][:100],
                                       component=self.link))
        self.add_item(discord.ui.Label(text=reg["minecraft_label"][:45],
                                       description=reg["minecraft_description"][:100], component=self.mc))
        self.add_item(discord.ui.Label(text=reg["kind_label"][:45], component=self.kind))

    async def on_submit(self, interaction: discord.Interaction):
        bot = interaction.client
        member = interaction.user
        await interaction.response.defer(ephemeral=True, thinking=True)
        kind = self.kind.values[0] if self.kind.values else "both"
        mc = self.mc.value.strip()
        try:
            info, platform, row_id, created = await bot.register_channel(
                member.id, self.link.value, kind, mc, allow_unverified=True)
        except RegistrationError as e:
            return await interaction.followup.send(embed=style.error(REG_ERRORS.get(e.code, GENERIC_ERROR)),
                                                   ephemeral=True)
        except Exception:
            log.exception("registration failed")
            return await interaction.followup.send(embed=style.error(GENERIC_ERROR), ephemeral=True)

        if not await _give_media_role(bot, member):
            return await interaction.followup.send(embed=style.error(GENERIC_ERROR), ephemeral=True)
        await interaction.followup.send(embed=style.success(bot.cfg.info_welcome), ephemeral=True)
        await bot.log(
            f"New media member registered: {member.mention}\n"
            f"Channel: [{info['name']}]({info['url']}) ({texts.PLATFORM_NAME[platform]}, channel ID `{row_id}`)\n"
            f"Minecraft: `{mc}` | Type: {texts.KIND_NAME.get(kind, kind)}", level="success")


class AcceptButton(discord.ui.Button):
    def __init__(self, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.success, custom_id=ACCEPT_ID)

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        cfg = bot.cfg
        member = interaction.user
        if interaction.guild is None or not isinstance(member, discord.Member):
            return await interaction.response.send_message(
                embed=style.error("این دکمه فقط توی سرور کار می‌کنه."), ephemeral=True)
        new_role = interaction.guild.get_role(cfg.role_new)
        media_role = interaction.guild.get_role(cfg.role_media)
        if media_role and media_role in member.roles and (new_role is None or new_role not in member.roles):
            return await interaction.response.send_message(
                embed=style.info("تو قبلاً قوانین رو قبول کردی."), ephemeral=True)
        await interaction.response.send_modal(RegisterModal(cfg.reg))


def accept_view(label: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(AcceptButton(label))
    return view


# ------------------------------------------------------------------ Minecraft username (DM, English)
class MinecraftModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Minecraft username", timeout=900)
        self.name = discord.ui.TextInput(style=discord.TextStyle.short, min_length=3, max_length=20,
                                         required=True, placeholder="Steve")
        self.add_item(discord.ui.Label(text="Your Minecraft username", component=self.name,
                                       description="3-20 characters: English letters, numbers, _ or ."))

    async def on_submit(self, interaction: discord.Interaction):
        bot = interaction.client
        name = self.name.value.strip()
        if not valid_minecraft(name):
            return await interaction.response.send_message(embed=style.error(
                "That doesn't look like a valid Minecraft username. Use 3-20 characters: "
                "English letters, numbers, _ or . - then click the button again."), ephemeral=True)
        if not await bot.db.get_member(interaction.user.id):
            return await interaction.response.send_message(
                embed=style.error("You're not on the media list."), ephemeral=True)
        await bot.db.set_minecraft(interaction.user.id, name)
        done = style.success(f"Thanks! Your Minecraft username is saved as **{name}**.", title="Saved")
        try:
            await interaction.response.edit_message(embed=done, view=None)
        except discord.HTTPException:
            await interaction.response.send_message(embed=done)
        await bot.log(f"<@{interaction.user.id}> set their Minecraft username: `{name}`", level="info")


class MinecraftButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lmc:mc"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Enter Minecraft username", style=discord.ButtonStyle.primary,
                                           custom_id="lmc:mc"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(MinecraftModal())


def minecraft_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(MinecraftButton())
    return view


# ------------------------------------------------------------------ "was it about the server?" buttons (DM)
class ConfirmButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"lmc:sr:(?P<kind>[sv]):(?P<id>\d+):(?P<ans>[yn])"):
    def __init__(self, kind: str, item_id: int, ans: str):
        yes = ans == "y"
        super().__init__(discord.ui.Button(
            label="Yes, it was about the server" if yes else "No",
            style=discord.ButtonStyle.success if yes else discord.ButtonStyle.danger,
            custom_id=f"lmc:sr:{kind}:{item_id}:{ans}",
        ))
        self.kind = kind
        self.item_id = item_id
        self.ans = ans

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["kind"], int(match["id"]), match["ans"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        table = "streams" if self.kind == "s" else "videos"
        kind_name = "stream" if self.kind == "s" else "video"
        row = await bot.db.one(f"SELECT * FROM {table} WHERE id = ?", (self.item_id,))
        if row is None:
            return await interaction.response.edit_message(
                embed=style.error("This item no longer exists."), view=None)
        if interaction.user.id != row["discord_id"]:
            return await interaction.response.send_message(
                embed=style.error("This question isn't for you."), ephemeral=True)

        if row["server_related"] is not None:
            return await interaction.response.edit_message(
                embed=texts.confirm_done_embed(kind_name, row["server_related"] == 1, already=True), view=None)

        answer = self.ans == "y"
        await bot.db.update(table, self.item_id, server_related=1 if answer else 0)
        await interaction.response.edit_message(embed=texts.confirm_done_embed(kind_name, answer), view=None)
        await bot.refresh_announcement(kind_name, self.item_id)


def confirm_view(kind: str, item_id: int) -> discord.ui.View:
    code = "s" if kind == "stream" else "v"
    view = discord.ui.View(timeout=None)
    view.add_item(ConfirmButton(code, item_id, "y"))
    view.add_item(ConfirmButton(code, item_id, "n"))
    return view
