"""Free-text → verified artifact, through the cognition boundary (one slice).

The Gate-C vertical slice.  It adds **one** narrowly bounded production surface
and no new execution authority:

    user free text
      -> normalize + bound + derive an authoritative idempotency key
      -> CognitionGateway.run                 (router -> registry-derived schema
                                               -> producer -> bridge -> bus)
      -> CommandBus                           (policy + authorizer + apply)
      -> timeline.trim artifact + independent verification

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
* the source asset facts (clip id + duration) come from the **authoritative
  project state**, never from a caller default or the model — a missing/ambiguous
  source fails closed *before* any render;
* the model provider is injected (host-owned composition); this module never
  constructs one and never reads settings;
* with no model configured the slice returns an explicit clarification outcome,
  never a fabricated command (the Model Kill Test at the slice level);
* the cognition decision is recorded as a provenance observation against a real
  authoritative identity — and recording failure never changes what executes.

See ``docs/overnight/COGNITION_CONVERGENCE.md`` and ``nagar_overnight/REPORT.md``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.studio.models import Project
from nexus_ai_agent.nagar.cognition.context import CognitionContext
from nexus_ai_agent.nagar.cognition.gateway import (
    CognitionGateway,
    CognitionRefused,
)
from nexus_ai_agent.nagar.cognition.router import (
    CognitionLevel,
    IntentClass,
    RoutingRequest,
)
from nexus_ai_agent.nagar.creative.handoff import (
    MAX_INTENT_CHARS,
    TRIM_OPERATION,
    CognitionJobOutcome,
    HandoffRefusal,
    derive_intent_idempotency_key,
    normalize_intent,
    run_free_text_intent_durable,
    source_asset_facts,
)
from nexus_ai_agent.nagar.observation import (
    CognitionDecision,
    record_cognition_decision,
)


class FreeTextOutcome(BaseModel):
    """The honest result of a free-text intent.

    ``status`` is one of:

    * ``applied`` — a typed command was authorized and the real command bus
      applied it; ``result`` is the canonical output envelope.
    * ``clarification_required`` — no model was configured (or the router chose
      the human level); nothing was executed.
    * ``blocked`` — no eligible reasoning level exists at all; nothing executed.
    * ``refused`` — a model, the deterministic substrate, or missing
      authoritative facts rejected the intent; ``refusal_reason`` carries the
      stable code and nothing executed.

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
    return normalize_intent(text)


def _schema_for(gateway: CognitionGateway, operation: str, attr: str) -> dict[str, Any]:
    """A declared registry schema for ``operation``, or ``{}`` on any lookup miss.

    Derived from the *same* registry the bus authorizes against (single source of
    truth).  A lookup failure degrades the prompt only — the parser still enforces
    the schema, so a bad hint can never widen what a proposal may contain.
    """
    try:
        registry = gateway._bus_registry()
        spec = registry.get_spec(operation)
        model = getattr(spec, attr, None)
        if model is None:
            return {}
        return model.model_json_schema()
    except Exception:  # noqa: BLE001 — a missing schema is a prompt degradation, not an error
        return {}


async def run_free_text_intent(
    text: str,
    *,
    gateway: CognitionGateway,
    project: Project,
    journal: Any | None = None,
    clip_asset_id: str | None = None,
) -> FreeTextOutcome:
    """Turn free text into at most one authorized ``timeline.trim`` — or refuse.

    The gateway, the project (authoritative state) and the optional journal are
    **injected by the host composition root**; the actor/project authority lives
    inside the gateway already.  Source facts come from ``project``; the
    idempotency key is derived from the real intent identity.  Every failure is
    a typed ``FreeTextOutcome`` — the function never raises for caller input.
    """
    intent = _normalize(text)
    if not intent:
        return FreeTextOutcome(
            status="refused", refusal_reason="malformed", detail="empty intent text"
        )

    # Authoritative source facts, or fail closed before any cognition/render.
    try:
        facts = source_asset_facts(project, clip_asset_id)
    except KeyError as exc:
        return FreeTextOutcome(status="refused", refusal_reason="missing_source", detail=str(exc))

    operation = TRIM_OPERATION
    idempotency_key = derive_intent_idempotency_key(gateway.project_id, gateway.actor, intent)
    job_id = f"intent-{idempotency_key.split(':', 1)[-1]}"
    # The routing request must be honest about the configured substrate: a null
    # producer (no model configured) declares ``local_model_available=False``, so
    # the deterministic router selects the human/clarify level instead of a model
    # level that does not exist.
    model_available = bool(getattr(gateway.producer, "consults_model", False))
    request = RoutingRequest(
        intent_class=IntentClass.PARAMETRIC,
        deterministic_available=False,
        local_model_available=model_available,
        cloud_model_available=False,
        max_level=CognitionLevel.L2_LOCAL_MODEL,
        human_available=True,
    )

    # Deterministic routing decision: blocked -> refuse; human level -> clarify.
    decision = gateway.route(request)
    if decision.blocked:
        _record_refusal(journal, gateway, job_id, idempotency_key, decision.reason_code)
        return FreeTextOutcome(
            status="blocked", refusal_reason=decision.reason_code, detail=decision.explanation
        )
    if decision.level is CognitionLevel.L4_HUMAN:
        _record_refusal(journal, gateway, job_id, idempotency_key, decision.reason_code)
        return FreeTextOutcome(
            status="clarification_required",
            refusal_reason=decision.reason_code,
            detail=decision.explanation,
        )

    context = CognitionContext(
        subject_id=project.project_id,
        intent_text=intent,
        deterministic_facts=facts,
        hints={
            "operation": operation,
            "input_fields": _schema_for(gateway, operation, "input_model"),
            "output_fields": _schema_for(gateway, operation, "output_model"),
        },
    )
    try:
        result = await gateway.run(
            context,
            request,
            requested_operations=frozenset({operation}),
            idempotency_key=idempotency_key,
        )
    except CognitionRefused as exc:
        _record_refusal(journal, gateway, job_id, idempotency_key, exc.reason.value)
        return FreeTextOutcome(status="refused", refusal_reason=exc.reason.value, detail=exc.detail)

    output = dict(result.output or {})
    _record_acceptance(
        journal,
        gateway,
        job_id,
        idempotency_key,
        operation,
        {
            "command_id": str(getattr(result, "transaction_id", "") or ""),
            "operation": operation,
            "state_revision": result.state_revision,
            "state_hash": result.state_hash,
            "asset_id": output.get("asset_id"),
            "content_sha256": output.get("content_sha256"),
        },
    )
    return FreeTextOutcome(status="applied", operation=operation, result=output)


def _producer_class(gateway: CognitionGateway) -> str:
    return type(getattr(gateway, "_producer", gateway)).__name__


def _record_refusal(
    journal: Any | None,
    gateway: CognitionGateway,
    job_id: str,
    idempotency_key: str,
    reason: str,
) -> None:
    record_cognition_decision(
        journal,
        decision=CognitionDecision.REFUSED,
        producer_class=_producer_class(gateway),
        job_id=job_id,
        idempotency_key=idempotency_key,
        refusal_reason=reason,
    )


def _record_acceptance(
    journal: Any | None,
    gateway: CognitionGateway,
    job_id: str,
    idempotency_key: str,
    operation: str,
    execution_identity: dict[str, Any],
) -> None:
    record_cognition_decision(
        journal,
        decision=CognitionDecision.PROPOSAL_ACCEPTED,
        producer_class=_producer_class(gateway),
        job_id=job_id,
        idempotency_key=idempotency_key,
        operation=operation,
        execution_identity=execution_identity,
    )


def verify_trim_artifact(
    bus: Any,  # noqa: ANN401 — the real CommandBus
    outcome: FreeTextOutcome,
    *,
    artifact_path: str | None = None,
) -> dict[str, Any]:
    """Independently check an applied ``timeline.trim`` against committed state.

    The verifier re-reads the bus's committed project and confirms the derived
    asset exists with the reported hash, real lineage and a positive duration —
    it does not trust the handler's returned dict.  When ``artifact_path`` is
    given (a real file was produced), it also re-measures that file's sha256
    with the repository's canonical file hash and requires a match — so a forged
    or modified artifact turns the verdict RED.  Missing/unreadable bytes are a
    verification failure, never a pass.
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
    reported_sha = outcome.result.get("content_sha256")
    checks: dict[str, bool] = {
        "asset_present": True,
        "hash_matches": record.content_sha256 == reported_sha,
        "lineage_present": bool(record.parent_asset_ids),
        "duration_positive": (record.duration_us or 0) > 0,
    }
    measured_sha: str | None = None
    if artifact_path is not None:
        from pathlib import Path

        from nexus_ai_agent.creative.slideshow.ffmpeg import sha256_file

        try:
            measured_sha = sha256_file(Path(artifact_path))
            checks["artifact_readable"] = True
            checks["artifact_bytes_match"] = measured_sha == record.content_sha256
        except (OSError, ValueError):
            checks["artifact_readable"] = False
            checks["artifact_bytes_match"] = False
    return {
        "verified": all(checks.values()),
        "checks": checks,
        "asset_id": asset_id,
        "content_sha256": record.content_sha256,
        "measured_sha256": measured_sha,
        "parent_asset_ids": list(record.parent_asset_ids),
    }


__all__ = [
    "MAX_INTENT_CHARS",
    "TRIM_OPERATION",
    "CognitionJobOutcome",
    "FreeTextOutcome",
    "HandoffRefusal",
    "derive_intent_idempotency_key",
    "run_free_text_intent",
    "run_free_text_intent_durable",
    "source_asset_facts",
    "verify_trim_artifact",
]
