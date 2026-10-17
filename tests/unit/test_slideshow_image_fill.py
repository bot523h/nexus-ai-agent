"""Behaviour of ``creative/slideshow/image_fill`` (audit 2026-10-16).

This module is reachable from ``worker.py``, writes files into the operator's
workspace and is the only place that hands a *text* prompt to an image provider
during a slideshow render.  The contract that matters operationally is:

* only the deficit is generated (``existing_count`` … ``target_count - 1``), so
  a retry after a partial run does not pay for the images that already exist;
* uploaded photographs never reach the provider — the request is text only;
* a failure or a **cancellation** removes the partially written images, so the
  next attempt cannot mistake a truncated PNG for a finished slide.

A fake provider records the requests instead of talking to a network; every
assertion reads a file on disk or a recorded request.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from nexus_ai_agent.creative.image_gen import GeneratedImage, ImageRequest
from nexus_ai_agent.creative.slideshow import image_fill


@dataclass
class FakeProvider:
    """Records requests; can fail or hang on a chosen index."""

    fail_on_call: int | None = None
    data: bytes = b"\x89PNG\r\n\x1a\nfake"
    requests: list[ImageRequest] = field(default_factory=list)
    calls: int = 0

    async def generate(self, request: ImageRequest) -> GeneratedImage:
        self.calls += 1
        self.requests.append(request)
        if self.fail_on_call == self.calls:
            raise RuntimeError("provider exploded")
        return GeneratedImage(data=self.data, mime_type="image/png")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "render"
    directory.mkdir()
    return directory


async def test_only_the_deficit_is_generated(workspace: Path) -> None:
    provider = FakeProvider()

    paths = await image_fill.generate_missing_images(
        provider, workspace=workspace, prompt="a calm sea", existing_count=2, target_count=5
    )

    assert len(paths) == 3, "three slides were missing"
    assert [request.prompt for request in provider.requests] == [
        "a calm sea. Slideshow scene 3 of 5, a complementary visual, no text or watermarks.",
        "a calm sea. Slideshow scene 4 of 5, a complementary visual, no text or watermarks.",
        "a calm sea. Slideshow scene 5 of 5, a complementary visual, no text or watermarks.",
    ]
    assert {request.width for request in provider.requests} == {1280}
    assert {request.height for request in provider.requests} == {720}
    for path in paths:
        assert path.parent == workspace
        assert path.read_bytes() == provider.data


async def test_nothing_is_requested_when_the_target_is_already_met(workspace: Path) -> None:
    provider = FakeProvider()
    paths = await image_fill.generate_missing_images(
        provider, workspace=workspace, prompt="p", existing_count=4, target_count=4
    )
    assert paths == ()
    assert provider.calls == 0


async def test_generated_names_are_unique_per_slide(workspace: Path) -> None:
    provider = FakeProvider()
    paths = await image_fill.generate_missing_images(
        provider, workspace=workspace, prompt="p", existing_count=0, target_count=4
    )
    assert len(set(paths)) == 4
    assert sorted(path.name for path in paths) == [path.name for path in paths], (
        "slides must be returned in render order"
    )
    assert len(list(workspace.iterdir())) == 4


async def test_provider_failure_leaves_no_partial_image_behind(workspace: Path) -> None:
    provider = FakeProvider(fail_on_call=2)

    with pytest.raises(RuntimeError, match="provider exploded"):
        await image_fill.generate_missing_images(
            provider, workspace=workspace, prompt="p", existing_count=0, target_count=3
        )

    assert list(workspace.iterdir()) == [], "the first image must be cleaned up on failure"


async def test_cancellation_also_cleans_up(workspace: Path) -> None:
    class Cancelling(FakeProvider):
        """Raises ``CancelledError`` on the second call (a cancelled render)."""

        async def generate(self, request: ImageRequest) -> GeneratedImage:
            self.calls += 1
            self.requests.append(request)
            if self.calls == 2:
                raise asyncio.CancelledError
            return GeneratedImage(data=self.data, mime_type="image/png")

    provider = Cancelling()
    with pytest.raises(asyncio.CancelledError):
        await image_fill.generate_missing_images(
            provider, workspace=workspace, prompt="p", existing_count=0, target_count=3
        )

    assert list(workspace.iterdir()) == []


async def test_existing_files_are_never_touched(workspace: Path) -> None:
    existing = workspace / "photo_01.png"
    existing.write_bytes(b"operator upload")
    provider = FakeProvider()

    await image_fill.generate_missing_images(
        provider, workspace=workspace, prompt="p", existing_count=1, target_count=2
    )

    assert existing.read_bytes() == b"operator upload"
