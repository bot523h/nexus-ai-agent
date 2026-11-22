"""Real-disk tests for the provisional W4 plan reservation store."""

import json
import os
import sqlite3
import stat
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
    first.start("p", HASH, nonce="n")
    first.finish("p", HASH, evidence={"z": 2, "a": 1})
    replay = DurablePlanDedupeStore(path).reserve("p", HASH, external_effects=True)
    assert replay.status == "COMMITTED"
    assert replay.evidence_json is not None
    assert json.loads(replay.evidence_json) == {"a": 1, "z": 2}
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_in_doubt_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "db"
    first = DurablePlanDedupeStore(path)
    first.reserve("p", HASH, external_effects=True)
    first.start("p", HASH, nonce="n")
    for _ in range(2):
        with pytest.raises(PlanInDoubtQuarantineError):
            DurablePlanDedupeStore(path).reserve("p", HASH, external_effects=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone()[0] == "IN_DOUBT"
    with pytest.raises(PlanStoreStateError):
        first.finish("p", HASH, evidence={})


def test_explicit_quarantine_is_permanent(store: DurablePlanDedupeStore) -> None:
    store.reserve("p", HASH, external_effects=True)
    store.start("p", HASH, nonce="n")
    store.quarantine("p", HASH)
    with pytest.raises(PlanInDoubtQuarantineError):
        store.reserve("p", HASH, external_effects=True)
    with pytest.raises(PlanStoreStateError):
        store.quarantine("p", HASH)


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
    store.start("p", HASH, nonce="same")
    store.finish("p", HASH, evidence={})
    store.reserve("q", HASH, external_effects=False)
    with pytest.raises(PlanStoreStateError, match="nonce already consumed"):
        store.start("q", HASH, nonce="same")
    store.start("q", HASH, nonce="another")


def test_capacity_fails_closed_without_evicting_tombstone(
    store: DurablePlanDedupeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "MAX_RECORDS", 1)
    store.reserve("p", HASH, external_effects=True)
    store.start("p", HASH, nonce="n")
    store.quarantine("p", HASH)
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
    store.start("p", HASH, nonce="n")
    with pytest.raises(ValueError):
        store.finish("p", HASH, evidence={"bad": float("nan")})
    with pytest.raises(ValueError, match="byte limit"):
        store.finish("p", HASH, evidence="x" * (store_module.MAX_EVIDENCE_BYTES + 1))
    store.finish("p", HASH, evidence={"ok": True})
    with pytest.raises(PlanStoreStateError):
        store.finish("p", HASH, evidence={})


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
        store.start("same-plan", HASH, nonce="same-nonce", project_id=project)
        store.finish("same-plan", HASH, evidence={"value": value}, project_id=project)
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
    store.start("p", HASH, nonce="n")
    with pytest.raises(ValueError, match="byte limit"):
        store.finish("p", HASH, evidence=["x" * 1024] * 100)
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


@pytest.mark.parametrize("message", ["disk I/O error", "database is busy"])
def test_wal_setup_fails_closed_on_nonbusy_or_expired_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    def failed_connect(*_args: Any, **_kwargs: Any) -> sqlite3.Connection:
        raise sqlite3.OperationalError(message)

    monotonic_values = iter([0.0, 0.0, 3.0 if "busy" in message else 0.0])
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
    store.start("p", HASH, nonce="n")

    def unexpected_encoder(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("encoder should not be invoked")

    monkeypatch.setattr(json, "JSONEncoder", unexpected_encoder)
    with pytest.raises(ValueError, match="byte limit"):
        store.finish("p", HASH, evidence="x" * (store_module.MAX_EVIDENCE_BYTES + 1))
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
    store.start("p", HASH, nonce="n")
    with pytest.raises(ValueError, match="byte limit"):
        store.finish("p", HASH, evidence=evidence)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone()[0] == "EXECUTING"
