"""Unavailable caption adapter for profiles without installed speech models.

Fails closed with a typed CaptionProfileUnavailableError. Never falls back silently.
"""

from __future__ import annotations

from pathlib import Path

from nexus_ai_agent.application.ports.caption_engine import (
    CaptionEnginePort,
    CaptionProfileUnavailableError,
)
from nexus_ai_agent.creative.packs.caption.models import TranscriptRef


class UnavailableCaptionAdapter(CaptionEnginePort):
    """Caption engine adapter for headless or unconfigured profiles."""

    def __init__(
        self,
        reason: str = "No caption profile configured on this installation",
    ) -> None:
        self._reason = reason

    def is_available(self) -> bool:
        """Always reports False since no native caption profile is loaded."""
        return False

    async def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
    ) -> TranscriptRef:
        """Fail closed with a typed error. Silent fallback is prohibited."""
        raise CaptionProfileUnavailableError(
            f"caption_profile_unavailable: {self._reason} (audio: {audio_path})"
        )


UnavailableCaptionEngine = UnavailableCaptionAdapter
