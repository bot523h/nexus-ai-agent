"""Completion notices for background jobs (D4).

The handlers reply "I will tell you when it is ready"; this module is the
code behind that sentence.  ``TelegramJobNotifier`` is installed as the
in-process queue's completion hook by ``build_application`` (and by
``nexus jobs resume``): once a job is durably terminal it tells the chat the
job came from.

* Story: the rendered PNG is *delivered* (``send_photo``) — before this the
  file only ever sat in ``data/temp``.
* PDF: success reports the indexed pages (and truncation); failure carries a
  reason the user can act on.  Only ``PdfExtractionError`` messages are
  user-facing; every other error class is reported generically so internal
  paths and exception text stay in the log and the job store.

Fail-safe by contract: the notifier never raises — a Telegram outage is
logged as ``job_notification_failed`` and cannot change a job's outcome.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nexus_ai_agent.adapters.in_process_job_queue import JobRecord, JobStatus
from nexus_ai_agent.jobs import PDF_JOB, STORY_JOB
from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)

_TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED})

STORY_READY_CAPTION = "🎨 استوری شما آماده شد."
STORY_FAILED_TEXT = "❌ ساخت استوری ناموفق بود. لطفاً دوباره تلاش کنید."
PDF_UNSUPPORTED_TEXT = "پردازش PDF روی این سرور فعال نیست؛ لطفاً به مدیر ربات اطلاع دهید."
PDF_UNEXPECTED_TEXT = "خطای غیرمنتظره در پردازش فایل؛ لطفاً دوباره تلاش کنید."


def _error_parts(error: str | None) -> tuple[str, str]:
    """Split the stored ``"ExcType: message"`` into ``(type, message)``."""
    if not error:
        return "", ""
    exc_type, _, message = error.partition(":")
    return exc_type.strip(), message.strip()


def _pdf_failure_text(file_name: str, error: str | None) -> str:
    exc_type, message = _error_parts(error)
    if exc_type == "PdfExtractionError":
        reason = message
    elif exc_type == "PdfSupportMissingError":
        reason = PDF_UNSUPPORTED_TEXT
    else:
        reason = PDF_UNEXPECTED_TEXT
    return f"❌ پردازش فایل «{file_name}» ناموفق بود: {reason}"


def _pdf_success_text(file_name: str, result: dict[str, object] | None) -> str:
    result = result or {}
    pages = result.get("pages")
    total = result.get("total_pages")
    text = f"✅ فایل «{file_name}» پردازش شد"
    if isinstance(pages, int):
        text += f" ({pages} صفحه)"
    text += ".\nحالا می‌توانید درباره‌ی محتوای آن سؤال بپرسید."
    if result.get("truncated") and isinstance(pages, int) and isinstance(total, int):
        text += f"\n⚠️ فقط {pages} صفحه‌ی اول از {total} صفحه پردازش شد."
    elif result.get("truncated"):
        text += "\n⚠️ به دلیل حجم زیاد، فقط بخش اول فایل پردازش شد."
    return text


class TelegramJobNotifier:
    """Queue completion hook that reports terminal jobs to their originating chat."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def __call__(self, record: JobRecord) -> None:
        if record.status not in _TERMINAL:
            return
        if record.job_type not in (PDF_JOB, STORY_JOB):
            return
        chat_id = record.payload.get("chat_id")
        if not isinstance(chat_id, int):
            log.warning(
                "job_notification_skipped",
                job_id=record.job_id,
                job_type=record.job_type,
                reason="payload has no chat_id",
            )
            return
        try:
            if record.job_type == STORY_JOB:
                await self._notify_story(chat_id, record)
            else:
                await self._notify_pdf(chat_id, record)
        except Exception as exc:
            log.error(
                "job_notification_failed",
                job_id=record.job_id,
                job_type=record.job_type,
                chat_id=chat_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _notify_story(self, chat_id: int, record: JobRecord) -> None:
        if record.status is JobStatus.FAILED:
            await self.bot.send_message(chat_id=chat_id, text=STORY_FAILED_TEXT)
            return
        output_path = str(
            (record.result or {}).get("output_path") or record.payload.get("output_path")
        )
        await self.bot.send_photo(
            chat_id=chat_id,
            photo=Path(output_path).read_bytes(),
            caption=STORY_READY_CAPTION,
        )

    async def _notify_pdf(self, chat_id: int, record: JobRecord) -> None:
        file_name = str(record.payload.get("file_name") or "PDF")
        if record.status is JobStatus.FAILED:
            text = _pdf_failure_text(file_name, record.error)
        else:
            text = _pdf_success_text(file_name, record.result)
        await self.bot.send_message(chat_id=chat_id, text=text)
