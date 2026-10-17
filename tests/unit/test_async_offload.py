"""Audit A-12 — blocking file work inside ``async def`` runs off the event loop.

The census moved 35 blocking calls out of coroutines.  The two helpers that
carry the user-visible paths are pinned here: Telegram uploads read the
artifact in a worker thread and keep the filename, and the story renderer
produces its PNG without touching the loop thread.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from telegram import InputFile

from nexus_ai_agent.bot.handlers import local_input_file
from nexus_ai_agent.features.story_gen import AIStoryGenerator


async def test_local_input_file_reads_off_the_loop_and_keeps_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "render.png"
    artifact.write_bytes(b"\x89PNG-bytes")
    loop_thread = threading.current_thread()
    seen: dict[str, threading.Thread] = {}
    original = Path.read_bytes

    def recording_read_bytes(self: Path) -> bytes:
        seen["thread"] = threading.current_thread()
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)

    upload = await local_input_file(str(artifact))
    assert upload.name == "render.png"
    assert upload.read() == b"\x89PNG-bytes"
    assert seen["thread"] is not loop_thread, "the read must not run on the event loop thread"
    # PTB wraps file-like objects itself and infers the upload name from ``.name``.
    upload.seek(0)
    wrapped = InputFile(upload)
    assert wrapped.filename == "render.png"
    assert wrapped.input_file_content == b"\x89PNG-bytes"

    renamed = await local_input_file(artifact, filename="user-facing.png")
    assert renamed.name == "user-facing.png"


async def test_story_render_runs_in_a_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = AIStoryGenerator()
    loop_thread = threading.current_thread()
    seen: dict[str, threading.Thread] = {}
    original = AIStoryGenerator._render_story

    def recording_render(self: AIStoryGenerator, text: str) -> bytes:
        seen["thread"] = threading.current_thread()
        return original(self, text)

    monkeypatch.setattr(AIStoryGenerator, "_render_story", recording_render)
    output = tmp_path / "story.png"
    await generator.generate_story_image("سلام", str(output))
    assert output.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert seen["thread"] is not loop_thread
