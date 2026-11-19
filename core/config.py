"""Load and validate config.yml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


def _int(value, name: str, required: bool = True) -> int:
    try:
        v = int(value or 0)
    except (TypeError, ValueError):
        raise ConfigError(f"'{name}' in config.yml must be a number.")
    if required and v == 0:
        raise ConfigError(f"'{name}' is not set in config.yml.")
    return v


@dataclass
class Config:
    token: str
    guild_id: int
    youtube_api_key: str
    youtube_check_live_page: bool
    server_name: str
    keywords: list[str]
    ch_info: int
    ch_live_history: int
    ch_video_history: int
    ch_media_dt: int
    ch_log: int
    role_new: int
    role_media: int
    min_streams_per_week: int
    min_stream_minutes: int
    min_videos_per_month: int
    min_video_minutes: int
    report_day: int
    report_hour: int
    live_check_minutes: float
    video_check_minutes: float
    views_update_hours: float
    aparat_end_grace_minutes: float
    aparat_live_check_seconds: float
    views_update_max_days: int
    timezone: str
    database: str
    aparat_urls: dict
    aparat_proxy: str
    reg: dict
    info_title: str
    info_description: str
    info_button: str
    info_welcome: str
    raw: dict = field(default_factory=dict, repr=False)


APARAT_DEFAULTS = {
    "live": [
        "https://www.aparat.com/api/fa/v2/Live/LiveStream/show/username/{username}",
    ],
    "user_videos": [
        "https://www.aparat.com/etc/api/videoByUser/username/{username}/perpage/15",
        "https://www.aparat.com/rss/{username}",
    ],
    "video": [
        "https://www.aparat.com/api/fa/v1/video/video/show/videohash/{uid}?pr=1&mf=1",
        "https://www.aparat.com/etc/api/video/videohash/{uid}",
    ],
    "profile": [
        "https://www.aparat.com/api/fa/v1/user/user/information/username/{username}",
        "https://www.aparat.com/etc/api/profile/username/{username}",
    ],
}


REG_DEFAULTS = {
    "title": "ثبت‌نام مدیا",
    "link_label": "لینک چنل (یوتیوب یا آپارات)",
    "link_description": "لطفاً لینک چنلت رو وارد کن (لینک چنل، لایو یا یکی از ویدیوهات قبوله)",
    "minecraft_label": "یوزرنیم ماینکرفت",
    "minecraft_description": "لطفاً یوزرنیم ماینکرفتت رو وارد کن",
    "kind_label": "نوع فعالیت",
    "kind_streamer": "استریمر",
    "kind_creator": "کانتنت کریتور",
    "kind_both": "هر دو",
}


def _aparat_urls(ap: dict) -> dict:
    """Merge user-provided URLs (string or list, old key names supported) in front of the defaults."""
    out = {}
    for key, defaults in APARAT_DEFAULTS.items():
        user = ap.get(key) or ap.get(f"{key}_url") or ap.get(f"{key}_urls") or []
        if not isinstance(user, (str, list)):
            user = []
        if isinstance(user, str):
            user = [user]
        out[key] = list(dict.fromkeys([str(u) for u in user if u] + defaults))
    return out


def load_config(path: str | Path = "config.yml") -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} was not found.")
    with path.open(encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}

    bot = d.get("bot", {}) or {}
    yt = d.get("youtube", {}) or {}
    ch = d.get("channels", {}) or {}
    roles = d.get("roles", {}) or {}
    rules = d.get("rules", {}) or {}
    st = rules.get("streamer", {}) or {}
    cr = rules.get("creator", {}) or {}
    rep = d.get("report", {}) or {}
    iv = d.get("intervals", {}) or {}
    info = d.get("info_message", {}) or {}
    ap = d.get("aparat", {}) or {}

    # Secrets can come from environment variables (GitHub Actions secrets) instead of config.yml
    token = os.environ.get("DISCORD_TOKEN", "").strip() or str(bot.get("token", "")).strip()
    if not token or "توکن" in token:
        raise ConfigError("The bot token is not set (DISCORD_TOKEN secret or bot.token in config.yml).")

    yt_key = os.environ.get("YOUTUBE_API_KEY", "").strip() or str(yt.get("api_key", "")).strip()
    if "API_KEY" in yt_key:
        yt_key = ""

    keywords = [str(k) for k in (d.get("server_keywords") or []) if str(k).strip()]
    if not keywords:
        raise ConfigError("server_keywords needs at least one keyword.")

    report_day = _int(rep.get("day", 1), "report.day")
    if not 1 <= report_day <= 29:
        raise ConfigError("report.day must be between 1 and 29.")

    return Config(
        token=token,
        guild_id=_int(bot.get("guild_id"), "bot.guild_id"),
        youtube_api_key=yt_key,
        youtube_check_live_page=bool(yt.get("check_live_page", True)),
        server_name=str(d.get("server_name", "LunaMC")),
        keywords=keywords,
        ch_info=_int(ch.get("info"), "channels.info"),
        ch_live_history=_int(ch.get("live_history"), "channels.live_history"),
        ch_video_history=_int(ch.get("video_history"), "channels.video_history"),
        ch_media_dt=_int(ch.get("media_dt"), "channels.media_dt"),
        ch_log=_int(ch.get("log", 0), "channels.log", required=False),
        role_new=_int(roles.get("new"), "roles.new"),
        role_media=_int(roles.get("media"), "roles.media"),
        min_streams_per_week=int(st.get("min_streams_per_week", 2)),
        min_stream_minutes=int(st.get("min_stream_minutes", 120)),
        min_videos_per_month=int(cr.get("min_videos_per_month", 4)),
        min_video_minutes=int(cr.get("min_video_minutes", 8)),
        report_day=report_day,
        report_hour=int(rep.get("hour", 10)),
        live_check_minutes=max(1.0, float(iv.get("live_check_minutes", 3))),
        video_check_minutes=max(2.0, float(iv.get("video_check_minutes", 10))),
        views_update_hours=max(1.0, float(iv.get("views_update_hours", 24))),
        aparat_end_grace_minutes=max(0.0, float(iv.get("aparat_end_grace_minutes", 3))),
        aparat_live_check_seconds=max(30.0, float(iv.get("aparat_live_check_seconds", 120))),
        views_update_max_days=int(d.get("views_update_max_days", 0) or 0),
        timezone=str(d.get("timezone", "Asia/Tehran")),
        database=str(d.get("database", "data.db")),
        aparat_urls=_aparat_urls(ap),
        aparat_proxy=os.environ.get("APARAT_PROXY", "").strip() or str(ap.get("proxy") or "").strip(),
        reg={k: str((d.get("registration") or {}).get(k) or v) for k, v in REG_DEFAULTS.items()},
        info_title=str(info.get("title", "درود مدیای عزیز")),
        info_description=str(info.get("description", "")),
        info_button=str(info.get("button", "قوانین رو خوندم و قبول دارم"))[:80],
        info_welcome=str(info.get("welcome", "خوش اومدی!")),
        raw=d,
    )
