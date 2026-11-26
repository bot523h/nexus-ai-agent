"""Real SQLite tests for atomic plan acquisition and explicit takeover."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.durable_plan_dedupe import DurablePlanDedupeStore
from nexus_ai_agent.creative.studio.durable_dedupe import (
    ExecutionLease,
    PlanExecutionInProgressError,
    PlanInDoubtQuarantineError,
    PlanReplayConflictError,
    PlanStoreStateError,
)

HASH = "a" * 64


def test_nonce_failure_rolls_back_plan_reservation(tmp_path: Path) -> None:
    path = tmp_path / "db"
    store = DurablePlanDedupeStore(path, clock=lambda: 1.0)
    lease = store.reserve_and_start("p", HASH, external_effects=True, nonce="n")
    store.finish("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token, evidence={})
    with pytest.raises(PlanStoreStateError, match="nonce already consumed"):
        store.reserve_and_start("q", HASH, external_effects=False, nonce="n")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM plans WHERE plan_id='q'").fetchone()[0] == 0
    assert store.reserve_and_start("q", HASH, external_effects=False, nonce="fresh").attempt == 1
    with pytest.raises(PlanInDoubtQuarantineError):
        store.reserve_and_start("p", HASH, external_effects=True, nonce="different")


def test_duplicate_and_authorized_takeover_acquisition(tmp_path: Path) -> None:
    now = [10.0]
    store = DurablePlanDedupeStore(
        tmp_path / "db", clock=lambda: now[0], repair_authorizer=lambda _p, _o, _s, _a: True
    )
    old = store.reserve_and_start(
        "p", HASH, external_effects=True, nonce="one", lease_ttl_seconds=1
    )
    with pytest.raises(PlanExecutionInProgressError):
        store.reserve_and_start("p", HASH, external_effects=True, nonce="two")
    with pytest.raises(PlanReplayConflictError):
        store.reserve_and_start("p", "b" * 64, external_effects=True, nonce="two")
    now[0] = 11.0
    with pytest.raises(PlanInDoubtQuarantineError):
        store.reserve_and_start("p", HASH, external_effects=True, nonce="two")
    store.repair_in_doubt(
        "default",
        "p",
        HASH,
        expected_status="IN_DOUBT",
        expected_attempt=old.attempt,
        operator_id="admin",
        resolution_action="authorize_takeover",
        reason="worker stopped",
        recovery_evidence=store.inspect_recovery(
            "default",
            "p",
            HASH,
            expected_status="IN_DOUBT",
            expected_attempt=old.attempt,
            operator_id="admin",
        ),
    )
    with pytest.raises(PlanStoreStateError, match="nonce already consumed"):
        store.reserve_and_start("p", HASH, external_effects=True, nonce="one")
    new = store.reserve_and_start("p", HASH, external_effects=True, nonce="two")
    assert new.attempt == old.attempt + 1 and new.owner_token != old.owner_token
    store.finish("p", HASH, attempt=new.attempt, owner_token=new.owner_token, evidence={})


def test_never_steals_provisional_reservation(tmp_path: Path) -> None:
    store = DurablePlanDedupeStore(tmp_path / "db", clock=lambda: 1.0)
    store.reserve("p", HASH, external_effects=False)
    with pytest.raises(PlanInDoubtQuarantineError):
        store.reserve_and_start("p", HASH, external_effects=False, nonce="rival")
    assert store.start("p", HASH, nonce="owner").attempt == 1


def test_concurrent_acquisition_only_one_owner(tmp_path: Path) -> None:
    path = tmp_path / "db"
    DurablePlanDedupeStore(path, clock=lambda: 1.0)

    def acquire(nonce: str) -> ExecutionLease | None:
        try:
            return DurablePlanDedupeStore(path, clock=lambda: 1.0).reserve_and_start(
                "p", HASH, external_effects=True, nonce=nonce
            )
        except PlanExecutionInProgressError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(acquire, ("n1", "n2")))
    assert sum(value is not None for value in result) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM consumed_nonces").fetchone()[0] == 1
        assert (
            conn.execute("SELECT count(*) FROM plans WHERE status='EXECUTING'").fetchone()[0] == 1
        )


@pytest.mark.parametrize("nonce", ["", "x" * 257, None, 3])
def test_atomic_acquire_rejects_invalid_nonces(tmp_path: Path, nonce: object) -> None:
    store = DurablePlanDedupeStore(tmp_path / "db", clock=lambda: 1.0)
    with pytest.raises(ValueError, match="nonce"):
        store.reserve_and_start("p", HASH, external_effects=True, nonce=nonce)  # type: ignore[arg-type]


def test_atomic_acquire_validates_effect_flag_and_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.adapters import durable_plan_dedupe as module
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanStoreCapacityError

    store = DurablePlanDedupeStore(tmp_path / "db", clock=lambda: 1.0)
    with pytest.raises(ValueError, match="boolean"):
        store.reserve_and_start("p", HASH, external_effects=1, nonce="n")  # type: ignore[arg-type]
    monkeypatch.setattr(module, "MAX_RECORDS", 0)
    with pytest.raises(PlanStoreCapacityError):
        store.reserve_and_start("p", HASH, external_effects=True, nonce="n")


def test_atomic_takeover_rejects_exhausted_attempt(tmp_path: Path) -> None:
    path = tmp_path / "db"
    store = DurablePlanDedupeStore(
        path, clock=lambda: 1.0, repair_authorizer=lambda _p, _o, _s, _a: True
    )
    lease = store.reserve_and_start("p", HASH, external_effects=True, nonce="n")
    store.quarantine("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token)
    store.repair_in_doubt(
        "default",
        "p",
        HASH,
        expected_status="IN_DOUBT",
        expected_attempt=lease.attempt,
        operator_id="op",
        resolution_action="authorize_takeover",
        reason="checked",
        recovery_evidence=store.inspect_recovery(
            "default",
            "p",
            HASH,
            expected_status="IN_DOUBT",
            expected_attempt=lease.attempt,
            operator_id="op",
        ),
    )
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE plans SET attempt=?", (2**63 - 1,))
    with pytest.raises(PlanStoreStateError, match="exhausted"):
        store.reserve_and_start("p", HASH, external_effects=True, nonce="new")


def test_multiple_approval_nonces_are_consumed_atomically(tmp_path: Path) -> None:
    path = tmp_path / "db"
    store = DurablePlanDedupeStore(path, clock=lambda: 1.0)
    lease = store.reserve_and_start(
        "first", HASH, external_effects=True, nonce="n1", additional_nonces=("n2", "n3")
    )
    with pytest.raises(PlanStoreStateError, match="already consumed"):
        store.reserve_and_start(
            "second", HASH, external_effects=True, nonce="new", additional_nonces=("n2",)
        )
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT nonce FROM consumed_nonces ORDER BY nonce").fetchall() == [
            ("n1",),
            ("n2",),
            ("n3",),
        ]
        assert conn.execute("SELECT count(*) FROM plans WHERE plan_id='second'").fetchone()[0] == 0
    with pytest.raises(PlanStoreStateError, match="repeated"):
        store.reserve_and_start(
            "second", HASH, external_effects=True, nonce="dup", additional_nonces=("dup",)
        )
    store.finish(
        "first",
        HASH,
        attempt=lease.attempt,
        owner_token=lease.owner_token,
        evidence={"receipt": 42},
    )
    replay = store.replay_committed(
        "first", HASH, external_effects=True, nonce="fresh", additional_nonces=("fresh2",)
    )
    assert replay.evidence_json == '{"receipt":42}'
    assert store.replay_committed("first", HASH, external_effects=True, nonce="fresh") == replay
    assert store.replay_committed("first", HASH, external_effects=True, nonce="n1") == replay
    with pytest.raises(PlanReplayConflictError):
        store.replay_committed("first", "b" * 64, external_effects=True, nonce="unused")
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute("SELECT count(*) FROM consumed_nonces WHERE nonce='unused'").fetchone()[0]
            == 0
        )


def test_replay_never_returns_live_or_nonexistent_plan(tmp_path: Path) -> None:
    store = DurablePlanDedupeStore(tmp_path / "db", clock=lambda: 1.0)
    with pytest.raises(PlanStoreStateError, match="not found"):
        store.replay_committed("missing", HASH, external_effects=True, nonce="n")
    store.reserve_and_start("p", HASH, external_effects=True, nonce="n")
    with pytest.raises(PlanStoreStateError, match="no committed receipt"):
        store.replay_committed("p", HASH, external_effects=True, nonce="new")


@pytest.mark.parametrize("additional", [["x"], tuple(str(i) for i in range(128))])
def test_additional_nonces_are_bounded(tmp_path: Path, additional: object) -> None:
    store = DurablePlanDedupeStore(tmp_path / "db")
    with pytest.raises(ValueError, match="bounded tuple"):
        store.reserve_and_start(
            "p",
            HASH,
            external_effects=True,
            nonce="n",
            additional_nonces=additional,  # type: ignore[arg-type]
        )


def test_multibyte_nonce_bound_and_capacity_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.adapters import durable_plan_dedupe as module
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanStoreCapacityError

    path = tmp_path / "db"
    store = DurablePlanDedupeStore(path, clock=lambda: 1.0)
    with pytest.raises(ValueError, match="nonce"):
        store.reserve_and_start("p", HASH, external_effects=True, nonce="ک" * 129)
    monkeypatch.setattr(module, "MAX_CONSUMED_NONCES", 1)
    with pytest.raises(PlanStoreCapacityError, match="nonce capacity"):
        store.reserve_and_start(
            "p", HASH, external_effects=True, nonce="n", additional_nonces=("m",)
        )
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM plans").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM consumed_nonces").fetchone()[0] == 0


def test_replay_refuses_nonboolean_effects(tmp_path: Path) -> None:
    store = DurablePlanDedupeStore(tmp_path / "db")
    with pytest.raises(ValueError, match="boolean"):
        store.replay_committed("p", HASH, external_effects=1, nonce="n")  # type: ignore[arg-type]
