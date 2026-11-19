"""SQLite storage (aiosqlite)."""
from __future__ import annotations

from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    discord_id   INTEGER PRIMARY KEY,
    kind         TEXT    NOT NULL,          -- streamer | creator | both
    added_at     INTEGER NOT NULL,
    mc_username  TEXT,
    mc_asked     INTEGER DEFAULT 0          -- 0 not asked, 1 DM sent, 2 DM failed
);

CREATE TABLE IF NOT EXISTS channels (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id   INTEGER NOT NULL,
    platform     TEXT    NOT NULL,          -- youtube | aparat
    external_id  TEXT    NOT NULL,          -- YouTube channel id / Aparat username
    name         TEXT,
    url          TEXT,
    added_at     INTEGER NOT NULL,
    last_video_check INTEGER DEFAULT 0,
    last_session_key TEXT,
    UNIQUE(platform, external_id)
);

CREATE TABLE IF NOT EXISTS streams (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id     INTEGER NOT NULL,
    channel_row    INTEGER,
    channel_name   TEXT,
    platform       TEXT    NOT NULL,
    item_key       TEXT    NOT NULL,
    url            TEXT,
    title          TEXT,
    thumbnail      TEXT,
    status         TEXT    NOT NULL,        -- upcoming | live | ended
    started_at     INTEGER,
    ended_at       INTEGER,
    last_live_at   INTEGER,
    peak_viewers   INTEGER,
    server_related INTEGER,                 -- NULL = pending, 1 = yes, 0 = no
    dm_sent        INTEGER DEFAULT 0,
    message_id     INTEGER,
    message_channel INTEGER,
    created_at     INTEGER NOT NULL,
    UNIQUE(platform, item_key)
);

CREATE TABLE IF NOT EXISTS videos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id     INTEGER NOT NULL,
    channel_row    INTEGER,
    channel_name   TEXT,
    platform       TEXT    NOT NULL,
    item_key       TEXT    NOT NULL,
    url            TEXT,
    title          TEXT,
    thumbnail      TEXT,
    published_at   INTEGER NOT NULL,
    duration_sec   INTEGER,
    views          INTEGER,
    server_related INTEGER,
    dm_sent        INTEGER DEFAULT 0,
    message_id     INTEGER,
    message_channel INTEGER,
    views_updated_at INTEGER DEFAULT 0,
    UNIQUE(platform, item_key)
);

CREATE TABLE IF NOT EXISTS seen (
    platform TEXT NOT NULL,
    item_key TEXT NOT NULL,
    PRIMARY KEY(platform, item_key)
);

CREATE TABLE IF NOT EXISTS deleted_items (
    platform TEXT NOT NULL,
    item_key TEXT NOT NULL,
    PRIMARY KEY(platform, item_key)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_streams_member ON streams(discord_id, started_at);
CREATE INDEX IF NOT EXISTS idx_videos_member  ON videos(discord_id, published_at);
CREATE INDEX IF NOT EXISTS idx_streams_status ON streams(status);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        # DELETE journal mode keeps everything in ONE file (data.db), so copying the
        # bot folder to another server always carries all the data with it.
        await self.conn.execute("PRAGMA journal_mode=DELETE")
        await self.conn.execute("PRAGMA synchronous=FULL")
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self) -> None:
        """Add columns introduced in later versions to an existing database."""
        wanted = {
            "channels": {"last_session_key": "TEXT", "baselined": "INTEGER DEFAULT 1"},
            "streams": {"manual": "INTEGER DEFAULT 0"},
            "videos": {"manual": "INTEGER DEFAULT 0"},
            "members": {"mc_username": "TEXT", "mc_asked": "INTEGER DEFAULT 0"},
        }
        for table, cols in wanted.items():
            cur = await self.conn.execute(f"PRAGMA table_info({table})")
            have = {r[1] for r in await cur.fetchall()}
            for col, typ in cols.items():
                if col not in have:
                    await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ------------------------------------------------------------ generic
    async def execute(self, sql: str, params: tuple = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid

    async def insert(self, sql: str, params: tuple = ()) -> int:
        """Run an INSERT; returns the new row id, or 0 if nothing was inserted (OR IGNORE)."""
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else 0

    async def one(self, sql: str, params: tuple = ()) -> dict | None:
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None

    async def all(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = await self.conn.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]

    async def update(self, table: str, row_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self.execute(f"UPDATE {table} SET {cols} WHERE id = ?", (*fields.values(), row_id))

    # ------------------------------------------------------------ kv
    async def get_kv(self, key: str) -> str | None:
        row = await self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_kv(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------ seen
    async def is_seen(self, platform: str, key: str) -> bool:
        return await self.one("SELECT 1 FROM seen WHERE platform = ? AND item_key = ?", (platform, key)) is not None

    async def mark_seen(self, platform: str, keys) -> None:
        await self.conn.executemany(
            "INSERT OR IGNORE INTO seen(platform, item_key) VALUES(?, ?)",
            [(platform, k) for k in keys],
        )
        await self.conn.commit()

    # ------------------------------------------------------------ members / channels
    async def upsert_member(self, discord_id: int, kind: str, added_at: int) -> None:
        await self.execute(
            "INSERT INTO members(discord_id, kind, added_at) VALUES(?, ?, ?) "
            "ON CONFLICT(discord_id) DO UPDATE SET kind = excluded.kind",
            (discord_id, kind, added_at),
        )

    async def set_minecraft(self, discord_id: int, username: str) -> None:
        await self.execute("UPDATE members SET mc_username = ? WHERE discord_id = ?", (username, discord_id))

    @property
    def total_changes(self) -> int:
        return self.conn.total_changes if self.conn else 0

    async def snapshot(self, dest) -> None:
        """Write a consistent copy of the whole database to `dest`."""
        target = await aiosqlite.connect(str(dest))
        try:
            await self.conn.backup(target)
        finally:
            await target.close()

    async def backup(self, folder, keep: int = 7) -> str:
        """Copy the whole database into folder/data-YYYY-MM-DD.db and keep the newest `keep` copies."""
        import datetime as _dt
        from pathlib import Path
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"data-{_dt.date.today().isoformat()}.db"
        target = await aiosqlite.connect(dest)
        try:
            await self.conn.backup(target)
        finally:
            await target.close()
        for old in sorted(folder.glob("data-*.db"))[:-keep]:
            old.unlink(missing_ok=True)
        return str(dest)

    async def get_member(self, discord_id: int) -> dict | None:
        return await self.one("SELECT * FROM members WHERE discord_id = ?", (discord_id,))

    async def members(self) -> list[dict]:
        return await self.all("SELECT * FROM members ORDER BY added_at")

    async def channels(self, platform: str | None = None, discord_id: int | None = None) -> list[dict]:
        sql, params = "SELECT * FROM channels WHERE 1=1", []
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        if discord_id:
            sql += " AND discord_id = ?"
            params.append(discord_id)
        return await self.all(sql + " ORDER BY id", tuple(params))
