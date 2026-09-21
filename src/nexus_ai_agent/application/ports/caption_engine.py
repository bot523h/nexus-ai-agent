"""Provider-neutral caption engine port and typed errors."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from nexus_ai_agent.creative.packs.caption.models import TranscriptRef


class CaptionEngineError(Exception):
    """Base exception for caption engine errors."""


class CaptionProfileUnavailableError(CaptionEngineError):
    """Raised when caption transcription is requested on an unconfigured installation.

    Silent fallbacks to mock or cloud providers are strictly prohibited.
    """

    code: str = "caption_profile_unavailable"

    def __init__(
        self,
        message: str = (
            "No caption profile is available on this installation (caption_profile_unavailable)."
        ),
    ) -> None:
        super().__init__(message)


class CaptionEnginePort(Protocol):
    """Application port for local/native caption transcription engines."""

    async def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
    ) -> TranscriptRef: ...

    def is_available(self) -> bool: ...
