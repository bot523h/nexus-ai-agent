"""Background jobs of the modular monolith (R-001 / R-026).

These are the former Celery tasks (``worker.py``) as plain async handlers for
the in-process ``JobQueuePort`` adapter.  :func:`build_job_queue` is the
single composition point: it opens the adapter over the sidecar file next to
the application database and registers every job type.

Job type strings are persisted with each row — treat them as stable ids.

The engines behind these jobs (Pillow rendering; chromadb +
sentence-transformers embedding) are CPU-bound and synchronous underneath
their ``async`` signatures, so each handler pushes the engine call to a worker
thread with ``asyncio.to_thread`` — the in-process equivalent of the separate
worker process Celery provided — and the bot's event loop keeps serving
updates while a job runs.  Errors propagate to the queue, which records them
as ``failed`` + message; the old tasks returned ``"Error: ..."`` strings as
*successful* results.

Not carried over: ``nightly_channel_management``.  It was a Celery task with
no ``beat_schedule`` entry, i.e. it never ran (D2: deleted as dead code;
channel management is simulated until R-031).

Recovery (D1): nothing is resumed at boot.  :func:`run_resume` is the
operator entry point behind ``nexus jobs resume`` — dry-run by default, and
it refuses to run while another process owns the job store.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path

from nexus_ai_agent.adapters.in_process_job_queue import (
    CompletionHook,
    InProcessJobQueue,
    JobRecord,
    JobStatus,
)
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)

#: Chunk + embed an uploaded document into the user's RAG collection.
PDF_JOB = "process_pdf"
#: Render a Persian/RTL story image to disk.
STORY_JOB = "generate_story"

#: D3 — extraction is bounded: at most this many pages are read …
PDF_MAX_PAGES = 100
#: … and at most this many characters are handed to the RAG engine.
PDF_MAX_CHARACTERS = 250_000
PDF_EXTRA_HINT = "pip install 'nexus-ai-agent[pdf]'"


class PdfSupportMissingError(RuntimeError):
    """The optional ``[pdf]`` extra (pypdf) is not installed on this deployment."""


class PdfExtractionError(ValueError):
    """The upload is not a PDF we can extract text from (user-facing reason in the message)."""


def job_queue_db_path(db_path: str) -> str:
    """Sidecar file for durable job state, next to the application database.

    Deliberately *not* a table in the Alembic-managed schema (no migration) —
    the same convention as the SQLite-path lifecycle index
    (``<checkpoint>.lifecycle``).
    """
    return f"{db_path}.jobs"


def _field(payload: Mapping[str, object], key: str) -> object:
    try:
        return payload[key]
    except KeyError:
        raise KeyError(f"job payload is missing {key!r}") from None


def extract_pdf_text(file_path: str) -> tuple[str, int, int, bool]:
    """Extract text from a PDF, bounded by ``PDF_MAX_PAGES`` / ``PDF_MAX_CHARACTERS``.

    Returns ``(text, pages_read, total_pages, truncated)``.

    * Missing ``pypdf`` ⇒ :class:`PdfSupportMissingError` with the install hint
      (no UTF-8 fallback, no silent empty result).
    * Password-protected, unreadable or text-less PDFs ⇒ :class:`PdfExtractionError`
      with a reason a user can act on.  ``FileNotFoundError`` and other
      infrastructure errors propagate unchanged.
    """
    try:
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
    except ImportError as exc:
        raise PdfSupportMissingError(
            f"PDF text extraction requires the optional 'pdf' extra: {PDF_EXTRA_HINT}"
        ) from exc

    parts: list[str] = []
    characters = 0
    truncated = False
    try:
        reader = PdfReader(file_path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise PdfExtractionError("the PDF is password-protected; upload an unlocked copy")
        total_pages = len(reader.pages)
        pages_read = min(total_pages, PDF_MAX_PAGES)
        truncated = total_pages > pages_read
        for index in range(pages_read):
            page_text = (reader.pages[index].extract_text() or "").strip()
            if not page_text:
                continue
            separator = 2 if parts else 0  # the "\n\n" join below counts too
            room = PDF_MAX_CHARACTERS - characters - separator
            if room <= 0:
                truncated = True
                pages_read = index
                break
            if len(page_text) > room:
                page_text = page_text[:room]
                truncated = True
            parts.append(page_text)
            characters += separator + len(page_text)
    except PdfExtractionError:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, IndexError, RecursionError) as exc:
        # pypdf is lenient by default; anything it still cannot parse is not a
        # usable upload.  Keep the original type visible for operators.
        raise PdfExtractionError(f"not a readable PDF ({type(exc).__name__}: {exc})") from exc

    text = "\n\n".join(parts)
    if not text:
        raise PdfExtractionError(
            "the PDF has no extractable text (scanned images need OCR, which is not supported)"
        )
    return text, pages_read, total_pages, truncated


def _index_document(user_id: int, file_path: str, file_id: str) -> dict[str, object]:
    """Blocking body of :func:`process_pdf_job`; runs in a worker thread."""
    from nexus_ai_agent.features.rag import AdvancedRAGEngine

    text, pages_read, total_pages, truncated = extract_pdf_text(file_path)
    engine = AdvancedRAGEngine()
    # The engine API is ``async`` but does no real awaiting; drive it on a
    # private loop in this worker thread exactly as the Celery task did.
    asyncio.run(engine.add_document(user_id, text, {"file_id": file_id}))
    return {
        "file_id": file_id,
        "pages": pages_read,
        "total_pages": total_pages,
        "characters": len(text),
        "truncated": truncated,
    }


async def process_pdf_job(payload: dict[str, object]) -> dict[str, object]:
    """Index an uploaded PDF.  Payload: ``user_id``, ``file_path``, ``file_id``.

    D3: text is extracted with pypdf (optional ``[pdf]`` extra), bounded by
    ``PDF_MAX_PAGES`` / ``PDF_MAX_CHARACTERS``; the result reports
    ``pages`` / ``total_pages`` / ``characters`` / ``truncated``.  Errors
    propagate to the queue, which records them as ``failed`` + message.
    """
    user_id = int(str(_field(payload, "user_id")))
    file_path = str(_field(payload, "file_path"))
    file_id = str(_field(payload, "file_id"))
    return await asyncio.to_thread(_index_document, user_id, file_path, file_id)


def _render_story(text: str, output_path: str) -> None:
    """Blocking body of :func:`generate_story_job`; runs in a worker thread."""
    from nexus_ai_agent.features.story_gen import AIStoryGenerator

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(AIStoryGenerator().generate_story_image(text, output_path))


async def generate_story_job(payload: dict[str, object]) -> dict[str, object]:
    """Render a story PNG to ``output_path``.  Payload: ``user_id``, ``text``, ``output_path``."""
    text = str(_field(payload, "text"))
    output_path = str(_field(payload, "output_path"))
    await asyncio.to_thread(_render_story, text, output_path)
    return {"output_path": output_path}


def build_job_queue(settings: Settings) -> InProcessJobQueue:
    """Compose the in-process queue with every known job type registered."""
    queue = InProcessJobQueue(job_queue_db_path(settings.db_path))
    queue.register(PDF_JOB, process_pdf_job)
    queue.register(STORY_JOB, generate_story_job)
    return queue


def job_store_path(settings: Settings) -> str:
    """The sidecar file the bot's queue uses for ``settings``."""
    return job_queue_db_path(settings.db_path)


# ── D1: explicit, operator-driven recovery ───────────────────────────────


@dataclass
class ResumeReport:
    """Outcome of :func:`run_resume`; rendered by ``nexus jobs resume``."""

    store: str
    unfinished: list[JobRecord]
    applied: bool
    resumed: list[str] = field(default_factory=list)
    outcomes: dict[str, JobStatus] = field(default_factory=dict)
    notified: bool = False

    @property
    def ok(self) -> bool:
        """True when nothing was left behind: every resumed job succeeded."""
        if not self.applied:
            return True
        return all(status is JobStatus.SUCCEEDED for status in self.outcomes.values()) and len(
            self.outcomes
        ) == len(self.resumed)


#: Opens a completion hook for the duration of a resume run (e.g. a Telegram
#: notifier over an initialised bot client).  Composed in the ``bot`` layer;
#: this module stays free of Telegram imports.
NotifierFactory = Callable[[], AbstractAsyncContextManager[CompletionHook]]


class NotifierUnavailableError(RuntimeError):
    """The completion-notice channel could not be opened (e.g. Telegram unreachable)."""


async def run_resume(
    settings: Settings,
    *,
    apply: bool,
    timeout: float,
    notifier: NotifierFactory | None = None,
) -> ResumeReport:
    """Report — and with ``apply`` re-run — jobs a previous process left unfinished.

    * Dry run (``apply=False``) only reads the store.
    * ``apply=True`` requires store ownership: :class:`JobStoreBusyError`
      propagates when the bot (or another operator) holds the lock, so a
      live ``running`` row is never executed twice.
    * ``notifier`` (optional) opens the completion hook for the run — the
      CLI passes the same Telegram notifier the bot uses, so users still get
      their story / PDF notice.  It is opened *before* anything is resumed,
      so a failure there resumes nothing.
    * Waits up to ``timeout`` seconds for the resumed jobs, then records each
      one's status; jobs still running after the timeout are cancelled and
      stay ``running`` (eligible for another resume).
    """
    queue = build_job_queue(settings)
    report = ResumeReport(
        store=str(queue.db_path), unfinished=await queue.list_unfinished(), applied=apply
    )
    if not apply or not report.unfinished:
        await queue.close()
        return report

    async def _apply() -> None:
        report.resumed = await queue.resume_pending()
        await queue.close(timeout=timeout)
        for job_id in report.resumed:
            record = await queue.get_job(job_id)
            if record is not None:
                report.outcomes[job_id] = record.status

    try:
        if notifier is not None:
            async with notifier() as hook:
                queue.set_completion_hook(hook)
                report.notified = True
                await _apply()
        else:
            await _apply()
    finally:
        await queue.close(timeout=0)
    log.info(
        "jobs_resume_finished",
        resumed=len(report.resumed),
        outcomes={k: v.value for k, v in report.outcomes.items()},
    )
    return report
