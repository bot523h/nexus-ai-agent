"""Opt-in image generation before the existing slideshow planning/render lane."""

from pathlib import Path
from uuid import uuid4

from nexus_ai_agent.creative.image_gen import ImageGenProvider, ImageRequest


async def generate_missing_images(
    provider: ImageGenProvider,
    *,
    workspace: Path,
    prompt: str,
    existing_count: int,
    target_count: int,
) -> tuple[Path, ...]:
    """Create only the deficit; remove partial images on failure or cancellation.

    Only text leaves the process. Uploaded photographs are never passed to the
    provider. Distinct slide indices ensure missing slides have distinct keys.
    """
    generated: list[Path] = []
    try:
        for index in range(existing_count, target_count):
            result = await provider.generate(
                ImageRequest(
                    f"{prompt}. Slideshow scene {index + 1} of {target_count}, "
                    "a complementary visual, no text or watermarks.",
                    width=1280,
                    height=720,
                )
            )
            path = workspace / f"generated_{index:02d}_{uuid4().hex}{result.extension}"
            generated.append(path)
            path.write_bytes(result.data)
        return tuple(generated)
    except BaseException:
        for path in generated:
            path.unlink(missing_ok=True)
        raise
