"""User-facing message builders (English, no emojis)."""
from __future__ import annotations

import discord

from . import style
from .utils import fmt_duration, jdate, jdatetime_str, jtime, num

PLATFORM_NAME = {"youtube": "YouTube", "aparat": "Aparat"}
KIND_NAME = {"streamer": "Streamer", "creator": "Content Creator", "both": "Streamer and Content Creator"}


def related_label(value) -> str:
    if value == 1:
        return "Server-related"
    if value == 0:
        return "Not server-related"
    return "Awaiting confirmation"


# ------------------------------------------------------------------ live-history
def live_embed(s: dict) -> discord.Embed:
    duration = (s["ended_at"] - s["started_at"]) if s.get("ended_at") and s.get("started_at") else None
    desc = (
        f"**{s['channel_name']}**\n"
        f"Went live for **{fmt_duration(duration)}** on **{jdate(s['started_at'])}** at **{jtime(s['started_at'])}**.\n"
        f"Peak viewers during this stream: **{num(s.get('peak_viewers'))}**"
    )
    e = discord.Embed(
        title=(s.get("title") or "Live stream")[:250],
        url=s.get("url") or None,
        description=desc,
        color=style.status_color(s.get("server_related")),
    )
    e.add_field(name="Media", value=f"<@{s['discord_id']}>", inline=True)
    e.add_field(name="Platform", value=PLATFORM_NAME.get(s["platform"], s["platform"]), inline=True)
    e.add_field(name="Status", value=related_label(s.get("server_related")), inline=True)
    e.set_footer(text=f"Stream ID: {s['id']}")
    if s.get("thumbnail"):
        e.set_thumbnail(url=s["thumbnail"])
    return e


# ------------------------------------------------------------------ video-history
def video_embed(v: dict) -> discord.Embed:
    desc = (
        f"**{v['channel_name']}** uploaded a new video!\n\n"
        f"**Title:** {v.get('title') or '-'}\n"
        f"**Views:** {num(v.get('views'))}\n"
        f"**Length:** {fmt_duration(v.get('duration_sec'), with_seconds=True) if v.get('duration_sec') is not None else 'Unknown'}\n"
        f"**Uploaded:** {jdatetime_str(v['published_at'])}"
    )
    e = discord.Embed(
        title=(v.get("title") or "New video")[:250],
        url=v.get("url") or None,
        description=desc,
        color=style.status_color(v.get("server_related")),
    )
    e.add_field(name="Media", value=f"<@{v['discord_id']}>", inline=True)
    e.add_field(name="Platform", value=PLATFORM_NAME.get(v["platform"], v["platform"]), inline=True)
    e.add_field(name="Status", value=related_label(v.get("server_related")), inline=True)
    updated = v.get("views_updated_at") or v["published_at"]
    e.set_footer(text=f"Video ID: {v['id']} | Views updated daily, last update: {jdatetime_str(updated)}")
    if v.get("thumbnail"):
        e.set_image(url=v["thumbnail"])
    return e


# ------------------------------------------------------------------ DM
def confirm_dm_embed(name: str, kind: str, title: str, url: str, server_name: str, live_now: bool) -> discord.Embed:
    if kind == "stream":
        action = "just started a stream" if live_now else "had a stream"
        what = "stream"
    else:
        action = "just uploaded a new video"
        what = "video"
    desc = (
        f"Hi **{name}**,\n"
        f"You {action}, but the server name (**{server_name}**) wasn't in its title or description.\n\n"
        f"**{title}**\n{url}\n\n"
        f"Was this {what} about the server? Pick **Yes** or **No** below.\n\n"
        f"Tip: add the server name to the title or description next time and I won't need to ask."
    )
    return style.pending(desc, title="Was this about the server?")


def confirm_done_embed(kind: str, answer: bool, already: bool = False) -> discord.Embed:
    what = "stream" if kind == "stream" else "video"
    if answer:
        e = style.success(f"Got it. This {what} is marked as server-related and counts toward your activity.",
                          title="Saved")
    else:
        e = style.error(f"Got it. This {what} is marked as not server-related and won't count toward your activity.",
                        title="Saved")
    if already:
        e.set_footer(text="This was already answered before.")
    return e
