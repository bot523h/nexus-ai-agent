"""Studio render submission — the authenticated HTTP lane into the canonical chain.

The read half of the Studio API (``GET …/jobs/{job_id}/state``) could observe a
job but nothing on the HTTP surface could *create* one: every producer was a
Telegram handler or a CLI. This module is the missing half, and it adds no new
authority of its own:

    signed Studio envelope (HMAC over the raw body)
      + bearer/HttpOnly-session principal  (the deployment dashboard token)
      -> project scope policy              (``NEXUS_STUDIO_PROJECT_IDS``)
      -> typed submission                   (``StudioRenderSubmission``, extra=forbid)
      -> the EXISTING trust translation     (``nagar.creative.build_creative_payload``)
      -> the EXISTING durable queue         (``JobQueuePort.enqueue``)
      -> ``creative_render`` worker: CommandBus -> AttemptStaging -> real media ->
         independent verifier -> fenced publish -> ``register_verified_asset_version``

Three rules keep it honest:

1. **One trust translation.** The payload is built by
   :func:`nexus_ai_agent.nagar.creative.handoff.build_creative_payload` — the same
   function the cognition handoff uses — so the surface cannot drift into a second
   interpretation of "what an admitted job looks like". It validates the trim
   window, derives the idempotency key and mints the canonical intent.
2. **No canonical identity, no job.** Principal and channel binding are
   composition-root facts (deployment configuration), never request fields. When
   they are not configured the service refuses with
   :class:`StudioSubmissionRefused` instead of enqueueing a row the worker's
   authorizer would have to trust blindly.
3. **The worker owns execution.** Nothing here dispatches a command, renders a
   frame or verifies a byte; the returned ``job_id`` is a durable row id from the
   one queue, so the read route observes the same row the worker drains.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.studio.models import ActorIdentity
from nexus_ai_agent.nagar.creative.handoff import (
    CREATIVE_RENDER_JOB_TYPE,
    TRIM_COMMAND,
    TRIM_OPERATION,
    TRIM_SURFACE_OPERATION,
    CanonicalHandoffFacts,
    build_creative_payload,
    derive_intent_idempotency_key,
    normalize_intent,
)

__all__ = [
    "StudioCanonicalFacts",
    "StudioRenderSubmission",
    "StudioRenderSubmissionService",
    "StudioSubmissionRefused",
    "resolve_studio_facts_from_env",
]

#: The one surface operation this lane can admit. Widening it means widening
#: ``build_creative_payload`` first — never a second code path here.
SUBMISSION_OPERATION: Literal["trim"] = "trim"


class StudioSubmissionRefused(ValueError):
    """A typed, caller-facing refusal *before* anything is enqueued.

    ``reason`` is a stable code (it becomes the HTTP detail): ``invalid_request``
    (the submission is malformed), ``identity_unavailable`` (the deployment did
    not configure canonical facts), ``refused`` (the canonical handoff refused).
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


class StudioRenderSubmission(BaseModel):
    """One authenticated render request (a *request*, never an authorization)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["trim"] = SUBMISSION_OPERATION
    in_point_us: int = Field(ge=0)
    out_point_us: int = Field(gt=0)
    workspace_dir: str = Field(min_length=1, max_length=4096)
    input_path: str = Field(min_length=1, max_length=4096)
    media_duration_us: int = Field(ge=0)
    input_sha256: str | None = Field(default=None, min_length=64, max_length=71)
    lang: str = Field(default="en", min_length=2, max_length=16)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)


@dataclass(frozen=True)
class StudioCanonicalFacts:
    """Composition-root identity for the Studio lane (never from the request)."""

    principal_id: str
    channel_binding_id: str
    user_id: int = 0
    chat_id: int = 0


class _SubmissionProposal(BaseModel):
    """The proposal the canonical handoff consumes: the trim window, nothing else.

    Deliberately a *proposal*, not a command: it carries no operation allow-list,
    no identity and no lifecycle claim, so ``build_creative_payload`` remains the
    only thing that decides what an admitted job is.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str
    input: dict[str, Any]

    @classmethod
    def for_trim(cls, *, in_point_us: int, out_point_us: int) -> _SubmissionProposal:
        return cls(
            operation=TRIM_OPERATION,
            input={
                "clip_asset_id": "src",
                "in_point_us": in_point_us,
                "out_point_us": out_point_us,
            },
        )


class _RevisionStorePort(Protocol):
    async def load_head(self, project_id: str) -> Any: ...


class _QueuePort(Protocol):
    async def enqueue(
        self, *, job_type: str, idempotency_key: str, payload: dict[str, Any]
    ) -> str: ...

    async def get_job_facts_async(self, job_id: str) -> Any: ...


class StudioRenderSubmissionService:
    """Admit one Studio render into the existing durable queue."""

    def __init__(
        self,
        queue: _QueuePort,
        facts: StudioCanonicalFacts | None,
        revision_store: _RevisionStorePort | None = None,
    ) -> None:
        self._queue = queue
        self._facts = facts
        self._revision_store = revision_store

    @property
    def facts(self) -> StudioCanonicalFacts | None:
        return self._facts

    async def submit(
        self, *, project_id: str, submission: StudioRenderSubmission
    ) -> dict[str, Any]:
        """Validate, translate and enqueue; return the durable job identity."""
        if not project_id.strip():
            raise StudioSubmissionRefused("invalid_request", "project_id must not be empty")
        facts = self._facts
        if facts is None:
            raise StudioSubmissionRefused(
                "identity_unavailable",
                "the deployment has no Studio canonical identity configured",
            )
        if facts.principal_id == "" or facts.channel_binding_id == "":
            raise StudioSubmissionRefused(
                "identity_unavailable", "Studio canonical identity is incomplete"
            )

        # Admit against the *committed* head, never "whatever is current when the
        # worker runs": the pin is what makes the executed state auditable. A
        # project with no committed revision is refused (there is nothing to pin).
        revision_store = self._revision_store
        revision_pin: tuple[str, str] | None = None
        if revision_store is not None:
            try:
                head = await revision_store.load_head(project_id)
            except Exception as exc:  # noqa: BLE001 - a store failure is a refusal
                raise StudioSubmissionRefused(
                    "revision_unavailable", f"{type(exc).__name__}: {exc}"
                ) from exc
            if head is None:
                raise StudioSubmissionRefused(
                    "revision_unavailable", "the project has no committed revision to pin"
                )
            revision_pin = (str(head.revision_id), str(head.state_hash))

        source_text = normalize_intent(
            f"studio render {SUBMISSION_OPERATION} of {submission.input_path}"
        )
        idempotency_key = submission.idempotency_key or derive_intent_idempotency_key(
            project_id,
            ActorIdentity(kind="user", actor_id=facts.principal_id),
            f"{source_text}:{submission.in_point_us}:{submission.out_point_us}",
        )
        try:
            payload = build_creative_payload(
                _SubmissionProposal.for_trim(
                    in_point_us=submission.in_point_us, out_point_us=submission.out_point_us
                ),
                workspace_dir=submission.workspace_dir,
                input_path=submission.input_path,
                media_duration_us=submission.media_duration_us,
                user_id=facts.user_id,
                chat_id=facts.chat_id,
                lang=submission.lang,
                idempotency_key=idempotency_key,
                input_sha256=submission.input_sha256,
                canonical=CanonicalHandoffFacts(
                    principal_id=facts.principal_id,
                    channel_binding_id=facts.channel_binding_id,
                    project_id=project_id,
                ),
                source_text=source_text,
                revision_pin=revision_pin,
            )
        except Exception as exc:  # noqa: BLE001 - a refusal is a typed answer, never a 500
            raise StudioSubmissionRefused("refused", f"{type(exc).__name__}: {exc}") from exc

        job_id = await self._queue.enqueue(
            job_type=CREATIVE_RENDER_JOB_TYPE,
            idempotency_key=str(payload["idempotency_key"]),
            payload=payload,
        )
        facts_row = await self._queue.get_job_facts_async(job_id)
        attempt = int(getattr(facts_row, "attempt", 0) or 0)
        status = str(getattr(facts_row, "status", "pending") or "pending")
        return {
            "job_id": job_id,
            "attempt": attempt,
            "status": status,
            "operation": TRIM_SURFACE_OPERATION,
            "canonical_operation": TRIM_OPERATION,
            "command": TRIM_COMMAND,
            "idempotency_key": payload["idempotency_key"],
            "revision_id": payload.get("revision_id"),
            "state_url": f"/api/studio/projects/{project_id}/jobs/{job_id}/state",
        }


def _envelope_digest(submission: StudioRenderSubmission) -> str:
    """A stable digest of the admitted request (audit/debug aid, never authority)."""
    material = "|".join(
        [
            submission.operation,
            str(submission.in_point_us),
            str(submission.out_point_us),
            submission.workspace_dir,
            submission.input_path,
            str(submission.media_duration_us),
        ]
    )
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def resolve_studio_facts_from_env() -> StudioCanonicalFacts | None:
    """Read the deployment's Studio canonical identity, or ``None`` when unset.

    ``NEXUS_STUDIO_PRINCIPAL_ID`` and ``NEXUS_STUDIO_CHANNEL_BINDING_ID`` must name
    rows that exist in the application directory; this function only transports
    them. An unset deployment refuses submissions rather than inventing identity.
    """
    principal_id = os.getenv("NEXUS_STUDIO_PRINCIPAL_ID", "").strip()
    binding_id = os.getenv("NEXUS_STUDIO_CHANNEL_BINDING_ID", "").strip()
    if not principal_id or not binding_id:
        return None
    return StudioCanonicalFacts(principal_id=principal_id, channel_binding_id=binding_id)
