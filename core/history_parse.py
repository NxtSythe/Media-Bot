"""Read the bot's own old messages back into data.

Understands every message format the bot has used (the first Persian
version and the later English versions), so the database can be rebuilt
from the live-history, video-history and log channels.
"""
from __future__ import annotations

import re
from datetime import datetime

import jdatetime

from . import utils

FA_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
             "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
_TO_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
MENTION_RE = re.compile(r"<@!?(\d{15,25})>")
YT_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/live/|/shorts/)([A-Za-z0-9_-]{11})")
APARAT_LIVE_RE = re.compile(r"aparat\.com/([A-Za-z0-9_.\-]+)/live", re.I)
APARAT_VIDEO_RE = re.compile(r"aparat\.com/v/([A-Za-z0-9]+)", re.I)
KIND_FROM_TEXT = {"streamer and content creator": "both", "both": "both", "streamer": "streamer",
                  "content creator": "creator"}


def en(text) -> str:
    return str(text or "").translate(_TO_EN)


def to_int(text) -> int | None:
    s = en(text).replace(",", "").replace("٬", "").strip()
    m = re.search(r"\d+", s)
    return int(m.group(0)) if m and not re.search(r"unknown|نامشخص", s, re.I) else None


def parse_duration_text(text) -> int | None:
    s = en(text)
    if re.search(r"unknown|نامشخص", s, re.I):
        return None
    if "کمتر از یک دقیقه" in s:
        return 30
    h = re.search(r"(\d+)\s*(?:h\b|ساعت)", s)
    m = re.search(r"(\d+)\s*(?:m\b|دقیقه)", s)
    sec = re.search(r"(\d+)\s*(?:s\b|ثانیه)", s)
    if not (h or m or sec):
        return None
    return (int(h.group(1)) * 3600 if h else 0) + (int(m.group(1)) * 60 if m else 0) + (int(sec.group(1)) if sec else 0)


def parse_jalali(date_text, time_text=None) -> int | None:
    """'12 Aban 1405' / '۱۲ آبان ۱۴۰۵' (+ '17:30') -> unix time."""
    s = en(date_text).strip()
    m = re.search(r"(\d{1,2})\s+(\S+)\s+(\d{4})", s)
    if not m:
        return None
    day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = None
    for i, (e_name, f_name) in enumerate(zip(utils.MONTHS, FA_MONTHS)):
        if month_name.lower() == e_name.lower() or month_name == f_name:
            month = i + 1
            break
    if not month:
        return None
    hh = mm = 0
    t = re.search(r"(\d{1,2}):(\d{2})", en(time_text or s[m.end():]))
    if t:
        hh, mm = int(t.group(1)), int(t.group(2))
    try:
        g = jdatetime.datetime(year, month, day, hh, mm).togregorian()
    except ValueError:
        return None
    return int(datetime(g.year, g.month, g.day, g.hour, g.minute, tzinfo=utils.TZ).timestamp())


def related_from_text(text) -> int | None:
    s = str(text or "")
    if re.search(r"not server-related|مربوط به سرور نیست", s, re.I):
        return 0
    if re.search(r"awaiting|منتظر", s, re.I):
        return None
    if re.search(r"server-related|مربوط به سرور", s, re.I):
        return 1
    return None


def _fields(e: dict) -> dict:
    return {f.get("name", ""): f.get("value", "") for f in e.get("fields", []) or []}


def _field(fields: dict, *names) -> str:
    for k, v in fields.items():
        if any(n.lower() in k.lower() for n in names):
            return v
    return ""


def _platform(fields: dict, url: str) -> str | None:
    p = _field(fields, "platform", "پلتفرم").lower()
    if "youtube" in p or "یوتیوب" in p or "youtu" in (url or ""):
        return "youtube"
    if "aparat" in p or "آپارات" in p or "aparat" in (url or ""):
        return "aparat"
    return None


def _member(fields: dict, e: dict) -> int | None:
    for text in (_field(fields, "media", "مدیا"), *fields.values(), e.get("description", "")):
        m = MENTION_RE.search(text or "")
        if m:
            return int(m.group(1))
    return None


def _status(fields: dict, footer: str) -> int | None:
    status = _field(fields, "status")
    return related_from_text(status or footer)


# ------------------------------------------------------------------ live-history
def parse_live_embed(e: dict, msg_ts: int) -> dict | None:
    desc = e.get("description") or ""
    fields = _fields(e)
    footer = (e.get("footer") or {}).get("text", "")
    url = e.get("url") or ""
    discord_id = _member(fields, e)
    platform = _platform(fields, url)
    if not discord_id or not platform:
        return None
    if not re.search(r"Went live for|به مدت", desc):
        return None

    sid = re.search(r"(?:Stream ID|کد استریم)\s*:\s*(\d+)", en(footer))
    name = re.search(r"\*\*(.+?)\*\*", desc)
    dur = re.search(r"(?:Went live for|به مدت)\s*\*\*(.+?)\*\*", desc)
    duration = parse_duration_text(dur.group(1)) if dur else None
    when = (re.search(r"on \*\*(.+?)\*\* at \*\*(.+?)\*\*", desc)
            or re.search(r"تاریخ \*\*(.+?)\*\*\s*ساعت \*\*(.+?)\*\*", desc))
    started = parse_jalali(when.group(1), when.group(2)) if when else None
    peak_m = re.search(r"(?:Peak viewers|بیشترین بیننده)[^*]*\*\*([^*]+)\*\*", desc)
    peak = to_int(peak_m.group(1)) if peak_m else None

    if started is None:
        started = msg_ts - (duration or 0)
    ended = started + duration if duration is not None else msg_ts

    key = None
    if platform == "youtube":
        m = YT_ID_RE.search(url)
        key = m.group(1) if m else None
    else:
        m = APARAT_LIVE_RE.search(url)
        key = f"{m.group(1)}:{started}" if m else None
    return {
        "id": int(sid.group(1)) if sid else None,
        "discord_id": discord_id,
        "platform": platform,
        "item_key": key,
        "aparat_username": APARAT_LIVE_RE.search(url).group(1) if APARAT_LIVE_RE.search(url) else None,
        "channel_name": name.group(1).strip() if name else "",
        "title": e.get("title") or "",
        "url": url,
        "thumbnail": (e.get("thumbnail") or {}).get("url"),
        "started_at": started,
        "ended_at": ended,
        "peak_viewers": peak,
        "server_related": _status(fields, footer),
    }


# ------------------------------------------------------------------ video-history
def parse_video_embed(e: dict, msg_ts: int) -> dict | None:
    desc = e.get("description") or ""
    fields = _fields(e)
    footer = (e.get("footer") or {}).get("text", "")
    url = e.get("url") or ""
    discord_id = _member(fields, e)
    platform = _platform(fields, url)
    if not discord_id or not platform:
        return None
    if not re.search(r"uploaded a new video|ویدیو جدید آپلود کرد", desc):
        return None

    vid = re.search(r"(?:Video ID|کد ویدیو)\s*:\s*(\d+)", en(footer))
    name = (re.search(r"\*\*(.+?)\*\* uploaded a new video", desc)
            or re.search(r"\*\*(.+?)\*\* یه ویدیو جدید", desc))
    views_m = re.search(r"\*\*(?:Views|تعداد ویو):\*\*\s*([^\n]+)", desc)
    length_m = re.search(r"\*\*(?:Length|مدت ویدیو):\*\*\s*([^\n]+)", desc)
    up = (re.search(r"\*\*Uploaded:\*\*\s*([^,\n]+),\s*(\d{1,2}:\d{2})", desc)
          or re.search(r"\*\*تاریخ آپلود:\*\*\s*(.+?)\s*ساعت\s*([\d۰-۹]{1,2}:[\d۰-۹]{2})", desc))
    published = parse_jalali(up.group(1), up.group(2)) if up else None

    key = None
    if platform == "youtube":
        m = YT_ID_RE.search(url)
        key = m.group(1) if m else None
    else:
        m = APARAT_VIDEO_RE.search(url)
        key = m.group(1) if m else None
    return {
        "id": int(vid.group(1)) if vid else None,
        "discord_id": discord_id,
        "platform": platform,
        "item_key": key,
        "channel_name": name.group(1).strip() if name else "",
        "title": e.get("title") or "",
        "url": url,
        "thumbnail": (e.get("image") or {}).get("url"),
        "published_at": published or msg_ts,
        "duration_sec": parse_duration_text(length_m.group(1)) if length_m else None,
        "views": to_int(views_m.group(1)) if views_m else None,
        "server_related": _status(fields, footer),
    }


# ------------------------------------------------------------------ log channel
def parse_log_text(text: str) -> dict | None:
    """Registration and Minecraft-username log messages."""
    m = re.search(r"New media member registered:\s*<@!?(\d+)>", text)
    if m:
        out = {"type": "register", "discord_id": int(m.group(1))}
        ch = re.search(r"Channel:\s*\[(.+?)\]\((\S+?)\)\s*\((YouTube|Aparat)", text)
        if ch:
            out.update(channel_name=ch.group(1), channel_url=ch.group(2), platform=ch.group(3).lower())
        mc = re.search(r"Minecraft:\s*`([^`]+)`", text)
        if mc:
            out["minecraft"] = mc.group(1)
        kind = re.search(r"Type:\s*([A-Za-z ]+)", text)
        if kind:
            out["kind"] = KIND_FROM_TEXT.get(kind.group(1).strip().lower())
        return out
    m = re.search(r"<@!?(\d+)> set their Minecraft username:\s*`([^`]+)`", text)
    if m:
        return {"type": "minecraft", "discord_id": int(m.group(1)), "minecraft": m.group(2)}
    return None
