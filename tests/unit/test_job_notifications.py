"""D4 — the "I will notify you" reply is backed by code.

``TelegramJobNotifier`` is the queue's completion hook wired by
``build_application``: when a job reaches a terminal state it tells the
originating chat — the story PNG is *delivered* (it used to sit in
``data/temp`` forever), PDF indexing reports pages, and failures carry a
reason a user can act on.  Everything here runs against a recording fake of
the PTB ``Bot``; the notifier must never raise (fail-safe, logged).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from nexus_ai_agent import jobs
from nexus_ai_agent.adapters.in_process_job_queue import JobRecord, JobStatus
from nexus_ai_agent.bot.job_notifications import TelegramJobNotifier

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class FakeBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[dict[str, Any]] = []
        self.photos: list[dict[str, Any]] = []
        self.fail = fail

    async def send_message(self, *, chat_id: int, text: str, **kwargs: Any) -> None:
        if self.fail:
            raise ConnectionError("telegram down")
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})

    async def send_photo(self, *, chat_id: int, photo: bytes, caption: str, **kwargs: Any) -> None:
        if self.fail:
            raise ConnectionError("telegram down")
        self.photos.append({"chat_id": chat_id, "photo": photo, "caption": caption, **kwargs})


def _record(
    job_type: str,
    status: JobStatus,
    payload: dict[str, object],
    *,
    result: dict[str, object] | None = None,
    error: str | None = None,
) -> JobRecord:
    return JobRecord(
        job_id="job-1",
        job_type=job_type,
        idempotency_key="k",
        status=status,
        payload=payload,
        result=result,
        error=error,
        attempts=1,
        created_at="2026-09-20T00:00:00+00:00",
        updated_at="2026-09-20T00:00:01+00:00",
    )


async def test_story_success_delivers_the_png(tmp_path: Path) -> None:
    png = tmp_path / "story.png"
    png.write_bytes(PNG_MAGIC + b"payload")
    bot = FakeBot()
    record = _record(
        jobs.STORY_JOB,
        JobStatus.SUCCEEDED,
        {"chat_id": 777, "user_id": 9, "text": "x", "output_path": str(png)},
        result={"output_path": str(png)},
    )

    await TelegramJobNotifier(bot)(record)

    assert bot.messages == []
    assert len(bot.photos) == 1
    assert bot.photos[0]["chat_id"] == 777
    assert bot.photos[0]["photo"] == PNG_MAGIC + b"payload"
    assert "استوری" in bot.photos[0]["caption"]


async def test_story_failure_tells_the_chat() -> None:
    bot = FakeBot()
    record = _record(
        jobs.STORY_JOB,
        JobStatus.FAILED,
        {"chat_id": 777, "user_id": 9, "text": "x", "output_path": "nope.png"},
        error="OSError: cannot open font",
    )
    await TelegramJobNotifier(bot)(record)
    assert bot.photos == []
    assert len(bot.messages) == 1
    assert bot.messages[0]["chat_id"] == 777
    assert bot.messages[0]["text"].startswith("❌")
    # internal error text is not leaked to the user
    assert "OSError" not in bot.messages[0]["text"]


async def test_pdf_success_reports_pages_and_truncation() -> None:
    bot = FakeBot()
    record = _record(
        jobs.PDF_JOB,
        JobStatus.SUCCEEDED,
        {
            "chat_id": 5,
            "user_id": 1,
            "file_path": "x.pdf",
            "file_id": "f",
            "file_name": "paper.pdf",
        },
        result={"file_id": "f", "pages": 2, "total_pages": 2, "characters": 10, "truncated": False},
    )
    await TelegramJobNotifier(bot)(record)
    assert len(bot.messages) == 1
    text = bot.messages[0]["text"]
    assert text.startswith("✅") and "paper.pdf" in text and "2" in text
    assert "اول" not in text  # no truncation notice

    bot = FakeBot()
    record = _record(
        jobs.PDF_JOB,
        JobStatus.SUCCEEDED,
        {"chat_id": 5, "user_id": 1, "file_path": "x.pdf", "file_id": "f", "file_name": "big.pdf"},
        result={
            "file_id": "f",
            "pages": 100,
            "total_pages": 340,
            "characters": 1,
            "truncated": True,
        },
    )
    await TelegramJobNotifier(bot)(record)
    assert "100" in bot.messages[0]["text"] and "340" in bot.messages[0]["text"]


@pytest.mark.parametrize(
    ("error", "fragment", "leaked"),
    [
        (
            "PdfExtractionError: the PDF is password-protected; upload an unlocked copy",
            "password-protected",
            None,
        ),
        (
            "PdfSupportMissingError: PDF text extraction requires the 'pdf' extra: pip install",
            "فعال نیست",
            "pip install",
        ),
        ("FileNotFoundError: [Errno 2] /srv/data/temp/f.pdf", "غیرمنتظره", "/srv/data"),
    ],
)
async def test_pdf_failure_reasons_are_user_facing(
    error: str, fragment: str, leaked: str | None
) -> None:
    """Only the user-actionable class carries its message; internals stay in the log/store."""
    bot = FakeBot()
    record = _record(
        jobs.PDF_JOB,
        JobStatus.FAILED,
        {"chat_id": 5, "user_id": 1, "file_path": "x.pdf", "file_id": "f", "file_name": "a.pdf"},
        error=error,
    )
    await TelegramJobNotifier(bot)(record)
    text = bot.messages[0]["text"]
    assert text.startswith("❌") and "a.pdf" in text and fragment in text
    if leaked:
        assert leaked not in text


async def test_notifier_is_fail_safe() -> None:
    """Telegram errors are logged, never raised into the job runner."""
    bot = FakeBot(fail=True)
    record = _record(
        jobs.PDF_JOB,
        JobStatus.SUCCEEDED,
        {"chat_id": 5, "file_name": "a.pdf"},
        result={"pages": 1, "total_pages": 1, "truncated": False},
    )
    with capture_logs() as logs:
        await TelegramJobNotifier(bot)(record)
    assert any(entry["event"] == "job_notification_failed" for entry in logs)


async def test_notifier_skips_records_without_a_chat() -> None:
    bot = FakeBot()
    record = _record(jobs.STORY_JOB, JobStatus.SUCCEEDED, {"text": "x"}, result={})
    with capture_logs() as logs:
        await TelegramJobNotifier(bot)(record)
    assert bot.messages == [] and bot.photos == []
    assert any(entry["event"] == "job_notification_skipped" for entry in logs)


async def test_notifier_ignores_non_terminal_and_unknown_jobs() -> None:
    bot = FakeBot()
    notifier = TelegramJobNotifier(bot)
    await notifier(_record(jobs.STORY_JOB, JobStatus.RUNNING, {"chat_id": 1}))
    await notifier(_record("some_other_job", JobStatus.SUCCEEDED, {"chat_id": 1}, result={}))
    assert bot.messages == [] and bot.photos == []
