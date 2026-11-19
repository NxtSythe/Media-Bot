"""Shared registration helpers (kept out of bot.py so every module can import them)."""
from __future__ import annotations

import re

MC_NAME_RE = re.compile(r"^[A-Za-z0-9_.]{3,20}$")


class RegistrationError(Exception):
    """code: bad_link | not_found | taken | bad_minecraft"""

    def __init__(self, code: str, detail: str = "", other_id: int | None = None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.other_id = other_id


def valid_minecraft(name: str) -> bool:
    return bool(MC_NAME_RE.fullmatch(name or ""))
