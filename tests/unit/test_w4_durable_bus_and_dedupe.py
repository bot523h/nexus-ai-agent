"""Real-disk tests for the provisional W4 plan reservation store."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.creative.studio import durable_dedupe as store_module
from nexus_ai_agent.creative.studio.durable_dedupe import (
    DurablePlanDedupeStore,
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
            conn.execute("INSERT INTO consumed_nonces VALUES ('n')")
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
