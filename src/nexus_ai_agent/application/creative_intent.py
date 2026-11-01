"""Transport-free canonical identity for one validated creative intent.

The cognition boundary produces a proposal; trusted composition code turns the
accepted proposal into this content-addressed record.  The record is evidence
and execution input, never an authorization grant.  Principal identity,
project membership and channel binding are checked independently by the
server-side execution adapter.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CREATIVE_INTENT_SCHEMA_ID: Literal["nexus.creative_intent.v1"] = "nexus.creative_intent.v1"
MAX_INTENT_INPUT_BYTES = 512 * 1024
_PROJECT_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]*")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _content_id(kind: str, value: object) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"{kind}_{digest}"


class CanonicalCreativeIntent(BaseModel):
    """One validated trim proposal bound to a project and source-text digest.

    ``proposal_id`` identifies the full validated model proposal at the trust
    boundary.  Raw prompt text and model rationale are deliberately not copied
    into the durable queue payload; only their stable identifiers and the
    bounded executable input remain.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["nexus.creative_intent.v1"] = CREATIVE_INTENT_SCHEMA_ID
    intent_id: str = Field(pattern=r"^intent_[0-9a-f]{64}$")
    project_id: str = Field(min_length=1, max_length=128)
    proposal_id: str = Field(pattern=r"^proposal_[0-9a-f]{64}$")
    source_text_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    operation: Literal["timeline.trim"] = "timeline.trim"
    input: dict[str, Any] = Field(default_factory=dict)

    @field_validator("project_id")
    @classmethod
    def _project_reference_only(cls, value: str) -> str:
        if not _PROJECT_ID.fullmatch(value) or ".." in value:
            raise ValueError("project reference must be an opaque local identifier")
        return value

    @field_validator("input")
    @classmethod
    def _bounded_finite_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = _canonical_json(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("creative intent input must be finite JSON data") from exc
        if len(encoded.encode("utf-8")) > MAX_INTENT_INPUT_BYTES:
            raise ValueError("creative intent input exceeds 512 KiB")
        return value

    @model_validator(mode="after")
    def _content_identity_is_valid(self) -> CanonicalCreativeIntent:
        expected = self.identity(
            project_id=self.project_id,
            proposal_id=self.proposal_id,
            source_text_sha256=self.source_text_sha256,
            operation=self.operation,
            input_data=self.input,
        )
        if self.intent_id != expected:
            raise ValueError("creative intent ID does not match its canonical content")
        return self

    @staticmethod
    def identity(
        *,
        project_id: str,
        proposal_id: str,
        source_text_sha256: str,
        operation: str,
        input_data: dict[str, Any],
    ) -> str:
        return _content_id(
            "intent",
            {
                "schema_id": CREATIVE_INTENT_SCHEMA_ID,
                "project_id": project_id,
                "proposal_id": proposal_id,
                "source_text_sha256": source_text_sha256,
                "operation": operation,
                "input": input_data,
            },
        )

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        proposal_id: str,
        source_text_sha256: str,
        operation: Literal["timeline.trim"],
        input_data: dict[str, Any],
    ) -> CanonicalCreativeIntent:
        intent_id = cls.identity(
            project_id=project_id,
            proposal_id=proposal_id,
            source_text_sha256=source_text_sha256,
            operation=operation,
            input_data=input_data,
        )
        return cls(
            intent_id=intent_id,
            project_id=project_id,
            proposal_id=proposal_id,
            source_text_sha256=source_text_sha256,
            operation=operation,
            input=input_data,
        )


__all__ = [
    "CREATIVE_INTENT_SCHEMA_ID",
    "MAX_INTENT_INPUT_BYTES",
    "CanonicalCreativeIntent",
]
