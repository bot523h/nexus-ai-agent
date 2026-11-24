"""Real-disk tests for the provisional W4 plan reservation store."""

import json
import os
import sqlite3
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nexus_ai_agent.adapters import durable_plan_dedupe as store_module
from nexus_ai_agent.adapters.durable_plan_dedupe import DurablePlanDedupeStore
from nexus_ai_agent.creative.studio.durable_dedupe import (
    PlanInDoubtQuarantineError,
    PlanReplayConflictError,
    PlanStoreCapacityError,
    PlanStoreStateError,
)

HASH = "a" * 64
OTHER = "b" * 64


@pytest.fixture
def store(tmp_path: Path) -> DurablePlanDedupeStore:
    return DurablePlanDedupeStore(tmp_path / "plans.sqlite")


def test_wal_and_durable_redelivery(tmp_path: Path) -> None:
    path = tmp_path / "plans.sqlite"
    first = DurablePlanDedupeStore(path)
    assert first.reserve("p", HASH, external_effects=True).status == "RESERVED"
    lease = first.start("p", HASH, nonce="n")
    first.finish(
        "p", HASH, attempt=lease.attempt, owner_token=lease.owner_token, evidence={"z": 2, "a": 1}
    )
    replay = DurablePlanDedupeStore(path).reserve("p", HASH, external_effects=True)
    assert replay.status == "COMMITTED"
    assert replay.evidence_json is not None
    assert json.loads(replay.evidence_json) == {"a": 1, "z": 2}
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_in_doubt_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "db"
    first = DurablePlanDedupeStore(path, clock=lambda: 1.0)
    first.reserve("p", HASH, external_effects=True)
    lease = first.start("p", HASH, nonce="n")
    for _ in range(2):
        with pytest.raises(PlanInDoubtQuarantineError):
            DurablePlanDedupeStore(path, clock=lambda: 31.0).reserve(
                "p", HASH, external_effects=True
            )
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone()[0] == "IN_DOUBT"
    with pytest.raises(PlanStoreStateError):
        first.finish("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token, evidence={})


def test_explicit_quarantine_is_permanent(store: DurablePlanDedupeStore) -> None:
    store.reserve("p", HASH, external_effects=True)
    lease = store.start("p", HASH, nonce="n")
    store.quarantine("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token)
    with pytest.raises(PlanInDoubtQuarantineError):
        store.reserve("p", HASH, external_effects=True)
    with pytest.raises(PlanStoreStateError):
        store.quarantine("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token)


def test_conflicting_hash_and_effect_scope(store: DurablePlanDedupeStore) -> None:
    store.reserve("p", HASH, external_effects=True)
    with pytest.raises(PlanReplayConflictError):
        store.reserve("p", OTHER, external_effects=True)
    with pytest.raises(PlanReplayConflictError):
        store.reserve("p", HASH, external_effects=False)


def test_reserved_identity_does_not_replay(store: DurablePlanDedupeStore) -> None:
    store.reserve("p", HASH, external_effects=False)
    with pytest.raises(PlanInDoubtQuarantineError):
        store.reserve("p", HASH, external_effects=False)


def test_nonce_consumption_is_durable(store: DurablePlanDedupeStore) -> None:
    store.reserve("p", HASH, external_effects=False)
    lease = store.start("p", HASH, nonce="same")
    store.finish("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token, evidence={})
    store.reserve("q", HASH, external_effects=False)
    with pytest.raises(PlanStoreStateError, match="nonce already consumed"):
        store.start("q", HASH, nonce="same")
    store.start("q", HASH, nonce="another")


def test_capacity_fails_closed_without_evicting_tombstone(
    store: DurablePlanDedupeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "MAX_RECORDS", 1)
    store.reserve("p", HASH, external_effects=True)
    lease = store.start("p", HASH, nonce="n")
    store.quarantine("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token)
    with pytest.raises(PlanStoreCapacityError):
        store.reserve("q", HASH, external_effects=False)
    with pytest.raises(PlanInDoubtQuarantineError):
        store.reserve("p", HASH, external_effects=True)


@pytest.mark.parametrize("plan_id", ["", "x" * 257, None, 3])
def test_bad_plan_id(store: DurablePlanDedupeStore, plan_id: Any) -> None:
    with pytest.raises(ValueError):
        store.reserve(plan_id, HASH, external_effects=False)


@pytest.mark.parametrize("plan_hash", ["", "A" * 64, "a" * 63, "g" * 64, None])
def test_bad_plan_hash(store: DurablePlanDedupeStore, plan_hash: Any) -> None:
    with pytest.raises(ValueError):
        store.reserve("p", plan_hash, external_effects=False)


@pytest.mark.parametrize("nonce", ["", "x" * 257, None, 1])
def test_bad_nonce(store: DurablePlanDedupeStore, nonce: Any) -> None:
    with pytest.raises(ValueError):
        store.start("p", HASH, nonce=nonce)


def test_reject_memory_store() -> None:
    with pytest.raises(ValueError, match="persistent"):
        DurablePlanDedupeStore(":memory:")


def test_effect_declaration_must_be_boolean(store: DurablePlanDedupeStore) -> None:
    with pytest.raises(ValueError):
        store.reserve("p", HASH, external_effects=1)  # type: ignore[arg-type]  # adversarial


@pytest.mark.parametrize("kind", ["missing", "wrong_hash", "wrong_state"])
def test_start_requires_reserved_fingerprint(store: DurablePlanDedupeStore, kind: str) -> None:
    if kind != "missing":
        store.reserve("p", HASH, external_effects=False)
    if kind == "wrong_state":
        store.start("p", HASH, nonce="one")
    with pytest.raises(PlanStoreStateError):
        store.start("p", OTHER if kind == "wrong_hash" else HASH, nonce="two")


def test_finish_rejects_invalid_evidence_and_preserves_execution(
    store: DurablePlanDedupeStore,
) -> None:
    store.reserve("p", HASH, external_effects=False)
    lease = store.start("p", HASH, nonce="n")
    with pytest.raises(ValueError):
        store.finish(
            "p",
            HASH,
            attempt=lease.attempt,
            owner_token=lease.owner_token,
            evidence={"bad": float("nan")},
        )
    with pytest.raises(ValueError, match="byte limit"):
        store.finish(
            "p",
            HASH,
            attempt=lease.attempt,
            owner_token=lease.owner_token,
            evidence="x" * (store_module.MAX_EVIDENCE_BYTES + 1),
        )
    store.finish(
        "p", HASH, attempt=lease.attempt, owner_token=lease.owner_token, evidence={"ok": True}
    )
    with pytest.raises(PlanStoreStateError):
        store.finish("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token, evidence={})


def test_competing_reservations_are_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "db"
    DurablePlanDedupeStore(path)

    def reserve() -> str:
        try:
            return DurablePlanDedupeStore(path).reserve("p", HASH, external_effects=True).status
        except PlanInDoubtQuarantineError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: reserve(), range(2)))
    assert sorted(outcomes) == ["RESERVED", "refused"]


def test_transaction_cleanup_propagates_control_error(store: DurablePlanDedupeStore) -> None:
    with pytest.raises(KeyboardInterrupt):
        with store._transaction() as conn:
            conn.execute("INSERT INTO consumed_nonces VALUES ('default', 'n')")
            raise KeyboardInterrupt
    store.reserve("p", HASH, external_effects=False)
    store.start("p", HASH, nonce="n")


def test_transaction_already_rolled_back_preserves_original_error(
    store: DurablePlanDedupeStore,
) -> None:
    with pytest.raises(ValueError, match="original"):
        with store._transaction() as conn:
            conn.execute("ROLLBACK")
            raise ValueError("original")


def test_wal_unavailable_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connect = sqlite3.connect
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_args, **_kwargs: connect(":memory:", isolation_level=None),
    )
    with pytest.raises(PlanStoreStateError, match="WAL"):
        DurablePlanDedupeStore(tmp_path / "db")


def test_path_is_absolute_after_chdir_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store = DurablePlanDedupeStore("private/sub/plans.sqlite")
    monkeypatch.chdir(tmp_path.parent)
    store.reserve("p", HASH, external_effects=False)
    assert store.path == tmp_path / "private/sub/plans.sqlite"
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "private/sub").stat().st_mode & 0o777 == 0o700
    with store._transaction():
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{store.path}{suffix}")
            assert sidecar.exists()
            assert sidecar.stat().st_mode & 0o777 == 0o600
    os.chmod(store.path, 0o666)
    DurablePlanDedupeStore(store.path)
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_symlink_database_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("unchanged")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        DurablePlanDedupeStore(link)
    assert target.read_text() == "unchanged"


def test_distinct_projects_isolate_plans_evidence_and_nonces(store: DurablePlanDedupeStore) -> None:
    for project, value in (("project-a", "secret-a"), ("project-b", "secret-b")):
        store.reserve("same-plan", HASH, external_effects=True, project_id=project)
        lease = store.start("same-plan", HASH, nonce="same-nonce", project_id=project)
        store.finish(
            "same-plan",
            HASH,
            attempt=lease.attempt,
            owner_token=lease.owner_token,
            evidence={"value": value},
            project_id=project,
        )
    for project, value in (("project-a", "secret-a"), ("project-b", "secret-b")):
        record = store.reserve("same-plan", HASH, external_effects=True, project_id=project)
        assert record.evidence_json is not None
        assert json.loads(record.evidence_json) == {"value": value}
    store.reserve("fresh", HASH, external_effects=False, project_id="project-a")
    with pytest.raises(PlanStoreStateError, match="nonce already consumed"):
        store.start("fresh", HASH, nonce="same-nonce", project_id="project-a")


@pytest.mark.parametrize("project", ["", "   ", "é" * 129, None])
def test_invalid_project_identity(store: DurablePlanDedupeStore, project: Any) -> None:
    with pytest.raises(ValueError):
        store.reserve("p", HASH, external_effects=False, project_id=project)


def test_migrate_legacy_records_and_nonce_to_default_project(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE plans (plan_id TEXT PRIMARY KEY, plan_hash TEXT NOT NULL, "
            "status TEXT NOT NULL, external_effects INTEGER NOT NULL, evidence_json TEXT)"
        )
        conn.execute("CREATE TABLE consumed_nonces (nonce TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO plans VALUES (?, ?, 'COMMITTED', 1, ?)", ("p", HASH, '{"old":1}'))
        conn.execute("INSERT INTO consumed_nonces VALUES ('n')")
    store = DurablePlanDedupeStore(path)
    assert store.reserve("p", HASH, external_effects=True).evidence_json == '{"old":1}'
    store.reserve("p", OTHER, external_effects=True, project_id="other")
    store.start("p", OTHER, nonce="n", project_id="other")
    store.reserve("q", HASH, external_effects=False)
    with pytest.raises(PlanStoreStateError, match="nonce already consumed"):
        store.start("q", HASH, nonce="n")


def test_oversized_incremental_evidence_leaves_execution_state(
    store: DurablePlanDedupeStore,
) -> None:
    store.reserve("p", HASH, external_effects=False)
    lease = store.start("p", HASH, nonce="n")
    with pytest.raises(ValueError, match="byte limit"):
        store.finish(
            "p",
            HASH,
            attempt=lease.attempt,
            owner_token=lease.owner_token,
            evidence=["x" * 1024] * 100,
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT status, evidence_json FROM plans").fetchone() == (
            "EXECUTING",
            None,
        )


def test_plan_id_validation_agrees_with_bus(store: DurablePlanDedupeStore) -> None:
    from nexus_ai_agent.creative.studio.bus import PlanStep, PlanTransaction

    for plan_id in ("   ", "é" * 129):
        with pytest.raises(ValueError):
            store.reserve(plan_id, HASH, external_effects=False)
        with pytest.raises(ValueError):
            PlanTransaction(plan_id=plan_id, steps=(PlanStep("test", execute=lambda: 1),))


def test_missing_sidecars_are_safe(store: DurablePlanDedupeStore) -> None:
    assert not Path(f"{store.path}-wal").exists()
    assert not Path(f"{store.path}-shm").exists()
    store._protect_sidecars()


def test_nonregular_sidecar_is_refused(store: DurablePlanDedupeStore) -> None:
    Path(f"{store.path}-wal").mkdir()
    with pytest.raises(ValueError, match="sidecar must be a regular file"):
        store._protect_sidecars()


def test_nonregular_database_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stat, "S_ISREG", lambda _mode: False)
    with pytest.raises(ValueError, match="SQLite path must be a regular file"):
        DurablePlanDedupeStore(tmp_path / "db")


def test_bus_preflight_refuses_forged_protocol() -> None:
    from nexus_ai_agent.creative.studio.bus import CommandBus
    from nexus_ai_agent.creative.studio.models import (
        CommandValidationError,
        Timeline,
        TypedCommand,
        new_project,
    )

    bus = CommandBus(new_project("project", "name", Timeline(timeline_id="t", duration_us=0)))
    forged = TypedCommand(command_id="id", operation="w4.forged").model_copy(
        update={"protocol_version": "forged"}
    )
    with pytest.raises(CommandValidationError, match="unsupported protocol_version"):
        bus._validate_dispatch(forged)


def _open_store_in_process(path: str, barrier: Any, result_queue: Any) -> None:
    try:
        barrier.wait(timeout=10)
        store = DurablePlanDedupeStore(path)
        store.reserve(str(os.getpid()), HASH, external_effects=False)
        result_queue.put("ok")
    except Exception as exc:
        result_queue.put(f"{type(exc).__name__}: {exc}")


def test_simultaneous_multiprocess_initialization(tmp_path: Path) -> None:
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    workers = 6
    barrier = context.Barrier(workers)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_open_store_in_process,
            args=(str(tmp_path / "concurrent.sqlite"), barrier, result_queue),
        )
        for _ in range(workers)
    ]
    try:
        for process in processes:
            process.start()
        results = [result_queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=10)
        assert results == ["ok"] * workers
        assert all(process.exitcode == 0 for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=10)
        result_queue.close()
        result_queue.join_thread()


def test_wal_setup_retries_only_busy_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = sqlite3.connect
    attempts = 0

    def flaky_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        return cast(sqlite3.Connection, connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", flaky_connect)
    store = DurablePlanDedupeStore(tmp_path / "db")
    assert attempts == 2
    assert store.path.exists()


def test_wal_setup_retries_protocol_error_without_sqlite_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is a synthetic coded error, not evidence of the Python 3.10 API.
    connect = sqlite3.connect
    attempts = 0

    def flaky_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            error = sqlite3.OperationalError("protocol error")
            error.sqlite_errorcode = 15
            raise error
        return cast(sqlite3.Connection, connect(*args, **kwargs))

    monkeypatch.delattr(sqlite3, "SQLITE_PROTOCOL", raising=False)
    monkeypatch.setattr(sqlite3, "connect", flaky_connect)
    store = DurablePlanDedupeStore(tmp_path / "db")
    assert attempts == 2
    assert store.path.exists()


def test_wal_setup_propagates_real_sqlite_connect_error_across_python_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SQLite itself creates the exception; no error-code attribute is injected.
    connect = sqlite3.connect
    seen: list[sqlite3.OperationalError] = []
    missing = tmp_path / "does-not-exist.sqlite"
    uri = f"{missing.as_uri()}?mode=ro"

    def fail_connect(*_args: Any, **_kwargs: Any) -> sqlite3.Connection:
        try:
            return connect(uri, uri=True)
        except sqlite3.OperationalError as exc:
            seen.append(exc)
            raise

    monkeypatch.delattr(sqlite3, "SQLITE_PROTOCOL", raising=False)
    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        DurablePlanDedupeStore(tmp_path / "db")
    assert len(seen) == 1  # A real non-busy error must not be retried.
    if sys.version_info < (3, 11):
        assert not hasattr(seen[0], "sqlite_errorcode")
    else:
        assert getattr(seen[0], "sqlite_errorcode", None) == getattr(sqlite3, "SQLITE_CANTOPEN", 14)


@pytest.mark.parametrize("message", ["disk I/O error", "database is busy"])
def test_wal_setup_fails_closed_on_nonbusy_or_expired_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    def failed_connect(*_args: Any, **_kwargs: Any) -> sqlite3.Connection:
        raise sqlite3.OperationalError(message)

    monotonic_values = iter([0.0, 0.0, 3.0 if "busy" in message else 0.0])
    monkeypatch.delattr(sqlite3, "SQLITE_PROTOCOL", raising=False)
    monkeypatch.setattr(sqlite3, "connect", failed_connect)
    monkeypatch.setattr(
        store_module, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values))
    )
    with pytest.raises(sqlite3.OperationalError, match=message):
        DurablePlanDedupeStore(tmp_path / "db")


def test_setup_error_closes_open_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []
    connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            raise sqlite3.OperationalError("disk I/O error")

        def close(self) -> None:
            closed.append(True)
            super().close()

    def create_connection(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return cast(sqlite3.Connection, connect(*args, **kwargs, factory=FailingConnection))

    monkeypatch.delattr(sqlite3, "SQLITE_PROTOCOL", raising=False)
    monkeypatch.setattr(sqlite3, "connect", create_connection)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        DurablePlanDedupeStore(tmp_path / "db")
    assert closed == [True]


def test_interrupt_before_connection_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(*_args: Any, **_kwargs: Any) -> sqlite3.Connection:
        raise KeyboardInterrupt

    monkeypatch.setattr(sqlite3, "connect", interrupt)
    with pytest.raises(KeyboardInterrupt):
        DurablePlanDedupeStore(tmp_path / "db")


def test_nonregular_lock_is_refused(
    store: DurablePlanDedupeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stat, "S_ISREG", lambda _mode: False)
    with pytest.raises(ValueError, match="lock must be a regular file"), store._store_lock():
        pytest.fail("unreachable")


def test_store_lock_timeout_fails_closed(
    store: DurablePlanDedupeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def locked(_fd: int, _flags: int) -> None:
        raise BlockingIOError

    clock = iter([0.0, 3.0])
    monkeypatch.setattr(store_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(store_module, "fcntl", SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=locked))
    with pytest.raises(PlanStoreStateError, match="lock timed out"), store._store_lock():
        pytest.fail("unreachable")


def test_evidence_precheck_rejects_oversized_string_before_encoder(
    store: DurablePlanDedupeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.reserve("p", HASH, external_effects=False)
    lease = store.start("p", HASH, nonce="n")

    def unexpected_encoder(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("encoder should not be invoked")

    monkeypatch.setattr(json, "JSONEncoder", unexpected_encoder)
    with pytest.raises(ValueError, match="byte limit"):
        store.finish(
            "p",
            HASH,
            attempt=lease.attempt,
            owner_token=lease.owner_token,
            evidence="x" * (store_module.MAX_EVIDENCE_BYTES + 1),
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone()[0] == "EXECUTING"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_precheck_nonfinite(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        store_module._precheck_evidence_bounds({"x": value})


@pytest.mark.parametrize(
    "value",
    [object(), b"binary", {1: "bad"}, "\ud800", 1 << 300_000],
    ids=["object", "bytes", "key", "surrogate", "large-integer"],
)
def test_precheck_refuses_invalid_types_or_sizes(value: Any) -> None:
    with pytest.raises(ValueError):
        store_module._precheck_evidence_bounds(value)


def test_precheck_depth_cycle_unicode_and_json_booleans() -> None:
    store_module._precheck_evidence_bounds({"text": "aé中🙂", "yes": True, "no": False})
    recursive: list[Any] = []
    recursive.append(recursive)
    with pytest.raises(ValueError, match="depth limit"):
        store_module._precheck_evidence_bounds(recursive)
    with pytest.raises(ValueError, match="byte limit"):
        store_module._precheck_evidence_bounds([""] * 100, max_bytes=10)
    with pytest.raises(ValueError, match="byte limit"):
        store_module._precheck_evidence_bounds("é" * 10, max_bytes=15)


def test_symlink_parent_is_refused(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.mkdir()
    link = tmp_path / "link"
    link.symlink_to(original, target_is_directory=True)
    with pytest.raises(ValueError, match="parent path must not contain a symlink"):
        DurablePlanDedupeStore(link / "db")
    assert not (original / "db").exists()


def test_group_writable_parent_is_refused(tmp_path: Path) -> None:
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o700)
    parent.chmod(0o770)
    with pytest.raises(ValueError, match="not group/world writable"):
        DurablePlanDedupeStore(parent / "db")
    assert not (parent / "db").exists()


def test_precheck_accepts_finite_and_empty_containers() -> None:
    store_module._precheck_evidence_bounds({"value": 1.5, "empty": (), "absent": None})


@pytest.mark.parametrize("evidence", ["中" * 11000, ["中" * 6000, "中" * 6000]])
def test_encoder_escaped_output_cannot_exceed_limit(
    store: DurablePlanDedupeStore, evidence: Any
) -> None:
    store.reserve("p", HASH, external_effects=False)
    lease = store.start("p", HASH, nonce="n")
    with pytest.raises(ValueError, match="byte limit"):
        store.finish(
            "p", HASH, attempt=lease.attempt, owner_token=lease.owner_token, evidence=evidence
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone()[0] == "EXECUTING"


def test_live_lease_duplicate_is_refused_without_disrupting_owner(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanExecutionInProgressError

    clock = [100.0]
    path = tmp_path / "leases.db"
    first = DurablePlanDedupeStore(path, clock=lambda: clock[0])
    first.reserve("p", HASH, external_effects=True)
    lease = first.start("p", HASH, nonce="n", lease_ttl_seconds=10)
    second = DurablePlanDedupeStore(path, clock=lambda: clock[0])
    with pytest.raises(PlanExecutionInProgressError):
        second.reserve("p", HASH, external_effects=True)
    with pytest.raises(PlanExecutionInProgressError):
        second.start("p", HASH, nonce="another")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status, attempt, owner_token FROM plans").fetchone() == (
            "EXECUTING",
            lease.attempt,
            lease.owner_token,
        )
    first.finish("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token, evidence={})
    assert second.reserve("p", HASH, external_effects=True).status == "COMMITTED"


def test_expired_lease_quarantines_and_fences_owner(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanFencingTokenMismatchError

    clock = [100.0]
    path = tmp_path / "leases.db"
    store = DurablePlanDedupeStore(path, clock=lambda: clock[0])
    store.reserve("p", HASH, external_effects=True)
    lease = store.start("p", HASH, nonce="n", lease_ttl_seconds=10)
    with pytest.raises(PlanFencingTokenMismatchError):
        store.finish("p", HASH, attempt=lease.attempt, owner_token="f" * 64, evidence={})
    with pytest.raises(PlanFencingTokenMismatchError):
        store.quarantine("p", HASH, attempt=lease.attempt + 1, owner_token=lease.owner_token)
    clock[0] = 110.0
    with pytest.raises(PlanFencingTokenMismatchError):
        store.finish("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token, evidence={})
    with pytest.raises(PlanInDoubtQuarantineError):
        store.reserve("p", HASH, external_effects=True)
    with pytest.raises(PlanFencingTokenMismatchError):
        store.quarantine("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token)


@pytest.mark.parametrize("ttl", [True, 0, -1, float("nan"), float("inf"), 86401, "3"])
def test_invalid_lease_ttl_is_refused(store: DurablePlanDedupeStore, ttl: Any) -> None:
    store.reserve("p", HASH, external_effects=False)
    with pytest.raises(ValueError, match="lease_ttl_seconds"):
        store.start("p", HASH, nonce="n", lease_ttl_seconds=ttl)
    lease = store.start("p", HASH, nonce="n")
    assert lease.attempt == 1


def test_expired_duplicate_start_commits_quarantine(tmp_path: Path) -> None:
    clock = [100.0]
    store = DurablePlanDedupeStore(tmp_path / "db", clock=lambda: clock[0])
    store.reserve("p", HASH, external_effects=False)
    store.start("p", HASH, nonce="n", lease_ttl_seconds=1)
    clock[0] = 101.0
    with pytest.raises(PlanInDoubtQuarantineError):
        store.start("p", HASH, nonce="new")
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone()[0] == "IN_DOUBT"


@pytest.mark.parametrize("succeeded,status", [(True, "ROLLED_BACK"), (False, "ROLLBACK_FAILED")])
def test_compensation_outcome_persists_under_fence(
    tmp_path: Path, succeeded: bool, status: str
) -> None:
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanFencingTokenMismatchError

    path = tmp_path / "db"
    store = DurablePlanDedupeStore(path, clock=lambda: 1.0)
    store.reserve("p", HASH, external_effects=True)
    lease = store.start("p", HASH, nonce="n")
    with pytest.raises(PlanFencingTokenMismatchError):
        store.mark_compensating("p", HASH, attempt=lease.attempt, owner_token="e" * 64)
    store.mark_compensating("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token)
    with pytest.raises(PlanFencingTokenMismatchError):
        store.record_compensation(
            "p",
            HASH,
            attempt=lease.attempt + 1,
            owner_token=lease.owner_token,
            succeeded=succeeded,
            evidence={"undo": status},
        )
    store.record_compensation(
        "p",
        HASH,
        attempt=lease.attempt,
        owner_token=lease.owner_token,
        succeeded=succeeded,
        evidence={"undo": status},
    )
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT status, compensation_status, compensation_evidence_json FROM plans"
        ).fetchone()
    assert row[:2] == (status, status)
    assert json.loads(row[2]) == {"undo": status}
    with pytest.raises(PlanInDoubtQuarantineError):
        DurablePlanDedupeStore(path).reserve("p", HASH, external_effects=True)


def test_expired_compensation_quarantines(tmp_path: Path) -> None:
    clock = [1.0]
    store = DurablePlanDedupeStore(tmp_path / "db", clock=lambda: clock[0])
    store.reserve("p", HASH, external_effects=True)
    lease = store.start("p", HASH, nonce="n", lease_ttl_seconds=1)
    store.mark_compensating("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token)
    clock[0] = 2.0
    with pytest.raises(PlanInDoubtQuarantineError):
        store.reserve("p", HASH, external_effects=True)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone()[0] == "IN_DOUBT"


@pytest.mark.parametrize("status", ["IN_DOUBT", "ROLLBACK_FAILED"])
def test_repair_requires_authorization_and_persists_resolution(tmp_path: Path, status: str) -> None:
    path = tmp_path / "db"
    clock = [10.0]
    store = DurablePlanDedupeStore(path, clock=lambda: clock[0])
    store.reserve("p", HASH, external_effects=True)
    lease = store.start("p", HASH, nonce="n")
    if status == "IN_DOUBT":
        store.quarantine("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token)
    else:
        store.mark_compensating("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token)
        store.record_compensation(
            "p",
            HASH,
            attempt=lease.attempt,
            owner_token=lease.owner_token,
            succeeded=False,
            evidence={"error": "failed"},
        )
    kwargs: dict[str, Any] = {
        "operator_id": "admin",
        "resolution_action": "confirm_rolled_back",
        "reason": "checked",
        "expected_status": status,
        "expected_attempt": 1,
    }
    with pytest.raises(PlanStoreStateError, match="authorization"):
        store.repair_in_doubt("default", "p", HASH, **kwargs)
    authorized = DurablePlanDedupeStore(
        path,
        clock=lambda: clock[0],
        repair_authorizer=lambda project, operator, status, attempt: (
            project == "default"
            and operator == "admin"
            and status in ("IN_DOUBT", "ROLLBACK_FAILED")
            and attempt == 1
        ),
    )
    with pytest.raises(PlanStoreStateError, match="authorization"):
        authorized.repair_in_doubt("default", "p", HASH, **{**kwargs, "operator_id": "other"})
    evidence = authorized.inspect_recovery(
        "default", "p", HASH, expected_status=status, expected_attempt=1, operator_id="admin"
    )
    authorized.repair_in_doubt("default", "p", HASH, recovery_evidence=evidence, **kwargs)
    with sqlite3.connect(path) as conn:
        actual_status, evidence = conn.execute(
            "SELECT status, repair_evidence_json FROM plans"
        ).fetchone()
    assert actual_status == "ROLLED_BACK"
    recorded = json.loads(evidence)
    assert recorded.pop("recovery_evidence")["recovery_nonce"]
    assert recorded == {
        "operator_id": "admin",
        "action": "confirm_rolled_back",
        "reason": "checked",
        "previous_status": status,
        "previous_attempt": 1,
        "resolved_at": 10.0,
    }
    with pytest.raises(PlanStoreStateError, match="eligible"):
        authorized.repair_in_doubt("default", "p", HASH, **kwargs)
    with pytest.raises(PlanInDoubtQuarantineError):
        authorized.reserve("p", HASH, external_effects=True)


def test_upgrade_tenant_database_quarantines_legacy_executing(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE plans (project_id TEXT NOT NULL, plan_id TEXT NOT NULL, "
            "plan_hash TEXT NOT NULL, status TEXT NOT NULL, external_effects INTEGER NOT NULL, "
            "evidence_json TEXT, PRIMARY KEY(project_id, plan_id));"
            "CREATE TABLE consumed_nonces (project_id TEXT NOT NULL, nonce TEXT NOT NULL, "
            "PRIMARY KEY(project_id, nonce));"
        )
        conn.execute(
            "INSERT INTO plans VALUES (?, ?, ?, 'EXECUTING', 1, NULL)", ("default", "p", HASH)
        )
        conn.execute("INSERT INTO consumed_nonces VALUES ('default', 'old')")
    store = DurablePlanDedupeStore(path, clock=lambda: 1.0)
    with pytest.raises(PlanInDoubtQuarantineError):
        store.reserve("p", HASH, external_effects=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status, attempt, owner_token FROM plans").fetchone() == (
            "IN_DOUBT",
            0,
            None,
        )
        assert conn.execute("SELECT nonce FROM consumed_nonces").fetchone()[0] == "old"


@pytest.mark.parametrize("value", [True, None, "1", float("nan"), float("inf"), 10**400])
def test_invalid_clock_fails_closed_and_does_not_consume_nonce(tmp_path: Path, value: Any) -> None:
    path = tmp_path / "db"
    store = DurablePlanDedupeStore(path, clock=lambda: value)
    store.reserve("p", HASH, external_effects=False)
    with pytest.raises(PlanStoreStateError, match="clock"):
        store.start("p", HASH, nonce="n")
    healthy = DurablePlanDedupeStore(path, clock=lambda: 1.0)
    assert healthy.start("p", HASH, nonce="n").attempt == 1


def test_extreme_clock_and_attempt_counter_refused(tmp_path: Path) -> None:
    path = tmp_path / "db"
    store = DurablePlanDedupeStore(path, clock=lambda: 1e300)
    store.reserve("p", HASH, external_effects=False)
    with pytest.raises(ValueError, match="lease expiry"):
        store.start("p", HASH, nonce="n")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE plans SET attempt=?", (2**63 - 1,))
    with pytest.raises(PlanStoreStateError, match="counter exhausted"):
        store.start("p", HASH, nonce="n")


@pytest.mark.parametrize(
    "attempt,token",
    [(True, "a" * 64), ("1", "a" * 64), (0, "a" * 64), (2**63, "a" * 64), (1, None), (1, "x")],
)
def test_invalid_fences_fail_closed(
    store: DurablePlanDedupeStore, attempt: Any, token: Any
) -> None:
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanFencingTokenMismatchError

    store.reserve("p", HASH, external_effects=False)
    lease = store.start("p", HASH, nonce="n")
    with pytest.raises(PlanFencingTokenMismatchError):
        store.finish("p", HASH, attempt=attempt, owner_token=token, evidence={})
    store.finish("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token, evidence={})
    with pytest.raises(PlanStoreStateError):
        store.start("p", HASH, nonce="new")


def test_repair_validation_and_compensation_validation(store: DurablePlanDedupeStore) -> None:
    store.reserve("p", HASH, external_effects=True)
    lease = store.start("p", HASH, nonce="n")
    store.mark_compensating("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token)
    with pytest.raises(ValueError, match="boolean"):
        store.record_compensation(
            "p",
            HASH,
            attempt=lease.attempt,
            owner_token=lease.owner_token,
            succeeded=1,  # type: ignore[arg-type]
            evidence={},
        )
    with pytest.raises(ValueError, match="action"):
        store.repair_in_doubt(
            "default",
            "p",
            HASH,
            operator_id="op",
            expected_status="IN_DOUBT",
            expected_attempt=1,
            resolution_action="replay",
            reason="x",
        )
    for notes in ("", " ", "x" * 2049):
        with pytest.raises(ValueError, match="reason"):
            store.repair_in_doubt(
                "default",
                "p",
                HASH,
                operator_id="op",
                resolution_action="confirm_rolled_back",
                expected_status="IN_DOUBT",
                expected_attempt=1,
                reason=notes,
            )


def test_invalid_injected_dependencies(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="clock"):
        DurablePlanDedupeStore(tmp_path / "a", clock=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="repair_authorizer"):
        DurablePlanDedupeStore(tmp_path / "b", repair_authorizer=1)  # type: ignore[arg-type]


def test_competing_live_starts_issue_exactly_one_lease(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanExecutionInProgressError

    path = tmp_path / "db"
    store = DurablePlanDedupeStore(path, clock=lambda: 1.0)
    store.reserve("p", HASH, external_effects=True)

    def start(nonce: str) -> str:
        other = DurablePlanDedupeStore(path, clock=lambda: 1.0)
        try:
            return other.start("p", HASH, nonce=nonce).owner_token
        except PlanExecutionInProgressError:
            return "live"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, ["n1", "n2"]))
    assert results.count("live") == 1
    owner = next(value for value in results if value != "live")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status, attempt, owner_token FROM plans").fetchone() == (
            "EXECUTING",
            1,
            owner,
        )
        assert conn.execute("SELECT count(*) FROM consumed_nonces").fetchone()[0] == 1


def test_authorized_takeover_fences_old_worker_and_preserves_new_owner(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanFencingTokenMismatchError

    now = [100.0]
    path = tmp_path / "lease.db"
    store = DurablePlanDedupeStore(
        path,
        clock=lambda: now[0],
        repair_authorizer=lambda p, o, status, attempt: (
            p == "default"
            and o == "admin"
            and status in ("IN_DOUBT", "ROLLBACK_FAILED")
            and attempt == 1
        ),
    )
    store.reserve("p", HASH, external_effects=True)
    old = store.start("p", HASH, nonce="old", lease_ttl_seconds=5)
    now[0] = 104.0
    renewed = store.heartbeat(
        "p", HASH, attempt=old.attempt, owner_token=old.owner_token, lease_ttl_seconds=5
    )
    assert renewed.lease_expires_at == 109.0
    now[0] = 109.0
    with pytest.raises(PlanFencingTokenMismatchError):
        store.heartbeat("p", HASH, attempt=old.attempt, owner_token=old.owner_token)
    with pytest.raises(PlanInDoubtQuarantineError):
        store.reserve("p", HASH, external_effects=True)
    with pytest.raises(PlanStoreStateError, match="expected status"):
        store.repair_in_doubt(
            "default",
            "p",
            HASH,
            expected_status="ROLLBACK_FAILED",
            expected_attempt=1,
            operator_id="admin",
            resolution_action="authorize_takeover",
            reason="old worker stopped",
        )
    store.repair_in_doubt(
        "default",
        "p",
        HASH,
        expected_status="IN_DOUBT",
        expected_attempt=1,
        operator_id="admin",
        resolution_action="authorize_takeover",
        reason="old worker stopped",
        recovery_evidence=store.inspect_recovery(
            "default",
            "p",
            HASH,
            expected_status="IN_DOUBT",
            expected_attempt=1,
            operator_id="admin",
        ),
    )
    with pytest.raises(PlanStoreStateError, match="nonce already consumed"):
        store.start("p", HASH, nonce="old")
    new = store.start("p", HASH, nonce="new")
    assert new.fencing_epoch == old.fencing_epoch + 1
    assert new.owner_token != old.owner_token
    with sqlite3.connect(path) as conn:
        before = conn.execute("SELECT * FROM plans").fetchone()
    for action in (
        lambda: store.finish(
            "p", HASH, attempt=old.attempt, owner_token=old.owner_token, evidence={}
        ),
        lambda: store.heartbeat("p", HASH, attempt=old.attempt, owner_token=old.owner_token),
        lambda: store.mark_compensating(
            "p", HASH, attempt=old.attempt, owner_token=old.owner_token
        ),
        lambda: store.quarantine("p", HASH, attempt=old.attempt, owner_token=old.owner_token),
        lambda: store.record_compensation(
            "p", HASH, attempt=old.attempt, owner_token=old.owner_token, succeeded=True, evidence={}
        ),
    ):
        with pytest.raises(PlanFencingTokenMismatchError):
            action()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM plans").fetchone() == before
        event = conn.execute("SELECT evidence_json FROM plan_repair_events").fetchone()[0]
        assert json.loads(event)["previous_attempt"] == old.attempt
    store.finish(
        "p", HASH, attempt=new.attempt, owner_token=new.owner_token, evidence={"new": True}
    )
    assert store.reserve("p", HASH, external_effects=True).status == "COMMITTED"


def test_full_legacy_migration_preserves_all_rows_and_nonces(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old_rows = [
        ("done", HASH, "COMMITTED", 1, '{"legacy":true}'),
        ("pending", OTHER, "RESERVED", 0, None),
        ("running", HASH, "EXECUTING", 1, None),
    ]
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE plans (plan_id TEXT PRIMARY KEY, plan_hash TEXT NOT NULL, "
            "status TEXT NOT NULL, external_effects INTEGER NOT NULL, evidence_json TEXT)"
        )
        conn.execute("CREATE TABLE consumed_nonces (nonce TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO plans VALUES (?, ?, ?, ?, ?)", old_rows)
        conn.executemany("INSERT INTO consumed_nonces VALUES (?)", [("n1",), ("n2",)])
    DurablePlanDedupeStore(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT plan_id, plan_hash, status, external_effects, evidence_json "
            "FROM plans ORDER BY plan_id"
        ).fetchall() == sorted(old_rows)
        assert conn.execute(
            "SELECT project_id, nonce FROM consumed_nonces ORDER BY nonce"
        ).fetchall() == [("default", "n1"), ("default", "n2")]
        assert conn.execute(
            "SELECT DISTINCT project_id, attempt, owner_token, lease_expires_at FROM plans"
        ).fetchall() == [("default", 0, None, None)]


def test_repair_requires_valid_expected_status_and_bounded_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "db"
    store = DurablePlanDedupeStore(
        path, clock=lambda: 1.0, repair_authorizer=lambda _p, _o, _status, _attempt: True
    )
    store.reserve("p", HASH, external_effects=True)
    lease = store.start("p", HASH, nonce="n")
    store.quarantine("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token)
    with pytest.raises(ValueError, match="expected status"):
        store.repair_in_doubt(
            "default",
            "p",
            HASH,
            expected_status="EXECUTING",
            expected_attempt=1,
            operator_id="admin",
            resolution_action="authorize_takeover",
            reason="investigated",
        )
    evidence = store.inspect_recovery(
        "default", "p", HASH, expected_status="IN_DOUBT", expected_attempt=1, operator_id="admin"
    )
    monkeypatch.setattr(store_module, "MAX_RECORDS", 0)
    with pytest.raises(PlanStoreCapacityError, match="audit capacity"):
        store.repair_in_doubt(
            "default",
            "p",
            HASH,
            expected_status="IN_DOUBT",
            expected_attempt=1,
            operator_id="admin",
            resolution_action="authorize_takeover",
            reason="investigated",
            recovery_evidence=evidence,
        )
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone()[0] == "IN_DOUBT"
        assert conn.execute("SELECT count(*) FROM plan_repair_events").fetchone()[0] == 0


def _integrated_bus(path: Path, calls: list[str], *, fail: bool = False) -> tuple[Any, Any]:
    """SQLite is real; identity and effect authorities are test doubles."""
    from nexus_ai_agent.creative.studio.authorization import ProjectAccess
    from nexus_ai_agent.creative.studio.bus import CommandBus
    from nexus_ai_agent.creative.studio.capabilities import (
        CapabilityRegistry,
        OperationContext,
        OperationOutcome,
        OperationSpec,
        PlayCommandInput,
    )
    from nexus_ai_agent.creative.studio.models import (
        ActorIdentity,
        PermissionLevel,
        Project,
        Timeline,
        new_project,
    )

    actor = ActorIdentity(kind="user", actor_id="alice")
    project = new_project("project", "demo", Timeline(timeline_id="timeline", duration_us=0))
    registry = CapabilityRegistry()

    def handler(state: Project, context: OperationContext) -> OperationOutcome:
        assert context.effect_key is not None and context.fencing_epoch is not None
        assert context.durable_heartbeat is not None
        context.durable_heartbeat()
        calls.append(context.effect_key)
        if fail:
            raise RuntimeError("effect failed")
        state.name = "changed"
        return OperationOutcome(state, context.history, {"ok": True})

    registry.register_operation(
        "example",
        "playback",
        OperationSpec("example.play", "test", PermissionLevel.IMMEDIATE, PlayCommandInput, handler),
    )
    access = ProjectAccess(actor, "project", frozenset({"project:read"}))
    return CommandBus(
        project,
        registry=registry,
        authorizer=access,
        durable_store=DurablePlanDedupeStore(path),
        durable_verifiers={"example.play": lambda _project, _command, _result, _key: True},
    ), actor


def _integrated_plan(bus: Any, actor: Any, nonce: str, *, plan_id: str = "plan") -> tuple[Any, Any]:
    from dataclasses import asdict
    from datetime import datetime, timezone

    from nexus_ai_agent.creative.studio.bus import PlanStep, PlanTransaction
    from nexus_ai_agent.creative.studio.durable_dedupe import ApprovalBinding
    from nexus_ai_agent.creative.studio.models import TypedCommand

    command = TypedCommand(
        command_id="cmd", operation="example.play", actor=actor, idempotency_key="key"
    )
    plan = PlanTransaction(plan_id=plan_id, steps=(PlanStep("example.play", command=command),))
    (scope,) = bus.durable_approval_requirements(
        plan, policy_version="v1", authenticated_actor=actor
    )
    now = datetime.now(timezone.utc).timestamp()
    approval = ApprovalBinding(
        **asdict(scope),
        approval_id="approval",
        nonce=nonce,
        issued_at=now - 10,
        expires_at=now + 300,
    )
    return plan, approval


class _MockApproval:
    def verify(self, binding: Any) -> bool:
        return True


def _run_integrated(bus: Any, plan: Any, actor: Any, approval: Any) -> Any:
    import time

    return bus.dispatch_durable_plan(
        plan,
        (approval,),
        policy_version="v1",
        authenticated_actor=actor,
        approval_verifier=_MockApproval(),
        deadline_monotonic_ns=time.monotonic_ns() + 10**10,
    )


def test_bus_durable_verified_commit_and_rehydrate_on_restart(tmp_path: Path) -> None:
    path = tmp_path / "db"
    calls: list[str] = []
    first, actor = _integrated_bus(path, calls)
    plan, approval = _integrated_plan(first, actor, "nonce-1")
    summary = _run_integrated(first, plan, actor, approval)
    assert first.project.name == "changed" and first.state_revision == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone() == ("COMMITTED",)
        assert conn.execute("SELECT status, effect_key FROM compensation_journal").fetchone() == (
            "VERIFIED",
            calls[0],
        )
    second, actor2 = _integrated_bus(path, calls)
    same, approval2 = _integrated_plan(second, actor2, "nonce-2")
    replay = _run_integrated(second, same, actor2, approval2)
    assert replay == summary and len(calls) == 1
    assert second.project == first.project and second.history == first.history


def test_bus_durable_pending_failure_is_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "db"
    calls: list[str] = []
    bus, actor = _integrated_bus(path, calls, fail=True)
    plan, approval = _integrated_plan(bus, actor, "nonce-1")
    with pytest.raises(Exception, match="effect failed"):
        _run_integrated(bus, plan, actor, approval)
    assert bus.project.name == "demo" and bus.state_revision == 0
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status, evidence_json FROM plans").fetchone() == (
            "IN_DOUBT",
            None,
        )
        assert conn.execute("SELECT status FROM compensation_journal").fetchone() == ("PENDING",)
    second, actor2 = _integrated_bus(path, calls)
    same, approval2 = _integrated_plan(second, actor2, "nonce-2")
    with pytest.raises(PlanInDoubtQuarantineError):
        _run_integrated(second, same, actor2, approval2)
    assert len(calls) == 1


def test_bus_durable_rejects_nonce_reuse_for_other_plan(tmp_path: Path) -> None:
    path = tmp_path / "db"
    calls: list[str] = []
    first, actor = _integrated_bus(path, calls)
    plan, approval = _integrated_plan(first, actor, "nonce")
    _run_integrated(first, plan, actor, approval)
    second, actor2 = _integrated_bus(path, calls)
    other, reused = _integrated_plan(second, actor2, "nonce", plan_id="other")
    with pytest.raises(PlanStoreStateError, match="nonce already consumed"):
        _run_integrated(second, other, actor2, reused)
    assert len(calls) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM plans WHERE plan_id='other'").fetchone() == (0,)


def test_bus_durable_receipt_survives_process_restart(tmp_path: Path) -> None:
    import subprocess

    path = tmp_path / "db"
    child = """
import json, runpy, sys
m = runpy.run_path("tests/unit/test_w4_durable_bus_and_dedupe.py")
from pathlib import Path
calls = []
bus, actor = m["_integrated_bus"](Path(sys.argv[1]), calls)
plan, approval = m["_integrated_plan"](bus, actor, "child-nonce")
m["_run_integrated"](bus, plan, actor, approval)
print(json.dumps(bus.project.model_dump(mode="json")))
"""
    proc = subprocess.run(
        [sys.executable, "-c", child, str(path)], check=True, capture_output=True, text=True
    )
    expected_project = json.loads(proc.stdout)
    calls: list[str] = []
    bus, actor = _integrated_bus(path, calls)
    plan, approval = _integrated_plan(bus, actor, "parent-nonce")
    _run_integrated(bus, plan, actor, approval)
    assert bus.project.model_dump(mode="json") == expected_project
    assert not calls and bus.history[-1].operation == "example.play"


def test_bus_durable_crash_with_pending_intent_is_not_reexecuted(tmp_path: Path) -> None:
    import subprocess
    import time

    path = tmp_path / "db"
    child = """
import os, runpy, sys
m = runpy.run_path("tests/unit/test_w4_durable_bus_and_dedupe.py")
from pathlib import Path
bus, actor = m["_integrated_bus"](Path(sys.argv[1]), [])
bus._durable_store.verify_effect_intent = lambda *a, **k: os._exit(23)
plan, approval = m["_integrated_plan"](bus, actor, "crash-nonce")
m["_run_integrated"](bus, plan, actor, approval)
"""
    proc = subprocess.run([sys.executable, "-c", child, str(path)], check=False)
    assert proc.returncode == 23
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM compensation_journal").fetchone() == ("PENDING",)
        assert conn.execute("SELECT status FROM plans").fetchone() == ("EXECUTING",)
    calls: list[str] = []
    bus, actor = _integrated_bus(path, calls)
    bus._durable_store._clock = lambda: time.monotonic_ns() / 1e9 + 60
    plan, approval = _integrated_plan(bus, actor, "fresh-nonce")
    with pytest.raises(PlanInDoubtQuarantineError):
        _run_integrated(bus, plan, actor, approval)
    assert not calls
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone() == ("IN_DOUBT",)


def test_bus_durable_replay_rejects_corrupted_project_snapshot(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.studio.bus import _canonical_hash

    path = tmp_path / "db"
    calls: list[str] = []
    first, actor = _integrated_bus(path, calls)
    plan, approval = _integrated_plan(first, actor, "nonce-1")
    _run_integrated(first, plan, actor, approval)
    with sqlite3.connect(path) as conn:
        raw = conn.execute("SELECT evidence_json FROM plans").fetchone()[0]
        receipt = json.loads(raw)
        receipt["payload"]["project"]["name"] = "tampered"
        receipt["digest"] = _canonical_hash(receipt["payload"])
        conn.execute("UPDATE plans SET evidence_json=?", (json.dumps(receipt),))
    second, actor2 = _integrated_bus(path, calls)
    same, approval2 = _integrated_plan(second, actor2, "nonce-2")
    with pytest.raises(PlanStoreStateError, match="invalid durable receipt"):
        _run_integrated(second, same, actor2, approval2)
    assert second.state_revision == 0 and second.project.name == "demo" and len(calls) == 1


def test_bus_durable_compensates_verified_steps_in_reverse_order(tmp_path: Path) -> None:
    import time
    from dataclasses import asdict
    from datetime import datetime, timezone

    from nexus_ai_agent.creative.studio.bus import PlanStep, PlanTransaction, PlanTransactionState
    from nexus_ai_agent.creative.studio.durable_dedupe import ApprovalBinding, ApprovalBindingError
    from nexus_ai_agent.creative.studio.models import TypedCommand

    class RejectThirdStep:
        calls = 0

        def verify(self, _binding: Any) -> bool:
            self.calls += 1
            # Three preflight approvals, then two boundary approvals succeed.
            return self.calls != 6

    effects: list[str] = []
    compensations: list[list[int]] = [[], [], []]
    bus, actor = _integrated_bus(tmp_path / "db", effects)
    order: list[str] = []
    original_record = bus._durable_store.record_compensation_step

    def record(*args: Any, **kwargs: Any) -> None:
        order.append(kwargs["step_id"])
        original_record(*args, **kwargs)

    bus._durable_store.record_compensation_step = record

    def make_compensator(target: list[int]) -> Any:
        def compensate(result: Any) -> None:
            target.append(result.state_revision)

        return compensate

    steps = tuple(
        PlanStep(
            "example.play",
            command=TypedCommand(
                command_id=f"cmd-{i}",
                operation="example.play",
                actor=actor,
                idempotency_key=f"key-{i}",
            ),
            compensate=make_compensator(compensations[i]),
        )
        for i in range(3)
    )
    plan = PlanTransaction(plan_id="multi", steps=steps)
    scopes = bus.durable_approval_requirements(plan, policy_version="v1", authenticated_actor=actor)
    now = datetime.now(timezone.utc).timestamp()
    bindings = tuple(
        ApprovalBinding(
            **asdict(scope),
            approval_id=f"a-{i}",
            nonce=f"n-{i}",
            issued_at=now - 10,
            expires_at=now + 300,
        )
        for i, scope in enumerate(scopes)
    )
    with pytest.raises(ApprovalBindingError):
        bus.dispatch_durable_plan(
            plan,
            bindings,
            policy_version="v1",
            authenticated_actor=actor,
            approval_verifier=RejectThirdStep(),
            deadline_monotonic_ns=time.monotonic_ns() + 10**10,
        )
    assert compensations == [[1], [2], []] and order == ["step-1", "step-0"] and len(effects) == 2
    assert bus.state_revision == 0 and plan.state is PlanTransactionState.ROLLED_BACK
    with sqlite3.connect(tmp_path / "db") as conn:
        assert conn.execute("SELECT status FROM plans").fetchone() == ("ROLLED_BACK",)
        assert conn.execute(
            "SELECT status FROM compensation_journal ORDER BY entry_id"
        ).fetchall() == [("COMPENSATED",), ("COMPENSATED",)]


def test_bus_durable_operator_repair_requires_permissions_and_attempt(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.studio.authorization import ProjectAccess
    from nexus_ai_agent.creative.studio.models import AuthorizationError

    path = tmp_path / "db"
    bus, actor = _integrated_bus(path, [], fail=True)
    plan, approval = _integrated_plan(bus, actor, "nonce")
    with pytest.raises(Exception, match="effect failed"):
        _run_integrated(bus, plan, actor, approval)
    plan_hash = approval.plan_hash
    kwargs = dict(
        authenticated_operator=actor,
        expected_status="IN_DOUBT",
        expected_attempt=1,
        resolution_action="authorize_takeover",
        reason="worker stopped",
    )
    with pytest.raises(AuthorizationError):
        bus.recover_durable_plan("plan", plan_hash, **kwargs)
    bus._authorizer = ProjectAccess(actor, "project", frozenset({"project:read", "project:repair"}))
    # MOCK: the store repair authorizer stands in for independent operator authentication.
    bus._durable_store._repair_authorizer = lambda _p, _o, _s, _a: True
    with pytest.raises(PlanStoreStateError, match="expected status/attempt"):
        bus.recover_durable_plan("plan", plan_hash, **{**kwargs, "expected_attempt": 2})
    with pytest.raises(PlanStoreStateError, match="evidence is required"):
        bus.recover_durable_plan("plan", plan_hash, **kwargs)
    with pytest.raises(PlanStoreStateError, match="destination is unavailable"):
        bus.inspect_durable_recovery(
            "plan",
            plan_hash,
            authenticated_operator=actor,
            expected_status="IN_DOUBT",
            expected_attempt=1,
        )
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status, attempt, takeover_ready FROM plans").fetchone() == (
            "IN_DOUBT",
            1,
            0,
        )


def test_bus_external_effect_is_independently_checked_from_disk(tmp_path: Path) -> None:
    """Real file I/O; the destination's fencing capability is simulated, not production proof."""
    import hashlib
    import time
    from dataclasses import asdict
    from datetime import datetime, timezone

    from nexus_ai_agent.creative.studio.authorization import ProjectAccess
    from nexus_ai_agent.creative.studio.bus import CommandBus, PlanStep, PlanTransaction
    from nexus_ai_agent.creative.studio.capabilities import (
        CapabilityRegistry,
        OperationContext,
        OperationOutcome,
        OperationSpec,
        PlayCommandInput,
    )
    from nexus_ai_agent.creative.studio.durable_dedupe import ApprovalBinding
    from nexus_ai_agent.creative.studio.models import (
        ActorIdentity,
        PermissionLevel,
        Project,
        Timeline,
        TypedCommand,
        new_project,
    )

    path = tmp_path / "db"
    actor = ActorIdentity(kind="user", actor_id="alice")
    access = ProjectAccess(actor, "project", frozenset({"project:read"}))
    registry = CapabilityRegistry()
    calls: list[str] = []

    def handler(state: Project, context: OperationContext) -> OperationOutcome:
        assert context.effect_key is not None and context.durable_heartbeat is not None
        context.durable_heartbeat()
        target = tmp_path / context.effect_key
        fd = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        try:
            os.write(fd, b"verified file effect")
        finally:
            os.close(fd)
        calls.append(context.effect_key)
        return OperationOutcome(
            state, context.history, {"digest": hashlib.sha256(b"verified file effect").hexdigest()}
        )

    def independent(_project: Any, _command: Any, result: Any, key: str) -> bool:
        fd = os.open(tmp_path / key, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            return (
                stat.S_ISREG(os.fstat(fd).st_mode)
                and hashlib.sha256(os.read(fd, 1024)).hexdigest() == result.output["digest"]
            )
        finally:
            os.close(fd)

    def compensate(result: Any, root: str = str(tmp_path)) -> None:
        __import__("os").unlink(f"{root}/{result.diagnostics['durable_effect_key']}")

    registry.register_operation(
        "example",
        "playback",
        OperationSpec(
            "example.play",
            "test",
            PermissionLevel.IMMEDIATE,
            PlayCommandInput,
            handler,
            external_effects=True,
            durable_effects=True,
        ),
    )

    def create(nonce: str) -> tuple[Any, Any, Any]:
        project = new_project("project", "demo", Timeline(timeline_id="timeline", duration_us=0))
        bus = CommandBus(
            project,
            registry=registry,
            authorizer=access,
            durable_store=DurablePlanDedupeStore(path),
            durable_verifiers={"example.play": independent},
        )
        command = TypedCommand(
            command_id="cmd", operation="example.play", actor=actor, idempotency_key="key"
        )
        plan = PlanTransaction(
            plan_id="external",
            steps=(PlanStep("example.play", command=command, compensate=compensate),),
        )
        (scope,) = bus.durable_approval_requirements(
            plan, policy_version="v1", authenticated_actor=actor
        )
        now = datetime.now(timezone.utc).timestamp()
        approval = ApprovalBinding(
            **asdict(scope), approval_id="a", nonce=nonce, issued_at=now - 1, expires_at=now + 60
        )
        return bus, plan, approval

    first, plan, approval = create("n1")
    summary = first.dispatch_durable_plan(
        plan,
        (approval,),
        policy_version="v1",
        authenticated_actor=actor,
        approval_verifier=_MockApproval(),
        deadline_monotonic_ns=time.monotonic_ns() + 10**10,
    )
    assert len(calls) == 1 and (tmp_path / calls[0]).read_bytes() == b"verified file effect"
    second, plan2, approval2 = create("n2")
    replay = second.dispatch_durable_plan(
        plan2,
        (approval2,),
        policy_version="v1",
        authenticated_actor=actor,
        approval_verifier=_MockApproval(),
        deadline_monotonic_ns=time.monotonic_ns() + 10**10,
    )
    assert summary == replay and second.project == first.project and len(calls) == 1


def test_bus_durable_rejects_verifier_failure_without_committing(tmp_path: Path) -> None:
    path = tmp_path / "db"
    calls: list[str] = []
    bus, actor = _integrated_bus(path, calls)
    bus._durable_verifiers["example.play"] = lambda *_args: False
    plan, approval = _integrated_plan(bus, actor, "nonce")
    with pytest.raises(Exception, match="independent effect verification failed"):
        _run_integrated(bus, plan, actor, approval)
    assert calls and bus.state_revision == 0
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone() == ("IN_DOUBT",)
        assert conn.execute("SELECT status FROM compensation_journal").fetchone() == ("PENDING",)


def test_bus_durable_control_exception_cleans_up_and_reraises(tmp_path: Path) -> None:
    path = tmp_path / "db"
    bus, actor = _integrated_bus(path, [])

    def interrupted(*_args: Any) -> bool:
        raise KeyboardInterrupt()

    bus._durable_verifiers["example.play"] = interrupted
    plan, approval = _integrated_plan(bus, actor, "nonce")
    with pytest.raises(KeyboardInterrupt):
        _run_integrated(bus, plan, actor, approval)
    assert bus.state_revision == 0 and not bus._executing and bus._durable_context == {}
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone() == ("IN_DOUBT",)


@pytest.mark.parametrize("deadline", [True, 0, -1, "invalid"])
def test_bus_durable_invalid_deadline_never_reserves(tmp_path: Path, deadline: Any) -> None:
    bus, actor = _integrated_bus(tmp_path / "db", [])
    plan, approval = _integrated_plan(bus, actor, "nonce")
    with pytest.raises((ValueError, TimeoutError)):
        bus.dispatch_durable_plan(
            plan,
            (approval,),
            policy_version="v1",
            authenticated_actor=actor,
            approval_verifier=_MockApproval(),
            deadline_monotonic_ns=deadline,
        )
    with sqlite3.connect(tmp_path / "db") as conn:
        assert conn.execute("SELECT count(*) FROM plans").fetchone() == (0,)


def test_bus_durable_requires_independent_effect_verifier(tmp_path: Path) -> None:
    bus, actor = _integrated_bus(tmp_path / "db", [])
    plan, approval = _integrated_plan(bus, actor, "nonce")
    bus._durable_verifiers.clear()
    with pytest.raises(Exception, match="no independent durable verifier"):
        _run_integrated(bus, plan, actor, approval)
    with sqlite3.connect(tmp_path / "db") as conn:
        assert conn.execute("SELECT count(*) FROM plans").fetchone() == (0,)


def test_clock_domain_migration_quarantines_old_leases_and_fences_old_store(tmp_path: Path) -> None:
    path = tmp_path / "db"
    old = DurablePlanDedupeStore(path, clock=lambda: 100.0)
    lease = old.reserve_and_start("p", HASH, external_effects=True, nonce="n")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE plan_clock_domain")
    DurablePlanDedupeStore(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone() == ("IN_DOUBT",)
    with pytest.raises(PlanStoreStateError, match="clock domain changed"):
        old.finish("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token, evidence={})


def test_default_lease_clock_is_monotonic_and_explicit_deadline_is_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    path = tmp_path / "db"
    store = DurablePlanDedupeStore(path)
    monkeypatch.setattr(time, "time", lambda: 10**12)
    lease = store.reserve_and_start("p", HASH, external_effects=False, nonce="n")
    assert lease.lease_expires_at < 10**12
    with pytest.raises(TimeoutError):
        store.finish(
            "p",
            HASH,
            attempt=lease.attempt,
            owner_token=lease.owner_token,
            evidence={},
            deadline_monotonic_ns=time.monotonic_ns() - 1,
        )
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status, evidence_json FROM plans").fetchone() == (
            "EXECUTING",
            None,
        )
    with pytest.raises(ValueError, match="integer"):
        store.finish(
            "p",
            HASH,
            attempt=lease.attempt,
            owner_token=lease.owner_token,
            evidence={},
            deadline_monotonic_ns=True,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "digest",
        "version",
        "plan_id",
        "before_revision",
        "project_id",
        "project_hash",
        "project_revision",
        "history_count",
        "history_operation",
        "history_previous_hash",
        "history_new_hash",
        "step_count",
        "step_status",
        "step_input",
        "reservation_count",
        "reservation_key",
        "reservation_fingerprint",
        "reservation_result",
        "invalid_json",
        "nonfinite",
        "duplicate_keys",
        "oversized",
    ],
)
def test_bus_durable_receipt_validation_fails_closed(tmp_path: Path, mutation: str) -> None:
    from nexus_ai_agent.creative.studio.bus import _durable_hash

    path = tmp_path / "db"
    calls: list[str] = []
    first, actor = _integrated_bus(path, calls)
    plan, approval = _integrated_plan(first, actor, "nonce-1")
    _run_integrated(first, plan, actor, approval)
    with sqlite3.connect(path) as conn:
        receipt = json.loads(conn.execute("SELECT evidence_json FROM plans").fetchone()[0])
        payload = receipt["payload"]
        if mutation == "version":
            payload["version"] = True
        elif mutation == "plan_id":
            payload["plan_id"] = "different"
        elif mutation == "before_revision":
            payload["before_revision"] = True
        elif mutation == "project_id":
            payload["project"]["project_id"] = "different"
        elif mutation == "project_hash":
            payload["project"]["state_hash"] = "invalid"
        elif mutation == "project_revision":
            payload["project"]["state_revision"] = True
        elif mutation == "history_count":
            payload["history"] = []
        elif mutation == "history_operation":
            payload["history"][0]["operation"] = "different"
        elif mutation == "history_previous_hash":
            payload["history"][0]["previous_state_hash"] = "invalid"
        elif mutation == "history_new_hash":
            payload["history"][0]["new_state_hash"] = "invalid"
        elif mutation == "step_count":
            payload["steps"] = []
        elif mutation == "step_status":
            payload["steps"][0]["status"] = "failed"
        elif mutation == "step_input":
            payload["steps"][0]["input_hash"] = "invalid"
        elif mutation == "reservation_count":
            payload["idempotency"] = []
        elif mutation == "reservation_key":
            payload["idempotency"][0]["key"] = ["different", "x", "y"]
        elif mutation == "reservation_fingerprint":
            payload["idempotency"][0]["fingerprint"] = "invalid"
        elif mutation == "reservation_result":
            payload["idempotency"][0]["result"]["transaction_id"] = "invalid"
        receipt["digest"] = _durable_hash(payload)
        if mutation == "digest":
            receipt["digest"] = "invalid"
        raw = json.dumps(receipt)
        if mutation == "invalid_json":
            raw = "{"
        elif mutation == "nonfinite":
            raw = '{"payload":NaN,"digest":"invalid"}'
        elif mutation == "duplicate_keys":
            raw = '{"payload":{},"payload":{},"digest":"invalid"}'
        elif mutation == "oversized":
            raw = " " * 65537
        conn.execute("UPDATE plans SET evidence_json=?", (raw,))
    second, actor2 = _integrated_bus(path, calls)
    same, approval2 = _integrated_plan(second, actor2, "nonce-2")
    with pytest.raises(PlanStoreStateError, match="invalid durable receipt"):
        _run_integrated(second, same, actor2, approval2)
    assert second.state_revision == 0 and second.project.name == "demo" and len(calls) == 1


def test_bus_durable_multi_nonce_conflict_rolls_back_reservation(tmp_path: Path) -> None:
    import time
    from dataclasses import asdict
    from datetime import datetime, timezone

    from nexus_ai_agent.creative.studio.bus import PlanStep, PlanTransaction
    from nexus_ai_agent.creative.studio.durable_dedupe import ApprovalBinding
    from nexus_ai_agent.creative.studio.models import TypedCommand

    path = tmp_path / "db"
    calls: list[str] = []
    first, actor = _integrated_bus(path, calls)
    plan, approval = _integrated_plan(first, actor, "used")
    _run_integrated(first, plan, actor, approval)
    bus, actor = _integrated_bus(path, calls)
    steps = tuple(
        PlanStep(
            "example.play",
            command=TypedCommand(
                command_id=f"c{i}", operation="example.play", actor=actor, idempotency_key=f"k{i}"
            ),
        )
        for i in range(2)
    )
    other = PlanTransaction(plan_id="other", steps=steps)
    scopes = bus.durable_approval_requirements(
        other, policy_version="v1", authenticated_actor=actor
    )
    now = datetime.now(timezone.utc).timestamp()
    approvals = tuple(
        ApprovalBinding(
            **asdict(scope),
            approval_id=f"a{i}",
            nonce=nonce,
            issued_at=now - 1,
            expires_at=now + 60,
        )
        for i, (scope, nonce) in enumerate(zip(scopes, ("fresh", "used"), strict=True))
    )
    with pytest.raises(PlanStoreStateError, match="nonce already consumed"):
        bus.dispatch_durable_plan(
            other,
            approvals,
            policy_version="v1",
            authenticated_actor=actor,
            approval_verifier=_MockApproval(),
            deadline_monotonic_ns=time.monotonic_ns() + 10**10,
        )
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM plans WHERE plan_id='other'").fetchone() == (0,)
        assert conn.execute("SELECT nonce FROM consumed_nonces").fetchall() == [("used",)]
    assert len(calls) == 1


def test_bus_durable_two_steps_rehydrate_history_and_reservations(tmp_path: Path) -> None:
    import time
    from dataclasses import asdict
    from datetime import datetime, timezone

    from nexus_ai_agent.creative.studio.bus import PlanStep, PlanTransaction
    from nexus_ai_agent.creative.studio.durable_dedupe import ApprovalBinding
    from nexus_ai_agent.creative.studio.models import TypedCommand

    path = tmp_path / "db"
    calls: list[str] = []

    def prepare(nonce_prefix: str) -> tuple[Any, Any, Any, Any]:
        bus, actor = _integrated_bus(path, calls)
        steps = tuple(
            PlanStep(
                "example.play",
                command=TypedCommand(
                    command_id=f"command-{i}",
                    operation="example.play",
                    actor=actor,
                    idempotency_key=f"key-{i}",
                ),
            )
            for i in range(2)
        )
        plan = PlanTransaction(plan_id="multi-success", steps=steps)
        scopes = bus.durable_approval_requirements(
            plan, policy_version="v1", authenticated_actor=actor
        )
        now = datetime.now(timezone.utc).timestamp()
        approvals = tuple(
            ApprovalBinding(
                **asdict(scope),
                approval_id=f"a{i}",
                nonce=f"{nonce_prefix}-{i}",
                issued_at=now - 1,
                expires_at=now + 60,
            )
            for i, scope in enumerate(scopes)
        )
        return bus, actor, plan, approvals

    first, actor, plan, approvals = prepare("n1")
    result = first.dispatch_durable_plan(
        plan,
        approvals,
        policy_version="v1",
        authenticated_actor=actor,
        approval_verifier=_MockApproval(),
        deadline_monotonic_ns=time.monotonic_ns() + 10**10,
    )
    second, actor2, plan2, approvals2 = prepare("n2")
    replay = second.dispatch_durable_plan(
        plan2,
        approvals2,
        policy_version="v1",
        authenticated_actor=actor2,
        approval_verifier=_MockApproval(),
        deadline_monotonic_ns=time.monotonic_ns() + 10**10,
    )
    assert result == replay and first.project == second.project and first.history == second.history
    assert (
        first.state_revision == 2 and len(calls) == 2 and first._idempotency == second._idempotency
    )


def test_clock_domain_switch_fences_old_live_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "db"
    monkeypatch.setattr(store_module, "_monotonic_clock_domain", lambda: "boot-a")
    old = DurablePlanDedupeStore(path)
    lease = old.reserve_and_start("p", HASH, external_effects=True, nonce="n")
    monkeypatch.setattr(store_module, "_monotonic_clock_domain", lambda: "boot-b")
    new = DurablePlanDedupeStore(path)
    with pytest.raises(PlanStoreStateError, match="clock domain changed"):
        old.finish("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token, evidence={})
    with pytest.raises(PlanInDoubtQuarantineError):
        new.reserve("p", HASH, external_effects=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone() == ("IN_DOUBT",)


def test_clock_domain_metadata_rejects_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "db"
    DurablePlanDedupeStore(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO plan_clock_domain VALUES ('duplicate')")
    with pytest.raises(PlanStoreStateError, match="invalid lease clock domain metadata"):
        DurablePlanDedupeStore(path)


@pytest.mark.parametrize("payload", [b"bad-id", b"\xff"])
def test_boot_identity_is_checked_before_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    target = tmp_path / "boot-id"
    target.write_bytes(payload)
    original_open = os.open

    def fake_open(path: Any, flags: int, *args: Any) -> int:
        if path == "/proc/sys/kernel/random/boot_id":
            return original_open(target, flags)
        return original_open(path, flags, *args)

    monkeypatch.setattr(os, "open", fake_open)
    with pytest.raises(PlanStoreStateError, match="kernel boot identity"):
        store_module._monotonic_clock_domain()


def test_missing_proc_uses_process_scoped_clock_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_path: Any, _flags: int, *_args: Any) -> int:
        raise FileNotFoundError()

    monkeypatch.setattr(os, "open", missing)
    assert store_module._monotonic_clock_domain().startswith("process-")


@pytest.mark.parametrize(
    "scenario", ["missing_store", "missing_key", "duplicate_key", "unfenced_external"]
)
def test_bus_durable_preflight_refuses_unsafe_execution(tmp_path: Path, scenario: str) -> None:
    import time
    from dataclasses import asdict, replace
    from datetime import datetime, timezone

    from nexus_ai_agent.creative.studio.bus import PlanStep, PlanTransaction, PlanTransactionError
    from nexus_ai_agent.creative.studio.durable_dedupe import ApprovalBinding
    from nexus_ai_agent.creative.studio.models import CommandValidationError, TypedCommand

    path = tmp_path / "db"
    calls: list[str] = []
    bus, actor = _integrated_bus(path, calls)
    approvals: tuple[Any, ...]
    if scenario == "missing_store":
        plan, binding = _integrated_plan(bus, actor, "nonce")
        bus._durable_store = None
        approvals = (binding,)
    else:
        if scenario == "unfenced_external":
            spec = bus.registry.get_spec("example.play")
            bus.registry._index["example.play"] = replace(
                spec, external_effects=True, durable_effects=False
            )
        count = 2 if scenario == "duplicate_key" else 1
        steps = tuple(
            PlanStep(
                "example.play",
                command=TypedCommand(
                    command_id=f"c{i}",
                    operation="example.play",
                    actor=actor,
                    idempotency_key=None if scenario == "missing_key" else "same",
                ),
                compensate=(lambda _result: None) if scenario == "unfenced_external" else None,
            )
            for i in range(count)
        )
        plan = PlanTransaction(plan_id="preflight", steps=steps)
        scopes = bus.durable_approval_requirements(
            plan, policy_version="v1", authenticated_actor=actor
        )
        now = datetime.now(timezone.utc).timestamp()
        approvals = tuple(
            ApprovalBinding(
                **asdict(scope),
                approval_id=f"a{i}",
                nonce=f"n{i}",
                issued_at=now - 1,
                expires_at=now + 60,
            )
            for i, scope in enumerate(scopes)
        )
    with pytest.raises((PlanTransactionError, CommandValidationError)):
        bus.dispatch_durable_plan(
            plan,
            approvals,
            policy_version="v1",
            authenticated_actor=actor,
            approval_verifier=_MockApproval(),
            deadline_monotonic_ns=time.monotonic_ns() + 10**10,
        )
    assert calls == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM plans").fetchone() == (0,)
