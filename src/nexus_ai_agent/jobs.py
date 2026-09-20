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
no ``beat_schedule`` entry, i.e. it never ran; scheduling it would be new
behaviour without a contract and is left to an explicit follow-up.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.config.settings import Settings

#: Chunk + embed an uploaded document into the user's RAG collection.
PDF_JOB = "process_pdf"
#: Render a Persian/RTL story image to disk.
STORY_JOB = "generate_story"


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


def _index_document(user_id: int, file_path: str, file_id: str) -> int:
    """Blocking body of :func:`process_pdf_job`; runs in a worker thread."""
    from nexus_ai_agent.features.rag import AdvancedRAGEngine

    with open(file_path, encoding="utf-8") as handle:
        text = handle.read()
    engine = AdvancedRAGEngine()
    # The engine API is ``async`` but does no real awaiting; drive it on a
    # private loop in this worker thread exactly as the Celery task did.
    asyncio.run(engine.add_document(user_id, text, {"file_id": file_id}))
    return len(text)


async def process_pdf_job(payload: dict[str, object]) -> dict[str, object]:
    """Index an uploaded document.  Payload: ``user_id``, ``file_path``, ``file_id``.

    The file is read as UTF-8 text exactly as the Celery task did; a binary
    PDF therefore fails with ``UnicodeDecodeError`` — now *visibly* (the queue
    stores ``failed`` + the error) instead of as an ``"Error processing"``
    string returned as a successful result.  Real PDF text extraction is a
    separate product change, not part of the Celery removal.
    """
    user_id = int(str(_field(payload, "user_id")))
    file_path = str(_field(payload, "file_path"))
    file_id = str(_field(payload, "file_id"))
    characters = await asyncio.to_thread(_index_document, user_id, file_path, file_id)
    return {"file_id": file_id, "characters": characters}


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
