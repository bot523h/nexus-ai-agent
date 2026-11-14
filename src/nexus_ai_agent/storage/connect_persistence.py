"""Durable CONNECT credential store using the repository's existing DB authority.

The table is created by the explicit Alembic migration in this slice. This
module deliberately declares no engine, metadata, ORM table, or secret-bearing
model. KEKs never enter SQL; only encrypted DEK envelopes and ciphertext do.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nexus_ai_agent.connect.contracts import (
    ConnectionContract,
    ConnectionState,
    CredentialDescriptor,
)
from nexus_ai_agent.connect.crypto import EncryptedSecret, EnvelopeCipher, SecretEnvelopeError
from nexus_ai_agent.connect.key_provider import KeyProvider
from nexus_ai_agent.connect.persistence import PersistedConnection
from nexus_ai_agent.storage.db import get_session

_TABLE = "nexus_connect_credential"


class ConnectionPersistenceError(RuntimeError):
    """A durable CONNECT operation failed without exposing secret material."""


class ConnectionConflict(ConnectionPersistenceError):
    """Optimistic version check rejected a stale writer."""


@dataclass(frozen=True)
class _Row:
    connection: ConnectionContract
    envelope: EncryptedSecret
    row_version: int
    revoked_at: datetime | None
    updated_at: datetime


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dt(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("stored CONNECT timestamp is not ISO-8601") from exc
    if not isinstance(value, datetime):
        raise TypeError("stored CONNECT timestamp has an unsupported type")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def _execute(session: Any, statement: Any, params: Any = None) -> Any:
    """Execute Core SQL via the connection, avoiding SQLModel legacy warnings.

    Lightweight test doubles expose only ``execute``; real SQLModel sessions use
    their SQLAlchemy AsyncConnection directly.
    """
    connection_method = getattr(session, "connection", None)
    if connection_method is not None:
        connection = await connection_method()
        return (
            await connection.execute(statement, params)
            if params is not None
            else await connection.execute(statement)
        )
    return (
        await session.execute(statement, params)
        if params is not None
        else await session.execute(statement)
    )


class SqlConnectionStore:
    """Async CRUD and rotation operations over ``get_session()``.

    ``session_factory`` is injectable for tests and must return the same async
    context-manager shape as ``get_session``. No caller can inject a separate
    engine through this boundary.
    """

    def __init__(self, key_provider: KeyProvider, session_factory: Any | None = None) -> None:
        self._cipher = EnvelopeCipher(key_provider)
        self._session_factory = session_factory or (lambda: get_session())

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        async with self._session_factory() as session:
            yield session

    @staticmethod
    def _metadata(connection: ConnectionContract) -> dict[str, Any]:
        return {
            "connection_id": connection.connection_id,
            "principal_kind": connection.principal.kind,
            "principal_id": connection.principal.actor_id,
            "project_ids_json": _json(connection.grant.project_ids),
            "scopes_json": _json(sorted(connection.grant.scopes)),
            "credential_id": connection.credential.credential_id,
            "credential_state": connection.credential.state.value,
            "connection_state": connection.state.value,
            "credential_expires_at": connection.credential.expires_at,
            "connection_expires_at": connection.expires_at,
            "created_at": connection.created_at,
            "last_used_at": connection.last_used_at,
            "rotated_from": connection.credential.rotated_from,
        }

    @staticmethod
    def _from_row(row: Any) -> _Row:
        from nexus_ai_agent.connect.contracts import ScopeGrant
        from nexus_ai_agent.creative.studio.models import ActorIdentity

        try:
            created_at = _dt(row.created_at)
            if created_at is None:
                raise ValueError("created_at cannot be null")
            updated_at = _dt(row.updated_at)
            if updated_at is None:
                raise ValueError("updated_at cannot be null")
            connection = ConnectionContract(
                connection_id=row.connection_id,
                principal=ActorIdentity(kind=row.principal_kind, actor_id=row.principal_id),
                grant=ScopeGrant(
                    project_ids=tuple(json.loads(row.project_ids_json)),
                    scopes=frozenset(json.loads(row.scopes_json)),
                ),
                credential=CredentialDescriptor(
                    credential_id=row.credential_id,
                    state=row.credential_state,
                    expires_at=_dt(row.credential_expires_at),
                    rotated_from=row.rotated_from,
                    last_used_at=_dt(row.last_used_at),
                ),
                state=row.connection_state,
                expires_at=_dt(row.connection_expires_at),
                created_at=created_at,
                last_used_at=_dt(row.last_used_at),
            )
            envelope = EncryptedSecret(
                key_id=row.kek_id,
                wrapped_dek=bytes(row.wrapped_dek),
                wrap_nonce=bytes(row.wrap_nonce),
                data_nonce=bytes(row.data_nonce),
                ciphertext=bytes(row.ciphertext),
                aad=bytes(row.aad),
                schema_version=row.schema_version,
            )
            return _Row(
                connection=connection,
                envelope=envelope,
                row_version=row.row_version,
                revoked_at=_dt(row.revoked_at),
                updated_at=updated_at,
            )
        except Exception as exc:
            raise ConnectionPersistenceError("stored CONNECT record failed validation") from exc

    async def create(self, connection: ConnectionContract, secret: bytes) -> PersistedConnection:
        envelope = self._cipher.encrypt(
            secret,
            connection_id=connection.connection_id,
            credential_id=connection.credential.credential_id,
        )
        metadata = self._metadata(connection)
        now = datetime.now(timezone.utc)
        params = {
            **metadata,
            "kek_id": envelope.key_id,
            "wrapped_dek": envelope.wrapped_dek,
            "wrap_nonce": envelope.wrap_nonce,
            "data_nonce": envelope.data_nonce,
            "ciphertext": envelope.ciphertext,
            "aad": envelope.aad,
            "schema_version": envelope.schema_version,
            "row_version": 1,
            "revoked_at": None,
            "updated_at": now,
        }
        query = text(
            f"INSERT INTO {_TABLE} (connection_id, principal_kind, principal_id, "
            "project_ids_json, scopes_json, credential_id, credential_state, connection_state, "
            "credential_expires_at, connection_expires_at, created_at, last_used_at, rotated_from, "
            "kek_id, wrapped_dek, wrap_nonce, data_nonce, ciphertext, aad, schema_version, "
            "row_version, revoked_at, updated_at) VALUES "
            "(:connection_id, :principal_kind, :principal_id, :project_ids_json, :scopes_json, "
            ":credential_id, :credential_state, :connection_state, :credential_expires_at, "
            ":connection_expires_at, :created_at, :last_used_at, :rotated_from, :kek_id, "
            ":wrapped_dek, :wrap_nonce, :data_nonce, :ciphertext, :aad, :schema_version, "
            ":row_version, :revoked_at, :updated_at)"
        )
        try:
            async with self._session() as session:
                await _execute(session, query, params)
                await session.commit()
        except IntegrityError as exc:
            raise ConnectionPersistenceError("CONNECT connection already exists") from exc
        return PersistedConnection(connection, envelope, 1, None, now)

    async def get(self, connection_id: str) -> PersistedConnection | None:
        async with self._session() as session:
            result = await _execute(
                session,
                text(f"SELECT * FROM {_TABLE} WHERE connection_id = :connection_id"),
                {"connection_id": connection_id},
            )
            row = result.mappings().first()
        if row is None:
            return None
        parsed = self._from_row(row)
        updated_at = parsed.updated_at
        if updated_at is None:
            raise ConnectionPersistenceError("stored CONNECT record has no update time")
        return PersistedConnection(
            parsed.connection,
            parsed.envelope,
            parsed.row_version,
            parsed.revoked_at,
            updated_at,
        )

    async def get_secret(self, connection_id: str, *, now: datetime | None = None) -> bytes:
        stored = await self.get(connection_id)
        if stored is None:
            raise ConnectionPersistenceError("CONNECT connection not found")
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ConnectionPersistenceError("request time must be timezone-aware")
        if stored.revoked_at is not None or not stored.connection.is_usable(instant):
            raise ConnectionPersistenceError("CONNECT connection is inactive or revoked")
        try:
            secret = self._cipher.decrypt(
                stored.envelope,
                connection_id=stored.connection.connection_id,
                credential_id=stored.connection.credential.credential_id,
            )
        except SecretEnvelopeError as exc:
            raise ConnectionPersistenceError("CONNECT secret authentication failed") from exc
        used_at = datetime.now(timezone.utc)
        async with self._session() as session:
            result = await _execute(
                session,
                text(
                    f"UPDATE {_TABLE} SET last_used_at = :last_used_at, "
                    "row_version = row_version + 1, updated_at = :updated_at "
                    "WHERE connection_id = :connection_id AND row_version = :row_version"
                ),
                {
                    "last_used_at": used_at,
                    "updated_at": used_at,
                    "connection_id": connection_id,
                    "row_version": stored.row_version,
                },
            )
            if result.rowcount != 1:
                await session.rollback()
                raise ConnectionConflict("CONNECT connection changed during secret use")
            await session.commit()
        return secret

    async def revoke(self, connection_id: str, *, expected_version: int) -> PersistedConnection:
        now = datetime.now(timezone.utc)
        async with self._session() as session:
            result = await _execute(
                session,
                text(
                    f"UPDATE {_TABLE} SET connection_state = :state, revoked_at = :revoked_at, "
                    "row_version = row_version + 1, updated_at = :updated_at "
                    "WHERE connection_id = :connection_id AND row_version = :expected_version"
                ),
                {
                    "state": ConnectionState.REVOKED.value,
                    "revoked_at": now,
                    "updated_at": now,
                    "connection_id": connection_id,
                    "expected_version": expected_version,
                },
            )
            if result.rowcount != 1:
                await session.rollback()
                raise ConnectionConflict("CONNECT connection version is stale")
            await session.commit()
        stored = await self.get(connection_id)
        if stored is None:
            raise ConnectionPersistenceError("CONNECT connection disappeared after revoke")
        return stored

    async def rewrap_all(self) -> int:
        async with self._session() as session:
            result = await _execute(session, text(f"SELECT * FROM {_TABLE}"))
            rows = [self._from_row(row) for row in result.mappings().all()]
            changed = 0
            for row in rows:
                rotated = self._cipher.rewrap(row.envelope)
                if rotated == row.envelope:
                    continue
                update_result = await _execute(
                    session,
                    text(
                        f"UPDATE {_TABLE} SET kek_id = :kek_id, wrapped_dek = :wrapped_dek, "
                        "wrap_nonce = :wrap_nonce, row_version = row_version + 1, "
                        "updated_at = :updated_at WHERE connection_id = :connection_id "
                        "AND row_version = :row_version"
                    ),
                    {
                        "kek_id": rotated.key_id,
                        "wrapped_dek": rotated.wrapped_dek,
                        "wrap_nonce": rotated.wrap_nonce,
                        "updated_at": datetime.now(timezone.utc),
                        "connection_id": row.connection.connection_id,
                        "row_version": row.row_version,
                    },
                )
                if update_result.rowcount != 1:
                    await session.rollback()
                    raise ConnectionConflict("CONNECT connection changed during key rotation")
                changed += 1
            await session.commit()
            return changed


__all__ = [
    "ConnectionConflict",
    "ConnectionPersistenceError",
    "PersistedConnection",
    "SqlConnectionStore",
]
