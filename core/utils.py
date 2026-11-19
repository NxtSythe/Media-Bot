"""Helpers: Jalali dates, durations, keyword matching."""
from __future__ import annotations

import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import jdatetime

TZ = ZoneInfo("Asia/Tehran")

MONTHS = [
    "Farvardin", "Ordibehesht", "Khordad", "Tir", "Mordad", "Shahrivar",
    "Mehr", "Aban", "Azar", "Dey", "Bahman", "Esfand",
]

_TO_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def set_timezone(name: str) -> None:
    global TZ
    TZ = ZoneInfo(name)


def now_ts() -> int:
    return int(time.time())


# ---------------------------------------------------------------- numbers
def num(n) -> str:
    if n is None:
        return "Unknown"
    return f"{int(n):,}"


# ---------------------------------------------------------------- dates (Jalali, English month names)
def to_local(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), TZ)


def jalali(ts: int) -> jdatetime.datetime:
    return jdatetime.datetime.fromgregorian(datetime=to_local(ts).replace(tzinfo=None))


def jdate(ts: int) -> str:
    j = jalali(ts)
    return f"{j.day} {MONTHS[j.month - 1]} {j.year}"


def jtime(ts: int) -> str:
    return to_local(ts).strftime("%H:%M")


def jdatetime_str(ts: int) -> str:
    return f"{jdate(ts)}, {jtime(ts)}"


def month_label(jy: int, jm: int) -> str:
    return f"{MONTHS[jm - 1]} {jy}"


def current_jalali_month(ts: int | None = None) -> tuple[int, int]:
    j = jalali(ts or now_ts())
    return j.year, j.month


def previous_jalali_month(jy: int, jm: int) -> tuple[int, int]:
    return (jy - 1, 12) if jm == 1 else (jy, jm - 1)


def _local_midnight_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())


def jalali_month_dates(jy: int, jm: int) -> tuple[date, date]:
    """Return (first gregorian date, first gregorian date of next month)."""
    start = jdatetime.date(jy, jm, 1).togregorian()
    ny, nm = (jy + 1, 1) if jm == 12 else (jy, jm + 1)
    end = jdatetime.date(ny, nm, 1).togregorian()
    return start, end


def jalali_month_range(jy: int, jm: int) -> tuple[int, int]:
    start, end = jalali_month_dates(jy, jm)
    return _local_midnight_ts(start), _local_midnight_ts(end)


def month_weeks(jy: int, jm: int) -> list[tuple[int, int, date, date]]:
    """Persian weeks (Saturday..Friday) whose Friday falls inside the month.

    Each week belongs to exactly one month, and every week of a month is
    finished by the time the month ends.
    Returns [(start_ts, end_ts, saturday_date, friday_date), ...]
    """
    start, end = jalali_month_dates(jy, jm)
    weeks = []
    d = start
    while d < end:
        if d.weekday() == 4:  # Friday
            sat = d - timedelta(days=6)
            weeks.append((_local_midnight_ts(sat), _local_midnight_ts(d + timedelta(days=1)), sat, d))
        d += timedelta(days=1)
    return weeks


def short_jdate(d: date) -> str:
    j = jdatetime.date.fromgregorian(date=d)
    return f"{j.day} {MONTHS[j.month - 1]}"


_DT_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?")


def parse_any_time(value) -> int | None:
    """Parse a timestamp in whatever shape a website returns it.

    Handles unix seconds/ms, ISO 8601 (with or without timezone), and
    'YYYY-MM-DD HH:MM[:SS]' in Gregorian or Jalali (years < 1700 are Jalali).
    Times without a timezone are taken as local (config timezone).
    """
    if value is None or value == "" or value is False:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v / 1000 if v > 1e12 else v) if v > 0 else None
    s = str(value).translate(_TO_EN).strip()
    if not s or s.lower() in ("null", "none", "0"):
        return None
    if s.isdigit():
        v = int(s)
        return int(v / 1000 if v > 1e12 else v) if v > 0 else None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.year >= 1700:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            return int(dt.timestamp())
    except ValueError:
        pass
    m = _DT_RE.search(s)
    if not m:
        return None
    y, mo, d, h, mi, se = (int(x) if x else 0 for x in m.groups())
    try:
        if y < 1700:
            g = jdatetime.datetime(y, mo, d, h, mi, se).togregorian()
            dt = datetime(g.year, g.month, g.day, g.hour, g.minute, g.second, tzinfo=TZ)
        else:
            dt = datetime(y, mo, d, h, mi, se, tzinfo=TZ)
    except ValueError:
        return None
    return int(dt.timestamp())


# ---------------------------------------------------------------- durations
def fmt_duration(seconds: int | None, with_seconds: bool = False) -> str:
    if seconds is None:
        return "Unknown"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if with_seconds and s:
        parts.append(f"{s}s")
    if not parts:
        return f"{s}s" if s else "0m"
    return " ".join(parts)


# ---------------------------------------------------------------- keywords
_STRIP_RE = re.compile(r"[\s\-_.,:;!|#@/\\()\[\]{}'\"`~*+=<>«»،؛​-‏‪-‮⁦-⁩]+")
_HARAKAT_RE = re.compile(r"[ً-ٰٟـ]")


def normalize_text(text: str | None) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = (text.replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")
                .replace("ة", "ه").replace("أ", "ا").replace("إ", "ا").replace("آ", "ا"))
    text = text.translate(_TO_EN)
    text = _HARAKAT_RE.sub("", text)
    return _STRIP_RE.sub("", text)


def has_keyword(text: str | None, keywords: list[str]) -> bool:
    n = normalize_text(text)
    if not n:
        return False
    for k in keywords:
        nk = normalize_text(k)
        if nk and nk in n:
            return True
    return False


def chunk_lines(lines: list[str], limit: int = 3900) -> list[str]:
    """Join lines into blocks no longer than `limit` characters."""
    blocks, cur = [], ""
    for line in lines:
        line = line[:limit]
        if len(cur) + len(line) + 1 > limit:
            blocks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        blocks.append(cur)
    return blocks
