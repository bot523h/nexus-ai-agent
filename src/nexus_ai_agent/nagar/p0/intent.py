"""Versioned, authority-separated User Intent contracts for Nagar P0."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION: Literal["nagar.intent.v1"] = "nagar.intent.v1"


class IntentStatus(str, Enum):
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    PREVIEW_READY = "PREVIEW_READY"
    REJECTED = "REJECTED"
    COMMIT_READY = "COMMIT_READY"


class IntentSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["text", "transcript", "ui_selection", "api"]
    text: str = Field(min_length=1, max_length=16_384)
    source_ref: str | None = Field(default=None, max_length=256)


class UserIntent(BaseModel):
    """Model-produced meaning; actor, tenant and authorization are backend-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["nagar.intent.v1"] = SCHEMA_VERSION
    intent_id: str = Field(default_factory=lambda: f"intent_{uuid4().hex}")
    project_id: str = Field(min_length=1, max_length=128)
    source: IntentSource
    goal: str = Field(min_length=1, max_length=512)
    entities: tuple[dict[str, Any], ...] = ()
    semantic_references: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    ambiguities: tuple[str, ...] = ()
    correlation_id: str = Field(min_length=1, max_length=128)
    requires_confirmation: bool = True


class IntentResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intent: UserIntent | None = None
    status: IntentStatus
    diagnostics: tuple[str, ...] = ()
    ast: Any | None = None


def resolve_intent(
    *,
    text: str,
    project_id: str,
    source_kind: Literal["text", "transcript", "ui_selection", "api"] = "text",
    correlation_id: str | None = None,
    semantic_references: tuple[str, ...] = (),
    ambiguities: tuple[str, ...] = (),
) -> IntentResolution:
    """Normalize an input without guessing missing semantics.

    This function intentionally does not accept actor, tenant, permission, or policy
    fields. A caller may only move past clarification when all references are resolved.
    """
    normalized = " ".join(text.split()) if isinstance(text, str) else ""
    if not normalized:
        return IntentResolution(status=IntentStatus.REJECTED, diagnostics=("empty_source",))
    intent = UserIntent(
        project_id=project_id,
        source=IntentSource(kind=source_kind, text=normalized),
        goal=normalized,
        semantic_references=semantic_references,
        ambiguities=ambiguities,
        correlation_id=correlation_id or uuid4().hex,
        requires_confirmation=bool(ambiguities),
    )
    if ambiguities:
        return IntentResolution(
            intent=intent, status=IntentStatus.NEEDS_CLARIFICATION, diagnostics=ambiguities
        )
    return IntentResolution(intent=intent, status=IntentStatus.PREVIEW_READY)
