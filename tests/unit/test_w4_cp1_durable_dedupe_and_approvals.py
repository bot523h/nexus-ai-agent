"""CP1b's public plan entry point, using real SQLite and real file effects.

Identity and destination fencing are test doubles; exclusive file creation only
models effect deduplication, not a production destination's fencing authority.
"""

import hashlib
import os
import sqlite3
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.durable_plan_dedupe import DurablePlanDedupeStore
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.bus import (
    CommandBus,
    PlanResultSummary,
    PlanStep,
    PlanTransaction,
    PlanTransactionError,
    PlanTransactionState,
)
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    PlayCommandInput,
)
from nexus_ai_agent.creative.studio.durable_dedupe import (
    ApprovalBinding,
    ApprovalBindingError,
    PlanExecutionInProgressError,
    PlanInDoubtQuarantineError,
)
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    CommandExecutionError,
    CommandResult,
    PermissionLevel,
    Project,
    Timeline,
    TypedCommand,
    new_project,
)


class ApprovalVerifier:
    def verify(self, binding: ApprovalBinding) -> bool:
        return True  # Mock authority: not evidence of authentication.


def make_bus(
    root: Path, *, revision: int = 0, fail_at: str = ""
) -> tuple[CommandBus, ActorIdentity]:
    actor = ActorIdentity(kind="user", actor_id="alice")
    project = new_project("project", "before", Timeline(timeline_id="timeline", duration_us=0))
    project = project.model_copy(update={"state_revision": revision})
    registry = CapabilityRegistry()

    def handler(state: Project, context: OperationContext) -> OperationOutcome:
        assert context.effect_key is not None and context.durable_heartbeat is not None
        context.durable_heartbeat()
        path = root / context.effect_key
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, context.command.command_id.encode())
        finally:
            os.close(fd)
        if context.command.command_id == fail_at:
            raise RuntimeError("unknown external effect")
        state.name = "after"
        return OperationOutcome(state, context.history, {"command": context.command.command_id})

    def verifier(_project: Project, command: TypedCommand, result: CommandResult, key: str) -> bool:
        expected = command.command_id
        return (
            bool(result.output.get("command") == expected)
            and (root / key).read_bytes() == expected.encode()
        )

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
    return (
        CommandBus(
            project,
            registry=registry,
            authorizer=ProjectAccess(actor, "project", frozenset({"project:read"})),
            durable_dedupe_store=DurablePlanDedupeStore(root / "plans.db"),
            durable_verifiers={"example.play": verifier},
        ),
        actor,
    )


def make_plan(root: Path, actor: ActorIdentity, count: int = 1) -> PlanTransaction:
    def compensate(result: CommandResult, directory: str = str(root)) -> None:
        key = result.diagnostics["durable_effect_key"]
        with open(f"{directory}/compensation.log", "a", encoding="utf-8") as log:
            log.write(key + "\n")
        __import__("os").unlink(f"{directory}/{key}")

    return PlanTransaction(
        plan_id="plan",
        steps=tuple(
            PlanStep(
                "example.play",
                command=TypedCommand(
                    command_id=f"cmd-{index}",
                    operation="example.play",
                    actor=actor,
                    idempotency_key=f"key-{index}",
                ),
                compensate=compensate,
            )
            for index in range(count)
        ),
    )


def approvals(
    bus: CommandBus, plan: PlanTransaction, actor: ActorIdentity, tag: str
) -> tuple[ApprovalBinding, ...]:
    scopes = bus.durable_approval_requirements(plan, policy_version="v1", authenticated_actor=actor)
    now = time.time()
    return tuple(
        ApprovalBinding(
            **asdict(scope),
            approval_id=f"{tag}-{index}",
            nonce=f"{tag}-{index}",
            issued_at=now - 1,
            expires_at=now + 300,
        )
        for index, scope in enumerate(scopes)
    )


def dispatch(
    bus: CommandBus,
    plan: PlanTransaction,
    actor: ActorIdentity,
    bindings: tuple[ApprovalBinding, ...],
) -> PlanResultSummary:
    result = bus.dispatch_plan(
        plan,
        approval_attestations=bindings,
        policy_version="v1",
        authenticated_actor=actor,
        approval_verifier=ApprovalVerifier(),
        deadline_monotonic_ns=time.monotonic_ns() + 10**10,
    )
    assert isinstance(result, PlanResultSummary)
    return result


def test_public_plan_path_replays_and_rehydrates_from_real_sqlite(tmp_path: Path) -> None:
    first, actor = make_bus(tmp_path)
    plan = make_plan(tmp_path, actor)
    initial = approvals(first, plan, actor, "first")
    receipt = dispatch(first, plan, actor, initial)
    expected_project, expected_history = first.project, first.history
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT status FROM plans").fetchone() == ("COMMITTED",)
        (key, status) = conn.execute(
            "SELECT effect_key, status FROM compensation_journal"
        ).fetchone()
    assert status == "VERIFIED" and (tmp_path / key).read_bytes() == b"cmd-0"
    del first
    second, actor2 = make_bus(tmp_path)
    redelivery = make_plan(tmp_path, actor2)
    # A fresh authenticated approval is deliberately required even on replay.
    replay = dispatch(second, redelivery, actor2, approvals(second, redelivery, actor2, "replay"))
    assert replay == receipt
    assert second.project == expected_project and second.history == expected_history
    assert (tmp_path / key).read_bytes() == b"cmd-0"
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT count(*) FROM compensation_journal").fetchone() == (1,)


@pytest.mark.parametrize("kind", ["old_revision", "tampered_scope", "expired"])
def test_invalid_approval_is_rejected_before_nonce_consumption(tmp_path: Path, kind: str) -> None:
    bus, actor = make_bus(tmp_path)
    plan = make_plan(tmp_path, actor)
    binding = approvals(bus, plan, actor, "nonce")[0]
    if kind == "old_revision":
        bus, actor = make_bus(tmp_path, revision=1)
        plan = make_plan(tmp_path, actor)
    elif kind == "tampered_scope":
        binding = replace(binding, plan_hash="a" * 64)
    else:
        binding = replace(binding, issued_at=time.time() - 20, expires_at=time.time() - 10)
    with pytest.raises(ApprovalBindingError):
        dispatch(bus, plan, actor, (binding,))
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT count(*) FROM consumed_nonces").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM plans").fetchone() == (0,)
    assert not list(tmp_path.glob("[0-9a-f]" * 64))


def test_unknown_middle_effect_quarantines_and_compensates_earlier_effects_lifo(
    tmp_path: Path,
) -> None:
    bus, actor = make_bus(tmp_path, fail_at="cmd-2")
    plan = make_plan(tmp_path, actor, count=4)
    with pytest.raises(CommandExecutionError, match="unknown external effect"):
        dispatch(bus, plan, actor, approvals(bus, plan, actor, "first"))
    assert bus.state_revision == 0 and bus.project.name == "before"
    assert plan.state is PlanTransactionState.ROLLBACK_FAILED
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        rows = conn.execute(
            "SELECT effect_key, status FROM compensation_journal ORDER BY entry_id"
        ).fetchall()
        assert conn.execute("SELECT status, evidence_json FROM plans").fetchone() == (
            "IN_DOUBT",
            None,
        )
    assert [row[1] for row in rows] == ["COMPENSATED", "COMPENSATED", "PENDING"]
    assert (tmp_path / "compensation.log").read_text().splitlines() == [rows[1][0], rows[0][0]]
    assert not (tmp_path / rows[0][0]).exists() and not (tmp_path / rows[1][0]).exists()
    assert (tmp_path / rows[2][0]).read_bytes() == b"cmd-2"


def test_pending_intent_cannot_be_committed(tmp_path: Path) -> None:
    store = DurablePlanDedupeStore(tmp_path / "plans.db")
    plan_hash = hashlib.sha256(b"plan").hexdigest()
    lease = store.reserve_and_start("plan", plan_hash, external_effects=True, nonce="nonce")
    store.record_compensation_intent(
        "plan",
        plan_hash,
        attempt=lease.attempt,
        owner_token=lease.owner_token,
        step_id="step",
        effect_key="b" * 64,
    )
    with pytest.raises(PlanInDoubtQuarantineError):
        store.finish(
            "plan", plan_hash, attempt=lease.attempt, owner_token=lease.owner_token, evidence={}
        )
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT status, evidence_json FROM plans").fetchone() == (
            "IN_DOUBT",
            None,
        )
        assert conn.execute("SELECT status FROM compensation_journal").fetchone() == ("PENDING",)


def test_configured_bus_cannot_bypass_durable_lifecycle(tmp_path: Path) -> None:
    bus, actor = make_bus(tmp_path)
    plan = make_plan(tmp_path, actor)
    with pytest.raises(PlanTransactionError, match="requires approval context"):
        bus.dispatch_plan(plan)
    with pytest.raises(PlanTransactionError, match="durable store configured"):
        plan.commit(bus)
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT count(*) FROM plans").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM consumed_nonces").fetchone() == (0,)
    assert bus.state_revision == 0


def test_inflight_conflict_does_not_consume_nonce_or_execute(tmp_path: Path) -> None:
    bus, actor = make_bus(tmp_path)
    plan = make_plan(tmp_path, actor)
    binding = approvals(bus, plan, actor, "dispatch")[0]
    store = DurablePlanDedupeStore(tmp_path / "plans.db")
    store.reserve_and_start(
        "plan", binding.plan_hash, project_id="project", external_effects=True, nonce="owner"
    )
    with pytest.raises(PlanExecutionInProgressError):
        dispatch(bus, plan, actor, (binding,))
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT nonce FROM consumed_nonces").fetchall() == [("owner",)]
    assert bus.state_revision == 0


def test_expired_deadline_never_marks_pending_effect_verified(tmp_path: Path) -> None:
    store = DurablePlanDedupeStore(tmp_path / "plans.db")
    plan_hash = "a" * 64
    lease = store.reserve_and_start("plan", plan_hash, external_effects=True, nonce="nonce")
    store.record_compensation_intent(
        "plan",
        plan_hash,
        step_id="step",
        effect_key="b" * 64,
        attempt=lease.attempt,
        owner_token=lease.owner_token,
    )
    with pytest.raises(TimeoutError, match="deadline"):
        store.verify_effect_intent(
            "plan",
            plan_hash,
            step_id="step",
            effect_key="b" * 64,
            verification_digest="c" * 64,
            deadline_monotonic_ns=time.monotonic_ns() - 1,
            attempt=lease.attempt,
            owner_token=lease.owner_token,
        )
    with pytest.raises(ValueError, match="deadline"):
        store.verify_effect_intent(
            "plan",
            plan_hash,
            step_id="step",
            effect_key="b" * 64,
            verification_digest="c" * 64,
            deadline_monotonic_ns=True,
            attempt=lease.attempt,
            owner_token=lease.owner_token,
        )
    assert store.compensation_journal("plan", plan_hash)[0].status == "PENDING"


def test_redelivery_requires_fresh_approval_nonce(tmp_path: Path) -> None:
    bus, actor = make_bus(tmp_path)
    plan = make_plan(tmp_path, actor)
    binding = approvals(bus, plan, actor, "first")
    dispatch(bus, plan, actor, binding)
    other, actor2 = make_bus(tmp_path)
    same = make_plan(tmp_path, actor2)
    with pytest.raises(PlanInDoubtQuarantineError):
        dispatch(other, same, actor2, binding)
    assert other.state_revision == 0
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT count(*) FROM compensation_journal").fetchone() == (1,)
