from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

import aiosqlite

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class PlaybackEntry:
    item_id: Optional[str]
    item_name: str
    item_type: Optional[str]
    started_at: datetime
    duration_seconds: int
    client_name: Optional[str]
    device_name: Optional[str]


class PlaybackReportingStore:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        quoted = quote(str(db_path))
        self._db_uri = f"file:{quoted}?mode=ro&cache=shared"
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lock:
            if self._conn is not None:
                return
            conn = await aiosqlite.connect(self._db_uri, uri=True)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA query_only = TRUE")
            self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def get_watchtime_seconds(self, user_id: str, days: Optional[int]) -> int:
        conn = await self._ensure_conn()
        params = [user_id]
        query = "SELECT COALESCE(SUM(PlayDuration), 0) AS total FROM PlaybackActivity WHERE UserId = ?"
        if days is not None:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
            params.append(since.strftime(DATETIME_FORMAT))
            query += " AND DateCreated >= ?"
        async with conn.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return int(row["total"] or 0)

    async def get_last_play(self, user_id: str) -> Optional[PlaybackEntry]:
        conn = await self._ensure_conn()
        query = (
            "SELECT ItemId, ItemName, ItemType, DateCreated, PlayDuration, ClientName, DeviceName "
            "FROM PlaybackActivity WHERE UserId = ? ORDER BY DateCreated DESC LIMIT 1"
        )
        async with conn.execute(query, (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _row_to_entry(row)

    async def get_recent_plays(self, user_id: str, limit: int = 5) -> List[PlaybackEntry]:
        conn = await self._ensure_conn()
        query = (
            "SELECT ItemId, ItemName, ItemType, DateCreated, PlayDuration, ClientName, DeviceName "
            "FROM PlaybackActivity WHERE UserId = ? ORDER BY DateCreated DESC LIMIT ?"
        )
        async with conn.execute(query, (user_id, limit)) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_entry(row) for row in rows]

    async def _ensure_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            await self.connect()
        assert self._conn is not None  # nosec
        return self._conn


def _row_to_entry(row: aiosqlite.Row) -> PlaybackEntry:
    raw_date = row["DateCreated"]
    started_at = _parse_timestamp(raw_date)
    item_id = None
    try:
        if "ItemId" in row.keys():
            item_id = row["ItemId"]
    except Exception:
        item_id = None
    return PlaybackEntry(
        item_id=item_id,
        item_name=row["ItemName"],
        item_type=row["ItemType"],
        started_at=started_at,
        duration_seconds=int(row["PlayDuration"] or 0),
        client_name=row["ClientName"],
        device_name=row["DeviceName"],
    )


def _parse_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.strptime(value.split(".")[0], DATETIME_FORMAT)
