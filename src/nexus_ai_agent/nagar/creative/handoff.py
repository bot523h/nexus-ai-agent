"""Durable cognition -> creative-queue handoff (Gate C step 2).

The Gate-C slice originally turned an accepted proposal into an *inline*
``CommandBus.dispatch``.  This module is its durable successor: the cognition
boundary only **proposes** (:meth:`CognitionGateway.propose` — no dispatch), and
the proposal becomes a typed, canonical ``creative_render`` payload that is
persisted through the **existing** ``JobQueuePort`` and executed by the
**existing** ``creative_render_job`` worker.

    free text
      -> normalize + bound
      -> deterministic route
      -> model (propose only; never dispatch)
      -> TypedProposal
      -> typed canonical handoff (``CreativeRenderPayload``; model picks points only)
      -> ``JobQueuePort.enqueue``                 (durable SQLite row)
      -> ``InProcessJobQueue`` reservation/fencing
      -> ``creative_render_job``                  (existing worker)
      -> existing CommandBus (registry/policy/authorization)
      -> existing render lane (FFmpeg) -> measured artifact
      -> registered independent verifier -> durable result

The slice owns no bus, no registry, no authorizer, no queue and no verifier:
every one of those is the *existing* one.  The model can choose exactly the
trim in/out points; the operation is a closed mapping, the actor/project/queue
identity are composition-root facts, and authority is applied once, inside the
worker's ``CommandBus``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: The single canonical operation this slice can hand off.  It is registered,
#: AVAILABLE and REVERSIBLE in the edit pack; the worker's closed
#: ``SURFACE_TO_CANONICAL`` maps ``("edit", "trim")`` to it.
TRIM_OPERATION = "timeline.trim"
#: Surface (command, operation) the canonical worker accepts for a trim.
TRIM_COMMAND = "edit"
TRIM_SURFACE_OPERATION = "trim"
#: The queue job type the existing worker registers (``worker.py``).
CREATIVE_RENDER_JOB_TYPE = "creative_render"
#: Bound on free-text intent length before it enters the context.
MAX_INTENT_CHARS = 500
#: The one asset id the canonical worker's self-contained project exposes.
WORKER_SOURCE_ASSET_ID = "src"


class HandoffRefusal(ValueError):
    """A typed, caller-facing refusal *before* anything is enqueued.

    A refusal never enqueues, never dispatches and never mutates state: it is
    the honest "I will not turn this into a job" answer.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


class CognitionJobOutcome(BaseModel):
    """The honest result of a free-text intent through the durable handoff.

    ``status`` is one of:

    * ``enqueued`` — a typed canonical ``creative_render`` payload was persisted
      through the existing queue; ``job_id`` is the durable row id.
    * ``clarification_required`` — no model was configured (or the router chose
      the human level); nothing was enqueued.
    * ``blocked`` — no eligible reasoning level exists at all; nothing enqueued.
    * ``refused`` — the model, the deterministic substrate, missing authoritative
      facts, or the typed handoff rejected the intent; ``refusal_reason`` carries
      the stable code and nothing was enqueued.

    There is no ``enqueued`` outcome without a real ``queue.enqueue``: the slice
    never fabricates one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["enqueued", "clarification_required", "blocked", "refused"]
    job_id: str | None = None
    operation: str | None = None
    idempotency_key: str | None = None
    refusal_reason: str | None = None
    detail: str = Field(default="", max_length=2_000)


def normalize_intent(text: str) -> str:
    """Collapse whitespace and bound the length.  Untrusted text stays data."""
    if not isinstance(text, str):
        raise TypeError("intent text must be a string")
    return " ".join(text.split())[:MAX_INTENT_CHARS]


def derive_intent_idempotency_key(project_id: str, actor: Any, intent: str) -> str:
    """A deterministic, content-addressed identity for one free-text intent.

    Redelivering the *same* intent by the *same* actor in the *same* project
    reproduces the key, so the durable queue collapses the retry into one logical
    job (``nexus_job_queue.idempotency_key`` is UNIQUE).  A different intent is a
    different key — never a collision.  The raw text is hashed, not stored, so no
    prompt leaks into an identifier.
    """
    import hashlib

    seed = f"{project_id}\x00{actor.kind}\x00{actor.actor_id}\x00{intent}"
    return "cognition:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def durable_job_identity(idempotency_key: str) -> str:
    """The *intent-scoped* observation id for a durable handoff.

    This is deliberately **not** the queue ``job_id`` (a row uuid) and **not**
    the worker ``command_id`` (derived from the same key).  It is a stable label
    under which the cognition decision is recorded against the durable job, so
    the causal journal can reference an identity that exists before the row does.
    """
    return f"intent-{idempotency_key.split(':', 1)[-1]}"


def source_asset_facts(project: Any, clip_asset_id: str | None) -> dict[str, Any]:
    """The **authoritative** source facts for a trim, or raise ``KeyError``.

    Derives the source clip and its duration from committed project state — the
    slice's single source of truth for "what are we trimming".  The model never
    invents a duration and a caller default never stands in for unknown metadata.
    Ambiguity (no clip named, no such asset, zero duration) fails closed at the
    caller, which turns the ``KeyError`` into a refusal *before* any enqueue.
    """
    assets = {asset.asset_id: asset for asset in project.assets}
    if clip_asset_id:
        asset = assets.get(clip_asset_id)
        if asset is None:
            raise KeyError(f"clip asset {clip_asset_id!r} is not in the project")
    else:
        candidates = [a for a in project.assets if a.media_kind == "video" and a.duration_us > 0]
        if len(candidates) != 1:
            raise KeyError(f"expected exactly one source video asset, found {len(candidates)}")
        asset = candidates[0]
    if asset.duration_us <= 0:
        raise KeyError(f"source asset {asset.asset_id!r} has no measured duration")
    return {"clip_asset_id": asset.asset_id, "duration_us": int(asset.duration_us)}


def stage_source_media(source_path: str, workspace_dir: str) -> str:
    """Copy a real source file into the job's guarded workspace as ``input.mp4``.

    The workspace must be exactly what the canonical worker's ``_guarded_workspace``
    accepts (under the creative temp root, ``creative_`` prefix); that guard is the
    worker's, not this slice's, so this helper only refuses a missing/unreadable
    source and never invents bytes.  Raises :class:`HandoffRefusal` (``missing_source``)
    when the file is absent — a fabricated demo asset is never a production source.
    """
    import shutil
    from pathlib import Path

    src = Path(source_path)
    if not src.is_file():
        raise HandoffRefusal("missing_source", f"source media not found: {source_path!r}")
    workspace = Path(workspace_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    destination = workspace / "input.mp4"
    if src.resolve() != destination.resolve():
        shutil.copy2(src, destination)
    return str(destination)


def _seconds(microseconds: int) -> str:
    """A clean seconds string for the worker's ``float()`` argv parser."""
    return f"{microseconds / 1_000_000:.6f}".rstrip("0").rstrip(".") or "0"


def build_creative_payload(
    proposal: Any,
    *,
    workspace_dir: str,
    input_path: str,
    media_duration_us: int,
    user_id: int,
    chat_id: int,
    lang: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Turn a validated proposal into a typed canonical ``creative_render`` payload.

    This is the whole trust translation: the model supplies **only** the trim
    in/out points; the command, the surface operation, the workspace, the input
    path, the duration, the user/chat identity and the idempotency key are all
    composition-root facts.  Any operation other than the offered ``timeline.trim``
    is refused here, and the points are strictly validated (finite, ordered,
    within the authoritative duration) before they can become a queue row.
    """
    if getattr(proposal, "operation", None) != TRIM_OPERATION:
        raise HandoffRefusal("unsupported_operation", f"only {TRIM_OPERATION} is handoff-able")
    inputs = dict(getattr(proposal, "input", {}) or {})
    raw_in = inputs.get("in_point_us")
    raw_out = inputs.get("out_point_us")
    if not isinstance(raw_in, int) or isinstance(raw_in, bool):
        raise HandoffRefusal("invalid_points", "in_point_us must be an integer microsecond value")
    if not isinstance(raw_out, int) or isinstance(raw_out, bool):
        raise HandoffRefusal("invalid_points", "out_point_us must be an integer microsecond value")
    in_us: int = raw_in
    out_us: int = raw_out
    if in_us < 0:
        raise HandoffRefusal("invalid_points", "in_point_us must be >= 0")
    if out_us <= in_us:
        raise HandoffRefusal("invalid_points", "trim needs in_point_us < out_point_us")
    if media_duration_us > 0:
        out_us = min(out_us, media_duration_us)
        if out_us <= in_us:
            raise HandoffRefusal(
                "invalid_points", "trim window is empty within the source duration"
            )
    return {
        "command": TRIM_COMMAND,
        "operation": TRIM_SURFACE_OPERATION,
        "args": [_seconds(in_us), _seconds(out_us)],
        "workspace_dir": workspace_dir,
        "input_path": input_path,
        "media_duration_us": int(media_duration_us),
        "user_id": int(user_id),
        "chat_id": int(chat_id),
        "lang": str(lang),
        "idempotency_key": idempotency_key,
    }


def _schema_for(gateway: Any, operation: str, attr: str) -> dict[str, Any]:
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


def _routing_request(gateway: Any) -> Any:
    """An honest routing request for the configured substrate (no model -> clarify)."""
    from nexus_ai_agent.nagar.cognition.router import (
        CognitionLevel,
        IntentClass,
        RoutingRequest,
    )

    model_available = bool(getattr(gateway.producer, "consults_model", False))
    return RoutingRequest(
        intent_class=IntentClass.PARAMETRIC,
        deterministic_available=False,
        local_model_available=model_available,
        cloud_model_available=False,
        max_level=CognitionLevel.L2_LOCAL_MODEL,
        human_available=True,
    )


def _record(journal: Any | None, *, decision: Any, gateway: Any, job_id: str, **kw: Any) -> None:
    from nexus_ai_agent.nagar.observation import record_cognition_decision

    record_cognition_decision(
        journal,
        decision=decision,
        producer_class=type(getattr(gateway, "_producer", gateway)).__name__,
        job_id=job_id,
        **kw,
    )


async def run_free_text_intent_durable(
    text: str,
    *,
    gateway: Any,
    queue: Any,
    project: Any,
    source_path: str,
    workspace_dir: str,
    user_id: int,
    chat_id: int,
    lang: str = "en",
    clip_asset_id: str | None = None,
    journal: Any | None = None,
) -> CognitionJobOutcome:
    """Turn free text into at most one durable ``creative_render`` job — or refuse.

    The gateway, the queue, the project (authoritative state), the staged source
    path and the workspace are **injected by the host composition root**; the
    actor/project authority lives inside the gateway already.  Nothing here
    dispatches: the proposal is persisted through the existing queue and executed
    by the existing worker.  Every failure is a typed ``CognitionJobOutcome`` —
    the function never raises for caller input.
    """
    from nexus_ai_agent.nagar.cognition.gateway import CognitionRefused
    from nexus_ai_agent.nagar.cognition.router import CognitionLevel
    from nexus_ai_agent.nagar.observation import CognitionDecision

    intent = normalize_intent(text)
    if not intent:
        return CognitionJobOutcome(
            status="refused", refusal_reason="malformed", detail="empty intent text"
        )

    try:
        facts = source_asset_facts(project, clip_asset_id)
    except KeyError as exc:
        return CognitionJobOutcome(
            status="refused", refusal_reason="missing_source", detail=str(exc)
        )

    idempotency_key = derive_intent_idempotency_key(gateway.project_id, gateway.actor, intent)
    intent_job_id = durable_job_identity(idempotency_key)

    request = _routing_request(gateway)
    decision = gateway.route(request)
    if decision.blocked:
        _record(
            journal,
            decision=CognitionDecision.REFUSED,
            gateway=gateway,
            job_id=intent_job_id,
            idempotency_key=idempotency_key,
            refusal_reason=decision.reason_code,
        )
        return CognitionJobOutcome(
            status="blocked", refusal_reason=decision.reason_code, detail=decision.explanation
        )
    if decision.level is CognitionLevel.L4_HUMAN:
        _record(
            journal,
            decision=CognitionDecision.REFUSED,
            gateway=gateway,
            job_id=intent_job_id,
            idempotency_key=idempotency_key,
            refusal_reason=decision.reason_code,
        )
        return CognitionJobOutcome(
            status="clarification_required",
            refusal_reason=decision.reason_code,
            detail=decision.explanation,
        )

    # Stage the real source into the guarded workspace BEFORE proposing: a
    # missing source is a refusal, never a job that would fail in the worker.
    try:
        input_path = stage_source_media(source_path, workspace_dir)
    except HandoffRefusal as exc:
        _record(
            journal,
            decision=CognitionDecision.REFUSED,
            gateway=gateway,
            job_id=intent_job_id,
            idempotency_key=idempotency_key,
            refusal_reason=exc.reason,
        )
        return CognitionJobOutcome(status="refused", refusal_reason=exc.reason, detail=exc.detail)

    from nexus_ai_agent.nagar.cognition.context import CognitionContext

    context = CognitionContext(
        subject_id=project.project_id,
        intent_text=intent,
        deterministic_facts=facts,
        hints={
            "operation": TRIM_OPERATION,
            "input_fields": _schema_for(gateway, TRIM_OPERATION, "input_model"),
            "output_fields": _schema_for(gateway, TRIM_OPERATION, "output_model"),
        },
    )
    try:
        proposal = await gateway.propose(
            context,
            request,
            requested_operations=frozenset({TRIM_OPERATION}),
            idempotency_key=idempotency_key,
        )
    except CognitionRefused as exc:
        _record(
            journal,
            decision=CognitionDecision.REFUSED,
            gateway=gateway,
            job_id=intent_job_id,
            idempotency_key=idempotency_key,
            refusal_reason=exc.reason.value,
        )
        return CognitionJobOutcome(
            status="refused", refusal_reason=exc.reason.value, detail=exc.detail
        )

    try:
        payload = build_creative_payload(
            proposal,
            workspace_dir=workspace_dir,
            input_path=input_path,
            media_duration_us=int(facts["duration_us"]),
            user_id=user_id,
            chat_id=chat_id,
            lang=lang,
            idempotency_key=idempotency_key,
        )
    except HandoffRefusal as exc:
        _record(
            journal,
            decision=CognitionDecision.REFUSED,
            gateway=gateway,
            job_id=intent_job_id,
            idempotency_key=idempotency_key,
            refusal_reason=exc.reason,
        )
        return CognitionJobOutcome(status="refused", refusal_reason=exc.reason, detail=exc.detail)

    # The one durable handoff: the existing queue persists the typed payload and
    # the existing worker executes it.  No dispatch happens here.
    job_id = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    _record(
        journal,
        decision=CognitionDecision.PROPOSAL_ACCEPTED,
        gateway=gateway,
        job_id=intent_job_id,
        idempotency_key=idempotency_key,
        operation=TRIM_OPERATION,
        execution_identity={
            "durable_job_id": job_id,
            "operation": TRIM_OPERATION,
            "in_point_us": proposal.input.get("in_point_us"),
            "out_point_us": proposal.input.get("out_point_us"),
        },
    )
    return CognitionJobOutcome(
        status="enqueued",
        job_id=job_id,
        operation=TRIM_OPERATION,
        idempotency_key=idempotency_key,
    )


__all__ = [
    "CREATIVE_RENDER_JOB_TYPE",
    "MAX_INTENT_CHARS",
    "TRIM_COMMAND",
    "TRIM_OPERATION",
    "TRIM_SURFACE_OPERATION",
    "WORKER_SOURCE_ASSET_ID",
    "CognitionJobOutcome",
    "HandoffRefusal",
    "build_creative_payload",
    "derive_intent_idempotency_key",
    "durable_job_identity",
    "normalize_intent",
    "run_free_text_intent_durable",
    "source_asset_facts",
    "stage_source_media",
]
