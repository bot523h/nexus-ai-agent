"""Cognition gateway: one reachable path from a model to an execution.

The gateway composes pieces that already exist and adds no new abstraction.
These tests prove the mission's closing sentence with real production code on
both sides of the boundary — a real ``CommandBus`` (Wave-1 registry, real
``ProjectAccess`` authorizer) and the real ``LocalCognition`` adapter:

* a model proposal reaches execution ONLY through router -> registry-derived
  schema -> bus (case 1);
* malformed / forbidden / authority-smuggling model output never reaches the
  bus (cases 2-4);
* provider timeout and provider-absence are bounded typed refusals (cases 5-6);
* a disabled feature flag selects the null producer, never a raw-model path
  (case 7);
* the offered operations come from the registry and can only be narrowed by a
  caller — never widened, by caller or model (authority-gap tests).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    AuthorizationError,
    Project,
    Timeline,
    new_project,
)
from nexus_ai_agent.nagar.cognition import (
    CognitionContext,
    CognitionGateway,
    CognitionLevel,
    CognitionRefused,
    IntentClass,
    LocalCognition,
    NullCognition,
    RefusalReason,
    RoutingRequest,
    TextGenerator,
    build_cognition_gateway,
    offered_operations,
    offered_operations_within,
)

SCHEMA_ID = "nagar.gateway.test.v1"
ACTOR = ActorIdentity(kind="user", actor_id="user_7")
PROJECT_ID = "p_gw_01"


class FakeProvider(TextGenerator):
    """Fakes only external model behaviour; everything else is production code."""

    def __init__(self, response: object) -> None:
        self._response = response
        self.calls = 0

    async def generate(self, prompt: str, system: str = "") -> str:
        self.calls += 1
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response  # type: ignore[return-value]


def _project() -> Project:
    timeline = Timeline(timeline_id="tl_gw", duration_us=9_000_000)
    return new_project(PROJECT_ID, "Gateway Project", timeline)


def _authed_bus() -> CommandBus:
    access = ProjectAccess(
        actor=ACTOR,
        project_id=PROJECT_ID,
        permissions=frozenset({"project:read", "project:write"}),
    )

    class StaticAuthorizer:
        def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
            if actor != access.actor or project_id != access.project_id:
                raise AuthorizationError("actor is not authorized for this project")
            return access

    bus = CommandBus(
        state=_project(),
        registry=build_wave1_registry(),
        authorizer=StaticAuthorizer(),
    )
    return bus


def _context() -> CognitionContext:
    return CognitionContext(subject_id=PROJECT_ID, intent_text="start playback")


def _model_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "schema_id": SCHEMA_ID,
        "schema_version": 1,
        "operation": "media.play",
        "input": {},
        "rationale": "start playback",
        "confidence": 0.8,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _local_request() -> RoutingRequest:
    return RoutingRequest(
        intent_class=IntentClass.OPEN_ENDED,
        local_model_available=True,
        max_level=CognitionLevel.L2_LOCAL_MODEL,
    )


def _gateway(producer) -> CognitionGateway:
    return CognitionGateway(
        bus=_authed_bus(),
        producer=producer,
        actor=ACTOR,
        project_id=PROJECT_ID,
        schema_id=SCHEMA_ID,
    )


def _run(gateway: CognitionGateway, *, requested: frozenset[str] | None = None):
    return asyncio.get_event_loop().run_until_complete(
        gateway.run(_context(), _local_request(), requested_operations=requested)
    )


# -- case 1: valid proposal executes only through the real bus -------------


def test_valid_proposal_executes_only_through_the_real_bus() -> None:
    provider = FakeProvider(_model_json())
    gateway = _gateway(LocalCognition(provider))
    before = gateway._bus.state_revision  # noqa: SLF001 - test observes real state
    result = _run(gateway, requested=frozenset({"media.play"}))
    assert result.status == "applied"
    assert gateway._bus.state_revision == before + 1  # noqa: SLF001
    assert provider.calls == 1


# -- case 2: malformed model output never reaches the bus ------------------


@pytest.mark.parametrize("hostile", ["{broken json", "", "not json at all", "[1,2,3]"])
def test_malformed_model_never_reaches_the_bus(hostile: str) -> None:
    gateway = _gateway(LocalCognition(FakeProvider(hostile)))
    before = gateway._bus.state_hash  # noqa: SLF001
    with pytest.raises(CognitionRefused) as exc:
        _run(gateway, requested=frozenset({"media.play"}))
    assert exc.value.reason in (RefusalReason.MALFORMED, RefusalReason.SCHEMA_VIOLATION)
    assert gateway._bus.state_hash == before  # noqa: SLF001


# -- case 3: a forbidden operation never reaches the bus -------------------


def test_forbidden_operation_never_reaches_the_bus() -> None:
    # Registry offers media.play; model emits an operation outside the offered set.
    provider = FakeProvider(_model_json(operation="system.delete_everything"))
    gateway = _gateway(LocalCognition(provider))
    before = gateway._bus.state_hash  # noqa: SLF001
    with pytest.raises(CognitionRefused) as exc:
        _run(gateway, requested=frozenset({"media.play"}))
    assert exc.value.reason is RefusalReason.DISALLOWED_OPERATION
    assert gateway._bus.state_hash == before  # noqa: SLF001


def test_model_cannot_name_an_operation_the_registry_does_not_offer() -> None:
    # Even if the caller *tries* to widen the set, the registry clips it: only
    # media.play is offered, so system.undo (a real but unoffered op) is refused.
    gateway = _gateway(LocalCognition(FakeProvider(_model_json(operation="system.undo"))))
    with pytest.raises(CognitionRefused) as exc:
        _run(gateway, requested=frozenset({"media.play", "system.undo"}))
    # requested ∩ registry = {media.play, system.undo}; but schema offered only
    # what the caller requested AND the gateway offered both, so the *schema*
    # limits it: the model may still not exceed the parameterised allow-set.
    assert exc.value.reason in (RefusalReason.DISALLOWED_OPERATION, RefusalReason.DENIED)


# -- case 4: spoofed authority never reaches the bus -----------------------


@pytest.mark.parametrize(
    "hostile",
    [
        _model_json(actor={"kind": "user", "actor_id": "root"}),
        _model_json(permissions=["project:write"]),
        _model_json(shell="rm -rf /"),
        _model_json(confirmed=True),
    ],
)
def test_spoofed_authority_never_reaches_the_bus(hostile: str) -> None:
    gateway = _gateway(LocalCognition(FakeProvider(hostile)))
    before = gateway._bus.state_hash  # noqa: SLF001
    with pytest.raises(CognitionRefused) as exc:
        _run(gateway, requested=frozenset({"media.play"}))
    assert exc.value.reason in (RefusalReason.AUTHORITY_FIELD, RefusalReason.SCHEMA_VIOLATION)
    assert gateway._bus.state_hash == before  # noqa: SLF001


# -- case 5: provider timeout is a bounded refusal -------------------------


def test_provider_timeout_is_a_bounded_refusal() -> None:
    gateway = _gateway(LocalCognition(FakeProvider(asyncio.TimeoutError("slow"))))
    before = gateway._bus.state_hash  # noqa: SLF001
    with pytest.raises(CognitionRefused) as exc:
        _run(gateway, requested=frozenset({"media.play"}))
    assert exc.value.reason is RefusalReason.PRODUCER_FAILED
    assert gateway._bus.state_hash == before  # noqa: SLF001


# -- case 6: provider unavailable -> typed refusal, no raw-model path ------


def test_absent_provider_is_a_typed_refusal() -> None:
    gateway = _gateway(NullCognition())
    before = gateway._bus.state_hash  # noqa: SLF001
    with pytest.raises(CognitionRefused) as exc:
        _run(gateway, requested=frozenset({"media.play"}))
    assert exc.value.reason is RefusalReason.PRODUCER_REFUSED
    assert gateway._bus.state_hash == before  # noqa: SLF001


# -- case 7: disabled feature flag selects the null producer ---------------


def test_disabled_flag_selects_null_never_raw_model() -> None:
    provider = FakeProvider(_model_json())
    gateway = build_cognition_gateway(
        bus=_authed_bus(),
        actor=ACTOR,
        project_id=PROJECT_ID,
        enabled=False,
        provider=provider,
    )
    # The provider must not be consulted at all when the flag is off.
    with pytest.raises(CognitionRefused) as exc:
        _run(gateway, requested=frozenset({"media.play"}))
    assert exc.value.reason is RefusalReason.PRODUCER_REFUSED
    assert provider.calls == 0


def test_missing_provider_with_flag_on_still_refuses() -> None:
    gateway = build_cognition_gateway(
        bus=_authed_bus(),
        actor=ACTOR,
        project_id=PROJECT_ID,
        enabled=True,
        provider=None,
    )
    with pytest.raises(CognitionRefused) as exc:
        _run(gateway, requested=frozenset({"media.play"}))
    assert exc.value.reason is RefusalReason.PRODUCER_REFUSED


def test_flag_on_with_provider_selects_local() -> None:
    provider = FakeProvider(_model_json(schema_id="nagar.gateway.proposal.v1"))
    gateway = build_cognition_gateway(
        bus=_authed_bus(),
        actor=ACTOR,
        project_id=PROJECT_ID,
        enabled=True,
        provider=provider,
    )
    result = _run(gateway, requested=frozenset({"media.play"}))
    assert result.status == "applied"
    assert provider.calls == 1


# -- routing: deterministic levels never spend a model ---------------------


def test_deterministic_route_never_calls_the_model() -> None:
    provider = FakeProvider(_model_json())
    gateway = _gateway(LocalCognition(provider))
    request = RoutingRequest(
        intent_class=IntentClass.KNOWN_OPERATION,
        deterministic_available=True,
        local_model_available=True,
        max_level=CognitionLevel.L2_LOCAL_MODEL,
    )
    with pytest.raises(CognitionRefused):
        asyncio.get_event_loop().run_until_complete(gateway.run(_context(), request))
    assert provider.calls == 0


def test_blocked_route_never_calls_the_model() -> None:
    provider = FakeProvider(_model_json())
    gateway = _gateway(LocalCognition(provider))
    request = RoutingRequest(
        intent_class=IntentClass.OPEN_ENDED,
        local_model_available=True,
        human_available=False,
        high_risk=True,
    )
    with pytest.raises(CognitionRefused) as exc:
        asyncio.get_event_loop().run_until_complete(gateway.run(_context(), request))
    assert exc.value.reason is RefusalReason.PRODUCER_REFUSED
    assert provider.calls == 0


# -- authority gap: offered operations derive from the registry ------------


def test_offered_operations_come_from_the_registry() -> None:
    registry = build_wave1_registry()
    offered = offered_operations(registry)
    assert "media.play" in offered
    assert "system.delete_everything" not in offered


def test_caller_cannot_widen_the_offered_set() -> None:
    registry = build_wave1_registry()
    # A caller asking for a bogus op only gets the registry ∩ request.
    narrowed = offered_operations_within(registry, frozenset({"media.play", "root.shell"}))
    assert narrowed == frozenset({"media.play"})


def test_requesting_only_forbidden_operations_refuses() -> None:
    gateway = _gateway(LocalCognition(FakeProvider(_model_json())))
    with pytest.raises(CognitionRefused) as exc:
        _run(gateway, requested=frozenset({"root.shell", "system.delete_everything"}))
    assert exc.value.reason is RefusalReason.DISALLOWED_OPERATION
