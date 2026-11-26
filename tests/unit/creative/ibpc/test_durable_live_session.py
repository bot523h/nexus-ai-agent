"""Wave 3 slice 3.1 — durable live sessions and turns (real-disk SQLite WAL).

Every invariant is proven against a REAL file (never ``:memory:``), so the test
exercises the same ``BEGIN IMMEDIATE`` / ``PRAGMA`` path production uses:

1. strict monotonic ``turn_index`` ordering per session;
2. idempotent submission (``f(f(x)) == f(x)``) with no side effect;
3. stale-revision protection (``StaleSessionRevisionError``) before any write;
4. deterministic termination that invalidates unconsumed preview approvals;
5. full state reconstruction across ``close()`` + a fresh store instance;
6. concurrent serialization through the optimistic ``version_epoch`` CAS.

The digest-only storage contract is asserted directly against the SQLite rows:
the raw utterance must never appear in the file.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.nagar.ibpc.compiler import compile_ast
from nexus_ai_agent.nagar.ibpc.intent import parse_intent
from nexus_ai_agent.nagar.ibpc.live_session import (
    TURN_TRANSITIONS,
    ConcurrentSessionMutationError,
    DurableLiveSessionStore,
    SessionStatus,
    StaleSessionError,
    StaleSessionRevisionError,
    TurnStatus,
    TurnStoreError,
    durable_live_session_path,
    utterance_digest,
)
from nexus_ai_agent.nagar.ibpc.preview import build_preview

NOW = lambda: "2026-11-25T00:00:00Z"  # noqa: E731
BINDING = "cbn_" + "a" * 64
SESSION = "sess_w3_0001"
PROJECT = "project-w3"
PRINCIPAL = "prn_" + "1" * 32


@pytest.fixture(scope="module")
def plan_and_preview():
    """The real compiler + preview over the real runtime registry."""
    parsed = parse_intent(
        "trim from 0.5 to 1.5 seconds",
        project_id=PROJECT,
        asset_id="asset-w3",
        source_revision=0,
        duration_us=5_000_000,
        channel_binding_id=BINDING,
    )
    ast = parsed.ast
    plan = compile_ast(ast, registry=build_runtime_registry())
    preview = build_preview(plan, ast, source_duration_us=5_000_000, fps=30.0)
    return plan, preview


@pytest.fixture()
def store(tmp_path: Path):
    handle = DurableLiveSessionStore(tmp_path / "sessions.sqlite3", now=NOW)
    yield handle
    handle.close()


def _open(store: DurableLiveSessionStore, **overrides) -> object:
    params = dict(
        session_id=SESSION,
        project_id=PROJECT,
        principal_id=PRINCIPAL,
        base_revision_id="rev:0",
    )
    params.update(overrides)
    return store.open_session(**params)  # type: ignore[arg-type]


def _turn_payload(plan, **overrides):
    params = dict(
        session_id=SESSION,
        turn_id="turn-1",
        idempotency_key="idem-1",
        user_utterance="trim from 0.5 to 1.5 seconds",
        base_revision_id="rev:0",
        scope_signature="sha256:" + "b" * 64,
        plan=plan,
    )
    params.update(overrides)
    return params


class TestSessionStoreConfiguration:
    def test_file_is_wal_with_full_sync_and_foreign_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "cfg.sqlite3"
        store = DurableLiveSessionStore(path, now=NOW)
        try:
            assert store.busy_timeout_ms == 5000
            mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert str(mode).lower() == "wal"
            assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
            assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            assert store.path == path
            # the file is private to the owning process
            assert (path.stat().st_mode & 0o077) == 0
        finally:
            store.close()

    def test_canonical_path_is_under_the_queue_directory(self, tmp_path: Path) -> None:
        expected = tmp_path / "queue" / "ibpc_live_session.sqlite3"
        assert durable_live_session_path(tmp_path) == expected

    def test_declared_turn_transition_table_is_terminal_at_the_ends(self) -> None:
        assert TURN_TRANSITIONS[TurnStatus.EXECUTED] == frozenset()
        assert TURN_TRANSITIONS[TurnStatus.REJECTED] == frozenset()
        assert TURN_TRANSITIONS[TurnStatus.SUPERSEDED] == frozenset()
        assert TurnStatus.APPROVED in TURN_TRANSITIONS[TurnStatus.PREVIEW_READY]
        assert TurnStatus.EXECUTED in TURN_TRANSITIONS[TurnStatus.APPROVED]


class TestStrictMonotonicOrdering:
    def test_turn_indexes_are_monotonic_per_session(self, store, plan_and_preview) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        first = store.submit_turn(**_turn_payload(plan, turn_id="t1", idempotency_key="k1"))
        second = store.submit_turn(**_turn_payload(plan, turn_id="t2", idempotency_key="k2"))
        third = store.submit_turn(**_turn_payload(plan, turn_id="t3", idempotency_key="k3"))
        assert [first.turn_index, second.turn_index, third.turn_index] == [1, 2, 3]
        assert [t.turn_id for t in store.list_turns(SESSION)] == ["t1", "t2", "t3"]
        assert store.latest_turn(SESSION).turn_id == "t3"
        assert store.get_session(SESSION).last_turn_index == 3

    def test_unique_constraint_forbids_two_turns_at_one_index(
        self, store, plan_and_preview
    ) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        store.submit_turn(**_turn_payload(plan, turn_id="t1", idempotency_key="k1"))
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO ibpc_turns (turn_id, session_id, turn_index, idempotency_key,"
                " user_utterance, base_revision_id, scope_signature, compiled_plan_json,"
                " compiled_plan_sha256, turn_status, created_at_utc)"
                " VALUES ('dup', ?, 1, 'other-key', 'sha256:' || printf('%064d', 0), 'rev:0',"
                " 'sig', '{}', 'sha256:' || printf('%064d', 0), 'DRAFTED', 'now')",
                (SESSION,),
            )

    def test_latest_turn_on_an_empty_session_is_a_typed_refusal(
        self, store, plan_and_preview
    ) -> None:
        _open(store)
        with pytest.raises(StaleSessionError) as exc:
            store.latest_turn(SESSION)
        assert exc.value.reason == "no_turns"


class TestIdempotentSubmission:
    def test_duplicate_submission_returns_the_stored_record(self, store, plan_and_preview) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        payload = _turn_payload(plan)
        first = store.submit_turn(**payload)
        again = store.submit_turn(**payload)
        assert again == first
        # f(f(x)) == f(x): no second row, no index movement, no epoch movement.
        assert len(store.list_turns(SESSION)) == 1
        assert store.get_session(SESSION).last_turn_index == 1

    def test_replaying_the_turn_id_with_a_new_key_returns_the_stored_turn(
        self, store, plan_and_preview
    ) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        first = store.submit_turn(**_turn_payload(plan))
        replay = store.submit_turn(**_turn_payload(plan, idempotency_key="another-key"))
        assert replay == first
        assert len(store.list_turns(SESSION)) == 1

    def test_same_key_different_turn_is_an_idempotency_conflict(
        self, store, plan_and_preview
    ) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        store.submit_turn(**_turn_payload(plan))
        with pytest.raises(TurnStoreError) as exc:
            store.submit_turn(**_turn_payload(plan, turn_id="turn-other"))
        assert exc.value.reason == "idempotency_conflict"

    def test_same_key_different_payload_is_an_idempotency_conflict(
        self, store, plan_and_preview
    ) -> None:
        plan, preview = plan_and_preview
        _open(store)
        store.submit_turn(**_turn_payload(plan))
        other_plan = compile_ast(
            parse_intent(
                "trim from 1.0 to 2.0 seconds",
                project_id=PROJECT,
                asset_id="asset-w3",
                source_revision=0,
                duration_us=5_000_000,
                channel_binding_id=BINDING,
            ).ast,
            registry=build_runtime_registry(),
        )
        with pytest.raises(TurnStoreError) as exc:
            store.submit_turn(**_turn_payload(other_plan))
        assert exc.value.reason == "idempotency_conflict"
        assert preview.plan_digest  # the fixture's preview stays usable

    def test_empty_idempotency_key_is_refused(self, store, plan_and_preview) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        with pytest.raises(TurnStoreError) as exc:
            store.submit_turn(**_turn_payload(plan, idempotency_key=""))
        assert exc.value.reason == "invalid_idempotency_key"

    def test_invalid_turn_status_is_refused(self, store, plan_and_preview) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        with pytest.raises(TurnStoreError) as exc:
            store.submit_turn(**_turn_payload(plan, turn_status="DRAFTED"))  # type: ignore[arg-type]
        assert exc.value.reason == "invalid_turn_status"

    def test_get_by_idempotency_key_round_trips(self, store, plan_and_preview) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        stored = store.submit_turn(**_turn_payload(plan))
        assert store.get_turn_by_idempotency_key("idem-1") == stored
        with pytest.raises(TurnStoreError) as exc:
            store.get_turn_by_idempotency_key("absent")
        assert exc.value.reason == "unknown_idempotency_key"


class TestStaleRevisionProtection:
    def test_submitting_against_a_stale_base_is_refused_before_writing(
        self, store, plan_and_preview
    ) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        with pytest.raises(StaleSessionRevisionError) as exc:
            store.submit_turn(**_turn_payload(plan, base_revision_id="rev:stale"))
        assert exc.value.reason == "stale_revision"
        assert store.list_turns(SESSION) == ()
        assert store.get_session(SESSION).last_turn_index == 0

    def test_advancing_the_revision_makes_old_bases_stale(self, store, plan_and_preview) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        store.advance_revision(SESSION, revision_id="rev:1")
        with pytest.raises(StaleSessionRevisionError):
            store.submit_turn(**_turn_payload(plan, base_revision_id="rev:0"))
        accepted = store.submit_turn(**_turn_payload(plan, base_revision_id="rev:1"))
        assert accepted.base_revision_id == "rev:1"

    def test_editing_a_turn_under_a_stale_revision_is_refused(
        self, store, plan_and_preview
    ) -> None:
        plan, preview = plan_and_preview
        _open(store)
        store.submit_turn(**_turn_payload(plan))
        store.advance_revision(SESSION, revision_id="rev:1")
        with pytest.raises(StaleSessionRevisionError) as exc:
            store.set_turn_preview(
                SESSION, "turn-1", preview=preview, approval_id="appr_1", base_revision_id="rev:0"
            )
        assert exc.value.reason == "stale_revision"
        assert store.get_turn(SESSION, "turn-1").turn_status is TurnStatus.DRAFTED

    def test_empty_revision_is_refused(self, store) -> None:
        _open(store)
        with pytest.raises(TurnStoreError) as exc:
            store.advance_revision(SESSION, revision_id="")
        assert exc.value.reason == "invalid_revision"


class TestDeterministicTermination:
    def test_close_revokes_unconsumed_approvals_and_supersedes_open_turns(
        self, store, plan_and_preview
    ) -> None:
        plan, preview = plan_and_preview
        _open(store)
        store.submit_turn(**_turn_payload(plan, turn_id="t1", idempotency_key="k1"))
        store.submit_turn(**_turn_payload(plan, turn_id="t2", idempotency_key="k2"))
        store.set_turn_preview(
            SESSION, "t2", preview=preview, approval_id="appr_preview", base_revision_id="rev:0"
        )
        revoked: list[str] = []
        closed = store.close_session(SESSION, revoke_approvals=revoked.append)
        assert closed.status is SessionStatus.CLOSED
        assert revoked == [SESSION]
        assert {t.turn_status for t in store.list_turns(SESSION)} == {TurnStatus.SUPERSEDED}
        target = store._conn.execute(
            "SELECT COUNT(*) FROM ibpc_turns WHERE session_id = ? AND approval_id IS NOT NULL"
            " AND turn_status = 'PREVIEW_READY'",
            (SESSION,),
        ).fetchone()[0]
        assert target == 0

    def test_close_is_idempotent(self, store) -> None:
        _open(store)
        first = store.close_session(SESSION)
        second = store.close_session(SESSION)
        assert first.status is second.status is SessionStatus.CLOSED

    def test_close_after_abort_is_a_typed_refusal(self, store) -> None:
        _open(store)
        store.abort_session(SESSION)
        with pytest.raises(TurnStoreError) as exc:
            store.close_session(SESSION)
        assert exc.value.reason == "session_terminated"

    def test_abort_is_terminal_for_turn_submission(self, store, plan_and_preview) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        store.abort_session(SESSION)
        with pytest.raises(TurnStoreError) as exc:
            store.submit_turn(**_turn_payload(plan))
        assert exc.value.reason == "session_terminated"
        assert store.list_turns(SESSION) == ()

    def test_terminated_sessions_refuse_edits_and_revisions(self, store, plan_and_preview) -> None:
        plan, preview = plan_and_preview
        _open(store)
        store.submit_turn(**_turn_payload(plan))
        store.close_session(SESSION)
        with pytest.raises(TurnStoreError) as exc:
            store.advance_revision(SESSION, revision_id="rev:2")
        assert exc.value.reason == "session_terminated"
        with pytest.raises(TurnStoreError):
            store.set_turn_preview(
                SESSION, "turn-1", preview=preview, approval_id="a", base_revision_id="rev:0"
            )
        with pytest.raises(TurnStoreError):
            store.pause_session(SESSION)

    def test_pause_and_resume_lifecycle(self, store) -> None:
        _open(store)
        paused = store.pause_session(SESSION)
        assert paused.status is SessionStatus.PAUSED
        resumed = store.resume_session(SESSION)
        assert resumed.status is SessionStatus.ACTIVE
        with pytest.raises(TurnStoreError) as exc:
            store.resume_session(SESSION)
        assert exc.value.reason == "invalid_status"


class TestReconstructionAcrossRestart:
    def test_state_survives_close_and_reopen(self, tmp_path: Path, plan_and_preview) -> None:
        plan, preview = plan_and_preview
        path = tmp_path / "restart.sqlite3"
        first = DurableLiveSessionStore(path, now=NOW)
        _open(first)
        first.submit_turn(**_turn_payload(plan))
        first.set_turn_preview(
            SESSION, "turn-1", preview=preview, approval_id="appr_1", base_revision_id="rev:0"
        )
        first.mark_turn_approved(SESSION, "turn-1", base_revision_id="rev:0")
        first.close()

        second = DurableLiveSessionStore(path, now=NOW)
        try:
            session = second.get_session(SESSION)
            assert session.status is SessionStatus.ACTIVE
            assert session.last_turn_index == 1
            turn = second.get_turn(SESSION, "turn-1")
            assert turn.turn_status is TurnStatus.APPROVED
            assert turn.approval_id == "appr_1"
            assert turn.compiled_plan().plan_digest == plan.plan_digest
            assert turn.preview_manifest() is not None
            assert turn.preview_manifest().preview_digest == preview.preview_digest  # type: ignore[union-attr]
            # the next turn continues the monotonic series
            second.submit_turn(**_turn_payload(plan, turn_id="turn-2", idempotency_key="idem-2"))
            assert second.latest_turn(SESSION).turn_index == 2
        finally:
            second.close()

    def test_raw_utterance_never_reaches_the_file(self, store, plan_and_preview) -> None:
        plan, _preview = plan_and_preview
        secret = "the private phrase that must never be persisted"
        _open(store)
        stored = store.submit_turn(**_turn_payload(plan, user_utterance=secret))
        assert stored.user_utterance == utterance_digest(secret)
        assert secret not in stored.user_utterance
        store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        raw = store.path.read_bytes()
        assert secret.encode("utf-8") not in raw
        assert utterance_digest(secret).encode("utf-8") in raw


class TestConcurrentSerialization:
    def test_epoch_cas_detects_a_conflicting_stale_writer(self, store, plan_and_preview) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        stale_view = store.get_session(SESSION)
        store.submit_turn(**_turn_payload(plan))
        with pytest.raises(ConcurrentSessionMutationError) as exc:
            store._cas_session(stale_view, "status = ?", ("PAUSED",), "epoch_conflict")
        assert exc.value.reason == "epoch_conflict"

    def test_parallel_threads_allocate_distinct_indexes(
        self, tmp_path: Path, plan_and_preview
    ) -> None:
        plan, _preview = plan_and_preview
        path = tmp_path / "threads.sqlite3"
        store = DurableLiveSessionStore(path, now=NOW)
        _open(store)
        errors: list[BaseException] = []
        indexes: list[int] = []
        lock = threading.Lock()

        def worker(number: int) -> None:
            try:
                record = store.submit_turn(
                    **_turn_payload(
                        plan,
                        turn_id=f"turn-{number}",
                        idempotency_key=f"key-{number}",
                    )
                )
                with lock:
                    indexes.append(record.turn_index)
            except BaseException as exc:  # noqa: BLE001 - collected for assertion
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        store.close()
        assert errors == []
        assert sorted(indexes) == list(range(1, 9))

    def test_parallel_processes_open_the_same_file_safely(
        self, tmp_path: Path, plan_and_preview
    ) -> None:
        """Two independent store instances (as two processes would) interleave writes."""
        plan, _preview = plan_and_preview
        path = tmp_path / "processes.sqlite3"
        primary = DurableLiveSessionStore(path, now=NOW)
        secondary = DurableLiveSessionStore(path, now=NOW)
        try:
            _open(primary)
            primary.submit_turn(**_turn_payload(plan, turn_id="t1", idempotency_key="k1"))
            # the second connection observes the committed row immediately
            assert secondary.latest_turn(SESSION).turn_id == "t1"
            secondary.submit_turn(**_turn_payload(plan, turn_id="t2", idempotency_key="k2"))
            assert [t.turn_id for t in primary.list_turns(SESSION)] == ["t1", "t2"]
        finally:
            primary.close()
            secondary.close()

    def test_unavailable_write_lock_is_a_typed_failure(
        self, tmp_path: Path, plan_and_preview
    ) -> None:
        """A lock that cannot be obtained raises, never hangs or silently proceeds."""
        plan, _preview = plan_and_preview
        path = tmp_path / "locked.sqlite3"
        store = DurableLiveSessionStore(path, now=NOW)
        _open(store)
        blocker = sqlite3.connect(str(path), isolation_level=None)
        try:
            blocker.execute("BEGIN EXCLUSIVE")
            with pytest.raises(ConcurrentSessionMutationError) as exc:
                store.submit_turn(**_turn_payload(plan))
            assert exc.value.reason in {"write_lock_unavailable", "commit_conflict"}
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
            store.close()

    def test_commit_conflict_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A COMMIT that loses the race is a typed error, never a silent success."""
        store = DurableLiveSessionStore(tmp_path / "commit.sqlite3", now=NOW)
        _open(store)

        class _ExplodingCommit:
            def __init__(self, wrapped) -> None:
                self._wrapped = wrapped

            def execute(self, statement: str, *args):
                if statement == "COMMIT":
                    raise sqlite3.OperationalError("database is locked")
                return self._wrapped.execute(statement, *args)

        monkeypatch.setattr(store, "_conn", _ExplodingCommit(store._conn))
        with pytest.raises(ConcurrentSessionMutationError) as exc:
            with store._transaction():
                pass
        assert exc.value.reason == "commit_conflict"
        monkeypatch.undo()
        store.close()


class TestOpenSessionContract:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"session_id": ""},
            {"project_id": ""},
            {"principal_id": ""},
            {"base_revision_id": ""},
        ],
    )
    def test_missing_required_identity_is_refused(self, store, overrides) -> None:
        with pytest.raises(TurnStoreError) as exc:
            _open(store, **overrides)
        assert exc.value.reason == "invalid_session"

    def test_open_is_idempotent_for_the_identical_request(self, store) -> None:
        first = _open(store)
        again = _open(store)
        assert again == first

    def test_open_with_a_different_identity_is_a_duplicate(self, store) -> None:
        _open(store)
        with pytest.raises(TurnStoreError) as exc:
            _open(store, base_revision_id="rev:9")
        assert exc.value.reason == "duplicate_session"

    def test_second_open_session_for_the_same_project_and_principal_is_refused(self, store) -> None:
        _open(store)
        with pytest.raises(TurnStoreError) as exc:
            _open(store, session_id="sess_other")
        assert exc.value.reason == "duplicate_open_session"

    def test_a_closed_session_frees_the_slot(self, store) -> None:
        _open(store)
        store.close_session(SESSION)
        assert _open(store, session_id="sess_other").status is SessionStatus.ACTIVE

    def test_unknown_session_and_turn_are_typed(self, store) -> None:
        with pytest.raises(TurnStoreError) as exc:
            store.get_session("nope")
        assert exc.value.reason == "unknown_session"
        with pytest.raises(TurnStoreError) as turn_exc:
            store.get_turn(SESSION, "nope")
        assert turn_exc.value.reason == "unknown_turn"

    def test_active_revision_override_is_respected(self, store) -> None:
        record = _open(store, active_revision_id="rev:active")
        assert record.base_revision_id == "rev:0"
        assert record.active_revision_id == "rev:active"


class TestTurnStatusLadder:
    def _previewed(self, store, plan, preview):
        _open(store)
        store.submit_turn(**_turn_payload(plan))
        return store.set_turn_preview(
            SESSION, "turn-1", preview=preview, approval_id="appr_1", base_revision_id="rev:0"
        )

    def test_preview_approve_execute_round_trip(self, store, plan_and_preview) -> None:
        plan, preview = plan_and_preview
        previewed = self._previewed(store, plan, preview)
        assert previewed.turn_status is TurnStatus.PREVIEW_READY
        assert previewed.preview_sha256 and previewed.approval_id == "appr_1"
        approved = store.mark_turn_approved(SESSION, "turn-1", base_revision_id="rev:0")
        assert approved.turn_status is TurnStatus.APPROVED
        executed = store.mark_turn_executed(SESSION, "turn-1", base_revision_id="rev:0")
        assert executed.turn_status is TurnStatus.EXECUTED

    def test_illegal_transitions_are_typed_refusals(self, store, plan_and_preview) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        store.submit_turn(**_turn_payload(plan))
        with pytest.raises(TurnStoreError) as exc:
            store.mark_turn_executed(SESSION, "turn-1", base_revision_id="rev:0")
        assert exc.value.reason == "invalid_transition"

    def test_executed_is_terminal(self, store, plan_and_preview) -> None:
        plan, preview = plan_and_preview
        self._previewed(store, plan, preview)
        store.mark_turn_approved(SESSION, "turn-1", base_revision_id="rev:0")
        store.mark_turn_executed(SESSION, "turn-1", base_revision_id="rev:0")
        for attempt in (
            lambda: store.reject_turn(SESSION, "turn-1", base_revision_id="rev:0"),
            lambda: store.mark_turn_approved(SESSION, "turn-1", base_revision_id="rev:0"),
        ):
            with pytest.raises(TurnStoreError) as exc:
                attempt()
            assert exc.value.reason == "invalid_transition"

    def test_reject_is_terminal(self, store, plan_and_preview) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        store.submit_turn(**_turn_payload(plan))
        rejected = store.reject_turn(SESSION, "turn-1", base_revision_id="rev:0")
        assert rejected.turn_status is TurnStatus.REJECTED
        with pytest.raises(TurnStoreError):
            store.set_turn_preview(
                SESSION, "turn-1", preview=_preview, approval_id="a", base_revision_id="rev:0"
            )


class TestTurnRecordViewsAndSeams:
    def test_a_drafted_turn_has_no_preview_manifest(self, store, plan_and_preview) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        record = store.submit_turn(**_turn_payload(plan))
        assert record.preview_manifest() is None
        assert record.compiled_plan().plan_id == plan.plan_id

    def test_a_previewed_turn_exposes_its_manifest(self, store, plan_and_preview) -> None:
        plan, preview = plan_and_preview
        _open(store)
        store.submit_turn(**_turn_payload(plan))
        record = store.set_turn_preview(
            SESSION,
            "turn-1",
            preview=preview,
            approval_id="approval-1",
            base_revision_id="rev:0",
        )
        assert record.preview_manifest() is not None
        assert record.approval_id == "approval-1"

    def test_the_transaction_seam_is_reentrant(self, store) -> None:
        _open(store)
        with store._transaction():
            with store._transaction():
                assert store.get_session(SESSION).session_id == SESSION

    def test_a_turn_index_collision_is_a_concurrency_error(self, store, plan_and_preview) -> None:
        plan, _preview = plan_and_preview
        _open(store)
        store.submit_turn(**_turn_payload(plan))
        # a competing writer already allocated index 2 for this session
        store._conn.execute(
            "INSERT INTO ibpc_turns (turn_id, session_id, turn_index, idempotency_key,"
            " user_utterance, base_revision_id, scope_signature, compiled_plan_json,"
            " compiled_plan_sha256, turn_status, created_at_utc)"
            " VALUES ('turn-ghost', ?, 2, 'idem-ghost', 'x', 'rev:0', 'sig', '{}', 'sha',"
            " 'DRAFTED', ?)",
            (SESSION, NOW()),
        )
        with pytest.raises(ConcurrentSessionMutationError) as exc:
            store.submit_turn(**_turn_payload(plan, turn_id="turn-2", idempotency_key="idem-2"))
        assert exc.value.reason == "turn_index_conflict"

    def test_a_lost_turn_update_is_a_concurrency_error(self, store, plan_and_preview) -> None:
        plan, preview = plan_and_preview
        _open(store)
        store.submit_turn(**_turn_payload(plan))

        class _ZeroRowcount:
            """A cursor proxy reporting that the conditional UPDATE matched nothing."""

            def __init__(self, inner) -> None:
                self._inner = inner
                self.rowcount = 0

            def __getattr__(self, name):
                return getattr(self._inner, name)

        class _ConnProxy:
            def __init__(self, inner) -> None:
                self._inner = inner

            def execute(self, sql, *args):
                cursor = self._inner.execute(sql, *args)
                return _ZeroRowcount(cursor) if "UPDATE ibpc_turns" in sql else cursor

            def __getattr__(self, name):
                return getattr(self._inner, name)

        real = store._conn
        store._conn = _ConnProxy(real)  # type: ignore[assignment]
        try:
            with pytest.raises(ConcurrentSessionMutationError) as exc:
                store.set_turn_preview(
                    SESSION,
                    "turn-1",
                    preview=preview,
                    approval_id="approval-1",
                    base_revision_id="rev:0",
                )
        finally:
            store._conn = real
        assert exc.value.reason == "turn_conflict"
