"""Cognition gateway — the one reachable path from a model to an execution.

This is the composition-root adapter the mission calls for.  It composes the
pieces that already exist and adds **no new abstraction**:

    intent
      -> determine offered operations from the CapabilityRegistry  (authority)
      -> DeterministicRouter decides *how* to answer                (pure)
      -> CognitionPort produces a TypedProposal | Refusal           (untrusted)
      -> proposal_to_command builds a *candidate* command           (pure)
      -> CommandBus authorizes + applies, or denies                 (authority)

Invariants (each is a test):

* **never MODEL -> EXECUTION** — a model's text becomes a proposal only through
  :func:`parse_proposal`, and a proposal becomes an execution only through the
  real ``CommandBus`` (registry + policy + authorizer).  There is no other
  call from here into execution.
* the offered operations come from the registry, never from the caller or the
  model; a caller may only *narrow* them.
* a router decision that is ``blocked`` produces a typed refusal — the gateway
  never falls through to a raw-model path.
* the actor/project/operation-schema-version are supplied by the composition
  root, never by the proposal; a proposal cannot self-grant.
* provider/parse failures are already typed refusals from the port and pass
  through unchanged.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    AuthorizationError,
    CommandResult,
    NagarError,
)
from nexus_ai_agent.nagar.cognition.bridge import proposal_to_command
from nexus_ai_agent.nagar.cognition.capabilities import (
    offered_operations,
    offered_operations_within,
)
from nexus_ai_agent.nagar.cognition.context import (
    CognitionBudget,
    CognitionContext,
    ProposalSchema,
)
from nexus_ai_agent.nagar.cognition.port import CognitionPort
from nexus_ai_agent.nagar.cognition.proposal import Refusal, RefusalReason, TypedProposal
from nexus_ai_agent.nagar.cognition.router import (
    CognitionLevel,
    DeterministicRouter,
    RoutingDecision,
    RoutingRequest,
)

#: Version of the schema descriptor this gateway hands to the producer.
GATEWAY_PROPOSAL_SCHEMA_ID = "nagar.gateway.proposal.v1"
GATEWAY_PROPOSAL_SCHEMA_VERSION = 1

#: Only these router levels justify spending a model.  L0/L1 are deterministic
#: and are executed by the caller's deterministic path; L4 asks a human.
_MODEL_LEVELS: tuple[CognitionLevel, ...] = (
    CognitionLevel.L2_LOCAL_MODEL,
    CognitionLevel.L3_CLOUD_MODEL,
)


class CognitionRefused(NagarError):
    """A typed, structured refusal from the gateway.

    Raised when the gateway cannot produce an *accepted, executable* command:
    the router blocked, the producer refused, or the producer returned nothing
    parseable.  It carries the same :class:`RefusalReason` taxonomy so callers
    can branch on a stable code instead of a message.
    """

    def __init__(self, reason: RefusalReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


class CognitionGateway:
    """Turns untrusted cognition into at most one authorized command.

    The gateway holds the *composition-root* facts (registry, bus, actor,
    project, router).  A cognition producer is injected and replaceable — the
    same null/local/cloud provider the port already defines.
    """

    def __init__(
        self,
        *,
        bus: Any,
        producer: CognitionPort,
        actor: ActorIdentity,
        project_id: str,
        router: DeterministicRouter | None = None,
        schema_id: str = GATEWAY_PROPOSAL_SCHEMA_ID,
        budget: CognitionBudget | None = None,
    ) -> None:
        self._bus = bus
        self._producer = producer
        self._actor = actor
        self._project_id = project_id
        self._router = router if router is not None else DeterministicRouter()
        self._schema_id = schema_id
        self._budget = budget if budget is not None else CognitionBudget()

    @property
    def actor(self) -> ActorIdentity:
        """The host-owned actor every proposal-derived command runs as."""
        return self._actor

    @property
    def project_id(self) -> str:
        """The host-owned project every proposal-derived command targets."""
        return self._project_id

    @property
    def producer(self) -> CognitionPort:
        """The configured producer (for honest routing/model-availability checks)."""
        return self._producer

    # -- the offered set is authoritative: from the registry only ------------

    def offered(self, requested: frozenset[str] | None = None) -> frozenset[str]:
        registry = self._bus_registry()
        if requested is None:
            return offered_operations(registry)
        return offered_operations_within(registry, requested)

    def _bus_registry(self) -> Any:
        # CommandBus owns the registry it authorizes against; read the *same*
        # registry so the offered set can never drift from the allow-list.
        registry = getattr(self._bus, "_registry", None)
        if registry is None:
            raise CognitionRefused(
                RefusalReason.SCHEMA_VIOLATION,
                "command bus has no registry; refusing to offer operations",
            )
        return registry

    def _schema(self, requested: frozenset[str] | None) -> ProposalSchema:
        return ProposalSchema(
            schema_id=self._schema_id,
            schema_version=GATEWAY_PROPOSAL_SCHEMA_VERSION,
            allowed_operations=self.offered(requested),
        )

    # -- routing -------------------------------------------------------------

    def route(self, request: RoutingRequest) -> RoutingDecision:
        """Deterministic, observable routing decision (no model call)."""
        return self._router.route(request)

    # -- the one path --------------------------------------------------------

    async def run(
        self,
        context: CognitionContext,
        request: RoutingRequest,
        *,
        requested_operations: frozenset[str] | None = None,
        idempotency_key: str | None = None,
        reason: str | None = None,
    ) -> CommandResult:
        """Route, propose, bridge, and dispatch — or raise :class:`CognitionRefused`.

        There is deliberately no fall-back path here: if the router blocks, if
        the producer refuses, or if the command is not authorized, the result is
        a typed refusal, never a raw-model execution.
        """
        decision = self._router.route(request)
        if decision.blocked:
            raise CognitionRefused(
                RefusalReason.PRODUCER_REFUSED,
                f"router blocked reasoning: {decision.reason_code}",
            )
        if decision.level not in _MODEL_LEVELS:
            # Deterministic/recipe/human levels are handled by the caller's
            # deterministic path, not by asking a model.  The gateway only ever
            # spends a model when the router explicitly selected one.
            raise CognitionRefused(
                RefusalReason.PRODUCER_REFUSED,
                f"router selected {decision.level.value}; no model call is warranted",
            )

        schema = self._schema(requested_operations)
        if not schema.allowed_operations:
            raise CognitionRefused(
                RefusalReason.DISALLOWED_OPERATION,
                "the registry offers no operation for this request",
            )

        outcome = await self._producer.propose(
            context, schema, self._budget, idempotency_key=idempotency_key
        )
        if isinstance(outcome, Refusal):
            raise CognitionRefused(
                outcome.reason, outcome.detail or "the cognition producer refused"
            )
        if not isinstance(outcome, TypedProposal):
            # Defensive: a misbehaving producer must never slip through.
            raise CognitionRefused(
                RefusalReason.MALFORMED,
                f"producer returned {type(outcome).__name__}, expected TypedProposal|Refusal",
            )

        try:
            command = proposal_to_command(
                outcome,
                actor=self._actor,
                project_id=self._project_id,
                operation_schema_version=self._operation_schema_version(outcome.operation),
                idempotency_key=idempotency_key,
                reason=reason,
            )
            return self._bus.dispatch(command)
        except AuthorizationError as exc:
            raise CognitionRefused(RefusalReason.AUTHORITY_FIELD, str(exc)) from exc
        except NagarError as exc:
            # Any deterministic rejection (unknown operation, schema, permission,
            # policy, precondition, reference, idempotency) is a refusal — never
            # a raw-model execution, never an unhandled crash.
            raise CognitionRefused(RefusalReason.DENIED, str(exc)) from exc

    def _operation_schema_version(self, operation: str) -> int:
        spec = self._bus_registry().get_spec(operation)
        return int(spec.schema_version)


def build_cognition_gateway(
    *,
    bus: Any,
    actor: ActorIdentity,
    project_id: str,
    enabled: bool,
    completion: Any | None = None,
    provider: Any | None = None,
    router: DeterministicRouter | None = None,
    budget: CognitionBudget | None = None,
) -> CognitionGateway:
    """Fail-closed gateway selector for a composition root.

    ``enabled`` selects the producer; it never selects authority — both
    producers hand any proposal through the *same* ``CommandBus`` pipeline.

    * ``enabled and completion is not None`` -> :class:`LocalCognition` over the
      injected completion port (the only place a model is ever consulted);
    * otherwise -> :class:`NullCognition`, which always refuses.  A disabled
      flag or a missing provider can therefore never fall back to raw-model
      execution; the worst case is an explicit refusal.

    ``completion`` is the canonical, ``LLMPort``-shaped dependency and is the
    only name a production composition root should pass.  ``provider`` is a
    deprecated alias kept for existing callers/tests; it is used only when
    ``completion`` is absent.  No configuration object is read here: the
    composition root passes explicit values (dependency injection, no global
    singleton).
    """
    # Imported lazily so the selector stays import-light and the null path
    # never drags the adapter in.
    from nexus_ai_agent.nagar.cognition.adapter import LocalCognition
    from nexus_ai_agent.nagar.cognition.null import NullCognition

    model_port = completion if completion is not None else provider
    producer = (
        LocalCognition(model_port) if (enabled and model_port is not None) else NullCognition()
    )
    return CognitionGateway(
        bus=bus,
        producer=producer,
        actor=actor,
        project_id=project_id,
        router=router,
        budget=budget,
    )


__all__ = [
    "GATEWAY_PROPOSAL_SCHEMA_ID",
    "GATEWAY_PROPOSAL_SCHEMA_VERSION",
    "CognitionGateway",
    "CognitionRefused",
    "build_cognition_gateway",
]
