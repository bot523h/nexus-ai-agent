"""Strict model-output boundary for the canonical :class:`Intent` model."""

from __future__ import annotations

import json
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.spine.models import Intent, IntentSourceRange


class IntentProposal(BaseModel):
    """Untrusted JSON shape requested from an LLM; contains no authority fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    objective: Literal["video_trim", "other", "unsupported"]
    goal: str = Field(min_length=1, max_length=2000)
    source_range: IntentSourceRange | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    ambiguity_state: Literal["clear", "clarify", "unsupported"]
    unresolved_requirements: tuple[str, ...] = ()
    semantic_intents: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()


class IntentResolverPort(Protocol):
    """Async, provider-agnostic text-to-Intent port."""

    async def resolve(self, text: str, *, project_id: str, request_id: str) -> Intent: ...


class LLMIntentResolver:
    """Ask a provider for typed meaning, then validate and stamp trusted identity."""

    def __init__(self, provider: object) -> None:
        self._provider = provider

    async def resolve(self, text: str, *, project_id: str, request_id: str) -> Intent:
        if not text.strip():
            raise ValueError("cannot resolve an empty request")
        if not request_id.strip():
            raise ValueError("a stable request_id is required for durable intent identity")
        prompt = json.dumps(
            {"user_request": text, "project_id": project_id},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        generate = getattr(self._provider, "generate", None)
        if not callable(generate):
            raise TypeError("intent provider must implement async generate(prompt, system=...)")
        raw = await generate(
            prompt=prompt,
            system=(
                "Return exactly one JSON object matching the requested typed intent schema. "
                "You may classify intent and extract explicit source ranges, requirements, "
                "semantic phrases, confidence and ambiguity only. Do not emit commands, "
                "operation ids, file paths, tool calls, actors, permissions, or executable code. "
                "If meaning or required details are uncertain, mark clarification required."
            ),
        )
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32_000:
            raise ValueError("intent model output exceeds the 32 KB schema bound")
        try:
            payload = json.loads(
                raw,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_json_object,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("intent model output is not strict JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("intent model output must be a JSON object")
        proposal = IntentProposal.model_validate(payload)
        return Intent(
            request_id=request_id,
            project_id=project_id,
            goal=proposal.goal,
            objective=proposal.objective,
            source_range=proposal.source_range,
            confidence=proposal.confidence,
            ambiguity_state=proposal.ambiguity_state,
            unresolved_requirements=proposal.unresolved_requirements,
            semantic_intents=proposal.semantic_intents,
            constraints=proposal.constraints,
            source_text=text,
            source="user",
        )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON numeric constant: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate intent JSON field: {key!r}")
        result[key] = value
    return result


__all__ = ["IntentProposal", "IntentResolverPort", "LLMIntentResolver"]
