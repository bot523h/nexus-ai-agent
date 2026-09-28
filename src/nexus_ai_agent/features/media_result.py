"""Shared file-producing engine contract (legacy dictionaries stay compatible).

Image and TTS metadata differ, but success/path/error never do. TypedDict
catches key drift at the producer; media_path rejects malformed or failed
results at the consumer. It does not open/delete the engine-owned cached file.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict


class MediaResult(TypedDict):
    success: bool
    path: str | None
    error: str | None


class ImageResult(MediaResult):
    prompt: str
    style: str
    size: str


class SpeechResult(MediaResult):
    lang: str


class MediaContractError(ValueError):
    """A file-producing engine did not return an unambiguous success."""


def media_path(result: Mapping[str, object]) -> Path:
    """Require canonical success before attempting I/O; never guess alias keys."""
    path = result.get("path")
    if (
        result.get("success") is not True
        or "error" not in result
        or result["error"] is not None
        or not isinstance(path, str)
        or not path.strip()
        or "\x00" in path
    ):
        raise MediaContractError("invalid media result: expected success=True, path, error=None")
    return Path(path)
