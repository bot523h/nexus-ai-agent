"""Typed strategy proposal for the first Agent Intelligence capability slice.

The strategy plane can choose a creative objective, but it emits only a closed
schema.  It has no registry, filesystem, executor, CommandBus, or authorization
imports.  The initial deterministic strategy accepts an explicit trim range and
preserves every semantic phrase for the existing semantics normalizer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_ai_agent.creative.spine.models import Intent


class StrategyError(ValueError):
    """A typed intent cannot be turned into a supported strategy."""


class SourceAssetDescriptor(BaseModel):
    """Trusted, measured media facts; never an execution path or byte handle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    media_kind: Literal["video"] = "video"
    duration_us: int = Field(gt=0)
    content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    width_px: int = Field(default=0, ge=0)
    height_px: int = Field(default=0, ge=0)
    frame_rate_milli: int = Field(default=0, ge=0)


class StrategyProposal(BaseModel):
    """Validated creative direction, bound to the source and exact trim range."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: str = Field(default="", max_length=128)
    objective: Literal["video_trim"]
    operation: Literal["timeline.trim"]
    creative_direction: Literal["preserve_source"] = "preserve_source"
    source_asset_id: str = Field(min_length=1, max_length=128)
    in_point_us: int = Field(ge=0)
    out_point_us: int = Field(gt=0)
    semantic_intents: tuple[str, ...] = ()
    rationale: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _range_and_identity(self) -> StrategyProposal:
        if self.out_point_us <= self.in_point_us:
            raise ValueError("strategy trim out-point must be greater than in-point")
        payload = self.model_dump(mode="json", exclude={"strategy_id"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        expected = "strat_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
        if self.strategy_id and self.strategy_id != expected:
            raise ValueError("strategy_id does not match the proposal content")
        object.__setattr__(self, "strategy_id", expected)
        return self


class StrategyProvider(Protocol):
    """Provider-agnostic strategy port; implementations return typed proposals."""

    async def propose(self, intent: Intent, source: SourceAssetDescriptor) -> StrategyProposal: ...


class DeterministicTrimStrategy:
    """Smallest honest strategy: one explicit source range, no inferred edit."""

    async def propose(self, intent: Intent, source: SourceAssetDescriptor) -> StrategyProposal:
        if intent.objective != "video_trim":
            raise StrategyError(f"unsupported creative objective: {intent.objective!r}")
        if intent.source_range is None:
            raise StrategyError("trim needs an explicit in/out range; clarification required")
        if intent.source_range.out_point_us > source.duration_us:
            raise StrategyError(
                "requested trim range exceeds the measured source duration; no clamping is allowed"
            )
        return StrategyProposal(
            objective="video_trim",
            operation="timeline.trim",
            source_asset_id=source.asset_id,
            in_point_us=intent.source_range.in_point_us,
            out_point_us=intent.source_range.out_point_us,
            semantic_intents=intent.semantic_intents,
            rationale="Preserve the source and apply only the explicitly requested trim.",
        )


__all__ = [
    "DeterministicTrimStrategy",
    "SourceAssetDescriptor",
    "StrategyError",
    "StrategyProposal",
    "StrategyProvider",
]
