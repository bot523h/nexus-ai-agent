"""Creative completion notifier (task-166, P0-B): the last mile to the user.

Reproduced on main: creative jobs had no completion branch at all — results
(workspaces, artifacts) died silently. This notifier must:

* deliver the measured artifact (video or document) with a translated caption,
* translate typed failures (never a raw code/key),
* own workspace cleanup in all outcomes (success, typed failure, FAILED job).

Telegram is faked at the ``telegram.Bot`` boundary; the i18n strings are the
REAL catalog values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import JobCompletion
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.bot.app import _notify_creative_completion
from nexus_ai_agent.i18n import i18n


@dataclass
class _FakeBot:
    sent_messages: list[tuple[int, str]] = field(default_factory=list)
    sent_videos: list[tuple[int, bytes, str]] = field(default_factory=list)
    sent_documents: list[tuple[int, bytes, str]] = field(default_factory=list)

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.sent_messages.append((chat_id, text))

    async def send_video(self, *, chat_id: int, video: Any, caption: str, **kw: Any) -> None:
        self.sent_videos.append((chat_id, video.read(), caption))

    async def send_document(self, *, chat_id: int, document: Any, caption: str) -> None:
        self.sent_documents.append((chat_id, document.read(), caption))


def _completion(**overrides: Any) -> JobCompletion:
    base: dict[str, Any] = {
        "job_id": "job-1",
        "job_type": "creative_render",
        "status": JobStatus.COMPLETED,
        "result": {"success": True},
        "error": None,
        "payload": {
            "command": "edit",
            "operation": "trim",
            "chat_id": 4242,
            "lang": "fa",
        },
    }
    base.update(overrides)
    return JobCompletion(**base)


@pytest.mark.asyncio
async def test_success_delivers_video_with_translated_caption_in_persian(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "creative_abc123def456"
    workspace.mkdir()
    artifact = workspace / "output.mp4"
    artifact.write_bytes(b"fake-video-bytes")
    fake = _FakeBot()
    monkeypatch.setattr("telegram.Bot", lambda token: fake)

    await _notify_creative_completion(
        _completion(
            result={
                "success": True,
                "artifact_path": str(artifact),
                "artifact_kind": "video",
                "duration_us": 5_000_000,
                "workspace_dir": str(workspace),
            }
        ),
        token="dummy",
    )

    assert len(fake.sent_videos) == 1
    chat_id, video_bytes, caption = fake.sent_videos[0]
    assert chat_id == 4242
    assert video_bytes == b"fake-video-bytes"
    assert caption == i18n.t("creative.completed", lang="fa", command="edit", operation="trim")
    assert "creative." not in caption
    assert not workspace.exists(), "workspace must be cleaned after delivery"


@pytest.mark.asyncio
async def test_document_kind_goes_out_as_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "creative_abc123def456"
    workspace.mkdir()
    artifact = workspace / "captions.srt"
    artifact.write_text("1\n00:00:00,000 --> 00:00:01,000\nسلام\n", encoding="utf-8")
    fake = _FakeBot()
    monkeypatch.setattr("telegram.Bot", lambda token: fake)

    await _notify_creative_completion(
        _completion(
            result={
                "success": True,
                "artifact_path": str(artifact),
                "artifact_kind": "document",
                "workspace_dir": str(workspace),
            },
            payload={"command": "caption", "operation": "transcribe", "chat_id": 7, "lang": "en"},
        ),
        token="dummy",
    )
    assert len(fake.sent_documents) == 1
    assert fake.sent_documents[0][1].startswith(b"1\n00:00:00,000")


@pytest.mark.asyncio
async def test_typed_failure_is_translated_and_cleans_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "creative_deadbeef0000"
    workspace.mkdir()
    fake = _FakeBot()
    monkeypatch.setattr("telegram.Bot", lambda token: fake)

    await _notify_creative_completion(
        _completion(
            result={
                "success": False,
                "error_code": "caption_profile_unavailable",
                "error_detail": "no caption engine on this installation",
                "workspace_dir": str(workspace),
            },
            payload={"command": "caption", "operation": "transcribe", "chat_id": 7, "lang": "fa"},
        ),
        token="dummy",
    )
    assert fake.sent_messages, "expected a user-visible failure message"
    text = fake.sent_messages[0][1]
    assert "creative." not in text and "error_code" not in text
    # task-181: failure copy carries the failure-class line (never success)
    # and then the translated typed message.
    assert text.endswith(
        i18n.t("creative.failed.caption_profile_unavailable", lang="fa", detail="x")
    )
    assert text.startswith("❌") or text.startswith("⚠️")
    assert not workspace.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "head"),
    [
        (JobStatus.FAILED_RETRYABLE, "⚠️"),
        (JobStatus.FAILED_TERMINAL, "❌"),
    ],
)
async def test_failed_job_status_maps_to_internal_typed_message(
    monkeypatch: pytest.MonkeyPatch, status: JobStatus, head: str
) -> None:
    # task-181: both failure states map to *failure* copy (visibly distinct
    # class lines), never success.
    fake = _FakeBot()
    monkeypatch.setattr("telegram.Bot", lambda token: fake)
    await _notify_creative_completion(
        _completion(status=status, error="boom", result={}),
        token="dummy",
    )
    text = fake.sent_messages[0][1]
    assert text.startswith(head)
    assert text.endswith(i18n.t("creative.failed.internal", lang="fa", detail="boom"))
    assert "boom" not in text  # details go to logs, not the user stub
    assert "✅" not in text
