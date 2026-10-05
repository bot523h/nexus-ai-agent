"""Free-text → verified artifact, through the cognition boundary (one slice).

The Gate-C vertical slice.  It adds **one** narrowly bounded production surface
and no new execution authority:

    user free text
      -> normalize + bound                    (untrusted input, data only)
      -> CognitionGateway.run                 (router -> registry-derived schema
                                               -> producer -> bridge -> bus)
      -> CommandBus                           (policy + authorizer + apply)
      -> timeline.trim artifact + verification

The slice is deliberately tiny: one registered, ``AVAILABLE``, REVERSIBLE
operation (``timeline.trim``) that already has a canonical handler, an input
model and a bus pipeline.  No capability, policy, actor source or second bus is
added — free text is the *only* new input, and it is typed before it can reach
execution.

Hard invariants (each is a test):

* free text NEVER reaches ``CommandBus.dispatch`` — it becomes a
  ``TypedProposal`` first, and the offered set is the registry ∩
  ``{timeline.trim}``, so a model can never name another operation;
* the actor, project and authority come from the composition root, never from
  the text or the model;
* with no model configured the slice returns an explicit clarification outcome,
  never a fabricated command (the Model Kill Test at the slice level).

See ``docs/overnight/COGNITION_CONVERGENCE.md`` and ``nagar_overnight/REPORT.md``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.studio.models import ActorIdentity
from nexus_ai_agent.nagar.cognition.context import CognitionBudget, CognitionContext
from nexus_ai_agent.nagar.cognition.gateway import (
    CognitionRefused,
    build_cognition_gateway,
)
from nexus_ai_agent.nagar.cognition.router import (
    CognitionLevel,
    IntentClass,
    RoutingRequest,
)

#: The single operation this slice exposes.  It is registered, AVAILABLE and
#: REVERSIBLE in ``creative/studio/capabilities`` + the edit pack.
TRIM_OPERATION = "timeline.trim"
#: The asset id the render/bus surface uses for the project's source clip.
SOURCE_ASSET_ID = "src"
#: Bound on free-text intent length before it enters the context.
MAX_INTENT_CHARS = 500


class FreeTextOutcome(BaseModel):
    """The honest result of a free-text intent.

    ``status`` is one of:

    * ``applied`` — a typed command was authorized the real command bus applied
      it; ``result`` is the canonical output envelope.
    * ``clarification_required`` — no model was configured (or the router chose
      the human level); nothing was executed.
    * ``blocked`` — no eligible reasoning level exists at all; nothing executed.
    * ``refused`` — a model or the deterministic substrate rejected the
      candidate; ``refusal_reason`` carries the stable :class:`RefusalReason`
      value and nothing executed.

    There is no ``applied`` outcome without a real bus apply: the slice never
    fabricates one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["applied", "clarification_required", "blocked", "refused"]
    operation: str | None = None
    result: dict[str, Any] | None = None
    refusal_reason: str | None = None
    detail: str = Field(default="", max_length=2_000)


def _normalize(text: str) -> str:
    """Collapse whitespace and bound the length.  Untrusted text stays data."""
    if not isinstance(text, str):
        raise TypeError("intent text must be a string")
    return " ".join(text.split())[:MAX_INTENT_CHARS]


def _offered_input_schema(bus: Any, operation: str) -> dict[str, Any]:  # noqa: ANN401
    """The operation's input schema, derived from the registry the bus uses.

    Derived (never hand-written) so the producer is prompted with the *real*
    field names and the slice stays a single source of truth.  A lookup failure
    yields ``{}`` — the prompt degrades, the parser still enforces the schema.
    """
    registry = getattr(bus, "_registry", None)
    if registry is None:
        return {}
    try:
        spec = registry.get_spec(operation)
    except Exception:  # noqa: BLE001 — a missing spec is a prompt degradation, not an error
        return {}
    return spec.input_model.model_json_schema()


async def run_free_text_intent(
    text: str,
    *,
    bus: Any,  # noqa: ANN401 — the real CommandBus; typed loosely to avoid a circular import
    actor: ActorIdentity,
    project_id: str,
    enabled: bool = False,
    provider: Any | None = None,  # noqa: ANN401 — an LLMProvider-shaped generator
    duration_us: int = 0,
    clip_asset_id: str = SOURCE_ASSET_ID,
    idempotency_key: str | None = None,
    budget: CognitionBudget | None = None,
) -> FreeTextOutcome:
    """Turn free text into at most one authorized ``timeline.trim`` — or refuse.

    ``enabled`` + ``provider`` are passed explicitly by the composition root
    (dependency injection, no global config).  A disabled flag or a missing
    provider selects the null producer, so the worst case is a clarification,
    never a fabricated command.
    """
    intent = _normalize(text)
    if not intent:
        return FreeTextOutcome(
            status="refused", refusal_reason="malformed", detail="empty intent text"
        )

    gateway = build_cognition_gateway(
        bus=bus,
        actor=actor,
        project_id=project_id,
        enabled=enabled,
        provider=provider,
        budget=budget,
    )
    model_available = enabled and provider is not None
    request = RoutingRequest(
        intent_class=IntentClass.PARAMETRIC,
        deterministic_available=False,
        local_model_available=model_available,
        cloud_model_available=False,
        max_level=CognitionLevel.L2_LOCAL_MODEL,
        human_available=True,
    )

    # Read the deterministic routing decision to translate a refusal into the
    # honest user-facing status.  The gateway re-routes identically (pure).
    decision = gateway.route(request)
    if decision.blocked:
        return FreeTextOutcome(
            status="blocked", refusal_reason=decision.reason_code, detail=decision.explanation
        )
    if decision.level is CognitionLevel.L4_HUMAN:
        return FreeTextOutcome(
            status="clarification_required",
            refusal_reason=decision.reason_code,
            detail=decision.explanation,
        )

    context = CognitionContext(
        subject_id=project_id,
        intent_text=intent,
        deterministic_facts={
            "clip_asset_id": clip_asset_id,
            "duration_us": int(duration_us),
        },
        hints={
            "operation": TRIM_OPERATION,
            "input_fields": _offered_input_schema(bus, TRIM_OPERATION),
        },
    )
    try:
        result = await gateway.run(
            context,
            request,
            requested_operations=frozenset({TRIM_OPERATION}),
            idempotency_key=idempotency_key,
        )
    except CognitionRefused as exc:
        return FreeTextOutcome(status="refused", refusal_reason=exc.reason.value, detail=exc.detail)
    return FreeTextOutcome(
        status="applied", operation=TRIM_OPERATION, result=dict(result.output or {})
    )


def build_provider(settings: Any | None = None) -> Any | None:  # noqa: ANN401
    """Composition root for the (optional) model provider.

    The **only** place a provider is constructed for this slice.  It is
    dependency-injected downward; nothing above it imports a concrete provider,
    so no other module can create a raw-model path.  Returns ``None`` when no
    settings/provider is available — the slice then routes to the null producer
    and refuses, rather than fabricating an answer.

    Provider credentials never leave this function: only the returned provider
    object flows onward, and nothing here logs settings.
    """
    if settings is None:
        from nexus_ai_agent.config.settings import get_settings

        settings = get_settings()
    try:
        from nexus_ai_agent.llm.litellm_provider import build_llm_provider

        provider, _label = build_llm_provider(settings)
        return provider
    except Exception:  # noqa: BLE001 — no provider available must not crash the slice
        return None


def verify_trim_artifact(bus: Any, outcome: FreeTextOutcome) -> dict[str, Any]:  # noqa: ANN401
    """Independently check an applied ``timeline.trim`` against committed state.

    The verifier re-reads the bus's committed project and confirms the derived
    asset exists with the reported hash, real lineage and a positive duration —
    it does not trust the handler's returned dict.  This is the *judgment* step
    that keeps the producing handler from being the sole judge of its artifact.
    """
    if outcome.status != "applied" or not outcome.result:
        return {"verified": False, "reason": "no applied artifact to verify"}
    asset_id = outcome.result.get("asset_id")
    if not asset_id:
        return {"verified": False, "reason": "outcome carries no asset_id"}
    project = bus.project
    record = next((a for a in project.assets if a.asset_id == asset_id), None)
    if record is None:
        return {"verified": False, "reason": f"asset {asset_id!r} absent from committed state"}
    checks = {
        "asset_present": True,
        "hash_matches": record.content_sha256 == outcome.result.get("content_sha256"),
        "lineage_present": bool(record.parent_asset_ids),
        "duration_positive": (record.duration_us or 0) > 0,
    }
    return {
        "verified": all(checks.values()),
        "checks": checks,
        "asset_id": asset_id,
        "content_sha256": record.content_sha256,
        "parent_asset_ids": list(record.parent_asset_ids),
    }


__all__ = [
    "MAX_INTENT_CHARS",
    "SOURCE_ASSET_ID",
    "TRIM_OPERATION",
    "FreeTextOutcome",
    "build_provider",
    "run_free_text_intent",
    "verify_trim_artifact",
]
