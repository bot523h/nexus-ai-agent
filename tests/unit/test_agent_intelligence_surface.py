"""Telegram /agent_edit staging and durable-workspace ownership tests."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nexus_ai_agent.bot.agent_intelligence_surface import build_agent_intelligence_handler
from nexus_ai_agent.bot.creative_surface import idempotency_key
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.creative.spine.compiler import request_idempotency_key
from nexus_ai_agent.i18n import i18n


class _TelegramFile:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def download_to_drive(self, custom_path: Path) -> None:
        custom_path.write_bytes(self.content)


class _Bot:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def get_file(self, file_id: str) -> _TelegramFile:
        assert file_id == "telegram-file-1"
        return _TelegramFile(self.content)


class _Graph:
    def __init__(self, *, response: dict[str, Any] | None = None, fail: bool = False) -> None:
        self.response = response or {
            "response": "queued",
            "agent_intelligence": {"status": "pending", "job_id": "job-1"},
        }
        self.fail = fail
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def ainvoke(self, state: dict[str, Any], *, config: dict[str, Any]) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("simulated graph failure after request dispatch")
        self.calls.append((state, config))
        return {**state, **self.response}


def _update() -> tuple[Any, list[str]]:
    replies: list[str] = []

    async def reply_text(text: str) -> None:
        replies.append(text)

    media = SimpleNamespace(file_id="telegram-file-1", file_size=32, mime_type="video/mp4")
    original = SimpleNamespace(video=media, document=None)
    message = SimpleNamespace(
        reply_to_message=original,
        message_id=700,
        reply_text=reply_text,
    )
    update = SimpleNamespace(
        message=message,
        edited_message=None,
        effective_user=SimpleNamespace(id=42, language_code="tr"),
        effective_chat=SimpleNamespace(id=-1004242),
    )
    return update, replies


def _context(content: bytes, args: list[str] | None = None) -> Any:
    return SimpleNamespace(bot=_Bot(content), args=args or ["trim between zero and one second"])


def _patch_probe(monkeypatch: Any) -> None:
    from nexus_ai_agent.creative.slideshow import ffmpeg

    monkeypatch.setattr(ffmpeg, "resolve_ffmpeg_bin", lambda: "not-invoked-by-fake-probe")
    monkeypatch.setattr(
        ffmpeg,
        "probe_video",
        lambda _path, binary=None: SimpleNamespace(
            duration_us=2_000_000,
            width=320,
            height=240,
            frame_rate_milli=15_000,
        ),
    )


def test_agent_edit_stages_reply_and_reuses_immutable_media_on_duplicate(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _patch_probe(monkeypatch)
    content = b"stable staged media bytes"
    root = tmp_path / "creative-root"
    root.mkdir()
    settings = Settings(creative_temp_dir=str(root))
    graph = _Graph()
    update, replies = _update()
    handler = build_agent_intelligence_handler(graph, settings)

    async def _run_twice() -> None:
        await handler(update, _context(content))
        state, config = graph.calls[-1]
        request_id = idempotency_key(42, -1004242, 700)
        expected_project = f"shot-{request_idempotency_key(request_id)}"
        staged = Path(state["creative_asset"]["input_path"])
        assert state["correlation_id"] == request_id
        assert state["creative_asset"]["project_id"] == expected_project
        assert state["creative_asset"]["source"]["content_sha256"] == (
            "sha256:" + hashlib.sha256(content).hexdigest()
        )
        assert Path(state["creative_asset"]["workspace_dir"]) == staged.parent
        assert config["configurable"]["thread_id"] == "tg:-1004242"
        inode_before = staged.stat().st_ino

        await handler(update, _context(content))
        assert staged.stat().st_ino == inode_before, (
            "duplicate updates must not replace a live input"
        )
        assert staged.read_bytes() == content
        assert not list(staged.parent.glob(".input-*.part.mp4"))

    asyncio.run(_run_twice())
    assert len(graph.calls) == 2
    assert replies == ["queued", "queued"]


def test_agent_edit_different_media_for_same_request_does_not_destroy_queued_input(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _patch_probe(monkeypatch)
    root = tmp_path / "creative-root"
    root.mkdir()
    settings = Settings(creative_temp_dir=str(root))
    graph = _Graph()
    update, replies = _update()
    handler = build_agent_intelligence_handler(graph, settings)
    original = b"first immutable source"

    async def _run() -> None:
        await handler(update, _context(original))
        staged = Path(graph.calls[0][0]["creative_asset"]["input_path"])
        await handler(update, _context(b"different source under the same Telegram request id"))
        assert staged.read_bytes() == original
        assert len(graph.calls) == 1

    asyncio.run(_run())
    assert len(replies) == 2
    assert replies[1] == i18n.t("creative.media_download_failed", lang="tr")


def test_graph_exception_preserves_workspace_when_enqueue_ownership_is_uncertain(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _patch_probe(monkeypatch)
    root = tmp_path / "creative-root"
    root.mkdir()
    settings = Settings(creative_temp_dir=str(root))
    graph = _Graph(fail=True)
    update, replies = _update()
    handler = build_agent_intelligence_handler(graph, settings)
    content = b"retain until durable queue ownership is known"

    asyncio.run(handler(update, _context(content)))

    workspace = next(root.glob("creative_*"))
    assert (workspace / "input.mp4").read_bytes() == content
    assert replies and "safely" in replies[-1]


def test_uncertain_queue_enqueue_preserves_workspace_for_durable_recovery(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _patch_probe(monkeypatch)
    root = tmp_path / "creative-root"
    root.mkdir()
    settings = Settings(creative_temp_dir=str(root))
    graph = _Graph(
        response={
            "response": "The durable queue could not confirm the request.",
            "agent_intelligence": {
                "status": "failed",
                "error_code": "queue_enqueue_failed",
                "job_id": None,
            },
        }
    )
    update, replies = _update()
    handler = build_agent_intelligence_handler(graph, settings)
    content = b"the queue may have committed before its acknowledgement failed"

    asyncio.run(handler(update, _context(content)))

    workspace = next(root.glob("creative_*"))
    assert (workspace / "input.mp4").read_bytes() == content
    assert replies == ["The durable queue could not confirm the request."]


def test_agent_edit_without_request_text_never_downloads_or_invokes_graph(
    tmp_path: Path,
) -> None:
    root = tmp_path / "creative-root"
    root.mkdir()
    settings = Settings(creative_temp_dir=str(root))
    graph = _Graph()
    update, replies = _update()
    handler = build_agent_intelligence_handler(graph, settings)
    context = _context(b"unused", args=[])

    asyncio.run(handler(update, context))

    assert graph.calls == []
    assert list(root.iterdir()) == []
    assert replies
