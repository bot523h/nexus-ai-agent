"""Trusted bus approval scope derivation; no durable execution yet."""

from dataclasses import asdict, replace
from typing import Any

import pytest

from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.bus import (
    CommandBus,
    PlanStep,
    PlanTransaction,
    PlanTransactionError,
)
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    PlayCommandInput,
)
from nexus_ai_agent.creative.studio.durable_dedupe import ApprovalBinding, ApprovalBindingError
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    AuthorizationError,
    PermissionLevel,
    Project,
    Timeline,
    TypedCommand,
    new_project,
)

ACTOR = ActorIdentity(kind="user", actor_id="alice")
PLAY = "example.play"


class Verifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.seen: list[ApprovalBinding] = []

    def verify(self, binding: ApprovalBinding) -> bool:
        self.seen.append(binding)
        return self.ok


def make_bus() -> CommandBus:
    project = new_project("project", "Demo", Timeline(timeline_id="timeline", duration_us=0))
    access = ProjectAccess(
        actor=ACTOR, project_id="project", permissions=frozenset({"project:read"})
    )
    registry = CapabilityRegistry()

    def handler(state: Project, context: OperationContext) -> OperationOutcome:
        return OperationOutcome(state, context.history, {"ok": True})

    registry.register_operation(
        "example",
        "playback",
        OperationSpec(
            PLAY,
            "test",
            PermissionLevel.IMMEDIATE,
            PlayCommandInput,
            handler,
            reference_fields=("start",),
        ),
    )
    return CommandBus(project, registry=registry, authorizer=access)


def make_plan(count: int = 1, *, actor: ActorIdentity = ACTOR) -> PlanTransaction:
    return PlanTransaction(
        plan_id="plan",
        steps=tuple(
            PlanStep(
                PLAY,
                command=TypedCommand(command_id=f"command-{i}", operation=PLAY, actor=actor),
            )
            for i in range(count)
        ),
    )


def approvals(bus: CommandBus, plan: PlanTransaction) -> tuple[ApprovalBinding, ...]:
    requirements = bus.durable_approval_requirements(
        plan, policy_version="policy-v1", authenticated_actor=ACTOR
    )
    return tuple(
        ApprovalBinding(
            **asdict(item),
            approval_id=f"approval-{i}",
            nonce=f"nonce-{i}",
            issued_at=1,
            expires_at=10,
        )
        for i, item in enumerate(requirements)
    )


def test_bus_derives_scopes_and_verifies_approvals_without_effects() -> None:
    bus = make_bus()
    plan = make_plan(2)
    before = (bus.project, bus.history)
    bindings = approvals(bus, plan)
    verifier = Verifier()
    scopes = bus.verify_durable_approvals(
        plan,
        bindings,
        policy_version="policy-v1",
        authenticated_actor=ACTOR,
        now=5,
        verifier=verifier,
    )
    assert [r.step_id for r in scopes] == ["step-0", "step-1"]
    assert len({r.plan_hash for r in scopes}) == 1
    assert all(
        r.actor_id == "alice" and r.project_id == "project" and r.capability_version == "1.0.0"
        for r in scopes
    )
    assert tuple(verifier.seen) == bindings
    assert (bus.project, bus.history) == before
    assert plan.state.value == "PENDING"


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", "other"),
        ("plan_id", "other"),
        ("plan_hash", "b" * 64),
        ("actor_id", "mallory"),
        ("step_id", "step-9"),
        ("capability_version", "2.0.0"),
        ("normalized_params_hash", "c" * 64),
        ("policy_version", "old"),
    ],
)
def test_bus_rejects_tampered_approval_before_effect(field: str, value: Any) -> None:
    bus = make_bus()
    plan = make_plan()
    binding = replace(approvals(bus, plan)[0], **{field: value})
    verifier = Verifier()
    with pytest.raises(ApprovalBindingError, match="match"):
        bus.verify_durable_approvals(
            plan,
            (binding,),
            policy_version="policy-v1",
            authenticated_actor=ACTOR,
            now=5,
            verifier=verifier,
        )
    assert not verifier.seen and bus.state_revision == 0


def test_bus_refuses_expired_untrusted_and_reused_nonces() -> None:
    bus = make_bus()
    plan = make_plan(2)
    b1, b2 = approvals(bus, plan)
    with pytest.raises(ApprovalBindingError, match="unique"):
        bus.verify_durable_approvals(
            plan,
            (b1, replace(b2, nonce=b1.nonce)),
            policy_version="policy-v1",
            authenticated_actor=ACTOR,
            now=5,
            verifier=Verifier(),
        )
    with pytest.raises(ApprovalBindingError, match="one approval"):
        bus.verify_durable_approvals(
            plan,
            (b1,),
            policy_version="policy-v1",
            authenticated_actor=ACTOR,
            now=5,
            verifier=Verifier(),
        )
    with pytest.raises(ApprovalBindingError, match="currently valid"):
        bus.verify_durable_approvals(
            plan,
            (b1, b2),
            policy_version="policy-v1",
            authenticated_actor=ACTOR,
            now=10,
            verifier=Verifier(),
        )
    with pytest.raises(ApprovalBindingError, match="authority"):
        bus.verify_durable_approvals(
            plan,
            (b1, b2),
            policy_version="policy-v1",
            authenticated_actor=ACTOR,
            now=5,
            verifier=Verifier(False),
        )


def test_bus_refuses_unauthenticated_or_unregistered_callbacks() -> None:
    bus = make_bus()
    fake = make_plan(actor=ActorIdentity(kind="user", actor_id="mallory"))
    with pytest.raises(AuthorizationError):
        bus.durable_approval_requirements(
            fake, policy_version="policy-v1", authenticated_actor=ACTOR
        )
    plain = CommandBus(bus.project)
    with pytest.raises(AuthorizationError, match="trusted"):
        plain.durable_approval_requirements(
            make_plan(), policy_version="policy-v1", authenticated_actor=ACTOR
        )
    plan = PlanTransaction(
        steps=(PlanStep("custom", execute=lambda: 1, compensate=lambda _: None),)
    )
    with pytest.raises(PlanTransactionError, match="arbitrary"):
        bus.durable_approval_requirements(
            plan, policy_version="policy-v1", authenticated_actor=ACTOR
        )


def test_approval_preflight_rejects_invalid_lifecycle_and_shapes() -> None:
    from nexus_ai_agent.creative.studio.bus import PlanTransactionState
    from nexus_ai_agent.creative.studio.models import CommandValidationError

    bus = make_bus()
    with pytest.raises(CommandValidationError, match="PlanTransaction"):
        bus.durable_approval_requirements(
            None,  # type: ignore[arg-type]
            policy_version="policy-v1",
            authenticated_actor=ACTOR,
        )
    plan = make_plan()
    plan.state = PlanTransactionState.EXECUTING
    with pytest.raises(PlanTransactionError, match="not available"):
        bus.durable_approval_requirements(
            plan, policy_version="policy-v1", authenticated_actor=ACTOR
        )
    with pytest.raises(PlanTransactionError, match="128"):
        bus.durable_approval_requirements(
            make_plan(129), policy_version="policy-v1", authenticated_actor=ACTOR
        )
    with pytest.raises(ApprovalBindingError, match="invalid approval"):
        bus.verify_durable_approvals(
            make_plan(),
            (None,),  # type: ignore[arg-type]
            policy_version="policy-v1",
            authenticated_actor=ACTOR,
            now=5,
            verifier=Verifier(),
        )


def test_approval_preflight_requires_actor_and_nonopaque_callback() -> None:
    bus = make_bus()
    command = TypedCommand(command_id="missing-actor", operation=PLAY)
    with pytest.raises(AuthorizationError, match="authenticated actor"):
        bus.durable_approval_requirements(
            PlanTransaction(steps=(PlanStep(PLAY, command=command),)),
            policy_version="policy-v1",
            authenticated_actor=ACTOR,
        )
    marker = object()

    def compensate(_: Any) -> None:
        _ = marker

    typed = TypedCommand(command_id="known-actor", operation=PLAY, actor=ACTOR)
    with pytest.raises(PlanTransactionError, match="opaque"):
        bus.durable_approval_requirements(
            PlanTransaction(steps=(PlanStep(PLAY, command=typed, compensate=compensate),)),
            policy_version="policy-v1",
            authenticated_actor=ACTOR,
        )


@pytest.mark.parametrize("start_kind", ["text", "playhead"])
def test_preflight_binds_resolved_reference_and_current_state(start_kind: str) -> None:
    bus = make_bus()
    start: Any = (
        "here" if start_kind == "text" else bus.project.timeline.playhead.model_dump(mode="json")
    )
    command = TypedCommand(command_id="ref", operation=PLAY, actor=ACTOR, input={"start": start})
    plan = PlanTransaction(plan_id="ref-plan", steps=(PlanStep(PLAY, command=command),))
    binding = approvals(bus, plan)[0]
    # A changed state cannot reuse the previously approved plan fingerprint.
    bus.dispatch(TypedCommand(command_id="other", operation=PLAY, actor=ACTOR))
    with pytest.raises(ApprovalBindingError, match="match"):
        bus.verify_durable_approvals(
            plan,
            (binding,),
            policy_version="policy-v1",
            authenticated_actor=ACTOR,
            now=5,
            verifier=Verifier(),
        )


def test_preflight_binds_actor_to_authenticated_context_and_authorizer() -> None:
    bus = make_bus()
    mallory = ActorIdentity(kind="user", actor_id="mallory")
    with pytest.raises(AuthorizationError, match="authenticated actor"):
        bus.durable_approval_requirements(
            make_plan(actor=mallory), policy_version="policy-v1", authenticated_actor=ACTOR
        )
    with pytest.raises(AuthorizationError, match="not authorized"):
        bus.durable_approval_requirements(
            make_plan(actor=mallory), policy_version="policy-v1", authenticated_actor=mallory
        )
    with pytest.raises(AuthorizationError, match="context"):
        bus.durable_approval_requirements(
            make_plan(),
            policy_version="policy-v1",
            authenticated_actor=None,  # type: ignore[arg-type]
        )


def test_preflight_revalidates_forged_actor_model() -> None:
    forged = ACTOR.model_copy(update={"kind": "forged"})
    with pytest.raises(AuthorizationError, match="invalid"):
        make_bus().durable_approval_requirements(
            make_plan(), policy_version="policy-v1", authenticated_actor=forged
        )
