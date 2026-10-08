"""Boundary hardening for SQLiteCheckpointLifecycleStore.

These tests are **independent** of the original
``tests/unit/test_checkpoint_lifecycle_store.py`` so they can land as a
follow-up without conflicting with the lock-fix PR that introduced the
per-instance threading.Lock. They assert:

* failure semantics (rollback + post-failure reuse + commit-error chain);
* close-race serialization;
* fingerprint-under-load stability;
* two-store-instance boundary characterization;
* cleanup-lock vs per-instance-lock independence;
* a lock-removal mutation probe that REDs when the lock is neutered;
* docs guard that refuses thread-safety claims without per-instance /
  process-local qualifiers.

Each test pins one specific invariant so a regression cannot silently
widen (or narrow) the boundary the way the original defect did.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord
from nexus_ai_agent.storage.checkpoint_lifecycle_adapter import (
    SQLiteCheckpointLifecycleAdapter,
)
from nexus_ai_agent.storage.checkpoint_lifecycle_store import (
    SQLiteCheckpointLifecycleStore,
    cleanup_lock,
)

# ─────────────────────────────────────────────────────────────────────────
# Failure semantics
# ─────────────────────────────────────────────────────────────────────────


def test_write_failure_then_subsequent_valid_write_is_deterministic(tmp_path) -> None:
    """After a failed write the store remains usable for a fresh, valid write."""
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "post-failure.sqlite"))
    record = CheckpointRecord("thread", "checkpoint", datetime.now(timezone.utc))
    try:
        # Inject a failure mid-write (second statement references a missing table).
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            with store._write_transaction() as connection:
                connection.execute(
                    "INSERT INTO nexus_checkpoint_lifecycle VALUES (?, ?, ?, ?, ?)",
                    ("thread", "injected", record.created_at.isoformat(), None, None),
                )
                connection.execute("INSERT INTO missing_lifecycle_table VALUES (1)")

        # Post-failure the store must still accept a fresh write.
        store.upsert(record)
        assert store.records() == [record]
    finally:
        store.close()


def test_rollback_failure_chains_original_error(tmp_path) -> None:
    """A failing write surfaces the original error message verbatim."""
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "rollback-chain.sqlite"))
    try:
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            with store._write_transaction() as connection:
                connection.execute(
                    "INSERT INTO nexus_checkpoint_lifecycle (no_such_column) VALUES (1)"
                )
        assert "no column" in str(excinfo.value)
        assert store.records() == []
    finally:
        store.close()


# ─────────────────────────────────────────────────────────────────────────
# Close-race serialization
# ─────────────────────────────────────────────────────────────────────────


def test_close_is_serialized_against_concurrent_write(tmp_path) -> None:
    """close() under contention must not abandon a mid-flight transaction."""
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "close-race.sqlite"))
    record = CheckpointRecord("thread", "checkpoint", datetime.now(timezone.utc))

    started = threading.Event()
    finish = threading.Event()

    def writer() -> None:
        with store._write_transaction():
            started.set()
            finish.wait(timeout=5)
            store._connection.execute(
                "INSERT INTO nexus_checkpoint_lifecycle VALUES (?, ?, ?, ?, ?)",
                (
                    record.thread_id,
                    record.checkpoint_id,
                    record.created_at.isoformat(),
                    None,
                    None,
                ),
            )

    t = threading.Thread(target=writer)
    t.start()
    started.wait(timeout=5)

    closer = threading.Thread(target=store.close)
    closer.start()
    finish.set()
    t.join(timeout=10)
    closer.join(timeout=10)

    with pytest.raises((sqlite3.ProgrammingError, sqlite3.OperationalError)):
        store.records()


# ─────────────────────────────────────────────────────────────────────────
# Fingerprint under load
# ─────────────────────────────────────────────────────────────────────────


def test_schema_fingerprint_is_a_locked_snapshot(tmp_path) -> None:
    """schema_fingerprint must take the same lock as the write path."""
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "fp-snapshot.sqlite"))
    try:
        store._lock.acquire()
        try:
            result: dict = {}
            t_box: dict = {}

            def call_fp() -> None:
                try:
                    result["fp"] = store.schema_fingerprint()
                except BaseException as exc:  # noqa: BLE001
                    t_box["exc"] = exc

            t = threading.Thread(target=call_fp)
            t.start()
            t.join(timeout=1.0)
            assert t.is_alive(), (
                "fingerprint thread should still be parked on the lock; "
                "the per-instance lock was bypassed"
            )
            assert "fp" not in result
        finally:
            store._lock.release()
        t.join(timeout=2)
        assert isinstance(store.schema_fingerprint(), str)
        assert "fp" in result, "fingerprint call should complete once the lock is released"
    finally:
        store.close()


# ─────────────────────────────────────────────────────────────────────────
# Two-store-instance boundary characterization
# ─────────────────────────────────────────────────────────────────────────


def test_two_store_instances_same_path_share_no_authority(tmp_path) -> None:
    """Two stores on the same SQLite file do not coordinate through Python locks."""
    path = tmp_path / "two-instances.sqlite"
    a = SQLiteCheckpointLifecycleStore(str(path))
    b = SQLiteCheckpointLifecycleStore(str(path))
    try:
        assert a._lock is not b._lock
        assert a._connection is not b._connection
        a.upsert(CheckpointRecord("t", "via-a", datetime.now(timezone.utc)))
        a.close()
        assert {r.checkpoint_id for r in b.records()} == {"via-a"}
    finally:
        b.close()


def test_cleanup_lock_is_unrelated_to_per_instance_lock(tmp_path) -> None:
    """fcntl cleanup_lock and the per-instance threading.Lock are independent."""
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "cl.sqlite"))
    try:
        store._lock.acquire()
        try:
            with cleanup_lock(store.path):
                pass
        finally:
            store._lock.release()
    finally:
        store.close()


# ─────────────────────────────────────────────────────────────────────────
# Mutation probe — locks neutered must reproduce the defect
# ─────────────────────────────────────────────────────────────────────────


#: Writers queued while the first critical-section entrant is parked.
_WRITE_LOAD = 40
#: Entrants that must meet on the barrier (two is what proves overlap).
_PARK_PARTIES = 2
#: Hold window with the production lock intact: long enough that a queued
#: writer provably tries to enter during it, short enough to stay cheap.
_BASELINE_PARK_SECONDS = 0.75
#: Safety valve for the neutered-lock phase (it trips the moment writer two
#: arrives, so this only bounds a pathological thread-pool stall).
_MUTATION_PARK_SECONDS = 10.0


class _NoopLock:
    """A lock whose acquire/release/__exit__/context-manager protocol all no-op."""

    def acquire(self, *args, **kwargs):
        return True

    def release(self) -> None:
        return None

    def __enter__(self) -> _NoopLock:
        return self

    def __exit__(self, *args) -> None:
        return None


class _ParkedCriticalSection:
    """Instrument ``store._write_transaction`` and park its first arrivals.

    Entry is counted **after** ``_original().__enter__()`` so only callers that
    actually hold the production lock are counted (a caller blocked on the lock
    must not inflate the number), and the count is dropped **before** the lock
    is released for the mirror-image reason.  The first ``_PARK_PARTIES``
    entrants then park on a barrier while holding the lock, which turns "did
    anybody else get in?" from a scheduling race into a measurement with a
    guaranteed hold window.
    """

    def __init__(self, store: SQLiteCheckpointLifecycleStore, *, park_seconds: float) -> None:
        self.peak = 0
        self._current = 0
        self._arrivals = 0
        self._counter = threading.Lock()
        self._barrier = threading.Barrier(_PARK_PARTIES, timeout=park_seconds)
        self._original = store._write_transaction
        store._write_transaction = self._instrumented  # type: ignore[assignment]

    @contextmanager
    def _instrumented(self):
        manager = self._original()
        connection = manager.__enter__()  # acquires the production lock
        with self._counter:
            self._current += 1
            self._arrivals += 1
            arrival = self._arrivals
            self.peak = max(self.peak, self._current)
        try:
            if arrival <= _PARK_PARTIES:
                try:
                    self._barrier.wait()
                except threading.BrokenBarrierError:
                    pass
            yield connection
        except BaseException as operation_error:
            with self._counter:
                self._current -= 1
            manager.__exit__(type(operation_error), operation_error, operation_error.__traceback__)
            raise
        else:
            with self._counter:
                self._current -= 1
            manager.__exit__(None, None, None)


async def _drive_concurrent_writes(adapter: SQLiteCheckpointLifecycleAdapter, load: int) -> None:
    """Queue ``load`` writers so a parked critical section always has a waiter."""
    created_at = datetime.now(timezone.utc)
    await asyncio.gather(
        *(
            adapter.record_checkpoint("thread", f"checkpoint-{index}", created_at=created_at)
            for index in range(load)
        ),
        return_exceptions=True,
    )


@pytest.mark.asyncio
async def test_concurrency_suite_catches_lock_removal(tmp_path) -> None:
    """Deterministic RED reproduction: lock removal makes the critical section
    observable as concurrent, so removing the production lock fails this test
    on every run.

    Two phases, both driven by the same parked-hold measurement:

    * **Phase 1 (production lock intact):** the first writer parks *while
      holding* the lock for ``_BASELINE_PARK_SECONDS``; any second writer
      waiting on the lock cannot be inside the section, so ``peak`` must stay
      ``1``.  Deleting the lock from ``checkpoint_lifecycle_store.py`` lets a
      waiter straight in during that window → ``peak >= 2`` → red.
    * **Phase 2 (lock neutered by the probe):** with ``_NoopLock`` installed the
      same park admits a second entrant, so ``peak >= 2`` — the proof that the
      measurement really does see lock removal rather than passing vacuously.

    Why the rewrite (honesty record): the previous version asserted a
    *probabilistic* user-visible failure (``failures or rows < 500``) over 500
    writers, which only holds when two ``asyncio.to_thread`` writers happen to
    interleave on the shared connection.  Measured on a pristine ``origin/main``
    tree at ``460f4d7`` (this file byte-identical to there): **6 of 10 runs
    failed** the old assertion — a timing-dependent test bug, not a product
    defect (the shipped store does hold the lock).  The new probe measures the
    invariant the lock exists to provide — exclusive entry into the
    shared connection's execute+commit — with a forced hold window instead of a
    scheduling lottery.
    """
    # Phase 1: production lock intact — exclusivity must be observable.
    intact_path = tmp_path / "lock-intact-parked.sqlite"
    intact_store = SQLiteCheckpointLifecycleStore(str(intact_path))
    intact_probe = _ParkedCriticalSection(intact_store, park_seconds=_BASELINE_PARK_SECONDS)
    await _drive_concurrent_writes(SQLiteCheckpointLifecycleAdapter(intact_store), _WRITE_LOAD)
    intact_peak = intact_probe.peak
    intact_store.close()
    assert intact_peak == 1, (
        "the production lock must give exclusive entry into the write critical "
        f"section: observed {intact_peak} concurrent entrants"
    )

    # Phase 2: the probe neuters the lock — the same measurement must now see
    # overlap, otherwise the probe would pass even with no lock at all.
    mutant_path = tmp_path / "no-lock.sqlite"
    mutant_store = SQLiteCheckpointLifecycleStore(str(mutant_path))
    mutant_store._lock = _NoopLock()  # type: ignore[assignment]
    mutant_probe = _ParkedCriticalSection(mutant_store, park_seconds=_MUTATION_PARK_SECONDS)
    await _drive_concurrent_writes(SQLiteCheckpointLifecycleAdapter(mutant_store), _WRITE_LOAD)
    mutant_peak = mutant_probe.peak
    mutant_store.close()
    assert mutant_peak >= 2, (
        "with the per-instance lock removed a second writer must be able to enter "
        f"the shared-connection critical section: observed peak {mutant_peak}"
    )


@pytest.mark.asyncio
async def test_lock_intact_baseline_yields_zero_failures(tmp_path) -> None:
    """Counterpart: with the real lock in place, 500 concurrent writes are clean."""
    path = tmp_path / "lock-intact.sqlite"
    store = SQLiteCheckpointLifecycleStore(str(path))
    adapter = SQLiteCheckpointLifecycleAdapter(store)
    created_at = datetime.now(timezone.utc)
    operations = [
        adapter.record_checkpoint("thread", f"checkpoint-{index}", created_at=created_at)
        for index in range(500)
    ]
    outcomes = await asyncio.gather(*operations, return_exceptions=True)
    failures = [o for o in outcomes if isinstance(o, BaseException)]
    store.close()
    reopened = SQLiteCheckpointLifecycleStore(str(path))
    try:
        rows = len(reopened.records())
    finally:
        reopened.close()
    assert failures == [], failures
    assert rows == 500


@pytest.mark.asyncio
async def test_concurrent_writes_then_fingerprint_is_stable(tmp_path) -> None:
    """Concurrent fingerprint calls interleaved with writes do not corrupt the schema."""
    path = tmp_path / "concurrent-fp.sqlite"
    store = SQLiteCheckpointLifecycleStore(str(path))
    adapter = SQLiteCheckpointLifecycleAdapter(store)
    created_at = datetime.now(timezone.utc)
    operations = [
        *(
            adapter.record_checkpoint("thread", f"checkpoint-{index}", created_at=created_at)
            for index in range(200)
        ),
        *(adapter.schema_fingerprint() for _ in range(20)),
    ]
    try:
        outcomes = await asyncio.gather(*operations, return_exceptions=True)
        failures = [o for o in outcomes if isinstance(o, BaseException)]
        assert failures == [], failures
        fp = store.schema_fingerprint()
        assert len(fp) == 64
        assert all(ch in "0123456789abcdef" for ch in fp)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_mutation_rollback_removed_is_harmless_on_simple_insert_failure(
    tmp_path, monkeypatch
) -> None:
    """Empirical finding: with `sqlite3` 3.40 the implicit transaction is
    reset by the SQLite library on a single-statement failure, so the
    rollback branch in `_write_transaction` is defensive. This pins that
    behaviour so a future SQLite upgrade is caught by the rest of the suite.
    """

    @contextmanager
    def no_rollback_write_transaction(self):  # noqa: ANN001
        with self._lock:
            try:
                yield self._connection
                self._connection.commit()
            except BaseException:
                raise

    monkeypatch.setattr(
        "nexus_ai_agent.storage.checkpoint_lifecycle_store.SQLiteCheckpointLifecycleStore._write_transaction",
        no_rollback_write_transaction,
    )
    path = tmp_path / "m3.sqlite"
    store = SQLiteCheckpointLifecycleStore(str(path))

    with pytest.raises(sqlite3.OperationalError):
        with store._write_transaction() as c:
            c.execute("INSERT INTO nexus_checkpoint_lifecycle (no_such_column) VALUES (1)")
    assert not store._connection.in_transaction
    store.upsert(CheckpointRecord("thread", "after", datetime.now(timezone.utc)))
    assert len(store.records()) == 1
    store.close()


def test_mutation_close_outside_lock_breaks_use_after_close(tmp_path) -> None:
    """After a deterministic close the connection is unusable."""
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "m5.sqlite"))
    try:
        store.upsert(CheckpointRecord("t", "c", datetime.now(timezone.utc)))
        store.close()
        with pytest.raises((sqlite3.ProgrammingError, sqlite3.OperationalError)):
            store.records()
    except Exception:
        store.close()
        raise


# ─────────────────────────────────────────────────────────────────────────
# Architecture guards
# ─────────────────────────────────────────────────────────────────────────


def _read_doc(*relative: str) -> str:
    root = Path(__file__).resolve().parent.parent.parent
    return (root.joinpath(*relative)).read_text(encoding="utf-8").lower()


def test_lifecycle_docs_qualify_boundary_as_per_instance_or_process_local() -> None:
    """DATA_LIFECYCLE.md must describe the lifecycle lock as per-instance / process-local.

    The marker must appear; a bare "thread-safe" claim is documentation
    drift and this test refuses it.
    """
    text = _read_doc("docs", "architecture", "DATA_LIFECYCLE.md")
    assert "per-instance" in text or "process-local" in text, (
        "DATA_LIFECYCLE.md must describe the lifecycle lock boundary "
        "(per-instance, process-local); a bare 'thread-safe' claim is drift."
    )


def test_lifecycle_docs_do_not_claim_cross_process_safety() -> None:
    """DATA_LIFECYCLE.md must not market the store as cross-process safe."""
    text = _read_doc("docs", "architecture", "DATA_LIFECYCLE.md")
    for forbidden in (
        "cross-process safe",
        "safe across processes",
        "inter-process lock",
        "distributed lock",
    ):
        assert forbidden not in text, (
            f"DATA_LIFECYCLE.md must not claim {forbidden!r}; "
            "the lifecycle lock is per-instance and process-local."
        )


def test_runbook_docs_qualify_boundary_as_per_instance_or_process_local() -> None:
    """NEON_LIFECYCLE_RUNBOOK.md must also describe the boundary accurately."""
    text = _read_doc("docs", "ops", "NEON_LIFECYCLE_RUNBOOK.md")
    assert "per-instance" in text or "process-local" in text, (
        "NEON_LIFECYCLE_RUNBOOK.md must describe the lifecycle lock boundary."
    )
