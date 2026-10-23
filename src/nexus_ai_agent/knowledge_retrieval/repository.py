"""Canonical source registry — the authoritative knowledge-source rows.

Precedent and pattern: ``nexus_ai_agent.creative.assets.repository`` — async
sessions from the shared :func:`nexus_ai_agent.storage.db.get_session`, SQLAlchemy
Core statements against the tables ``storage/models.py`` owns, **no**
``sqlmodel`` import outside the frozen baseline (law R2), no second engine
(law R19): this module never creates a connection of its own.

What lives here is deliberately small: identity (``source_id``), location
(``root`` + ``path``), version (``sha256``), status and timestamps.  Chunk
texts and embeddings are *not* duplicated into SQLite — they are derived
state owned by the index store and rebuilt from these rows plus the files
on disk.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.knowledge_retrieval.errors import KnowledgeStoreError
from nexus_ai_agent.knowledge_retrieval.models import SourceRecord, SourceStatus
from nexus_ai_agent.storage import models as orm

__all__ = ["KnowledgeSourceRepository", "session_error"]

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def session_error(exc: Exception) -> KnowledgeStoreError:
    """Wrap a database failure with an operator-safe typed error."""
    return KnowledgeStoreError(
        f"knowledge source registry failed ({type(exc).__name__})",
        hint="check NEXUS_DB_PATH / NEXUS_DATABASE_URL and retry",
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_record(row: orm.KnowledgeSource) -> SourceRecord:
    status = (
        SourceStatus.INGESTED if row.status == SourceStatus.INGESTED.value else SourceStatus.ERROR
    )
    return SourceRecord(
        source_id=row.source_id,
        root=row.root,
        path=row.path,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        status=status,
        chunks=row.chunks,
        indexed_at=row.indexed_at.isoformat() if row.indexed_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        last_error=row.last_error,
    )


class KnowledgeSourceRepository:
    """CRUD over ``knowledgesource`` rows; every call uses the shared session."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def list_all(self) -> list[SourceRecord]:
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(orm.KnowledgeSource).order_by(
                        orm.KnowledgeSource.root, orm.KnowledgeSource.path
                    )
                )
                return [_to_record(row) for row in result.scalars().all()]
        except Exception as exc:  # noqa: BLE001 — surfaced as a typed store error
            raise session_error(exc) from exc

    async def get(self, source_id: str) -> SourceRecord | None:
        try:
            async with self._session_factory() as session:
                row = await session.get(orm.KnowledgeSource, source_id)
                return _to_record(row) if row is not None else None
        except Exception as exc:  # noqa: BLE001
            raise session_error(exc) from exc

    async def get_many(self, source_ids: Sequence[str]) -> dict[str, SourceRecord]:
        """Batch lookup (one statement) — used to verify search citations."""
        if not source_ids:
            return {}
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(orm.KnowledgeSource).where(
                        orm.KnowledgeSource.source_id.in_(  # type: ignore[attr-defined]
                            list(source_ids)
                        )
                    )
                )
                return {row.source_id: _to_record(row) for row in result.scalars().all()}
        except Exception as exc:  # noqa: BLE001
            raise session_error(exc) from exc

    async def upsert_ingested(
        self,
        *,
        source_id: str,
        root: str,
        path: str,
        sha256: str,
        size_bytes: int,
        chunks: int,
        indexed_at: datetime | None = None,
    ) -> None:
        """Record a successful ingestion (insert-or-update of that identity).

        The SELECT-then-INSERT shape races two concurrent index runs over the
        same file (both see "no row", both insert, the UNIQUE constraints on
        ``source_id`` and ``(root, path)`` reject the loser).  The loser
        therefore retries as a plain UPDATE — the same
        "integrity conflict ⇒ update, not fail" discipline the Asset Library
        uses (``creative/assets/service.py``), so a duplicate run collapses
        into a convergent write instead of a systemic store error.
        """
        now = _utcnow()
        stamp = indexed_at or now
        try:
            await self._insert_ingested(
                source_id=source_id,
                root=root,
                path=path,
                sha256=sha256,
                size_bytes=size_bytes,
                chunks=chunks,
                stamp=stamp,
                now=now,
            )
        except IntegrityError:
            try:
                async with self._session_factory() as session:
                    await session.execute(
                        update(orm.KnowledgeSource)
                        .where(orm.KnowledgeSource.source_id == source_id)  # type: ignore[arg-type]
                        .values(
                            root=root,
                            path=path,
                            sha256=sha256,
                            size_bytes=size_bytes,
                            status=SourceStatus.INGESTED.value,
                            chunks=chunks,
                            indexed_at=stamp,
                            updated_at=now,
                            last_error=None,
                        )
                    )
                    await session.commit()
            except Exception as exc:  # noqa: BLE001
                raise session_error(exc) from exc
        except Exception as exc:  # noqa: BLE001
            raise session_error(exc) from exc

    async def _insert_ingested(
        self,
        *,
        source_id: str,
        root: str,
        path: str,
        sha256: str,
        size_bytes: int,
        chunks: int,
        stamp: datetime,
        now: datetime,
    ) -> None:
        async with self._session_factory() as session:
            row = await session.get(orm.KnowledgeSource, source_id)
            if row is None:
                session.add(
                    orm.KnowledgeSource(
                        source_id=source_id,
                        root=root,
                        path=path,
                        sha256=sha256,
                        size_bytes=size_bytes,
                        status=SourceStatus.INGESTED.value,
                        chunks=chunks,
                        indexed_at=stamp,
                        updated_at=now,
                        last_error=None,
                    )
                )
            else:
                await session.execute(
                    update(orm.KnowledgeSource)
                    .where(orm.KnowledgeSource.source_id == source_id)  # type: ignore[arg-type]
                    .values(
                        root=root,
                        path=path,
                        sha256=sha256,
                        size_bytes=size_bytes,
                        status=SourceStatus.INGESTED.value,
                        chunks=chunks,
                        indexed_at=stamp,
                        updated_at=now,
                        last_error=None,
                    )
                )
            await session.commit()

    async def set_error(self, source_id: str, *, root: str, path: str, reason: str) -> None:
        """Record an ingestion failure without ever storing file content."""
        now = _utcnow()
        try:
            async with self._session_factory() as session:
                row = await session.get(orm.KnowledgeSource, source_id)
                if row is None:
                    session.add(
                        orm.KnowledgeSource(
                            source_id=source_id,
                            root=root,
                            path=path,
                            sha256="",
                            size_bytes=0,
                            status=SourceStatus.ERROR.value,
                            chunks=0,
                            updated_at=now,
                            last_error=reason[:200],
                        )
                    )
                else:
                    await session.execute(
                        update(orm.KnowledgeSource)
                        .where(orm.KnowledgeSource.source_id == source_id)  # type: ignore[arg-type]
                        .values(
                            status=SourceStatus.ERROR.value,
                            chunks=0,
                            updated_at=now,
                            last_error=reason[:200],
                        )
                    )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            raise session_error(exc) from exc

    async def delete(self, source_id: str) -> bool:
        """Unregister one source; ``True`` when a row existed."""
        try:
            async with self._session_factory() as session:
                row = await session.get(orm.KnowledgeSource, source_id)
                if row is None:
                    return False
                await session.delete(row)
                await session.commit()
                return True
        except Exception as exc:  # noqa: BLE001
            raise session_error(exc) from exc

    async def delete_many(self, source_ids: Sequence[str]) -> int:
        """Batch unregister (stale cleanup); returns rows removed."""
        if not source_ids:
            return 0
        try:
            async with self._session_factory() as session:
                found = await session.execute(
                    select(orm.KnowledgeSource).where(
                        orm.KnowledgeSource.source_id.in_(  # type: ignore[attr-defined]
                            list(source_ids)
                        )
                    )
                )
                rows = list(found.scalars().all())
                for row in rows:
                    await session.delete(row)
                if rows:
                    await session.commit()
                return len(rows)
        except Exception as exc:  # noqa: BLE001
            raise session_error(exc) from exc
