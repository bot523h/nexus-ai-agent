from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite


class JobRegistry:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        async with self._init_lock:
            if self._initialized:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS creative_jobs (
                        id TEXT PRIMARY KEY,
                        job_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        input_data TEXT NOT NULL,
                        result TEXT,
                        error TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                await db.commit()
            self._initialized = True

    async def create_job(self, job_type: str, input_data: dict[str, Any]) -> str:
        await self.initialize()
        job_id = uuid4().hex
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO creative_jobs (id, job_type, status, input_data)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, job_type, "pending", json.dumps(input_data)),
            )
            await db.commit()
        return job_id

    async def update_job_status(
        self,
        job_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                UPDATE creative_jobs
                SET status = ?, result = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, json.dumps(result) if result is not None else None, error, job_id),
            )
            await db.commit()

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, job_type, status, input_data, result, error, created_at, updated_at
                FROM creative_jobs
                WHERE id = ?
                """,
                (job_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["input_data"] = json.loads(payload["input_data"])
        if payload["result"] is not None:
            payload["result"] = json.loads(payload["result"])
        return payload

    async def cleanup_old_jobs(self, days: int = 7) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                DELETE FROM creative_jobs
                WHERE created_at < datetime('now', ?)
                """,
                (f"-{days} day",),
            )
            await db.commit()
