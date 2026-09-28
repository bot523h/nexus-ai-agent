"""Compatibility facade: the legacy ``LLMProvider`` shape on top of the authority.

Every existing consumer in the repository — ``orchestration/graph.py``,
``agents/{chat,phi,qwen,gemma,executor}_agent.py``, ``memory/{short,long}_term.py``
— depends on the two-method :class:`~nexus_ai_agent.llm.provider.LLMProvider`
contract (``generate(prompt, system) -> str``, ``embed(text) -> list[float]``).
Rewriting all of them in one wave is the big-bang migration the mission forbids,
so this facade keeps their contract byte-compatible while making the gateway the
only thing underneath it (LAW 12).

Two failure modes, chosen explicitly by the constructor:

``on_failure="raise"`` (default, and what new code should use)
    A typed :class:`LLMError` propagates. The caller decides. Nothing is
    invented, and a failure can never be mistaken for an answer.

``on_failure="message"`` (the legacy composition only)
    The failure is rendered into the same user-facing string the pre-W2
    ``FallbackProvider`` produced, so ``nexus run-bot``'s LangGraph path keeps
    degrading gracefully instead of crashing the graph. This is a *rendering*
    decision taken at the boundary, not error detection: the typed error is
    classified first, then rendered. The old code did the reverse — it rendered
    first and then scanned the rendering for "429".

The distinction matters and is enforced by
``tests/architecture/test_llm_gateway_authority.py``: rendering an error into a
message is fine; deciding retry/fallback by reading a message is not.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeVar, cast

from nexus_ai_agent.llm.errors import GatewayInternalError, LLMError, LLMErrorKind
from nexus_ai_agent.llm.gateway.contract import (
    Caller,
    CallerCategory,
    GenerationParams,
    LLMOperation,
    LLMPriority,
    LLMRequest,
    LLMResponse,
    Message,
)
from nexus_ai_agent.llm.gateway.engine import LLMGateway
from nexus_ai_agent.llm.gateway.registry import get_llm_gateway
from nexus_ai_agent.llm.provider import LLMProvider

__all__ = [
    "DEGRADED_DISCLAIMER",
    "UNAVAILABLE_MESSAGE",
    "GatewayLLMProvider",
    "json_object_validator",
    "pydantic_validator",
]

#: Verbatim string from the pre-W2 ``llm/fallback_provider.py`` — kept so a
#: degraded answer is byte-for-byte the same as before the migration.
DEGRADED_DISCLAIMER = (
    "\n\n---\n⚠️ _Fallback mode_: The primary AI engine is currently rate-limited. "
    "This response was generated locally and may be less accurate. "
    "Please try again in a minute for the full AI experience._"
)

#: Verbatim string from the pre-W2 ``FallbackProvider._do_fallback`` both-failed path.
UNAVAILABLE_MESSAGE = (
    "⚠️ Sorry, both the primary and backup AI engines are currently unavailable. "
    "Please try again in a few minutes."
)

#: Kinds that mean "the service is busy", rendered as a retry-soon message.
_BUSY_KINDS = frozenset(
    {LLMErrorKind.OVERLOADED, LLMErrorKind.DEADLINE_EXCEEDED, LLMErrorKind.RATE_LIMITED}
)

T = TypeVar("T")


@dataclass
class GatewayLLMProvider(LLMProvider):
    """An ``LLMProvider`` whose every call goes through the gateway.

    This is the object to inject into graphs, agents and memory. Constructing it
    is free; the gateway is resolved lazily so import order never matters.
    """

    caller: Caller
    gateway: LLMGateway | None = None
    purpose: str = "chat"
    priority: LLMPriority = LLMPriority.NORMAL
    deadline_seconds: float | None = None
    generation: GenerationParams | None = None
    on_failure: Literal["raise", "message"] = "raise"
    degraded_disclaimer: str | None = DEGRADED_DISCLAIMER
    metadata: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.on_failure not in {"raise", "message"}:
            raise ValueError("on_failure must be 'raise' or 'message'")

    # ── resolution ───────────────────────────────────────────────────
    def authority(self) -> LLMGateway:
        """The one gateway this provider uses. Never builds a private provider."""

        return self.gateway if self.gateway is not None else get_llm_gateway()

    def bind(self, gateway: LLMGateway) -> GatewayLLMProvider:
        """Return a copy bound to an explicit gateway (composition roots/tests)."""

        return GatewayLLMProvider(
            caller=self.caller,
            gateway=gateway,
            purpose=self.purpose,
            priority=self.priority,
            deadline_seconds=self.deadline_seconds,
            generation=self.generation,
            on_failure=self.on_failure,
            degraded_disclaimer=self.degraded_disclaimer,
            metadata=self.metadata,
        )

    # ── LLMProvider contract ─────────────────────────────────────────
    async def generate(self, prompt: str, system: str = "") -> str:
        """Legacy contract: return text. Failure handling per ``on_failure``."""

        request = LLMRequest(
            caller=self.caller,
            purpose=self.purpose,
            operation=LLMOperation.CHAT,
            prompt=prompt,
            system=system or None,
            priority=self.priority,
            deadline_seconds=self.deadline_seconds,
            generation=self.generation or GenerationParams(),
            metadata=self.metadata or {},
        )
        return await self._text(request)

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        purpose: str | None = None,
        messages: tuple[Message, ...] = (),
        output_validator: Callable[[str], Any] | None = None,
        idempotency_key: str | None = None,
        cancellation: Any | None = None,
        deadline_seconds: float | None = None,
    ) -> LLMResponse:
        """Typed path: returns the full :class:`LLMResponse`, raises typed errors.

        New callers should use this rather than ``generate`` — it exposes usage,
        routing, timings, degradation and the validated structured payload.
        """

        request = LLMRequest(
            caller=self.caller,
            purpose=purpose or self.purpose,
            operation=LLMOperation.CHAT,
            prompt=prompt,
            system=system,
            messages=messages,
            priority=self.priority,
            deadline_seconds=(
                deadline_seconds if deadline_seconds is not None else self.deadline_seconds
            ),
            generation=self.generation or GenerationParams(),
            output_validator=output_validator,
            idempotency_key=idempotency_key,
            cancellation=cancellation,
            metadata=self.metadata or {},
        )
        return await self.authority().execute(request)

    async def embed(self, text: str) -> list[float]:
        """Legacy contract: return a ``list[float]`` vector."""

        request = LLMRequest(
            caller=self.caller,
            purpose="embeddings",
            operation=LLMOperation.EMBEDDINGS,
            prompt=text,
            priority=self.priority,
            deadline_seconds=self.deadline_seconds,
            metadata=self.metadata or {},
        )
        response = await self.authority().execute(request)
        if response.embedding is None:
            # An empty list would be a lie: it says "here is a vector with no
            # dimensions" when the truth is "this route produced no vector".
            # Same typed failure ``LLMGateway.embed()`` raises (LAW 3, LAW 11).
            raise GatewayInternalError(
                "embedding route returned no vector",
                provider=response.provider,
                model=response.model,
                request_id=response.request_id,
            )
        return list(response.embedding)

    async def embed_typed(self, text: str) -> LLMResponse:
        request = LLMRequest(
            caller=self.caller,
            purpose="embeddings",
            operation=LLMOperation.EMBEDDINGS,
            prompt=text,
            priority=self.priority,
            deadline_seconds=self.deadline_seconds,
            metadata=self.metadata or {},
        )
        return await self.authority().execute(request)

    # ── internals ────────────────────────────────────────────────────
    async def _text(self, request: LLMRequest) -> str:
        gateway = self.authority()
        try:
            response = await gateway.execute(request)
        except LLMError as exc:
            if self.on_failure == "raise":
                raise
            return self._render_failure(exc)
        if response.degraded and self.degraded_disclaimer:
            return response.text + self.degraded_disclaimer
        return response.text

    def _render_failure(self, exc: LLMError) -> str:
        """Render a *already classified* failure into a user-facing message.

        Only the legacy ``on_failure="message"`` composition reaches this. The
        message never contains provider detail: leaking an upstream error body to
        a chat surface is both a security smell and useless to the user.
        """

        # Typed comparison against the enum — never against a message substring.
        if exc.kind in _BUSY_KINDS:
            return "⏳ The AI service is busy right now. Please try again in a moment."
        return UNAVAILABLE_MESSAGE


# ═══════════════════════════════════════════════════════════════════════════
# Structured-output validators (caller-owned contract, gateway-owned boundary)
# ═══════════════════════════════════════════════════════════════════════════


def json_object_validator(
    *,
    required_keys: tuple[str, ...] = (),
    allow_fenced: bool = True,
) -> Callable[[str], dict[str, Any]]:
    """Build a validator that requires a JSON object, optionally key-checked.

    Validation happens inside the gateway, so a caller that wants JSON never
    writes ``text.find("{")`` again — that pattern (present in
    ``features/ai_memory.py`` before W2) silently accepts a model answer that
    merely contains a brace somewhere.
    """

    def validate(text: str) -> dict[str, Any]:
        candidate = text.strip()
        if allow_fenced and candidate.startswith("```"):
            candidate = _strip_code_fences(candidate)
        payload = json.loads(candidate)
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        missing = [key for key in required_keys if key not in payload]
        if missing:
            raise ValueError(f"missing required keys: {missing}")
        return payload

    return validate


def pydantic_validator(model_cls: type[T]) -> Callable[[str], T]:
    """Build a validator that parses model output into a pydantic model.

    Accepts both a JSON object body and a fenced code block, mirroring what
    ``creative/video_director.py`` had to hand-roll before W2.
    """

    parse = getattr(model_cls, "model_validate_json", None)
    if not callable(parse):
        raise TypeError("pydantic_validator requires a pydantic v2 model with model_validate_json")

    def validate(text: str) -> T:
        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = _strip_code_fences(candidate)
        return cast(T, parse(candidate))

    return validate


def _strip_code_fences(text: str) -> str:
    """Remove a leading/trailing ``` fence and an optional language tag."""

    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline == -1:
            return ""
        stripped = stripped[first_newline + 1 :]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


def agent_caller(name: str, *, tenant_id: int = 0) -> Caller:
    """Convenience identity for agent-side callers."""

    return Caller(category=CallerCategory.AGENT, name=name, tenant_id=tenant_id)


def surface_caller(name: str, *, tenant_id: int = 0) -> Caller:
    """Convenience identity for bot/API surface callers."""

    return Caller(category=CallerCategory.SURFACE, name=name, tenant_id=tenant_id)
