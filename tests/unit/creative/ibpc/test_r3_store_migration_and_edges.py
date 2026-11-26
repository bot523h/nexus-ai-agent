"""Wave 3-R3/R4 — the store's additive migrations and its refusal edges.

The durable store is opened on databases written by *older* code in production,
so the `tenant_id` / `execution_context_json` / `issued_expires_at_utc` columns
must arrive as backwards-compatible `ALTER TABLE`s that keep every existing row
readable. The rest of this module pins the typed refusals of the two writes the
recovery work added (``set_turn_context`` and ``record_failed_turn``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.nagar.ibpc.compiler import compile_ast
from nexus_ai_agent.nagar.ibpc.intent import parse_intent
from nexus_ai_agent.nagar.ibpc.live_session import (
    DurableLiveSessionStore,
    SessionStatus,
    StaleSessionRevisionError,
    TurnStatus,
    TurnStoreError,
)
from nexus_ai_agent.nagar.ibpc.preview import build_preview

NOW = lambda: "2026-11-25T00:00:00Z"  # noqa: E731
BINDING = "cbn_" + "a" * 64
SESSION = "sess_legacy"
PROJECT = "project-legacy"
PRINCIPAL = "prn_" + "5" * 32
REVISION = "rev:7"

#: The exact schema the store shipped before Wave 3-R3 added its columns.
_LEGACY_SCHEMA = (
    "CREATE TABLE ibpc_sessions ("
    " session_id TEXT PRIMARY KEY,"
    " project_id TEXT NOT NULL,"
    " principal_id TEXT NOT NULL,"
    " base_revision_id TEXT NOT NULL,"
    " active_revision_id TEXT NOT NULL,"
    " status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'PAUSED', 'CLOSED', 'ABORTED')),"
    " version_epoch INTEGER NOT NULL DEFAULT 1,"
    " last_turn_index INTEGER NOT NULL DEFAULT 0,"
    " created_at_utc TEXT NOT NULL,"
    " updated_at_utc TEXT NOT NULL)",
    "CREATE TABLE ibpc_turns ("
    " turn_id TEXT PRIMARY KEY,"
    " session_id TEXT NOT NULL REFERENCES ibpc_sessions(session_id),"
    " turn_index INTEGER NOT NULL,"
    " idempotency_key TEXT NOT NULL UNIQUE,"
    " user_utterance TEXT NOT NULL,"
    " base_revision_id TEXT NOT NULL,"
    " scope_signature TEXT NOT NULL,"
    " compiled_plan_json TEXT NOT NULL,"
    " compiled_plan_sha256 TEXT NOT NULL,"
    " preview_manifest_json TEXT,"
    " preview_sha256 TEXT,"
    " approval_id TEXT,"
    " turn_status TEXT NOT NULL,"
    " created_at_utc TEXT NOT NULL,"
    " UNIQUE(session_id, turn_index))",
)


def _legacy_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        for statement in _LEGACY_SCHEMA:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO ibpc_sessions (session_id, project_id, principal_id, base_revision_id,"
            " active_revision_id, status, version_epoch, last_turn_index, created_at_utc,"
            " updated_at_utc) VALUES (?, ?, ?, ?, ?, 'ACTIVE', 1, 1, ?, ?)",
            (SESSION, PROJECT, PRINCIPAL, REVISION, REVISION, NOW(), NOW()),
        )
        conn.execute(
            "INSERT INTO ibpc_turns (turn_id, session_id, turn_index, idempotency_key,"
            " user_utterance, base_revision_id, scope_signature, compiled_plan_json,"
            " compiled_plan_sha256, turn_status, created_at_utc)"
            " VALUES ('turn-1', ?, 1, 'legacy-key', 'sha256:"
            + "a" * 64
            + "', ?, 'sha256:"
            + "b" * 64
            + "', '{}', 'sha256:"
            + "c" * 64
            + "', 'PREVIEW_READY', ?)",
            (SESSION, REVISION, NOW()),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def plan_and_preview():
    parsed = parse_intent(
        "trim from 0.5 to 1.5 seconds",
        project_id=PROJECT,
        asset_id="asset-r3",
        source_revision=0,
        duration_us=5_000_000,
        channel_binding_id=BINDING,
    )
    plan = compile_ast(parsed.ast, registry=build_runtime_registry())
    preview = build_preview(plan, parsed.ast, source_duration_us=5_000_000, fps=30.0)
    return plan, preview


@pytest.fixture(scope="module")
def compiled_plan():
    """The real compiler over the real runtime registry."""
    parsed = parse_intent(
        "trim from 0.5 to 1.5 seconds",
        project_id=PROJECT,
        asset_id="asset-r3",
        source_revision=0,
        duration_us=5_000_000,
        channel_binding_id=BINDING,
    )
    return compile_ast(parsed.ast, registry=build_runtime_registry())


def _turn(turn_id: str = "turn-1", key: str = "key-1") -> dict:
    return dict(
        session_id="sess_r3",
        turn_id=turn_id,
        idempotency_key=key,
        user_utterance="trim from 0.5 to 1.5 seconds",
        base_revision_id=REVISION,
        scope_signature="sha256:" + "d" * 64,
    )


class TestTheAdditiveMigrations:
    def test_a_legacy_database_is_upgraded_and_its_rows_stay_readable(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.sqlite3"
        _legacy_database(path)
        store = DurableLiveSessionStore(path, now=NOW)
        try:
            columns = {row[1] for row in store._conn.execute("PRAGMA table_info(ibpc_sessions)")}
            assert "tenant_id" in columns
            turn_columns = {row[1] for row in store._conn.execute("PRAGMA table_info(ibpc_turns)")}
            assert {"execution_context_json", "issued_expires_at_utc"} <= turn_columns
            session = store.get_session(SESSION)
            assert session.status is SessionStatus.ACTIVE
            assert session.tenant_id == ""  # never invented for a pre-R3 row
            assert session.last_turn_index == 1
            turn = store.get_turn(SESSION, "turn-1")
            assert turn.turn_status is TurnStatus.PREVIEW_READY
            assert turn.execution_context_json is None
            assert turn.issued_expires_at_utc is None
            assert turn.refusal_reason is None
        finally:
            store.close()

    def test_reopening_an_upgraded_database_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.sqlite3"
        _legacy_database(path)
        for _ in range(2):
            store = DurableLiveSessionStore(path, now=NOW)
            try:
                assert store.get_session(SESSION).session_id == SESSION
            finally:
                store.close()


class TestTheRefusalsOfTheRecoveryWrites:
    @pytest.fixture()
    def store(self, tmp_path: Path):
        store = DurableLiveSessionStore(tmp_path / "sessions.sqlite3", now=NOW)
        store.open_session(
            session_id="sess_r3",
            project_id=PROJECT,
            principal_id=PRINCIPAL,
            base_revision_id=REVISION,
            tenant_id="ten",
        )
        yield store
        store.close()

    def test_a_context_on_a_stale_base_revision_is_refused(self, store, compiled_plan) -> None:
        store.submit_turn(**_turn(), plan=compiled_plan)
        with pytest.raises(StaleSessionRevisionError) as exc:
            store.set_turn_context(
                "sess_r3", "turn-1", base_revision_id="rev:8", execution_context_json="{}"
            )
        assert exc.value.reason == "stale_revision"

    def test_a_context_on_a_terminal_turn_is_refused(
        self, store, compiled_plan, plan_and_preview
    ) -> None:
        _, preview = plan_and_preview
        store.submit_turn(**_turn(), plan=compiled_plan)
        store.set_turn_preview(
            "sess_r3",
            "turn-1",
            preview=preview,
            approval_id="appr_1",
            base_revision_id=REVISION,
        )
        store.mark_turn_approved("sess_r3", "turn-1", base_revision_id=REVISION)
        store.mark_turn_executed("sess_r3", "turn-1", base_revision_id=REVISION)
        with pytest.raises(TurnStoreError) as exc:
            store.set_turn_context(
                "sess_r3", "turn-1", base_revision_id=REVISION, execution_context_json="{}"
            )
        assert exc.value.reason == "turn_terminal"

    def test_a_failed_turn_needs_a_key_and_a_refusal(self, store) -> None:
        common = dict(
            session_id="sess_r3",
            turn_id="turn-1",
            user_utterance="audio",
            base_revision_id=REVISION,
        )
        with pytest.raises(TurnStoreError) as exc:
            store.record_failed_turn(**common, idempotency_key="", refusal_reason="x")
        assert exc.value.reason == "invalid_idempotency_key"
        with pytest.raises(TurnStoreError) as exc:
            store.record_failed_turn(**common, idempotency_key="k", refusal_reason="")
        assert exc.value.reason == "invalid_refusal"

    def test_a_failed_turn_is_idempotent_and_digest_only(self, store) -> None:
        first = store.record_failed_turn(
            session_id="sess_r3",
            turn_id="turn-1",
            idempotency_key="key-1",
            user_utterance="the raw utterance",
            base_revision_id=REVISION,
            refusal_reason="transcription_failed:no_audio",
        )
        assert first.turn_status is TurnStatus.REJECTED
        assert first.refusal_reason == "transcription_failed:no_audio"
        assert first.user_utterance.startswith("sha256:")  # never the raw text
        again = store.record_failed_turn(
            session_id="sess_r3",
            turn_id="turn-1",
            idempotency_key="key-1",
            user_utterance="the raw utterance",
            base_revision_id=REVISION,
            refusal_reason="transcription_failed:no_audio",
        )
        assert again.turn_id == first.turn_id
        # the same key bound to another turn is a conflict, not a silent overwrite
        with pytest.raises(TurnStoreError) as exc:
            store.record_failed_turn(
                session_id="sess_r3",
                turn_id="turn-2",
                idempotency_key="key-1",
                user_utterance="other",
                base_revision_id=REVISION,
                refusal_reason="transcription_failed:no_audio",
            )
        assert exc.value.reason == "idempotency_conflict"

    def test_a_failed_turn_on_a_stale_revision_is_refused(self, store) -> None:
        with pytest.raises(StaleSessionRevisionError):
            store.record_failed_turn(
                session_id="sess_r3",
                turn_id="turn-1",
                idempotency_key="key-1",
                user_utterance="audio",
                base_revision_id="rev:1",
                refusal_reason="transcription_failed:no_audio",
            )

    def test_supersede_moves_an_approved_turn_to_a_terminal_state(
        self, store, compiled_plan, plan_and_preview
    ) -> None:
        _, preview = plan_and_preview
        store.submit_turn(**_turn(), plan=compiled_plan)
        store.set_turn_preview(
            "sess_r3",
            "turn-1",
            preview=preview,
            approval_id="appr_1",
            base_revision_id=REVISION,
        )
        store.mark_turn_approved("sess_r3", "turn-1", base_revision_id=REVISION)
        superseded = store.supersede_turn("sess_r3", "turn-1", base_revision_id=REVISION)
        assert superseded.turn_status is TurnStatus.SUPERSEDED
