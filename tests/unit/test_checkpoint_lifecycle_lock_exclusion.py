"""Deterministic mutation probe for the per-instance lifecycle lock.

Companion to ``test_checkpoint_lifecycle_store_boundary.py``.  That file's
``test_concurrency_suite_catches_lock_removal`` neuters ``_lock`` and *hopes*
500 racing writes interleave — on a low-CPU runner they can run without
interleaving, so a required gate can go RED even though the store is correct.

This probe replaces the lottery with synchronization primitives: the invariant
of a mutual-exclusion lock is observed directly, with no racing at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord
from nexus_ai_agent.storage.checkpoint_lifecycle_store import (
    SQLiteCheckpointLifecycleStore,
)


class _NoopLock:
    """A lock whose acquire/release/context-manager protocol all no-op."""

    def acquire(self, *args, **kwargs):
        return True

    def release(self) -> None:
        return None

    def __enter__(self) -> _NoopLock:
        return self

    def __exit__(self, *args) -> None:
        return None


def _second_writer_enters_while_first_holds(store: SQLiteCheckpointLifecycleStore) -> bool:
    """True when two writers occupy the critical section at the same time.

    Uses events only: ``second`` must not enter ``_write_transaction`` while
    ``first`` is still inside it.  With a real mutual-exclusion lock the wait
    times out; with a neutered lock it returns immediately.
    """
    import threading

    first_inside = threading.Event()
    release_first = threading.Event()
    second_inside = threading.Event()

    def first() -> None:
        with store._write_transaction():
            first_inside.set()
            release_first.wait(timeout=5)

    def second() -> None:
        first_inside.wait(timeout=5)
        with store._write_transaction():
            second_inside.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    first_inside.wait(timeout=5)
    overlapped = second_inside.wait(timeout=1.0)
    release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)
    return overlapped


def _fresh_store(tmp_path: Path, name: str) -> SQLiteCheckpointLifecycleStore:
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / name))
    # Keep the constructor path exercised so the store is a real, usable index.
    store.upsert(CheckpointRecord("thread", "seed", datetime.now(timezone.utc)))
    return store


def test_real_lock_forbids_overlapping_critical_sections(tmp_path: Path) -> None:
    """Baseline: with the shipped lock, writers are mutually exclusive."""
    store = _fresh_store(tmp_path, "intact.sqlite")
    try:
        assert not _second_writer_enters_while_first_holds(store)
    finally:
        store.close()


def test_lock_removal_is_caught_deterministically(tmp_path: Path) -> None:
    """Mutation probe: neutering the lock makes the overlap observable.

    This REDs on a single-core runner because the overlap is event-ordered, not
    statistically sampled — unlike the 500-way race it replaces.
    """
    store = _fresh_store(tmp_path, "no-lock.sqlite")
    store._lock = _NoopLock()  # type: ignore[assignment]
    try:
        assert _second_writer_enters_while_first_holds(store), (
            "neutered lock did not allow overlapping critical sections"
        )
    finally:
        store.close()
