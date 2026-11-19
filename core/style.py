"""Colour theme and small embed helpers.

Blue   = main / neutral
Green  = success
Yellow = pending / warning
Red    = failure / error
"""
from __future__ import annotations

import discord

BLUE = 0x3B82F6
GREEN = 0x22C55E
YELLOW = 0xEAB308
RED = 0xEF4444


def _embed(color: int, description: str, title: str | None = None) -> discord.Embed:
    e = discord.Embed(description=description[:4096], color=color)
    if title:
        e.title = title[:256]
    return e


def info(description: str, title: str | None = None) -> discord.Embed:
    return _embed(BLUE, description, title)


def success(description: str, title: str | None = None) -> discord.Embed:
    return _embed(GREEN, description, title)


def pending(description: str, title: str | None = None) -> discord.Embed:
    return _embed(YELLOW, description, title)


def error(description: str, title: str | None = None) -> discord.Embed:
    return _embed(RED, description, title)


def status_color(server_related) -> int:
    if server_related == 1:
        return GREEN
    if server_related == 0:
        return RED
    return YELLOW
