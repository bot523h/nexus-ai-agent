from datetime import datetime, timedelta, timezone

import pytest

from nexus_ai_agent.connect.contracts import (
    ConnectionContract,
    ConnectionState,
    CredentialState,
    ScopeGrant,
)
from nexus_ai_agent.connect.key_provider import StaticKeyRing
from nexus_ai_agent.connect.persistence import (
    ConnectionConflict,
    ConnectionPersistenceError,
    SqlConnectionStore,
)
from nexus_ai_agent.creative.studio.models import ActorIdentity
from nexus_ai_agent.storage.db import get_session

NOW = datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc)


def contract():
    return ConnectionContract(
        connection_id="conn_a",
        principal=ActorIdentity(kind="agent", actor_id="agent-a"),
        grant=ScopeGrant(project_ids=("project_a",), scopes=frozenset({"project.read"})),
        credential={
            "credential_id": "cred_a",
            "state": CredentialState.ACTIVE,
            "expires_at": NOW + timedelta(hours=1),
        },
        state=ConnectionState.ACTIVE,
        expires_at=NOW + timedelta(hours=1),
        created_at=NOW,
    )


class FakeResult:
    def __init__(self, rows=(), rowcount=1):
        self._rows = list(rows)
        self.rowcount = rowcount

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    def __init__(self):
        self.rows = {}
        self.committed = 0

    async def execute(self, query, params=None):
        sql = str(query)
        params = params or {}
        if sql.startswith("INSERT"):
            if params["connection_id"] in self.rows:
                from sqlalchemy.exc import IntegrityError

                raise IntegrityError("insert", params, Exception("duplicate"))
            self.rows[params["connection_id"]] = type("Row", (), params)()
            return FakeResult()
        if sql.startswith("SELECT *"):
            return FakeResult(self.rows.values())
        if sql.startswith("UPDATE"):
            if "SET kek_id" in sql:
                row = self.rows[params["connection_id"]]
                if row.row_version != params["row_version"]:
                    return FakeResult(rowcount=0)
                row.kek_id = params["kek_id"]
                row.wrapped_dek = params["wrapped_dek"]
                row.wrap_nonce = params["wrap_nonce"]
                row.row_version += 1
                row.updated_at = params["updated_at"]
                return FakeResult(rowcount=1)
            if "SET last_used_at" in sql:
                row = self.rows[params["connection_id"]]
                if row.row_version != params["row_version"]:
                    return FakeResult(rowcount=0)
                row.last_used_at = params["last_used_at"]
                row.row_version += 1
                row.updated_at = params["updated_at"]
                return FakeResult(rowcount=1)
            row = self.rows.get(params["connection_id"])
            if row is None or row.row_version != params["expected_version"]:
                return FakeResult(rowcount=0)
            row.connection_state = params["state"]
            row.revoked_at = params["revoked_at"]
            row.row_version += 1
            row.updated_at = params["updated_at"]
            return FakeResult(rowcount=1)
        if "WHERE connection_id = :connection_id" in sql:
            row = self.rows.get(params["connection_id"])
            return FakeResult([row] if row else [])
        raise AssertionError(sql)

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        pass


class SessionFactory:
    def __init__(self):
        self.session = FakeSession()

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_round_trip_never_returns_plaintext_from_metadata():
    factory = SessionFactory()
    store = SqlConnectionStore(StaticKeyRing({"k1": b"1" * 32}, active_key_id="k1"), factory)
    saved = await store.create(contract(), b"provider-secret")
    assert saved.envelope.ciphertext != b"provider-secret"
    assert (await store.get_secret("conn_a", now=NOW)) == b"provider-secret"


@pytest.mark.asyncio
async def test_revoke_and_stale_writer_fail_closed():
    factory = SessionFactory()
    store = SqlConnectionStore(StaticKeyRing({"k1": b"1" * 32}, active_key_id="k1"), factory)
    saved = await store.create(contract(), b"secret")
    revoked = await store.revoke("conn_a", expected_version=saved.row_version)
    assert revoked.connection.state is ConnectionState.REVOKED
    with pytest.raises(ConnectionPersistenceError):
        await store.get_secret("conn_a", now=NOW)
    with pytest.raises(ConnectionConflict):
        await store.revoke("conn_a", expected_version=saved.row_version)


@pytest.mark.asyncio
async def test_rewrap_all_changes_only_wrapping_material():
    factory = SessionFactory()
    old = SqlConnectionStore(StaticKeyRing({"k1": b"1" * 32}, active_key_id="k1"), factory)
    saved = await old.create(contract(), b"secret")
    new = SqlConnectionStore(
        StaticKeyRing({"k1": b"1" * 32, "k2": b"2" * 32}, active_key_id="k2"), factory
    )
    assert await new.rewrap_all() == 1
    loaded = await new.get("conn_a")
    assert loaded is not None
    assert loaded.envelope.key_id == "k2"
    assert loaded.envelope.ciphertext == saved.envelope.ciphertext
    assert await new.get_secret("conn_a", now=NOW) == b"secret"


@pytest.mark.asyncio
async def test_real_sqlite_bootstrap_persists_only_the_encrypted_envelope(tmp_path):
    store = SqlConnectionStore(
        StaticKeyRing({"k1": b"1" * 32}, active_key_id="k1"),
        session_factory=lambda: get_session(str(tmp_path / "connect.sqlite")),
    )
    saved = await store.create(contract(), b"real-secret")
    assert await store.get_secret("conn_a", now=NOW) == b"real-secret"
    loaded = await store.get("conn_a")
    assert loaded is not None
    assert loaded.envelope.ciphertext != b"real-secret"
    assert loaded.row_version == saved.row_version + 1
