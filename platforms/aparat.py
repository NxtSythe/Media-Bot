"""Aparat client.

Aparat has no official live API, so this reads the public endpoints the
website uses. Every kind of data has a list of endpoints (config.yml ->
aparat) and several request "profiles" (different headers). The client tries
them in order, remembers whichever worked, and retries on 5xx errors - so a
single endpoint going down (e.g. HTTP 503) no longer breaks tracking.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse

import aiohttp

from core.utils import parse_any_time
from .base import BROWSER_UA, PlatformError, parse_duration, to_int

log = logging.getLogger("aparat")

RESERVED = {"v", "result", "playlist", "video", "api", "etc", "live", "tag", "category", "rss",
            "explore", "dashboard", "signin", "login", "upload", "search", "channel"}
_TAG_RE = re.compile(r"<[^>]+>")
_UID_IN_LINK = re.compile(r"/v/([A-Za-z0-9]+)")

PROFILES: dict[str, dict] = {
    "plain": {"User-Agent": "Mozilla/5.0 (compatible; LunaMCMediaBot/1.1)",
              "Accept": "application/json, application/xml, text/xml, */*"},
    "browser": {"User-Agent": BROWSER_UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
                "Referer": "https://www.aparat.com/",
                "Origin": "https://www.aparat.com"},
    "bare": {},
}
RETRY_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


class AparatError(PlatformError):
    """kind: 'unreachable' (network / 5xx / blocked), 'not_found' (404) or 'bad_response'."""

    def __init__(self, message: str, kind: str = "unreachable"):
        super().__init__(message)
        self.kind = kind


def is_aparat_url(url: str) -> bool:
    return "aparat.com" in url.lower()


def strip_html(text) -> str:
    return html.unescape(_TAG_RE.sub(" ", str(text or ""))).strip()


def _find_key(obj, keys: tuple[str, ...]):
    """Breadth-first search for the first non-empty value under any of `keys`."""
    stack = [obj]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k in keys:
                if k in cur and cur[k] not in (None, "", [], {}):
                    return cur[k]
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _video_dicts(obj) -> list[dict]:
    """Collect every dict that looks like a video (has uid + title), in order."""
    found, stack = [], [obj]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            src = cur.get("attributes") if isinstance(cur.get("attributes"), dict) else cur
            if src.get("uid") and src.get("title"):
                found.append(src)
                continue
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return found


def _video_info(d: dict) -> dict:
    uid = str(d.get("uid"))
    views = to_int(d.get("visit_cnt_int"))
    if views is None:
        views = to_int(d.get("visit_cnt"))
    return {
        "uid": uid,
        "title": strip_html(d.get("title")),
        "description": strip_html(d.get("description") or d.get("descr") or ""),
        "views": views,
        "duration": parse_duration(d.get("duration")),
        "thumbnail": d.get("big_poster") or d.get("medium_poster") or d.get("small_poster"),
        "username": d.get("username"),
        "name": d.get("sender_name") or d.get("username"),
        "url": f"https://www.aparat.com/v/{uid}",
    }


def _rss_videos(root: ET.Element) -> list[dict]:
    out = []
    for item in root.iter():
        if item.tag.split("}")[-1] not in ("item", "entry"):
            continue
        fields = {c.tag.split("}")[-1]: (c.text or c.attrib.get("href") or "") for c in item}
        link = fields.get("link") or fields.get("guid") or ""
        m = _UID_IN_LINK.search(link)
        if not m:
            continue
        thumb = None
        for c in item:
            if c.tag.split("}")[-1] in ("thumbnail", "content", "enclosure") and c.attrib.get("url"):
                thumb = c.attrib["url"]
                break
        out.append({"uid": m.group(1), "title": strip_html(fields.get("title")),
                    "description": strip_html(fields.get("description")), "views": None, "duration": None,
                    "thumbnail": thumb, "username": None, "name": None,
                    "url": f"https://www.aparat.com/v/{m.group(1)}"})
    return out


def _parse_body(text: str):
    """JSON first, then XML. Returns None for HTML pages (blocked / challenge pages)."""
    t = text.lstrip()
    if t.startswith("{") or t.startswith("["):
        try:
            return json.loads(t)
        except ValueError:
            return None
    if t.startswith("<?xml") or t.startswith("<rss") or t.startswith("<feed"):
        try:
            return ET.fromstring(t)
        except ET.ParseError:
            return None
    return None


class AparatClient:
    def __init__(self, session: aiohttp.ClientSession, urls: dict, proxy: str | None = None):
        self.session = session
        self.urls = urls
        self.proxy = proxy or None
        self.good_profile = "plain"
        self.good_url: dict[str, int] = {}
        self.timeout = aiohttp.ClientTimeout(total=15)

    # -------------------------------------------------------------- low level
    async def _raw(self, url: str, profile: str) -> tuple[int, str, str]:
        async with self.session.get(url, headers=PROFILES[profile], allow_redirects=True,
                                    proxy=self.proxy, timeout=self.timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), await r.text(errors="replace")

    async def _get(self, url: str):
        """Fetch + parse one URL. None = 404.

        Uses the header profile that worked last time and tries one other
        profile only if the first attempt failed - at most 2 requests, so a
        struggling Aparat isn't hammered.
        """
        others = [p for p in PROFILES if p != self.good_profile]
        errors, saw_404 = [], False
        for prof in [self.good_profile, others[0]]:
            try:
                status, _, text = await self._raw(url, prof)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                errors.append(f"network error ({e.__class__.__name__})")
                continue
            if status == 404:
                saw_404 = True
                break
            if status == 200:
                data = _parse_body(text)
                if data is not None:
                    self.good_profile = prof
                    return data
                errors.append("got a web page instead of data (blocked or challenge page)")
                continue
            errors.append(f"HTTP {status}")
        if saw_404:
            return None
        raise AparatError(", ".join(dict.fromkeys(errors)) or "no response", "unreachable")

    def _ordered(self, kind: str) -> list[tuple[int, str]]:
        urls = list(enumerate(self.urls[kind]))
        good = self.good_url.get(kind)
        if good is not None:
            urls.sort(key=lambda x: x[0] != good)
        return urls

    async def _try_all(self, kind: str, parse, **fmt):
        """Try each endpoint of `kind`; return the first non-empty parsed result."""
        errors, any_ok, first_empty = [], False, None
        all_404 = True
        for idx, tmpl in self._ordered(kind):
            url = tmpl.format(**fmt)
            try:
                data = await self._get(url)
            except PlatformError as e:
                all_404 = False
                errors.append(f"{_short(url)}: {e}")
                continue
            if data is None:
                errors.append(f"{_short(url)}: HTTP 404")
                continue
            all_404 = False
            result = parse(data)
            if result:
                self.good_url[kind] = idx
                return result
            any_ok = True
            first_empty = result if first_empty is None else first_empty
            errors.append(f"{_short(url)}: unexpected response")
        if any_ok:
            return first_empty
        msg = " | ".join(errors) or "no endpoint configured"
        if errors and all_404:
            raise AparatError(msg, "not_found")
        if all(("unexpected response" in e) for e in errors):
            raise AparatError(msg, "bad_response")
        raise AparatError(msg, "unreachable")

    # -------------------------------------------------------------- resolving
    @staticmethod
    def username_from_url(url: str) -> str | None:
        """Channel username straight from a channel / live link, without asking Aparat."""
        parts = [unquote(p) for p in urlparse(url if "://" in url else "https://" + url).path.split("/") if p]
        if not parts or parts[0].lower() in RESERVED:
            return None
        return parts[0] if re.fullmatch(r"[A-Za-z0-9_.\-]{2,64}", parts[0]) else None

    def offline_resolve(self, url: str) -> dict | None:
        username = self.username_from_url(url)
        if not username:
            return None
        return {"external_id": username, "name": username, "url": f"https://www.aparat.com/{username}"}

    async def resolve(self, url: str) -> dict:
        parts = [unquote(p) for p in urlparse(url if "://" in url else "https://" + url).path.split("/") if p]
        if not parts:
            raise AparatError("That is not a valid Aparat link.", "not_found")
        if parts[0].lower() == "v" and len(parts) > 1:
            info = await self.video(parts[1])
            if not info or not info.get("username"):
                raise AparatError("That Aparat video was not found (or Aparat is not reachable). "
                                  "Send the channel link instead.", "not_found")
            username = info["username"]
        elif parts[0].lower() in RESERVED:
            raise AparatError("Can't find the channel from this link. Send the channel, live or a video link.",
                              "not_found")
        else:
            username = parts[0]

        name = None
        try:
            prof = await self._try_all("profile", lambda d: d if _find_key(d, ("username", "name")) else None,
                                       username=username)
            if prof:
                name = _find_key(prof, ("name", "sender_name"))
        except PlatformError:
            prof = None
        if not prof:
            # fall back to the live endpoint to at least confirm the channel exists
            try:
                await self.live_status(username)
            except AparatError as e:
                raise AparatError(f"Aparat channel '{username}' could not be checked: {e}", e.kind)
        return {
            "external_id": username,
            "name": strip_html(name) if name else username,
            "url": f"https://www.aparat.com/{username}",
        }

    # -------------------------------------------------------------- live
    async def live_status(self, username: str) -> dict:
        """Current live state plus the times of the last session."""
        def parse(d):
            if not isinstance(d, dict):
                return None
            status = d.get("live_status") if isinstance(d.get("live_status"), dict) else _find_key(d, ("live_status",))
            return d if isinstance(status, dict) else None

        data = await self._try_all("live", parse, username=username)
        if not data:
            raise AparatError("live endpoint returned an unexpected response", "bad_response")
        status = data.get("live_status") if isinstance(data.get("live_status"), dict) else _find_key(data, ("live_status",))
        stat = data.get("stat") if isinstance(data.get("stat"), dict) else (_find_key(data, ("stat",)) or {})
        if not isinstance(stat, dict):
            stat = {}
        attrs = status.get("attributes") if isinstance(status.get("attributes"), dict) else {}
        s_start = data.get("last_session_start_time")
        s_end = data.get("last_session_end_time")
        return {
            "live": str(status.get("type", "")).lower() == "connected",
            "status_type": status.get("type"),
            "title": strip_html(data.get("title")) or "Aparat live stream",
            "description": strip_html(data.get("descr")),
            "online": to_int(stat.get("online_cnt")),
            "peak": to_int(stat.get("peak_cnt")),
            "session_start_raw": s_start,
            "session_end_raw": s_end,
            "start_raw": data.get("startTime"),
            "session_start": parse_any_time(s_start),
            "session_end": parse_any_time(s_end),
            "thumbnail": attrs.get("cover"),
            "url": f"https://www.aparat.com/{username}/live",
        }

    # -------------------------------------------------------------- videos
    @staticmethod
    def _parse_video_list(data):
        if isinstance(data, ET.Element):
            return _rss_videos(data)
        return [_video_info(d) for d in _video_dicts(data)]

    async def user_videos(self, username: str) -> list[dict]:
        return await self._try_all("user_videos", self._parse_video_list, username=username) or []

    async def video(self, uid: str) -> dict | None:
        def parse(d):
            items = _video_dicts(d) if not isinstance(d, ET.Element) else []
            return _video_info(items[0]) if items else None
        try:
            return await self._try_all("video", parse, uid=uid)
        except PlatformError:
            return None

    # -------------------------------------------------------------- diagnostics
    async def diagnose(self, username: str) -> list[str]:
        """Human-readable report of what every endpoint returns from this server."""
        lines = [f"Aparat diagnostics for '{username}'", f"Proxy: {self.proxy or 'none'}", ""]
        sample_uid = None
        for kind in ("live", "user_videos", "profile", "video"):
            lines.append(f"[{kind}]")
            if kind == "video" and not sample_uid:
                lines.append("  skipped (no video uid found)")
                continue
            for tmpl in self.urls[kind]:
                url = tmpl.format(username=username, uid=sample_uid or "")
                lines.append(f"  {url}")
                for prof in PROFILES:
                    try:
                        status, ctype, text = await self._raw(url, prof)
                    except Exception as e:
                        lines.append(f"    {prof:8} -> {e.__class__.__name__}: {e}")
                        continue
                    data = _parse_body(text) if status == 200 else None
                    kind_txt = "json" if isinstance(data, (dict, list)) else "xml" if data is not None else "not data"
                    extra = ""
                    if data is not None and kind == "user_videos":
                        vids = self._parse_video_list(data)
                        extra = f", {len(vids)} videos"
                        if vids and not sample_uid:
                            sample_uid = vids[0]["uid"]
                    snippet = " ".join(text[:160].split())
                    lines.append(f"    {prof:8} -> HTTP {status}, {ctype.split(';')[0] or '?'}, {kind_txt}{extra}")
                    if data is None:
                        lines.append(f"               body: {snippet}")
            lines.append("")
        try:
            live = await self.live_status(username)
            lines.append("[parsed live status]")
            for k in ("live", "status_type", "online", "peak", "session_start_raw", "session_end_raw", "start_raw",
                      "session_start", "session_end"):
                lines.append(f"  {k}: {live.get(k)!r}")
        except PlatformError as e:
            lines.append(f"[parsed live status] FAILED: {e}")
        return lines


def _short(url: str) -> str:
    p = urlparse(url)
    path = p.path if len(p.path) < 60 else p.path[:57] + "..."
    return f"{p.netloc}{path}"
