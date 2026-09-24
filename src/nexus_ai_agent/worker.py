"""Application-owned background job implementations.

The functions in this module are ordinary async jobs. They are executed by
``InProcessJobQueue`` on the bot process event loop; no broker or worker
process is required.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

JobHandler = Callable[[dict[str, object]], Awaitable[dict[str, object]]]


def job_queue_db_path(db_path: str | Path) -> Path:
    """Return the SQLite sidecar path owned by the in-process job queue.

    Single source of truth for every composition root (bot process, CLI
    drain command) so they all address the same durable queue.
    """
    return Path(f"{db_path}.jobs.sqlite3")


def default_job_handlers() -> dict[str, JobHandler]:
    """Map every application job type to its handler.

    Composition roots register the full map so a resumed job always finds
    its handler, regardless of which process drains the queue.
    """
    from nexus_ai_agent.creative.render_jobs import creative_render_job
    from nexus_ai_agent.creative.slideshow.worker_adapter import slideshow_render_job

    return {
        "pdf_extract": process_pdf_job,
        "story": generate_story_job,
        # Wave 2.5: the Nagar slideshow lane reached through the queue, never inline.
        "slideshow_render": slideshow_render_job,
        # task-166 (P0-B): one-shot /edit, /caption, /grade through the same
        # canonical chain (registry → CommandBus → lane → measured artifact).
        "creative_render": creative_render_job,
    }


async def extract_pdf_text(file_path: str) -> str:
    """Extract a PDF's text layer with ``pypdf`` (D3).

    Parsing runs in a worker thread so the event loop stays responsive on
    large documents. A missing ``pypdf`` install raises a clear, actionable
    error — PDF bytes are never silently decoded as UTF-8 text.
    """

    def _extract() -> str:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError(
                "PDF extraction requires the optional 'pypdf' package. "
                "Install it with: pip install pypdf "
                "(or pip install 'nexus-ai-agent[pdf]')"
            ) from exc
        reader = PdfReader(file_path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    return await asyncio.to_thread(_extract)


async def process_pdf_task(user_id: int, file_path: str, file_id: str) -> str:
    """Process a PDF job and return a durable success message.

    Extraction runs first: a missing ``pypdf`` must surface as the clear
    dependency error before the heavier RAG stack is even imported.
    Failures are raised so the queue can persist ``failed`` rather than
    reporting success.
    """
    try:
        text = await extract_pdf_text(file_path)
        from nexus_ai_agent.features.rag import AdvancedRAGEngine

        engine = AdvancedRAGEngine()
        await engine.add_document(user_id, text, {"file_id": file_id})
    except Exception as exc:  # noqa: BLE001 - queue owns durable failure mapping
        raise RuntimeError(f"Error processing {file_id}: {exc}") from exc
    return f"Successfully processed {file_id}"


async def generate_story_task(user_id: int, text: str, output_path: str) -> str:
    """Generate a story image locally and return its output path."""
    from nexus_ai_agent.features.story_gen import AIStoryGenerator

    try:
        generator = AIStoryGenerator()
        await generator.generate_story_image(text, output_path)
    except Exception as exc:  # noqa: BLE001 - queue owns durable failure mapping
        raise RuntimeError(f"Error generating story for user {user_id}: {exc}") from exc
    return output_path


async def process_pdf_job(payload: dict[str, object]) -> dict[str, object]:
    result = await process_pdf_task(
        int(str(payload["user_id"])),
        str(payload["file_path"]),
        str(payload["file_id"]),
    )
    return {"message": result}


async def generate_story_job(payload: dict[str, object]) -> dict[str, object]:
    result = await generate_story_task(
        int(str(payload["user_id"])),
        str(payload["text"]),
        str(payload["output_path"]),
    )
    return {"output_path": result}


__all__ = [
    "default_job_handlers",
    "extract_pdf_text",
    "generate_story_job",
    "generate_story_task",
    "job_queue_db_path",
    "process_pdf_job",
    "process_pdf_task",
]
