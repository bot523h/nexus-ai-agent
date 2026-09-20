"""Telegram handlers submit background work through ``JobQueuePort`` (R-001 / R-026).

The handlers must not know *how* a job runs: they enqueue through the port
they find in ``bot_data`` and reply.  A recording fake of the port is enough
to prove the contract; the adapter itself is covered in
``tests/integration/test_in_process_job_queue.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent import jobs
from nexus_ai_agent.bot import handlers as handlers_module
from nexus_ai_agent.config.settings import Settings


class FakeJobQueue:
    """Structural ``JobQueuePort`` double that records submissions."""

    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []

    async def enqueue(
        self, *, job_type: str, idempotency_key: str, payload: dict[str, object]
    ) -> str:
        self.submissions.append(
            {"job_type": job_type, "idempotency_key": idempotency_key, "payload": payload}
        )
        return f"job-{len(self.submissions)}"

    async def get_status(self, job_id: str) -> str:
        return "pending"


class FakeMessage:
    def __init__(self, *, message_id: int, document: Any = None) -> None:
        self.message_id = message_id
        self.document = document
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


class FakeFile:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self._content)


class FakeDocument:
    def __init__(self, file_id: str, file_name: str, content: bytes) -> None:
        self.file_id = file_id
        self.file_unique_id = f"u-{file_id}"
        self.file_name = file_name
        self._content = content

    async def get_file(self) -> FakeFile:
        return FakeFile(self._content)


def _update(message: FakeMessage, *, user_id: int = 555, chat_id: int = 777) -> Any:
    return SimpleNamespace(
        message=message,
        edited_message=None,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
    )


def _context(queue: Any, *, args: list[str] | None = None) -> Any:
    bot_data: dict[str, Any] = {}
    if queue is not None:
        bot_data["job_queue"] = queue
    return SimpleNamespace(bot_data=bot_data, args=args or [])


@pytest.fixture()
def in_tmp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Handlers write uploads under the relative ``data/temp`` directory."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


async def test_pdf_handler_enqueues_through_the_port(in_tmp_cwd: Path) -> None:
    queue = FakeJobQueue()
    message = FakeMessage(message_id=31, document=FakeDocument("f1", "paper.pdf", b"hello world"))

    await handlers_module.pdf_handler(_update(message), _context(queue))

    assert len(queue.submissions) == 1
    job = queue.submissions[0]
    assert job["job_type"] == jobs.PDF_JOB
    # One upload message ⇒ one job; a redelivered update collapses onto it.
    assert job["idempotency_key"] == "pdf:777:31"
    assert job["payload"]["user_id"] == 555
    assert job["payload"]["file_id"] == "f1"
    assert job["payload"]["chat_id"] == 777  # D4: the completion notice goes here
    assert job["payload"]["file_name"] == "paper.pdf"
    saved = Path(str(job["payload"]["file_path"]))
    assert saved == Path("data/temp/f1.pdf")
    assert saved.read_bytes() == b"hello world"
    assert message.replies and "paper.pdf" in message.replies[0]


async def test_story_handler_enqueues_through_the_port(in_tmp_cwd: Path) -> None:
    queue = FakeJobQueue()
    message = FakeMessage(message_id=8)

    await handlers_module.story_cmd_handler(
        _update(message, user_id=9), _context(queue, args=["سلام", "دنیا"])
    )

    assert len(queue.submissions) == 1
    job = queue.submissions[0]
    assert job["job_type"] == jobs.STORY_JOB
    assert job["idempotency_key"] == "story:777:8"
    assert job["payload"]["user_id"] == 9
    assert job["payload"]["chat_id"] == 777  # D4: the PNG is delivered here
    assert job["payload"]["text"] == "سلام دنیا"
    output = str(job["payload"]["output_path"])
    assert output.startswith("data/temp/story_9_") and output.endswith(".png")
    assert os.path.isdir("data/temp")
    assert len(message.replies) == 2


async def test_story_handler_requires_text(in_tmp_cwd: Path) -> None:
    queue = FakeJobQueue()
    message = FakeMessage(message_id=1)
    await handlers_module.story_cmd_handler(_update(message), _context(queue, args=[]))
    assert queue.submissions == []
    assert message.replies == ["❌ استفاده: /story [متن]"]


@pytest.mark.parametrize("handler_name", ["pdf_handler", "story_cmd_handler"])
async def test_missing_job_queue_is_a_visible_error_not_a_silent_drop(
    in_tmp_cwd: Path, handler_name: str
) -> None:
    message = FakeMessage(message_id=2, document=FakeDocument("f2", "x.pdf", b"bytes"))
    handler = getattr(handlers_module, handler_name)
    await handler(_update(message), _context(None, args=["text"]))
    assert message.replies == [handlers_module.JOBS_UNAVAILABLE_TEXT]


def test_build_application_wires_the_in_process_queue(
    settings_override: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Composition root: one in-process queue per application, both jobs registered."""
    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
    from nexus_ai_agent.bot.app import build_application

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    application = build_application(
        settings_override, graph=object(), storage=object(), session_factory=lambda: None
    )
    queue = application.bot_data["job_queue"]
    assert isinstance(queue, InProcessJobQueue)
    assert set(queue.handlers) == {jobs.PDF_JOB, jobs.STORY_JOB}
    assert str(queue.db_path) == jobs.job_queue_db_path(settings_override.db_path)
    # Graceful shutdown drains in-flight jobs in post_stop (bot still usable);
    # both run modes honour it (run_polling natively, the webhook runner explicitly).
    assert application.post_stop is not None
    assert application.post_shutdown is None
    assert application.post_init is None, "nothing is resumed implicitly at startup"
    # D4: terminal jobs notify the originating chat through the application's bot.
    from nexus_ai_agent.bot.job_notifications import TelegramJobNotifier

    hook = queue.completion_hook
    assert isinstance(hook, TelegramJobNotifier)
    assert hook.bot is application.bot


async def test_application_shutdown_drains_in_flight_jobs(
    settings_override: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ``post_stop`` hook lets a running story job finish instead of destroying it."""
    from nexus_ai_agent.adapters.in_process_job_queue import JobStatus
    from nexus_ai_agent.bot.app import build_application

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    application = build_application(
        settings_override, graph=object(), storage=object(), session_factory=lambda: None
    )
    queue = application.bot_data["job_queue"]
    output = tmp_path / "story.png"
    job_id = await queue.enqueue(
        job_type=jobs.STORY_JOB,
        idempotency_key="story:drain",
        payload={"user_id": 1, "text": "خاموشی امن", "output_path": str(output)},
    )
    assert queue.in_flight == 1

    assert application.post_stop is not None
    await application.post_stop(application)  # what both run modes invoke after stop()

    assert queue.in_flight == 0
    assert await queue.get_status(job_id) == JobStatus.SUCCEEDED
    assert output.exists()


async def test_story_job_result_is_delivered_to_the_chat(
    settings_override: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end through the real application wiring: enqueue → render → send_photo."""
    from nexus_ai_agent.adapters.in_process_job_queue import JobStatus
    from nexus_ai_agent.bot.app import build_application

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    application = build_application(
        settings_override, graph=object(), storage=object(), session_factory=lambda: None
    )
    sent: list[dict[str, Any]] = []

    async def fake_send_photo(
        self: Any, *, chat_id: int, photo: bytes, caption: str, **kwargs: Any
    ) -> None:
        sent.append({"chat_id": chat_id, "size": len(photo), "caption": caption})

    # PTB bot instances are immutable; intercept at the class (restored by monkeypatch).
    monkeypatch.setattr(type(application.bot), "send_photo", fake_send_photo)
    queue = application.bot_data["job_queue"]
    output = tmp_path / "story.png"
    job_id = await queue.enqueue(
        job_type=jobs.STORY_JOB,
        idempotency_key="story:e2e",
        payload={"chat_id": 4242, "user_id": 1, "text": "تحویل واقعی", "output_path": str(output)},
    )
    assert await queue.wait(job_id, timeout=30) == JobStatus.SUCCEEDED
    await queue.close()
    assert sent and sent[0]["chat_id"] == 4242 and sent[0]["size"] == output.stat().st_size
