"""CP1b's public plan entry point, using real SQLite and real file effects.

Identity is a test double. The cooperative file destination holds a real SQLite
write transaction during fenced file creation; it is not a production destination.
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
    PlanStoreStateError,
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
    store = DurablePlanDedupeStore(root / "plans.db")

    def handler(state: Project, context: OperationContext) -> OperationOutcome:
        assert context.effect_key is not None and context.durable_heartbeat is not None
        context.durable_heartbeat()
        path = root / context.effect_key
        # The cooperative test destination serializes fence validation and
        # publication with all SQLite-mediated lease takeovers.
        with store._transaction() as conn:
            live = conn.execute(
                "SELECT 1 FROM plans p JOIN compensation_journal j "
                "ON p.project_id=j.project_id AND p.plan_id=j.plan_id AND p.plan_hash=j.plan_hash "
                "WHERE p.project_id=? AND p.plan_id=? AND p.plan_hash=? AND p.status='EXECUTING' "
                "AND p.attempt=? AND p.owner_token=? AND p.lease_expires_at>? "
                "AND j.attempt=p.attempt AND j.effect_key=? AND j.status='PENDING'",
                (
                    context.durable_project_id,
                    context.durable_plan_id,
                    context.durable_plan_hash,
                    context.fencing_epoch,
                    context.durable_owner_token,
                    store._now(),
                    context.effect_key,
                ),
            ).fetchone()
            if live is None:
                raise RuntimeError("file effect fenced")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, context.command.command_id.encode())
                os.fsync(fd)
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
            durable_dedupe_store=store,
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
        status, owner_token, evidence = conn.execute(
            "SELECT status, owner_token, evidence_json FROM plans"
        ).fetchone()
        assert status == "COMMITTED" and owner_token not in evidence
        (key, status) = conn.execute(
            "SELECT effect_key, status FROM compensation_journal"
        ).fetchone()
    assert status == "VERIFIED" and (tmp_path / key).read_bytes() == b"cmd-0"
    del first
    configured, actor2 = make_bus(tmp_path)
    second = CommandBus(
        None,
        registry=configured.registry,
        authorizer=ProjectAccess(actor2, "project", frozenset({"project:read"})),
        durable_dedupe_store=DurablePlanDedupeStore(tmp_path / "plans.db"),
        durable_verifiers=configured._durable_verifiers,
    )
    with pytest.raises(PlanTransactionError, match="project is not loaded"):
        _ = second.project
    redelivery = make_plan(tmp_path, actor2)
    replay = dispatch(second, redelivery, actor2, initial)
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


def test_redelivery_accepts_same_plan_approval_nonce(tmp_path: Path) -> None:
    bus, actor = make_bus(tmp_path)
    plan = make_plan(tmp_path, actor)
    binding = approvals(bus, plan, actor, "first")
    receipt = dispatch(bus, plan, actor, binding)
    other, actor2 = make_bus(tmp_path)
    same = make_plan(tmp_path, actor2)
    assert dispatch(other, same, actor2, binding) == receipt
    assert other.project == bus.project
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT count(*) FROM compensation_journal").fetchone() == (1,)


def test_old_project_replays_from_authorized_ancestor(tmp_path: Path) -> None:
    bus, actor = make_bus(tmp_path)
    template = make_plan(tmp_path, actor)
    command = TypedCommand(
        command_id="earlier", operation="example.play", actor=actor, idempotency_key="earlier"
    )
    earlier = PlanTransaction(
        plan_id="earlier", steps=(replace(template.steps[0], command=command),)
    )
    dispatch(bus, earlier, actor, approvals(bus, earlier, actor, "previous"))
    plan = make_plan(tmp_path, actor)
    binding = approvals(bus, plan, actor, "original")
    receipt = dispatch(bus, plan, actor, binding)
    stale, actor2 = make_bus(tmp_path)
    assert stale.state_revision == 0
    assert dispatch(stale, make_plan(tmp_path, actor2), actor2, binding) == receipt
    assert stale.project == bus.project and stale.history == bus.history


def test_cold_replay_refuses_bad_authority_or_receipt_without_publishing_state(
    tmp_path: Path,
) -> None:
    first, actor = make_bus(tmp_path)
    plan = make_plan(tmp_path, actor)
    binding = approvals(first, plan, actor, "original")
    dispatch(first, plan, actor, binding)
    cold = CommandBus(
        None,
        registry=first.registry,
        authorizer=ProjectAccess(actor, "project", frozenset({"project:read"})),
        durable_store=DurablePlanDedupeStore(tmp_path / "plans.db"),
        durable_verifiers=first._durable_verifiers,
    )

    class Denied:
        def verify(self, _binding: ApprovalBinding) -> bool:
            return False

    with pytest.raises(ApprovalBindingError):
        cold.dispatch_plan(
            make_plan(tmp_path, actor),
            approval_attestations=binding,
            policy_version="v1",
            authenticated_actor=actor,
            approval_verifier=Denied(),
            deadline_monotonic_ns=time.monotonic_ns() + 10**10,
        )
    with pytest.raises(PlanTransactionError, match="not loaded"):
        _ = cold.project
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        conn.execute("UPDATE plans SET evidence_json='{}'")
    with pytest.raises(PlanStoreStateError, match="invalid durable receipt"):
        dispatch(cold, make_plan(tmp_path, actor), actor, binding)
    with pytest.raises(PlanTransactionError, match="not loaded"):
        _ = cold.project


def test_nonce_binding_requires_matching_committed_plan_and_legacy_unknown_fails_closed(
    tmp_path: Path,
) -> None:
    store = DurablePlanDedupeStore(tmp_path / "plans.db")
    h = "a" * 64
    for name in ("first", "second"):
        lease = store.reserve_and_start(name, h, external_effects=False, nonce=name)
        store.finish(
            name, h, attempt=lease.attempt, owner_token=lease.owner_token, evidence={"ok": True}
        )
    with pytest.raises(PlanStoreStateError, match="another or unknown plan"):
        store.replay_committed("second", h, external_effects=False, nonce="first")
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        conn.execute("DELETE FROM nonce_plan_bindings WHERE nonce='second'")
    with pytest.raises(PlanStoreStateError, match="another or unknown plan"):
        store.replay_committed("second", h, external_effects=False, nonce="second")


def test_cooperative_file_destination_fences_expired_owner_after_takeover(tmp_path: Path) -> None:
    """An actual SQLite writer lock overlaps an attempted takeover.

    The clock and repair authority are test doubles. SQLite locking, threads,
    file creation, fsync and publication are real; this says nothing about a
    non-cooperating writer or an arbitrary external destination.
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeoutError
    from threading import Event

    from nexus_ai_agent.creative.studio.durable_dedupe import PlanFencingTokenMismatchError

    clock = [100.0]
    path = tmp_path / "plans.db"
    store = DurablePlanDedupeStore(
        path, clock=lambda: clock[0], repair_authorizer=lambda _p, _o, _s, _a: True
    )
    rival = DurablePlanDedupeStore(path, clock=lambda: clock[0])
    h = "a" * 64
    old = store.reserve_and_start(
        "plan", h, external_effects=True, nonce="old", lease_ttl_seconds=1
    )
    store.record_compensation_intent(
        "plan",
        h,
        attempt=old.attempt,
        owner_token=old.owner_token,
        step_id="old",
        effect_key="b" * 64,
    )
    checked, takeover_started, release_writer = Event(), Event(), Event()
    destination = tmp_path / "destination"
    staging = tmp_path / "old-staging"

    def old_writer() -> None:
        try:
            # BEGIN IMMEDIATE is held across both fence checks and publication.
            with store._transaction() as conn:
                valid = conn.execute(
                    "SELECT 1 FROM plans p JOIN compensation_journal j "
                    "ON p.project_id=j.project_id AND p.plan_id=j.plan_id "
                    "AND p.plan_hash=j.plan_hash WHERE p.project_id=? AND p.plan_id=? "
                    "AND p.plan_hash=? AND p.status='EXECUTING' AND p.attempt=? "
                    "AND p.owner_token=? AND p.lease_expires_at>? AND j.attempt=p.attempt "
                    "AND j.effect_key=? AND j.status='PENDING'",
                    ("default", "plan", h, old.attempt, old.owner_token, store._now(), "b" * 64),
                ).fetchone()
                assert valid is not None
                with staging.open("xb") as file:
                    file.write(b"stale")
                    file.flush()
                    os.fsync(file.fileno())
                checked.set()
                assert release_writer.wait(timeout=5)
                expiry = conn.execute(
                    "SELECT lease_expires_at FROM plans WHERE project_id=? AND plan_id=? "
                    "AND status='EXECUTING' AND attempt=? AND owner_token=?",
                    ("default", "plan", old.attempt, old.owner_token),
                ).fetchone()
                if expiry is None or store._now() >= expiry[0]:
                    raise PlanFencingTokenMismatchError("file writer lease expired")
                os.replace(staging, destination)
        finally:
            staging.unlink(missing_ok=True)

    def take_over() -> None:
        # The contender itself has tried SQLite's write lock before signalling.
        with sqlite3.connect(path, timeout=0, isolation_level=None) as conn:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                conn.execute("BEGIN IMMEDIATE")
        takeover_started.set()
        rival.reserve_and_start("plan", h, external_effects=True, nonce="new")

    with ThreadPoolExecutor(max_workers=2) as pool:
        old_task = pool.submit(old_writer)
        try:
            assert checked.wait(timeout=5)
            assert staging.read_bytes() == b"stale"
            takeover = pool.submit(take_over)
            assert takeover_started.wait(timeout=5)
            # A third real SQLite connection confirms the old worker owns the
            # write lock, independent of thread scheduling or a fake store.
            with sqlite3.connect(path, timeout=0, isolation_level=None) as observer:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    observer.execute("BEGIN IMMEDIATE")
            with pytest.raises(FutureTimeoutError):
                takeover.result(timeout=0.2)
            clock[0] = 102.0
        finally:
            release_writer.set()
        with pytest.raises(PlanFencingTokenMismatchError, match="expired"):
            old_task.result(timeout=5)
        with pytest.raises(PlanInDoubtQuarantineError):
            takeover.result(timeout=5)

    assert not staging.exists() and not destination.exists()
    store.repair_in_doubt(
        "default",
        "plan",
        h,
        expected_status="IN_DOUBT",
        expected_attempt=old.attempt,
        operator_id="operator",
        resolution_action="authorize_takeover",
        reason="investigated",
    )
    new = store.reserve_and_start("plan", h, external_effects=True, nonce="new")
    assert new.attempt == old.attempt + 1 and new.owner_token != old.owner_token
    # The old writer published nothing, including while the takeover waited.
    assert not destination.exists()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM compensation_journal").fetchone() == ("PENDING",)


def test_same_approval_cold_replay_survives_process_restart(tmp_path: Path) -> None:
    import json
    import subprocess
    import sys

    first, actor = make_bus(tmp_path)
    plan = make_plan(tmp_path, actor)
    binding = approvals(first, plan, actor, "initial")
    receipt = dispatch(first, plan, actor, binding)
    script = """
import json, runpy, sys
from pathlib import Path
m = runpy.run_path("tests/unit/test_w4_cp1_durable_dedupe_and_approvals.py")
root = Path(sys.argv[1])
configured, actor = m["make_bus"](root)
access = m["ProjectAccess"](actor, "project", frozenset({"project:read"}))
bus = m["CommandBus"](None, registry=configured.registry, authorizer=access,
    durable_store=m["DurablePlanDedupeStore"](root / "plans.db"),
    durable_verifiers=configured._durable_verifiers)
plan = m["make_plan"](root, actor)
binding = (m["ApprovalBinding"](**json.loads(sys.argv[2])),)
result = m["dispatch"](bus, plan, actor, binding)
print(json.dumps({"project": bus.project.model_dump(mode="json"),
                  "revision": result.committed_revision}))
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), json.dumps(asdict(binding[0]))],
        check=True,
        text=True,
        capture_output=True,
    )
    result = json.loads(child.stdout)
    assert result["project"] == first.project.model_dump(mode="json")
    assert result["revision"] == receipt.committed_revision
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT count(*) FROM compensation_journal").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM consumed_nonces").fetchone() == (1,)


def test_repeat_same_request_on_current_bus_is_a_replay(tmp_path: Path) -> None:
    bus, actor = make_bus(tmp_path)
    first = make_plan(tmp_path, actor)
    bindings = approvals(bus, first, actor, "initial")
    receipt = dispatch(bus, first, actor, bindings)
    assert dispatch(bus, make_plan(tmp_path, actor), actor, bindings) == receipt
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT count(*) FROM compensation_journal").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM consumed_nonces").fetchone() == (1,)


def test_cold_replay_does_not_overwrite_a_divergent_project(tmp_path: Path) -> None:
    bus, actor = make_bus(tmp_path)
    plan = make_plan(tmp_path, actor)
    bindings = approvals(bus, plan, actor, "initial")
    dispatch(bus, plan, actor, bindings)
    stale, actor2 = make_bus(tmp_path, revision=7)
    before = stale.project
    with pytest.raises(PlanStoreStateError, match="invalid durable receipt"):
        dispatch(stale, make_plan(tmp_path, actor2), actor2, bindings)
    assert stale.project == before


def test_preupgrade_unbound_nonce_cannot_authorize_same_plan_replay(tmp_path: Path) -> None:
    store = DurablePlanDedupeStore(tmp_path / "plans.db")
    lease = store.reserve_and_start("plan", "a" * 64, external_effects=False, nonce="old")
    store.finish(
        "plan", "a" * 64, attempt=lease.attempt, owner_token=lease.owner_token, evidence={}
    )
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        conn.execute("DELETE FROM nonce_plan_bindings")
    with pytest.raises(PlanStoreStateError, match="another or unknown plan"):
        DurablePlanDedupeStore(tmp_path / "plans.db").replay_committed(
            "plan", "a" * 64, external_effects=False, nonce="old"
        )


def test_cold_bus_cannot_execute_new_plan_without_a_project(tmp_path: Path) -> None:
    configured, actor = make_bus(tmp_path)
    plan = make_plan(tmp_path, actor)
    binding = approvals(configured, plan, actor, "nonce")
    cold = CommandBus(
        None,
        registry=configured.registry,
        authorizer=ProjectAccess(actor, "project", frozenset({"project:read"})),
        durable_store=DurablePlanDedupeStore(tmp_path / "plans.db"),
        durable_verifiers=configured._durable_verifiers,
    )
    with pytest.raises(PlanTransactionError, match="not loaded"):
        dispatch(cold, plan, actor, binding)
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT count(*) FROM plans").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM consumed_nonces").fetchone() == (0,)


def test_ibpc_mac_verifier_binds_entire_approval_and_bus_execution(tmp_path: Path) -> None:
    """Real IBPC MAC and disk, with a test issuer and a synthetic project grant."""
    from datetime import datetime, timezone

    from nexus_ai_agent.adapters.ibpc_plan_approval import IBPCPlanApprovalVerifier
    from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing, issue_envelope

    bus, actor = make_bus(tmp_path)
    plan = make_plan(tmp_path, actor)
    binding = approvals(bus, plan, actor, "signed")[0]
    keys = StaticKeyRing({"active": b"k" * 32}, active_key_id="active")
    issue = datetime.fromtimestamp(binding.issued_at, timezone.utc).isoformat()
    expires = datetime.fromtimestamp(binding.expires_at, timezone.utc).isoformat()
    envelope = issue_envelope(
        keys,
        approval_id=binding.approval_id,
        payload=asdict(binding),
        issued_at=issue,
        expires_at=expires,
    )
    verifier = IBPCPlanApprovalVerifier(keys, {binding.approval_id: envelope})
    assert verifier.verify(binding)
    assert not verifier.verify(object())
    assert not IBPCPlanApprovalVerifier(
        keys, {binding.approval_id: replace(envelope, issued_at="not-a-timestamp")}
    ).verify(binding)
    assert not IBPCPlanApprovalVerifier(
        keys, {binding.approval_id: replace(envelope, approval_id="different")}
    ).verify(binding)
    # A change to any field invalidates the actual MAC, not only bus preflight.
    for altered in (
        replace(binding, project_id="other"),
        replace(binding, plan_id="other"),
        replace(binding, actor_id="mallory"),
        replace(binding, plan_hash="f" * 64),
        replace(binding, step_id="step-9"),
        replace(binding, capability_version="other"),
        replace(binding, normalized_params_hash="e" * 64),
        replace(binding, policy_version="other"),
        replace(binding, nonce="other"),
        replace(binding, issued_at=binding.issued_at - 1),
        replace(binding, expires_at=binding.expires_at + 1),
    ):
        assert not verifier.verify(altered)
    assert not IBPCPlanApprovalVerifier(
        StaticKeyRing({"active": b"x" * 32}, active_key_id="active"),
        {binding.approval_id: envelope},
    ).verify(binding)
    assert not IBPCPlanApprovalVerifier(keys, {}).verify(binding)
    assert not IBPCPlanApprovalVerifier(
        keys,
        {binding.approval_id: envelope},
        now=lambda: datetime.fromtimestamp(binding.expires_at, timezone.utc),
    ).verify(binding)
    assert not IBPCPlanApprovalVerifier(
        keys,
        {binding.approval_id: envelope},
        now=lambda: datetime.fromtimestamp(binding.issued_at - 1, timezone.utc),
    ).verify(binding)
    assert not IBPCPlanApprovalVerifier(
        keys,
        {binding.approval_id: envelope},
        now=lambda: datetime(2026, 1, 1),
    ).verify(binding)
    forged = replace(binding, normalized_params_hash="e" * 64)
    with pytest.raises(ApprovalBindingError):
        bus.dispatch_plan(
            plan,
            approval_attestations=(forged,),
            policy_version="v1",
            authenticated_actor=actor,
            approval_verifier=verifier,
            deadline_monotonic_ns=time.monotonic_ns() + 10**10,
        )
    with pytest.raises(ApprovalBindingError):
        bus.dispatch_plan(
            plan,
            approval_attestations=(binding,),
            policy_version="v1",
            authenticated_actor=ActorIdentity(kind="user", actor_id="mallory"),
            approval_verifier=verifier,
            deadline_monotonic_ns=time.monotonic_ns() + 10**10,
        )
    from nexus_ai_agent.creative.studio.models import AuthorizationError

    foreign_bus = CommandBus(
        bus.project,
        registry=bus.registry,
        authorizer=ProjectAccess(actor, "other-project", frozenset({"project:read"})),
        durable_store=DurablePlanDedupeStore(tmp_path / "plans.db"),
        durable_verifiers=bus._durable_verifiers,
    )
    with pytest.raises(AuthorizationError):
        foreign_bus.dispatch_plan(
            plan,
            approval_attestations=(binding,),
            policy_version="v1",
            authenticated_actor=actor,
            approval_verifier=verifier,
            deadline_monotonic_ns=time.monotonic_ns() + 10**10,
        )
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT count(*) FROM consumed_nonces").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM plans").fetchone() == (0,)
    result = bus.dispatch_plan(
        plan,
        approval_attestations=(binding,),
        policy_version="v1",
        authenticated_actor=actor,
        approval_verifier=verifier,
        deadline_monotonic_ns=time.monotonic_ns() + 10**10,
    )
    assert isinstance(result, PlanResultSummary)
    assert bus.project.name == "after"
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT status FROM plans").fetchone() == ("COMMITTED",)
        assert conn.execute("SELECT status FROM compensation_journal").fetchone() == ("VERIFIED",)


@pytest.mark.parametrize(
    ("effect_key", "epoch", "status", "digest"),
    [
        ("bad", 1, "ABSENT", "c" * 64),
        ("a" * 64, True, "ABSENT", "c" * 64),
        ("a" * 64, 0, "ABSENT", "c" * 64),
        ("a" * 64, 1, "INVALID", "c" * 64),
        ("a" * 64, 1, "UNKNOWN", "c" * 64),
        ("a" * 64, 1, "ABSENT", "not-a-digest"),
    ],
)
def test_effect_observation_rejects_malformed_evidence(
    effect_key: str, epoch: object, status: object, digest: str
) -> None:
    from typing import Any, cast

    from nexus_ai_agent.creative.studio.durable_dedupe import ExternalEffectObservation

    with pytest.raises(ValueError):
        ExternalEffectObservation(effect_key, cast(Any, epoch), cast(Any, status), digest)


def test_effect_observation_is_only_a_claim_not_verification() -> None:
    from nexus_ai_agent.creative.studio.durable_dedupe import ExternalEffectObservation

    observed = ExternalEffectObservation("a" * 64, 3, "PRESENT", "b" * 64)
    assert observed.status == "PRESENT" and observed.fencing_epoch == 3
    assert ExternalEffectObservation("a" * 64, 3, "UNKNOWN", "").evidence_digest == ""


def test_recovery_inspects_real_pending_effect_without_executing_or_repairing(
    tmp_path: Path,
) -> None:
    """Disk inspection is real; operator and destination authorities are test doubles."""
    from nexus_ai_agent.creative.studio.durable_dedupe import (
        ExternalEffectObservation,
    )
    from nexus_ai_agent.creative.studio.models import AuthorizationError

    bus, actor = make_bus(tmp_path, fail_at="cmd-0")
    plan = make_plan(tmp_path, actor)
    binding = approvals(bus, plan, actor, "initial")
    with pytest.raises(CommandExecutionError, match="unknown external effect"):
        dispatch(bus, plan, actor, binding)
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        (key,) = conn.execute("SELECT effect_key FROM compensation_journal").fetchone()
    assert (tmp_path / key).read_bytes() == b"cmd-0"

    class FileInspector:
        def apply(
            self, *, effect_key: str, fencing_epoch: int, payload: bytes
        ) -> ExternalEffectObservation:
            raise AssertionError("inspection must never execute an effect")

        def inspect(self, *, effect_key: str, fencing_epoch: int) -> ExternalEffectObservation:
            data = (tmp_path / effect_key).read_bytes()
            return ExternalEffectObservation(
                effect_key, fencing_epoch, "PRESENT", hashlib.sha256(data).hexdigest()
            )

    class IndependentFileVerifier:
        def verify(self, observation: ExternalEffectObservation) -> bool:
            return (
                hashlib.sha256((tmp_path / observation.effect_key).read_bytes()).hexdigest()
                == observation.evidence_digest
            )

    inspector = FileInspector()
    verifier = IndependentFileVerifier()
    bus._authorizer = ProjectAccess(actor, "project", frozenset({"project:repair"}))
    with pytest.raises(AuthorizationError):
        bus.inspect_durable_recovery(
            plan.plan_id,
            binding[0].plan_hash,
            authenticated_operator=ActorIdentity(kind="user", actor_id="mallory"),
            expected_status="IN_DOUBT",
            expected_attempt=1,
            destinations={"step-0": inspector},
            verifier=verifier,
        )
    with pytest.raises(PlanStoreStateError, match="expected status/attempt"):
        bus.inspect_durable_recovery(
            plan.plan_id,
            binding[0].plan_hash,
            authenticated_operator=actor,
            expected_status="IN_DOUBT",
            expected_attempt=2,
            destinations={"step-0": inspector},
            verifier=verifier,
        )
    with pytest.raises(PlanStoreStateError, match="destination is unavailable"):
        bus.inspect_durable_recovery(
            plan.plan_id,
            binding[0].plan_hash,
            authenticated_operator=actor,
            expected_status="IN_DOUBT",
            expected_attempt=1,
            destinations={},
            verifier=verifier,
        )

    class RefusingVerifier:
        def verify(self, _observation: ExternalEffectObservation) -> bool:
            return False

    class MismatchedInspector(FileInspector):
        def inspect(self, *, effect_key: str, fencing_epoch: int) -> ExternalEffectObservation:
            return ExternalEffectObservation("f" * 64, fencing_epoch, "UNKNOWN", "")

    with pytest.raises(PlanStoreStateError, match="does not match intent"):
        bus.inspect_durable_recovery(
            plan.plan_id,
            binding[0].plan_hash,
            authenticated_operator=actor,
            expected_status="IN_DOUBT",
            expected_attempt=1,
            destinations={"step-0": MismatchedInspector()},
            verifier=verifier,
        )
    with pytest.raises(PlanStoreStateError, match="not independently verified"):
        bus.inspect_durable_recovery(
            plan.plan_id,
            binding[0].plan_hash,
            authenticated_operator=actor,
            expected_status="IN_DOUBT",
            expected_attempt=1,
            destinations={"step-0": inspector},
            verifier=RefusingVerifier(),
        )
    observed = bus.inspect_durable_recovery(
        plan.plan_id,
        binding[0].plan_hash,
        authenticated_operator=actor,
        expected_status="IN_DOUBT",
        expected_attempt=1,
        destinations={"step-0": inspector},
        verifier=verifier,
    )
    assert len(observed) == 1 and observed[0].status == "PRESENT"
    assert observed[0].effect_key == key
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT status, evidence_json FROM plans").fetchone() == (
            "IN_DOUBT",
            None,
        )
        assert conn.execute("SELECT status FROM compensation_journal").fetchone() == ("PENDING",)
    assert (tmp_path / key).read_bytes() == b"cmd-0"


@pytest.mark.parametrize("publish_first", [False, True])
def test_bus_inspects_real_fenced_destination_without_repair(
    tmp_path: Path, publish_first: bool
) -> None:
    """Real bus, SQLite and filesystem destination; identity and approval are mocks."""
    from nexus_ai_agent.adapters.fenced_filesystem_destination import (
        FencedFilesystemEffectDestination,
        FilesystemObservationVerifier,
        StaleEpochError,
    )

    destination = FencedFilesystemEffectDestination(tmp_path, project_id="project")
    independent_reader = FilesystemObservationVerifier(tmp_path, project_id="project")
    actor = ActorIdentity(kind="user", actor_id="alice")
    project = new_project("project", "before", Timeline(timeline_id="timeline", duration_us=0))
    registry = CapabilityRegistry()

    def handler(_state: Project, context: OperationContext) -> OperationOutcome:
        assert context.effect_key is not None and context.fencing_epoch is not None
        if publish_first:
            destination.apply(
                effect_key=context.effect_key,
                fencing_epoch=context.fencing_epoch,
                payload=b"effect",
            )
        raise RuntimeError("simulated handler interruption")

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
    store = DurablePlanDedupeStore(tmp_path / "plans.db")
    bus = CommandBus(
        project,
        registry=registry,
        authorizer=ProjectAccess(actor, "project", frozenset({"project:read", "project:repair"})),
        durable_dedupe_store=store,
        durable_verifiers={"example.play": lambda _p, _c, _r, _k: False},
    )
    plan = make_plan(tmp_path, actor)
    binding = approvals(bus, plan, actor, "initial")
    with pytest.raises(CommandExecutionError, match="simulated handler interruption"):
        dispatch(bus, plan, actor, binding)
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT status, attempt FROM plans").fetchone() == ("IN_DOUBT", 1)
        (effect_key,) = conn.execute("SELECT effect_key FROM compensation_journal").fetchone()
    observed = bus.inspect_durable_recovery(
        plan.plan_id,
        binding[0].plan_hash,
        authenticated_operator=actor,
        expected_status="IN_DOUBT",
        expected_attempt=1,
        destinations={"step-0": destination},
        verifier=independent_reader,
    )
    assert len(observed) == 1
    assert observed[0].effect_key == effect_key
    assert observed[0].status == ("PRESENT" if publish_first else "ABSENT")
    if not publish_first:
        with pytest.raises(StaleEpochError):
            destination.apply(effect_key=effect_key, fencing_epoch=1, payload=b"late")
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        assert conn.execute("SELECT status, attempt FROM plans").fetchone() == ("IN_DOUBT", 1)
        assert conn.execute("SELECT status FROM compensation_journal").fetchone() == ("PENDING",)
        assert conn.execute("SELECT count(*) FROM plan_repair_events").fetchone() == (0,)


def test_store_rejects_invalid_inspection_and_corrupt_committed_receipts(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanReplayConflictError

    store = DurablePlanDedupeStore(tmp_path / "plans.db")
    for status, attempt in (("EXECUTING", 1), ("IN_DOUBT", True), ("IN_DOUBT", -1)):
        with pytest.raises(ValueError):
            store.quarantined_journal(
                "p", "a" * 64, expected_status=status, expected_attempt=attempt
            )
    lease = store.reserve_and_start("p", "a" * 64, external_effects=True, nonce="n")
    with pytest.raises(ValueError, match="effect_key"):
        store.verify_effect_intent(
            "p",
            "a" * 64,
            attempt=lease.attempt,
            owner_token=lease.owner_token,
            step_id="step",
            effect_key="bad",
            verification_digest="b" * 64,
        )
    store.finish("p", "a" * 64, attempt=lease.attempt, owner_token=lease.owner_token, evidence={})
    with pytest.raises(PlanReplayConflictError):
        store.find_committed("p", "b" * 64, project_id="default")
    with sqlite3.connect(tmp_path / "plans.db") as conn:
        conn.execute("UPDATE plans SET evidence_json=NULL")
    with pytest.raises(PlanStoreStateError, match="receipt is missing"):
        store.find_committed("p", "a" * 64, project_id="default")


def test_store_rolls_back_when_takeover_cas_is_suppressed_by_database_trigger(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plans.db"
    store = DurablePlanDedupeStore(path, repair_authorizer=lambda *_args: True)
    lease = store.reserve_and_start("p", "a" * 64, external_effects=True, nonce="first")
    store.quarantine("p", "a" * 64, attempt=lease.attempt, owner_token=lease.owner_token)
    store.repair_in_doubt(
        "default",
        "p",
        "a" * 64,
        expected_status="IN_DOUBT",
        expected_attempt=1,
        operator_id="operator",
        resolution_action="authorize_takeover",
        reason="test authority",
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TRIGGER stop_start BEFORE UPDATE OF status ON plans "
            "WHEN NEW.status='EXECUTING' BEGIN SELECT RAISE(IGNORE); END"
        )
    with pytest.raises(PlanStoreStateError, match="takeover changed"):
        store.reserve_and_start("p", "a" * 64, external_effects=True, nonce="second")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status, attempt FROM plans").fetchone() == ("RESERVED", 1)
        assert conn.execute("SELECT nonce FROM consumed_nonces").fetchall() == [("first",)]


def test_store_rolls_back_when_repair_cas_is_suppressed_by_database_trigger(tmp_path: Path) -> None:
    path = tmp_path / "plans.db"
    store = DurablePlanDedupeStore(path, repair_authorizer=lambda *_args: True)
    lease = store.reserve_and_start("p", "a" * 64, external_effects=True, nonce="first")
    store.quarantine("p", "a" * 64, attempt=lease.attempt, owner_token=lease.owner_token)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TRIGGER stop_repair BEFORE UPDATE OF status ON plans "
            "WHEN NEW.status='RESERVED' BEGIN SELECT RAISE(IGNORE); END"
        )
    with pytest.raises(PlanStoreStateError, match="plan changed while repairing"):
        store.repair_in_doubt(
            "default",
            "p",
            "a" * 64,
            expected_status="IN_DOUBT",
            expected_attempt=1,
            operator_id="operator",
            resolution_action="authorize_takeover",
            reason="test authority",
        )
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status, attempt FROM plans").fetchone() == ("IN_DOUBT", 1)
        assert conn.execute("SELECT count(*) FROM plan_repair_events").fetchone() == (0,)
