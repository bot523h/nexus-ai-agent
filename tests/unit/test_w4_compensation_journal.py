"""Durability and fencing tests for the compensation intent journal."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.durable_plan_dedupe import DurablePlanDedupeStore
from nexus_ai_agent.creative.studio.durable_dedupe import (
    PlanFencingTokenMismatchError,
    PlanInDoubtQuarantineError,
    PlanStoreStateError,
)

HASH = "a" * 64
KEY1 = "b" * 64
KEY2 = "c" * 64


def test_heartbeat_and_journal_fence_compensating_owner(tmp_path: Path) -> None:
    now = [10.0]
    store = DurablePlanDedupeStore(tmp_path / "db", clock=lambda: now[0])
    store.reserve("p", HASH, external_effects=True)
    lease = store.start("p", HASH, nonce="n", lease_ttl_seconds=2)
    args: dict[str, Any] = {"attempt": lease.attempt, "owner_token": lease.owner_token}
    store.mark_compensating("p", HASH, **args)
    now[0] = 11.0
    renewed = store.heartbeat("p", HASH, lease_ttl_seconds=10, **args)
    assert renewed.lease_expires_at == 21.0
    with pytest.raises(PlanFencingTokenMismatchError):
        store.record_compensation_intent(
            "p", HASH, step_id="one", effect_key=KEY1, **{**args, "owner_token": "d" * 64}
        )
    store.record_compensation_intent("p", HASH, step_id="one", effect_key=KEY1, **args)
    with pytest.raises(PlanStoreStateError, match="already exists"):
        store.record_compensation_intent("p", HASH, step_id="one", effect_key=KEY1, **args)
    with pytest.raises(PlanStoreStateError, match="unresolved"):
        store.record_compensation("p", HASH, succeeded=True, evidence={}, **args)
    with pytest.raises(PlanFencingTokenMismatchError):
        store.record_compensation_step(
            "p",
            HASH,
            step_id="one",
            effect_key=KEY1,
            succeeded=True,
            evidence={},
            **{**args, "attempt": 2},
        )
    store.record_compensation_step(
        "p", HASH, step_id="one", effect_key=KEY1, succeeded=True, evidence={"result": "ok"}, **args
    )
    (entry,) = store.compensation_journal("p", HASH)
    assert (entry.step_id, entry.effect_key, entry.attempt, entry.status) == (
        "one",
        KEY1,
        1,
        "COMPENSATED",
    )
    assert json.loads(entry.evidence_json or "") == {"result": "ok"}
    with pytest.raises(PlanFencingTokenMismatchError):
        store.record_compensation_step(
            "p", HASH, step_id="one", effect_key=KEY1, succeeded=True, evidence={}, **args
        )
    store.record_compensation("p", HASH, succeeded=True, evidence={}, **args)
    with pytest.raises(PlanFencingTokenMismatchError):
        store.heartbeat("p", HASH, **args)


def test_crash_during_compensation_preserves_pending_and_quarantines(tmp_path: Path) -> None:
    path = tmp_path / "crash.db"
    child = """
import os, sys
from nexus_ai_agent.adapters.durable_plan_dedupe import DurablePlanDedupeStore
s = DurablePlanDedupeStore(sys.argv[1], clock=lambda: 100.0)
h = "a" * 64
s.reserve("p", h, external_effects=True)
l = s.start("p", h, nonce="n", lease_ttl_seconds=1)
a = dict(attempt=l.attempt, owner_token=l.owner_token)
s.mark_compensating("p", h, **a)
s.record_compensation_intent("p", h, step_id="first", effect_key="b" * 64, **a)
s.record_compensation_step("p", h, step_id="first", effect_key="b" * 64,
                           succeeded=True, evidence={"done": True}, **a)
s.record_compensation_intent("p", h, step_id="second", effect_key="c" * 64, **a)
os._exit(0)
"""
    assert subprocess.run([sys.executable, "-c", child, str(path)], check=False).returncode == 0
    store = DurablePlanDedupeStore(path, clock=lambda: 101.0)
    entries = store.compensation_journal("p", HASH)
    assert [(e.step_id, e.status) for e in entries] == [
        ("first", "COMPENSATED"),
        ("second", "PENDING"),
    ]
    with pytest.raises(PlanInDoubtQuarantineError):
        store.reserve("p", HASH, external_effects=True)
    with pytest.raises(PlanInDoubtQuarantineError):
        DurablePlanDedupeStore(path, clock=lambda: 102.0).reserve("p", HASH, external_effects=True)
    assert store.compensation_journal("p", HASH) == entries


def test_failed_journal_entry_cannot_claim_rollback_succeeded(tmp_path: Path) -> None:
    store = DurablePlanDedupeStore(tmp_path / "db", clock=lambda: 1.0)
    store.reserve("p", HASH, external_effects=True)
    lease = store.start("p", HASH, nonce="n")
    args: dict[str, Any] = {"attempt": lease.attempt, "owner_token": lease.owner_token}
    store.mark_compensating("p", HASH, **args)
    store.record_compensation_intent("p", HASH, step_id="x", effect_key=KEY1, **args)
    store.record_compensation_step(
        "p",
        HASH,
        step_id="x",
        effect_key=KEY1,
        succeeded=False,
        evidence={"error": "failure"},
        **args,
    )
    with pytest.raises(PlanStoreStateError, match="unresolved"):
        store.record_compensation("p", HASH, succeeded=True, evidence={}, **args)
    store.record_compensation("p", HASH, succeeded=False, evidence={"reason": "failed"}, **args)
    assert store.compensation_journal("p", HASH)[0].status == "FAILED"
    with pytest.raises(PlanInDoubtQuarantineError):
        store.reserve("p", HASH, external_effects=True)


def test_repair_authority_and_cas_bind_expected_attempt(tmp_path: Path) -> None:
    calls: list[tuple[str, str, str, int]] = []

    def authorize(project: str, actor: str, status: str, attempt: int) -> bool:
        calls.append((project, actor, status, attempt))
        return actor == "admin"

    store = DurablePlanDedupeStore(tmp_path / "db", clock=lambda: 1.0, repair_authorizer=authorize)
    store.reserve("p", HASH, external_effects=True)
    lease = store.start("p", HASH, nonce="n")
    store.quarantine("p", HASH, attempt=lease.attempt, owner_token=lease.owner_token)
    kwargs: dict[str, Any] = {
        "operator_id": "admin",
        "expected_status": "IN_DOUBT",
        "expected_attempt": 2,
        "resolution_action": "authorize_takeover",
        "reason": "checked",
    }
    with pytest.raises(PlanStoreStateError, match="expected status/attempt"):
        store.repair_in_doubt("default", "p", HASH, **kwargs)
    assert calls[-1] == ("default", "admin", "IN_DOUBT", 2)
    with pytest.raises(PlanStoreStateError, match="authorization"):
        store.repair_in_doubt(
            "default", "p", HASH, **{**kwargs, "operator_id": "attacker", "expected_attempt": 1}
        )
    store.repair_in_doubt("default", "p", HASH, **{**kwargs, "expected_attempt": 1})
    assert store.start("p", HASH, nonce="new").attempt == 2


@pytest.mark.parametrize("bad", [None, "x", "A" * 64])
def test_compensation_effect_key_must_be_hash(tmp_path: Path, bad: Any) -> None:
    store = DurablePlanDedupeStore(tmp_path / "db", clock=lambda: 1.0)
    store.reserve("p", HASH, external_effects=True)
    lease = store.start("p", HASH, nonce="n")
    args: dict[str, Any] = {"attempt": lease.attempt, "owner_token": lease.owner_token}
    store.mark_compensating("p", HASH, **args)
    with pytest.raises(ValueError, match="effect_key"):
        store.record_compensation_intent("p", HASH, step_id="x", effect_key=bad, **args)
    with pytest.raises(ValueError, match="effect_key"):
        store.record_compensation_step(
            "p", HASH, step_id="x", effect_key=bad, succeeded=True, evidence={}, **args
        )


def test_journal_is_bounded_and_succeeded_must_be_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.adapters import durable_plan_dedupe as module
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanStoreCapacityError

    store = DurablePlanDedupeStore(tmp_path / "db", clock=lambda: 1.0)
    store.reserve("p", HASH, external_effects=True)
    lease = store.start("p", HASH, nonce="n")
    args: dict[str, Any] = {"attempt": lease.attempt, "owner_token": lease.owner_token}
    store.mark_compensating("p", HASH, **args)
    with pytest.raises(ValueError, match="boolean"):
        store.record_compensation_step(
            "p",
            HASH,
            step_id="x",
            effect_key=KEY1,
            succeeded=1,  # type: ignore[arg-type]
            evidence={},
            **args,
        )
    monkeypatch.setattr(module, "MAX_COMPENSATION_ENTRIES", 0)
    with pytest.raises(PlanStoreCapacityError, match="journal"):
        store.record_compensation_intent("p", HASH, step_id="x", effect_key=KEY1, **args)
    assert not store.compensation_journal("p", HASH)


@pytest.mark.parametrize("attempt", [True, -1, 2**63, "1"])
def test_repair_rejects_invalid_attempt(tmp_path: Path, attempt: Any) -> None:
    store = DurablePlanDedupeStore(tmp_path / "db")
    with pytest.raises(ValueError, match="expected_attempt"):
        store.repair_in_doubt(
            "default",
            "p",
            HASH,
            expected_status="IN_DOUBT",
            expected_attempt=attempt,
            operator_id="op",
            resolution_action="authorize_takeover",
            reason="checked",
        )


def test_takeover_never_blindly_repeats_or_hides_previous_pending_intent(tmp_path: Path) -> None:
    now = [10.0]
    store = DurablePlanDedupeStore(
        tmp_path / "db", clock=lambda: now[0], repair_authorizer=lambda _p, _o, _s, _a: True
    )
    store.reserve("p", HASH, external_effects=True)
    first = store.start("p", HASH, nonce="n1", lease_ttl_seconds=1)
    old: dict[str, Any] = {"attempt": first.attempt, "owner_token": first.owner_token}
    store.mark_compensating("p", HASH, **old)
    store.record_compensation_intent("p", HASH, step_id="step", effect_key=KEY1, **old)
    now[0] = 11.0
    with pytest.raises(PlanInDoubtQuarantineError):
        store.reserve("p", HASH, external_effects=True)
    store.repair_in_doubt(
        "default",
        "p",
        HASH,
        expected_status="IN_DOUBT",
        expected_attempt=first.attempt,
        operator_id="admin",
        resolution_action="authorize_takeover",
        reason="old worker stopped",
    )
    second = store.start("p", HASH, nonce="n2")
    new: dict[str, Any] = {"attempt": second.attempt, "owner_token": second.owner_token}
    store.mark_compensating("p", HASH, **new)
    with pytest.raises(PlanStoreStateError, match="already exists"):
        store.record_compensation_intent("p", HASH, step_id="step", effect_key=KEY1, **new)
    with pytest.raises(PlanStoreStateError, match="unresolved"):
        store.record_compensation("p", HASH, succeeded=True, evidence={}, **new)
    assert store.compensation_journal("p", HASH)[0].status == "PENDING"


def test_false_compensation_is_failure_but_other_compensations_continue() -> None:
    from nexus_ai_agent.creative.studio.bus import (
        PlanRollbackError,
        PlanStep,
        PlanTransaction,
        PlanTransactionState,
    )

    events: list[str] = []

    def first() -> str:
        return "first"

    def second() -> str:
        return "second"

    def fail() -> None:
        raise ValueError("forward failure")

    def undo_first(_: object) -> None:
        events.append("undo-first")

    def undo_second(_: object) -> bool:
        events.append("undo-second")
        return False

    plan = PlanTransaction(
        steps=(
            PlanStep(operation="first", execute=first, compensate=undo_first),
            PlanStep(operation="second", execute=second, compensate=undo_second),
            PlanStep(operation="fail", execute=fail, compensate=lambda _: None),
        )
    )
    with pytest.raises(PlanRollbackError):
        plan.commit()
    assert events == ["undo-second", "undo-first"]
    assert plan.state is PlanTransactionState.ROLLBACK_FAILED
    assert [(entry.step_index, entry.status) for entry in plan.rollback_records] == [
        (1, "failed"),
        (0, "succeeded"),
    ]


def test_lock_retries_after_contention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from nexus_ai_agent.adapters import durable_plan_dedupe as module

    store = DurablePlanDedupeStore(tmp_path / "db")
    calls: list[float] = []

    def flock(_fd: int, _flags: int) -> None:
        if not calls:
            calls.append(1.0)
            raise BlockingIOError

    sleeps: list[float] = []
    monkeypatch.setattr(module, "fcntl", SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=flock))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=sleeps.append))
    with store._store_lock():
        assert calls == [1.0]
    assert sleeps == [0.01]


def test_intent_can_be_recorded_before_external_effect(tmp_path: Path) -> None:
    now = [1.0]
    path = tmp_path / "db"
    store = DurablePlanDedupeStore(path, clock=lambda: now[0])
    lease = store.reserve_and_start(
        "p", HASH, external_effects=True, nonce="n", lease_ttl_seconds=2
    )
    args: dict[str, Any] = {"attempt": lease.attempt, "owner_token": lease.owner_token}
    store.record_compensation_intent("p", HASH, step_id="first", effect_key=KEY1, **args)
    assert store.compensation_journal("p", HASH)[0].status == "PENDING"
    with pytest.raises(PlanFencingTokenMismatchError):
        store.record_compensation_step(
            "p", HASH, step_id="first", effect_key=KEY1, succeeded=True, evidence={}, **args
        )
    # On process death the durable intent remains visible and expiry quarantines.
    now[0] = 3.0
    with pytest.raises(PlanInDoubtQuarantineError):
        DurablePlanDedupeStore(path, clock=lambda: now[0]).reserve_and_start(
            "p", HASH, external_effects=True, nonce="another"
        )
    assert store.compensation_journal("p", HASH)[0].status == "PENDING"
