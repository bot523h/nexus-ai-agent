"""Transport-independent image generation contract; no bot or storage imports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ImageGenerationError(RuntimeError):
    """A sanitized provider failure, safe to expose without request URLs or keys."""


class PaidTierRequiredError(ImageGenerationError):
    """The operator has not authorized paid image generation."""


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    width: int = 1024
    height: int = 1024
    seed: int | None = None

    def __post_init__(self) -> None:
        if not self.prompt.strip() or len(self.prompt) > 2000:
            raise ValueError("prompt must contain 1–2000 characters")
        if not (256 <= self.width <= 2048 and 256 <= self.height <= 2048):
            raise ValueError("image dimensions must be between 256 and 2048")
        if self.seed is not None and not 0 <= self.seed <= 2**31 - 1:
            raise ValueError("seed must be a non-negative 32-bit integer")


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    mime_type: str

    @property
    def extension(self) -> str:
        return {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[self.mime_type]


class ImageGenProvider(Protocol):
    async def generate(self, request: ImageRequest) -> GeneratedImage:
        """Generate validated image bytes; cache hits incur no provider request."""
        ...
