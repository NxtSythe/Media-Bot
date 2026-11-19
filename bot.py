"""LunaMC Media Bot - entry point.  Run:  python bot.py"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

# Everything (config.yml, data.db, backups/, bot.log) lives next to this file,
# no matter where the bot is started from - copy the folder and the data comes with it.
BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

import aiohttp
import discord
from discord.ext import commands

from core import style, texts, utils
from core.config import ConfigError, load_config
from core.db import Database
from core.github_store import GitHubStore, StoreError
from core.registry import RegistrationError, valid_minecraft
from core.views import ConfirmButton, MinecraftButton, accept_view, confirm_view, minecraft_view
from platforms.aparat import AparatClient, AparatError, is_aparat_url
from platforms.base import PlatformError
from platforms.youtube import YouTubeClient, is_youtube_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("bot.log", encoding="utf-8")],
)
log = logging.getLogger("bot")

EXTENSIONS = ("cogs.onboarding", "cogs.media_admin", "cogs.stats", "cogs.importer", "cogs.tracker", "cogs.report",
              "cogs.maintenance")


class MediaBot(commands.Bot):
    def __init__(self, cfg):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.cfg = cfg
        self.db = Database(str(BASE_DIR / cfg.database))
        self.web: aiohttp.ClientSession | None = None
        self.youtube: YouTubeClient | None = None
        self.aparat: AparatClient | None = None
        self._log_throttle: dict[str, float] = {}

    async def setup_hook(self):
        utils.set_timezone(self.cfg.timezone)
        self.web = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))

        # ---- database: load it from GitHub first (if GitHub storage is set up)
        self.store = GitHubStore(self.web)
        self.store_ready = False
        if self.store.enabled:
            try:
                await self.store.check()
                got = await self.store.download(self.db.path)
                log.info("GitHub storage: %s - %s", self.store.describe(),
                         "database downloaded" if got else "nothing saved yet, starting a new database")
                self.store_ready = True
            except (StoreError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                # Never start with an empty database here - it would overwrite the saved one.
                log.critical("Could not load the database from GitHub: %s", e)
                raise SystemExit(1)
        await self.db.connect()
        self._save_lock = asyncio.Lock()
        self._saved_changes = self.db.total_changes

        # ---- stop cleanly on SIGTERM (and after RUN_SECONDS, used by GitHub Actions)
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(self.close()))
        except (NotImplementedError, RuntimeError):
            pass
        run_seconds = int(os.environ.get("RUN_SECONDS", "0") or 0)
        if run_seconds > 0:
            asyncio.ensure_future(self._stop_after(run_seconds))
        self.youtube = YouTubeClient(self.web, self.cfg.youtube_api_key, self.cfg.youtube_check_live_page)
        self.aparat = AparatClient(self.web, self.cfg.aparat_urls, self.cfg.aparat_proxy)

        self.add_view(accept_view(self.cfg.info_button))
        self.add_dynamic_items(ConfirmButton, MinecraftButton)

        for ext in EXTENSIONS:
            await self.load_extension(ext)

        self.tree.on_error = self._on_app_command_error
        guild = discord.Object(id=self.cfg.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Synced %d slash commands", len(synced))

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        log.info("Data folder: %s", BASE_DIR)
        if not self.cfg.youtube_api_key:
            log.info("No YouTube API key - using public YouTube pages.")

    async def _on_app_command_error(self, interaction: discord.Interaction, error):
        from discord import app_commands
        from core.perms import NO_ACCESS
        if isinstance(error, app_commands.CheckFailure):
            msg = NO_ACCESS
        else:
            log.error("command error", exc_info=error)
            msg = f"Something went wrong: `{getattr(error, 'original', error)}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=style.error(msg), ephemeral=True)
            else:
                await interaction.response.send_message(embed=style.error(msg), ephemeral=True)
        except discord.HTTPException:
            pass

    async def _stop_after(self, seconds: int):
        await asyncio.sleep(seconds)
        log.info("RUN_SECONDS reached - saving data and stopping so the next run can take over.")
        await self.close()

    async def save_data(self, backup_file: str | None = None, force: bool = False) -> bool:
        """Upload the database to GitHub (only if it changed, unless force)."""
        if not getattr(self, "store_ready", False) or self.db.conn is None:
            return False
        async with self._save_lock:
            changes = self.db.total_changes
            if not force and not backup_file and changes == self._saved_changes:
                return False
            with tempfile.TemporaryDirectory() as tmp:
                snap = Path(tmp) / "data.db"
                await self.db.snapshot(snap)
                await self.store.upload(snap, backup_file=backup_file)
            self._saved_changes = changes
            log.info("Database saved to GitHub.")
            return True

    async def close(self):
        if getattr(self, "_mediabot_closing", False):
            return
        self._mediabot_closing = True
        try:
            if getattr(self, "store_ready", False):
                await asyncio.wait_for(self.save_data(force=True), timeout=45)
        except Exception as e:
            log.error("Final save to GitHub failed: %s", e)
        await super().close()
        if self.web:
            await self.web.close()
        await self.db.close()

    # ================================================================ helpers
    @property
    def guild(self) -> discord.Guild | None:
        return self.get_guild(self.cfg.guild_id)

    async def text_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        if not channel_id:
            return None
        ch = self.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return ch

    async def log(self, text: str, throttle_key: str | None = None, every: float = 6 * 3600,
                  level: str = "warning") -> None:
        """Send a message to the log channel (optionally rate-limited per key).
        level: info (blue), success (green), warning (yellow), error (red)."""
        log.info("LOG[%s]: %s", level, text)
        if throttle_key:
            last = self._log_throttle.get(throttle_key, 0)
            if time.time() - last < every:
                return
            self._log_throttle[throttle_key] = time.time()
        ch = await self.text_channel(self.cfg.ch_log)
        if ch:
            maker = {"info": style.info, "success": style.success, "error": style.error}.get(level, style.pending)
            try:
                await ch.send(embed=maker(text[:4000]), allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    def is_server_related(self, *texts_: str) -> int | None:
        return 1 if utils.has_keyword(" ".join(t or "" for t in texts_), self.cfg.keywords) else None

    def member_name(self, discord_id: int) -> str:
        g = self.guild
        m = g.get_member(discord_id) if g else None
        return m.display_name if m else "there"

    # ---------------------------------------------------------------- announcements
    async def _post_or_edit(self, table: str, row: dict, embed: discord.Embed, channel_id: int, create: bool) -> None:
        if row.get("message_id"):
            ch = await self.text_channel(row["message_channel"] or channel_id)
            if ch is None:
                return
            try:
                await ch.get_partial_message(row["message_id"]).edit(embed=embed)
            except discord.NotFound:
                pass  # message was deleted on purpose - leave it
            except discord.HTTPException as e:
                log.warning("edit failed: %s", e)
            return
        if not create:
            return
        ch = await self.text_channel(channel_id)
        if ch is None:
            await self.log(f"History channel `{channel_id}` from config.yml was not found.", throttle_key=f"nochan:{channel_id}", level="error")
            return
        try:
            msg = await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            await self.log(f"Couldn't send a message in <#{channel_id}>: {e}", throttle_key=f"send:{channel_id}", level="error")
            return
        await self.db.update(table, row["id"], message_id=msg.id, message_channel=ch.id)

    async def announce_stream(self, stream_id: int, create: bool = True) -> None:
        s = await self.db.one("SELECT * FROM streams WHERE id = ?", (stream_id,))
        if not s or s["status"] != "ended":
            return
        await self._post_or_edit("streams", s, texts.live_embed(s), self.cfg.ch_live_history, create)

    async def announce_video(self, video_id: int, create: bool = True) -> None:
        v = await self.db.one("SELECT * FROM videos WHERE id = ?", (video_id,))
        if not v:
            return
        await self._post_or_edit("videos", v, texts.video_embed(v), self.cfg.ch_video_history, create)

    async def refresh_announcement(self, kind: str, item_id: int) -> None:
        if kind == "stream":
            await self.announce_stream(item_id, create=False)
        else:
            await self.announce_video(item_id, create=False)

    # ---------------------------------------------------------------- DM confirmation
    async def ask_confirmation(self, kind: str, item_id: int, live_now: bool = False) -> None:
        table = "streams" if kind == "stream" else "videos"
        row = await self.db.one(f"SELECT * FROM {table} WHERE id = ?", (item_id,))
        if not row or row["server_related"] is not None or row["dm_sent"]:
            return
        try:
            user = self.get_user(row["discord_id"]) or await self.fetch_user(row["discord_id"])
            embed = texts.confirm_dm_embed(
                self.member_name(row["discord_id"]), kind, row["title"] or "-", row["url"] or "",
                self.cfg.server_name, live_now)
            await user.send(embed=embed, view=confirm_view(kind, item_id))
            await self.db.update(table, item_id, dm_sent=1)
        except discord.HTTPException:
            await self.db.update(table, item_id, dm_sent=2)
            await self.log(
                f"Couldn't DM <@{row['discord_id']}> (their DMs are closed). "
                f"The {kind} \"{row['title']}\" (ID {item_id}) doesn't mention the server name. "
                f"Use `/media set-related` to mark it yourself.")


    # ---------------------------------------------------------------- registration
    async def register_channel(self, member_id: int, link: str, kind: str, minecraft: str | None = None,
                               allow_unverified: bool = False) -> tuple[dict, str, int, bool]:
        """Link a channel to a member (creating the member if needed).

        Returns (channel_info, platform, channel_row_id, created).
        Raises RegistrationError.
        """
        link = (link or "").strip().strip("<>")
        if minecraft is not None and not valid_minecraft(minecraft):
            raise RegistrationError("bad_minecraft")
        if is_youtube_url(link):
            platform, client = "youtube", self.youtube
        elif is_aparat_url(link):
            platform, client = "aparat", self.aparat
        else:
            raise RegistrationError("bad_link")

        try:
            info = await client.resolve(link)
        except PlatformError as e:
            fallback = client.offline_resolve(link) if allow_unverified else None
            definitely_missing = isinstance(e, AparatError) and e.kind == "not_found"
            if not fallback or definitely_missing:
                raise RegistrationError("not_found", str(e))
            info = fallback
            await self.log(f"Couldn't verify the {platform} link `{link}` right now ({e}). "
                           f"It was saved as `{info['external_id']}`.")

        existing = await self.db.one("SELECT * FROM channels WHERE platform = ? AND external_id = ?",
                                     (platform, info["external_id"]))
        if existing and existing["discord_id"] != member_id:
            raise RegistrationError("taken", other_id=existing["discord_id"])

        now = utils.now_ts()
        await self.db.upsert_member(member_id, kind, now)
        if minecraft:
            await self.db.set_minecraft(member_id, minecraft)
        if existing:
            return info, platform, existing["id"], False

        row_id = await self.db.insert(
            "INSERT INTO channels(discord_id, platform, external_id, name, url, added_at, last_video_check) "
            "VALUES(?,?,?,?,?,?,?)",
            (member_id, platform, info["external_id"], info["name"], info["url"], now, now))
        try:
            await self.baseline(platform, info["external_id"])
        except Exception as e:
            log.warning("baseline failed: %s", e)
        return info, platform, row_id, True

    async def baseline(self, platform: str, external_id: str) -> None:
        """Mark what a channel already has as seen, so only *new* stuff is announced.
        Whatever is live/scheduled on YouTube right now is left unseen so it gets tracked."""
        if platform == "youtube":
            ids = await self.youtube.rss_ids(external_id)
            live_id = await self.youtube.live_page_id(external_id)
            await self.db.mark_seen("youtube", [v for v in ids if v != live_id])
        else:
            vids = await self.aparat.user_videos(external_id)
            await self.db.mark_seen("aparat", [v["uid"] for v in vids])

    # ---------------------------------------------------------------- minecraft usernames
    async def ask_minecraft(self, discord_id: int) -> bool:
        """DM a member asking for their Minecraft username. Returns True if the DM was sent."""
        try:
            user = self.get_user(discord_id) or await self.fetch_user(discord_id)
            embed = style.pending(
                f"Hi **{self.member_name(discord_id)}**,\n"
                f"We now keep the Minecraft username of every {self.cfg.server_name} media member.\n\n"
                f"Please click the button below and enter your Minecraft username.",
                title="Minecraft username needed")
            await user.send(embed=embed, view=minecraft_view())
            await self.db.execute("UPDATE members SET mc_asked = 1 WHERE discord_id = ?", (discord_id,))
            return True
        except discord.HTTPException:
            await self.db.execute("UPDATE members SET mc_asked = 2 WHERE discord_id = ?", (discord_id,))
            return False


def main():
    try:
        cfg = load_config("config.yml")
    except ConfigError as e:
        print(f"[config.yml] {e}")
        sys.exit(1)
    bot = MediaBot(cfg)
    bot.run(cfg.token, log_handler=None)


if __name__ == "__main__":
    main()
