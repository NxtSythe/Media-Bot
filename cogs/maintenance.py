"""Daily database backups + saving the database to GitHub."""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from discord.ext import commands, tasks

log = logging.getLogger("maintenance")

BACKUP_DIR = Path("backups")
KEEP = 7


class Maintenance(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._save_errors = 0

    async def cog_load(self):
        self.backup_loop.start()
        self.save_loop.start()

    async def cog_unload(self):
        self.backup_loop.cancel()
        self.save_loop.cancel()

    @tasks.loop(hours=3)
    async def backup_loop(self):
        today = BACKUP_DIR / f"data-{dt.date.today().isoformat()}.db"
        if today.exists():
            return
        try:
            path = await self.bot.db.backup(BACKUP_DIR, keep=KEEP)
            log.info("database backup written: %s", path)
            if getattr(self.bot, "store_ready", False):
                await self.bot.save_data(backup_file=path)
        except Exception as e:
            log.exception("backup failed")
            await self.bot.log(f"Daily database backup failed: `{e}`", throttle_key="backup", level="error")

    @tasks.loop(minutes=10)
    async def save_loop(self):
        """Save the database to GitHub every 10 minutes (only when something changed)."""
        if not getattr(self.bot, "store_ready", False):
            return
        try:
            await self.bot.save_data()
            self._save_errors = 0
        except Exception as e:
            self._save_errors += 1
            log.warning("saving to GitHub failed: %s", e)
            if self._save_errors >= 3:
                await self.bot.log(f"Saving the database to GitHub keeps failing: `{str(e)[:300]}`",
                                   throttle_key="gh-save", level="error")

    @save_loop.before_loop
    async def _before_save(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Maintenance(bot))
