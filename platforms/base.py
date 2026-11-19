from __future__ import annotations

import re
from datetime import datetime


class PlatformError(Exception):
    """Error with a message that can be shown to the admin."""


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

_TO_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def parse_iso_ts(value) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def to_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).translate(_TO_EN).replace(",", "").replace("٬", "").strip()
    return int(s) if s.isdigit() else None


def parse_duration(value) -> int | None:
    """Seconds from 123 / "123" / "10:23" / "1:02:03"."""
    n = to_int(value)
    if n is not None:
        return n
    s = str(value or "").translate(_TO_EN).strip()
    if re.fullmatch(r"\d{1,3}(:\d{1,2}){1,2}", s):
        total = 0
        for part in s.split(":"):
            total = total * 60 + int(part)
        return total
    return None
