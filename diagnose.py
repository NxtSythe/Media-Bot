"""Test the Aparat endpoints from this server.

Usage:   python diagnose.py <aparat_username>
"""
import asyncio
import os
import sys
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

import aiohttp

from core import utils
from core.config import ConfigError, load_config
from platforms.aparat import AparatClient


async def main(username: str):
    try:
        cfg = load_config("config.yml")
        utils.set_timezone(cfg.timezone)
        urls = cfg.aparat_urls
        proxy = cfg.aparat_proxy
    except ConfigError as e:
        print(f"[config.yml] {e} - using default Aparat endpoints")
        from core.config import _aparat_urls
        urls = _aparat_urls({})
        proxy = None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
        client = AparatClient(session, urls, proxy)
        for line in await client.diagnose(username):
            print(line)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python diagnose.py <aparat_username>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
