"""LocalCognition — a real ``CognitionPort`` over the existing LLM abstraction.

This is the model-backed producer the boundary was designed for.  It is the
**only** place that talks to a language model, and it is deliberately the
narrowest possible adapter:

* it reuses the existing provider contract — any object with
  ``async generate(prompt, system) -> str`` (i.e. any
  ``nexus_ai_agent.llm.provider.LLMProvider``) is accepted.  No second model
  abstraction is introduced.  The provider is taken as a structural
  :class:`TextGenerator` so the cognition package stays import-light (it never
  imports the heavy ``nexus_ai_agent.llm`` package initialiser);
* the provider's text is **untrusted**: it goes straight into
  :func:`~nexus_ai_agent.nagar.cognition.proposal.parse_proposal`, the single
  fail-closed gate, and comes out as a validated ``TypedProposal`` or an
  explicit ``Refusal`` — never as authority;
* the adapter cannot dispatch, authorize, touch the filesystem, or run a shell.
  A ``TypedProposal`` is not executable; the caller's bridge + CommandBus
  decide that.

Failure is bounded and fail-closed: the call is wrapped in a wall-clock bound
(``asyncio.wait_for``) and retried at most ``budget.max_attempts`` times; every
provider error, timeout, oversized output or parse failure ends as a
``Refusal`` with a stable reason code.  A model can never make the system
execute anything by failing, and never by succeeding.

Observability records only *stages*, never prompt or response content: model
call attempted/succeeded/failed, parse success/failure, explicit refusal and
budget exhaustion.  Token accounting is **not** reported, because the provider
contract exposes no usage field (see ``docs/overnight/MODEL_OPTIONAL.md``).
"""

from __future__ import annotations

import asyncio
import json
import time

from nexus_ai_agent.nagar.cognition.context import (
    CognitionBudget,
    CognitionContext,
    ProducerIdentity,
    ProposalSchema,
)
from nexus_ai_agent.nagar.cognition.proposal import (
    ProposalProvenance,
    Refusal,
    RefusalReason,
    TypedProposal,
    parse_proposal,
)

#: The producer identity every LocalCognition result carries.
LOCAL_PRODUCER_NAME = "nagar.local-cognition"

#: Default cap on a raw model response, before parsing.  Generous versus the
#: 512 KiB proposal bound but still finite, so a runaway model cannot make the
#: boundary allocate without limit.  A response above this is refused *before*
#: JSON parsing.
DEFAULT_MAX_OUTPUT_CHARS = 65_536

#: The instruction prefix.  It asks for one JSON object and nothing else and
#: states the hard rule (a proposal is a request, never a permission).  It is
#: advisory: the parser, not the prompt, is the security boundary.
_SYSTEM_PROMPT = (
    "You are a bounded reasoning component for Nagar. Reply with a SINGLE JSON "
    "object and no prose, matching exactly this shape:\n"
    '{"schema_id": <string>, "schema_version": 1, "operation": <string>, '
    '"input": <object>, "rationale": <string>, "confidence": <number 0..1>}\n'
    "Rules:\n"
    "- `operation` MUST be one of the allowed operations listed in the request.\n"
    "- `input` MUST match the operation's input schema.\n"
    "- You may only PROPOSE. You cannot grant permissions, choose an actor, "
    "confirm execution, or run anything; such fields are rejected.\n"
    "- If you cannot produce a valid proposal, say so in a `rationale` and "
    "return an `input` of {} rather than inventing an operation."
)


class TextGenerator:
    """Legacy structural view of an async text producer (``generate``).

    Kept for backwards compatibility and for isolated producer tests.  The
    **canonical** model boundary is :class:`nexus_ai_agent.application.ports.llm.LLMPort`
    (``complete(prompt, *, idempotency_key=None)``); production wires that.  This
    shim exists so a structured producer that is plugged in behind the port may
    short-circuit with an explicit refusal; a plain provider only returns ``str``.
    """

    async def generate(self, prompt: str, system: str = "") -> str | Refusal:  # pragma: no cover
        raise NotImplementedError


def _combine(prompt: str, system: str) -> str:
    """Fold the advisory system text into one prompt for a canonical LLMPort.

    ``LLMPort`` has a single ``prompt`` argument, so the system instruction is
    prepended.  This is a pure, deterministic transformation — no authority and
    no execution — so the port stays the single canonical model seam.
    """
    return f"{system}\n\n{prompt}" if system else prompt


async def call_completion_port(
    port: object,
    prompt: str,
    system: str,
    *,
    idempotency_key: str | None,
) -> object:
    """Invoke either a canonical ``LLMPort`` or a legacy ``TextGenerator``.

    Preference order is deliberate: a **canonical** ``complete`` method wins so
    a real deployment always exercises the repository's single model seam; the
    legacy ``generate`` path remains only for isolated tests.  The return flows
    back to :meth:`LocalCognition.propose` unchanged for typing/refusal checks.
    """
    complete = getattr(port, "complete", None)
    if callable(complete):
        return await complete(_combine(prompt, system), idempotency_key=idempotency_key)
    generate = getattr(port, "generate", None)
    if callable(generate):
        return await generate(prompt, system)
    raise TypeError(
        "cognition provider must implement complete(prompt, *, idempotency_key=...) "
        "or generate(prompt, system)"
    )


class CognitionObserver:
    """A tiny, dependency-free stage counter.

    It records *which stages were reached*, not their contents, so it can never
    leak a prompt, a response, a secret or a large payload.  Wire one in when
    you want deterministic, testable instrumentation; pass ``None`` to skip.
    """

    _STAGES = (
        "cognition_request",
        "provider_call_attempted",
        "provider_call_succeeded",
        "provider_call_failed",
        "parse_success",
        "parse_failure",
        "refusal",
        "budget_exhausted",
    )

    def __init__(self) -> None:
        self._counts: dict[str, int] = dict.fromkeys(self._STAGES, 0)

    def record(self, stage: str) -> None:
        if stage not in self._counts:
            raise ValueError(f"unknown cognition stage: {stage!r}")
        self._counts[stage] += 1

    def count(self, stage: str) -> int:
        return self._counts[stage]

    def snapshot(self) -> dict[str, int]:
        return dict(self._counts)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _build_prompt(context: CognitionContext, schema: ProposalSchema) -> str:
    """Render the typed, authority-free request.  Pure; no I/O."""
    facts = json.dumps(context.deterministic_facts, ensure_ascii=False, allow_nan=False)
    hints = json.dumps(context.hints, ensure_ascii=False, allow_nan=False)
    allowed = json.dumps(sorted(schema.allowed_operations), ensure_ascii=False)
    input_schema = json.dumps(schema.input_schema, ensure_ascii=False, allow_nan=False)
    return (
        f"subject_id: {context.subject_id}\n"
        f"intent: {context.intent_text}\n"
        f"deterministic_facts: {facts}\n"
        f"hints: {hints}\n"
        f"schema_id: {schema.schema_id} (version {schema.schema_version})\n"
        f"allowed_operations: {allowed}\n"
        f"input_schema: {input_schema}\n"
    )


class LocalCognition:
    """A ``CognitionPort`` that asks a configured provider and parses the result."""

    #: This producer consults a real model (declared so callers can build an
    #: honest routing request without an isinstance hack).
    consults_model = True

    def __init__(
        self,
        provider: TextGenerator,
        *,
        observer: CognitionObserver | None = None,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        producer_name: str = LOCAL_PRODUCER_NAME,
    ) -> None:
        if max_output_chars < 1:
            raise ValueError("max_output_chars must be >= 1")
        self._provider = provider
        self._observer = observer
        self._max_output_chars = max_output_chars
        self._producer_name = producer_name

    def _observe(self, stage: str) -> None:
        if self._observer is not None:
            self._observer.record(stage)

    def _provenance(self) -> ProposalProvenance:
        return ProposalProvenance(
            producer=ProducerIdentity(kind="model", name=self._producer_name),
            created_at=_utc_now(),
            source=self._producer_name,
        )

    async def propose(
        self,
        context: CognitionContext,
        schema: ProposalSchema,
        budget: CognitionBudget,
        *,
        idempotency_key: str | None = None,
    ) -> TypedProposal | Refusal:
        self._observe("cognition_request")
        provenance = self._provenance()
        try:
            prompt = _build_prompt(context, schema)
        except (TypeError, ValueError):
            # A degenerate context/schema (e.g. NaN buried in input_schema) must
            # not crash the port; fail closed instead of raising.
            self._observe("refusal")
            return Refusal(
                reason=RefusalReason.MALFORMED,
                detail="cognition request could not be rendered as finite JSON",
                provenance=provenance,
            )
        deadline = time.monotonic() + budget.max_wall_clock_seconds
        last: Refusal | None = None

        for _ in range(budget.max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._observe("budget_exhausted")
                self._observe("refusal")
                return Refusal(
                    reason=RefusalReason.BUDGET_EXHAUSTED,
                    detail="cognition wall-clock budget exhausted",
                    provenance=provenance,
                )

            self._observe("provider_call_attempted")
            raw: object
            try:
                # One canonical model seam: a ``complete``-shaped LLMPort is
                # preferred; the legacy ``generate`` shape is a test-only shim.
                # The idempotency key is propagated to exactly that seam.
                raw = await asyncio.wait_for(
                    call_completion_port(
                        self._provider,
                        prompt,
                        _SYSTEM_PROMPT,
                        idempotency_key=idempotency_key,
                    ),
                    timeout=remaining,
                )
            except (TimeoutError, asyncio.TimeoutError):
                self._observe("provider_call_failed")
                last = Refusal(
                    reason=RefusalReason.PRODUCER_FAILED,
                    detail="model call timed out",
                    provenance=provenance,
                )
                continue
            except Exception as exc:  # noqa: BLE001 - provider failures become refusals
                self._observe("provider_call_failed")
                # Never echo the exception text into the refusal: a provider
                # error can carry headers, URLs or credentials.
                last = Refusal(
                    reason=RefusalReason.PRODUCER_FAILED,
                    detail=f"model call failed: {type(exc).__name__}",
                    provenance=provenance,
                )
                continue

            self._observe("provider_call_succeeded")

            # A structured producer behind the port may short-circuit with an
            # explicit Refusal instead of a string; honour it as-is.
            if isinstance(raw, Refusal):
                self._observe("refusal")
                return raw

            if not isinstance(raw, str):
                self._observe("parse_failure")
                last = Refusal(
                    reason=RefusalReason.MALFORMED,
                    detail=f"provider returned {type(raw).__name__}, expected str",
                    provenance=provenance,
                )
                continue

            if len(raw) > self._max_output_chars:
                self._observe("parse_failure")
                last = Refusal(
                    reason=RefusalReason.SCHEMA_VIOLATION,
                    detail="model response exceeded the configured size bound",
                    provenance=provenance,
                )
                continue

            parsed = parse_proposal(raw, schema, provenance=provenance)
            if isinstance(parsed, TypedProposal):
                self._observe("parse_success")
                return parsed

            self._observe("parse_failure")
            last = parsed
            # An explicit producer refusal is final; retrying will not change it.
            if parsed.reason is RefusalReason.PRODUCER_REFUSED:
                self._observe("refusal")
                return parsed

        self._observe("refusal")
        return last or Refusal(
            reason=RefusalReason.PRODUCER_FAILED,
            detail="no proposal produced within budget",
            provenance=provenance,
        )


__all__ = [
    "DEFAULT_MAX_OUTPUT_CHARS",
    "LOCAL_PRODUCER_NAME",
    "CognitionObserver",
    "LocalCognition",
    "TextGenerator",
]
