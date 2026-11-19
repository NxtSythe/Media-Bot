"""YouTube tracking - no API key needed.

* New uploads / streams: the channel's free RSS feed.
* Live detection: the channel's /live page.
* Details (live state, viewers, views, length): the public watch page.

If a YouTube Data API key is set in config.yml it is used for details
instead (more reliable), but it is optional.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import unquote

import aiohttp

from core.utils import parse_any_time
from .base import BROWSER_UA, PlatformError, parse_iso_ts, to_int

log = logging.getLogger("youtube")

API = "https://www.googleapis.com/youtube/v3/"
RSS = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
LIVE_PAGE = "https://www.youtube.com/channel/{cid}/live?hl=en&gl=US"
WATCH = "https://www.youtube.com/watch?v={vid}&hl=en&gl=US&persist_hl=1&has_verified=1"

VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/live/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")
CHANNEL_ID_RE = re.compile(r"/channel/(UC[A-Za-z0-9_-]{22})")
HANDLE_RE = re.compile(r"/@([^/?#]+)")
USER_RE = re.compile(r"/user/([^/?#]+)")
CUSTOM_RE = re.compile(r"/c/([^/?#]+)")
CANONICAL_WATCH_RE = re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})"')
CHANNEL_IN_PAGE_RES = [
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'<meta itemprop="identifier" content="(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'"externalId":"(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'"channelId":"(UC[A-Za-z0-9_-]{22})"'),
]
OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')

HEADERS = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
COOKIES = {"CONSENT": "YES+cb", "SOCS": "CAI"}

_DUR_RE = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")


def parse_iso_duration(value: str | None) -> int:
    m = _DUR_RE.fullmatch(value or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def is_youtube_url(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url, re.I))


# ======================================================================= API format
def describe(item: dict) -> dict:
    """Normalise a videos.list item (API mode)."""
    sn = item.get("snippet", {})
    cd = item.get("contentDetails", {})
    st = item.get("statistics", {})
    lsd = item.get("liveStreamingDetails")
    lbc = sn.get("liveBroadcastContent", "none")
    duration = parse_iso_duration(cd.get("duration"))
    is_premiere = bool(lsd) and lbc in ("live", "upcoming") and duration > 0
    is_stream = bool(lsd) and not is_premiere

    state = "none"
    if lsd:
        if lsd.get("actualEndTime"):
            state = "ended"
        elif lsd.get("actualStartTime"):
            state = "live"
        else:
            state = "upcoming"

    thumbs = sn.get("thumbnails", {})
    thumb = None
    for q in ("maxres", "standard", "high", "medium", "default"):
        if q in thumbs:
            thumb = thumbs[q].get("url")
            break

    lsd = lsd or {}
    return {
        "id": item["id"],
        "channel_id": sn.get("channelId"),
        "channel_title": sn.get("channelTitle") or "",
        "title": sn.get("title") or "",
        "description": sn.get("description") or "",
        "published_at": parse_iso_ts(sn.get("publishedAt")),
        "thumbnail": thumb,
        "duration": duration,
        "views": to_int(st.get("viewCount")),
        "is_stream": is_stream,
        "state": state,
        "actual_start": parse_iso_ts(lsd.get("actualStartTime")),
        "actual_end": parse_iso_ts(lsd.get("actualEndTime")),
        "concurrent": to_int(lsd.get("concurrentViewers")),
        "url": f"https://www.youtube.com/watch?v={item['id']}",
    }


# ======================================================================= web page format
def extract_json(html: str, var: str):
    """Pull `var = {...}` JSON out of a YouTube page."""
    for marker in (f"var {var} = ", f"{var} = ", f'window["{var}"] = '):
        start = 0
        while True:
            i = html.find(marker, start)
            if i == -1:
                break
            try:
                obj, _ = json.JSONDecoder().raw_decode(html, i + len(marker))
                return obj
            except ValueError:
                start = i + len(marker)
    return None


def _meta(html: str, prop: str) -> str | None:
    m = re.search(r'<meta itemprop="%s" content="([^"]*)"' % re.escape(prop), html)
    return m.group(1) if m else None


def _concurrent(html: str) -> int | None:
    m = re.search(r'"originalViewCount":"(\d+)"', html)
    if m:
        return int(m.group(1))
    m = re.search(r'"viewCount":\{"runs":\[\{"text":"([\d,]+)"\},\{"text":" watching(?: now)?"\}', html)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def describe_web(vid: str, html: str) -> dict | None:
    """Normalise a watch page. Returns None when the page has no usable info."""
    pr = extract_json(html, "ytInitialPlayerResponse") or {}
    vd = pr.get("videoDetails") or {}
    mf = (pr.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    lbd = mf.get("liveBroadcastDetails") or {}

    title = vd.get("title") or ((mf.get("title") or {}).get("simpleText")) or None
    channel_id = vd.get("channelId") or mf.get("externalChannelId") or _meta(html, "channelId")
    if not title:
        m = OG_TITLE_RE.search(html)
        title = m.group(1) if m else None
    if not channel_id or not title:
        return None

    duration = to_int(vd.get("lengthSeconds")) or to_int(mf.get("lengthSeconds"))
    if duration is None:
        duration = parse_iso_duration(_meta(html, "duration"))
    views = to_int(vd.get("viewCount")) or to_int(mf.get("viewCount")) or to_int(_meta(html, "interactionCount"))
    description = vd.get("shortDescription") or ((mf.get("description") or {}).get("simpleText")) or ""
    published = parse_any_time(mf.get("publishDate") or mf.get("uploadDate")
                               or _meta(html, "datePublished") or _meta(html, "uploadDate"))

    playability = (pr.get("playabilityStatus") or {}).get("status")
    is_live_content = vd.get("isLiveContent")
    if is_live_content is None:
        is_live_content = (_meta(html, "isLiveBroadcast") or "").lower() == "true"
    start_raw = lbd.get("startTimestamp") or _meta(html, "startDate")
    end_raw = lbd.get("endTimestamp") or _meta(html, "endDate")

    state = "none"
    if is_live_content:
        if end_raw:
            state = "ended"
        elif lbd.get("isLiveNow") or vd.get("isLive"):
            state = "live"
        elif vd.get("isUpcoming") or playability == "LIVE_STREAM_OFFLINE":
            state = "upcoming"
        elif duration:
            state = "ended"
        else:
            state = "live" if start_raw and (parse_any_time(start_raw) or 0) <= time.time() else "upcoming"

    actual_start = parse_any_time(start_raw) if state in ("live", "ended") else None
    actual_end = parse_any_time(end_raw) if state == "ended" else None
    if state == "ended" and not actual_end and actual_start and duration:
        actual_end = actual_start + duration

    thumbs = ((vd.get("thumbnail") or {}).get("thumbnails")) or []
    thumb = thumbs[-1]["url"] if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    return {
        "id": vid,
        "channel_id": channel_id,
        "channel_title": vd.get("author") or mf.get("ownerChannelName") or "",
        "title": title,
        "description": description,
        "published_at": published,
        "thumbnail": thumb.split("?")[0] if "i.ytimg.com" in thumb else thumb,
        "duration": duration or 0,
        "views": views,
        "is_stream": bool(is_live_content),
        "state": state,
        "actual_start": actual_start,
        "actual_end": actual_end,
        "concurrent": _concurrent(html) if state == "live" else None,
        "url": f"https://www.youtube.com/watch?v={vid}",
    }


def page_says_unavailable(html: str) -> bool:
    """True when YouTube clearly says the video doesn't exist (deleted / private)."""
    pr = extract_json(html, "ytInitialPlayerResponse") or {}
    ps = pr.get("playabilityStatus") or {}
    return ps.get("status") == "ERROR" and not pr.get("videoDetails")


# ======================================================================= client
class YouTubeClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str = "", check_live_page: bool = True):
        self.session = session
        self.key = api_key
        self.check_live_page = check_live_page
        self.sem = asyncio.Semaphore(3)
        self.timeout = aiohttp.ClientTimeout(total=20)

    @property
    def enabled(self) -> bool:
        return True

    @property
    def use_api(self) -> bool:
        return bool(self.key)

    # -------------------------------------------------------------- http
    async def _api(self, endpoint: str, **params) -> dict:
        params["key"] = self.key
        async with self.session.get(API + endpoint, params=params, timeout=self.timeout) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                msg = (data or {}).get("error", {}).get("message", f"HTTP {r.status}")
                raise PlatformError(f"YouTube API error: {msg}")
            return data

    async def _html(self, url: str) -> str | None:
        async with self.sem:
            async with self.session.get(url, headers=HEADERS, cookies=COOKIES, timeout=self.timeout) as r:
                if r.status == 404:
                    return None
                if r.status != 200:
                    raise PlatformError(f"YouTube returned HTTP {r.status}")
                return await r.text(errors="replace")

    # -------------------------------------------------------------- resolving
    async def resolve(self, url: str) -> dict:
        """Find the channel behind a channel / handle / video / live / shorts link."""
        cid = None
        if m := CHANNEL_ID_RE.search(url):
            cid = m.group(1)
        elif m := VIDEO_ID_RE.search(url):
            info = (await self.videos([m.group(1)])).get(m.group(1))
            if not info:
                raise PlatformError("That video was not found (it may be private or deleted).")
            cid = info["channel_id"]
        elif (m := HANDLE_RE.search(url)) or (m := USER_RE.search(url)) or (m := CUSTOM_RE.search(url)):
            if self.use_api and HANDLE_RE.search(url):
                data = await self._api("channels", part="id", forHandle="@" + unquote(m.group(1)))
                if data.get("items"):
                    cid = data["items"][0]["id"]
            if not cid:
                page_url = url.split("?")[0].rstrip("/")
                html = await self._html(page_url + ("&" if "?" in page_url else "?") + "hl=en")
                if html:
                    for rx in CHANNEL_IN_PAGE_RES:
                        if mm := rx.search(html):
                            cid = mm.group(1)
                            break
        if not cid:
            raise PlatformError("Couldn't find a YouTube channel from this link.")
        name = await self.channel_name(cid)
        if not name:
            raise PlatformError("YouTube channel not found.")
        return {"external_id": cid, "name": name, "url": f"https://www.youtube.com/channel/{cid}"}

    def offline_resolve(self, url: str) -> dict | None:
        m = CHANNEL_ID_RE.search(url)
        if not m:
            return None
        cid = m.group(1)
        return {"external_id": cid, "name": cid, "url": f"https://www.youtube.com/channel/{cid}"}

    async def channel_name(self, channel_id: str) -> str | None:
        root = await self._rss(channel_id)
        if root is None:
            return None
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for path in ("a:author/a:name", "a:title"):
            el = root.find(path, ns)
            if el is not None and (el.text or "").strip():
                return el.text.strip()
        return channel_id

    # -------------------------------------------------------------- discovery
    async def _rss(self, channel_id: str):
        try:
            async with self.session.get(RSS.format(cid=channel_id), headers=HEADERS, timeout=self.timeout) as r:
                if r.status != 200:
                    return None
                text = await r.text()
            return ET.fromstring(text)
        except (ET.ParseError, aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def rss_ids(self, channel_id: str) -> list[str]:
        """Latest ~15 uploads/streams from the free RSS feed."""
        root = await self._rss(channel_id)
        if root is None:
            return []
        ns = {"yt": "http://www.youtube.com/xml/schemas/2015", "a": "http://www.w3.org/2005/Atom"}
        return [e.text for e in root.findall("a:entry/yt:videoId", ns) if e.text]

    async def live_page_id(self, channel_id: str) -> str | None:
        """Video id shown at /channel/<id>/live (only present when live or scheduled)."""
        if not self.check_live_page:
            return None
        try:
            html = await self._html(LIVE_PAGE.format(cid=channel_id))
        except (PlatformError, aiohttp.ClientError, asyncio.TimeoutError):
            return None
        m = CANONICAL_WATCH_RE.search(html or "")
        return m.group(1) if m else None

    # -------------------------------------------------------------- details
    async def videos(self, ids: list[str]) -> dict[str, dict | None]:
        """Details for many videos.

        Result: {id: info} for found videos, {id: None} for videos that are
        definitely gone (deleted/private). Ids missing from the result could
        not be checked right now and should be retried later.
        """
        ids = list(dict.fromkeys(i for i in ids if i))
        if not ids:
            return {}
        if self.use_api:
            try:
                return await self._videos_api(ids)
            except (PlatformError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("YouTube API failed, using web pages instead: %s", e)
        return await self._videos_web(ids)

    async def _videos_api(self, ids: list[str]) -> dict[str, dict | None]:
        out: dict[str, dict | None] = {}
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            data = await self._api("videos", part="snippet,contentDetails,liveStreamingDetails,statistics",
                                   id=",".join(chunk), maxResults=50)
            found = {item["id"]: describe(item) for item in data.get("items", [])}
            for vid in chunk:
                out[vid] = found.get(vid)
        return out

    async def _videos_web(self, ids: list[str]) -> dict[str, dict | None]:
        out: dict[str, dict | None] = {}

        async def one(vid):
            try:
                html = await self._html(WATCH.format(vid=vid))
            except (PlatformError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.info("watch page %s failed: %s", vid, e)
                return
            if html is None or page_says_unavailable(html):
                out[vid] = None
                return
            info = describe_web(vid, html)
            if info:
                out[vid] = info

        await asyncio.gather(*(one(v) for v in ids))
        return out
