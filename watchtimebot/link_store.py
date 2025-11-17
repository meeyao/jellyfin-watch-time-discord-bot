from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Optional

import aiosqlite

from .config_loader import UserMapping


class UserLinkStore:
    """Persistence layer for Discord ↔ Jellyfin user mappings created via commands."""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lock:
            if self._conn is not None:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(self._db_path)
            conn.row_factory = aiosqlite.Row
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_links (
                    discord_id INTEGER PRIMARY KEY,
                    jellyfin_user_id TEXT NOT NULL,
                    display_name TEXT,
                    instance_name TEXT
                )
                """
            )
            await conn.commit()
            await self._ensure_instance_column(conn)
            self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def upsert_link(self, mapping: UserMapping) -> None:
        conn = await self._ensure_conn()
        await conn.execute(
            """
            INSERT INTO user_links(discord_id, jellyfin_user_id, display_name, instance_name)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE
            SET jellyfin_user_id = excluded.jellyfin_user_id,
                display_name = excluded.display_name,
                instance_name = excluded.instance_name
            """,
            (mapping.discord_id, mapping.jellyfin_user_id, mapping.display_name, mapping.instance_name),
        )
        await conn.commit()

    async def remove_link(self, discord_id: int) -> None:
        conn = await self._ensure_conn()
        await conn.execute("DELETE FROM user_links WHERE discord_id = ?", (discord_id,))
        await conn.commit()

    async def list_links(self) -> List[UserMapping]:
        conn = await self._ensure_conn()
        async with conn.execute(
            "SELECT discord_id, jellyfin_user_id, display_name, instance_name FROM user_links ORDER BY discord_id"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                UserMapping(
                    discord_id=int(row["discord_id"]),
                    jellyfin_user_id=str(row["jellyfin_user_id"]),
                    display_name=row["display_name"],
                    instance_name=row["instance_name"],
                )
                for row in rows
            ]

    async def get_mapping(self, discord_id: int) -> Optional[UserMapping]:
        conn = await self._ensure_conn()
        async with conn.execute(
            "SELECT discord_id, jellyfin_user_id, display_name, instance_name FROM user_links WHERE discord_id = ?",
            (discord_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return UserMapping(
                discord_id=int(row["discord_id"]),
                jellyfin_user_id=str(row["jellyfin_user_id"]),
                display_name=row["display_name"],
                instance_name=row["instance_name"],
            )

    async def _ensure_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            await self.connect()
        assert self._conn is not None  # nosec
        return self._conn

    async def _ensure_instance_column(self, conn: aiosqlite.Connection) -> None:
        async with conn.execute("PRAGMA table_info(user_links)") as cursor:
            rows = await cursor.fetchall()
        if any(row["name"] == "instance_name" for row in rows):
            return
        await conn.execute("ALTER TABLE user_links ADD COLUMN instance_name TEXT")
        await conn.commit()
