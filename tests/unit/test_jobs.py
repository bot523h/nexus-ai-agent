"""The former Celery tasks as plain in-process job handlers (R-001 / R-026).

``jobs.py`` is the only module that knows *what* background work exists;
``InProcessJobQueue`` is the only module that knows *how* it runs.  These
tests prove the two compose end-to-end through the real adapter, using the
real Pillow renderer for stories and a recording double for the RAG engine
(the real one downloads an embedding model — not something a unit test does).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent import jobs
from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue, JobStatus
from nexus_ai_agent.config.settings import Settings

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
#: A real two-page PDF (Helvetica text, valid xref) — see tests/fixtures/pdf/.
TWO_PAGE_PDF = Path(__file__).parents[1] / "fixtures" / "pdf" / "two_pages.pdf"


class _RecordingRAGEngine:
    """Stands in for ``AdvancedRAGEngine`` (chromadb + sentence-transformers)."""

    calls: list[tuple[int, str, dict[str, Any]]] = []
    threads: list[int] = []

    async def add_document(self, user_id: int, text: str, metadata: dict[str, Any]) -> None:
        type(self).calls.append((user_id, text, metadata))
        type(self).threads.append(threading.get_ident())


@pytest.fixture()
def rag_double(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingRAGEngine]:
    import nexus_ai_agent.features.rag as rag_module

    _RecordingRAGEngine.calls = []
    _RecordingRAGEngine.threads = []
    monkeypatch.setattr(rag_module, "AdvancedRAGEngine", _RecordingRAGEngine)
    return _RecordingRAGEngine


def test_job_types_are_stable_identifiers() -> None:
    """Job type strings are persisted; renaming them would orphan durable rows."""
    assert jobs.PDF_JOB == "process_pdf"
    assert jobs.STORY_JOB == "generate_story"


def test_job_queue_db_path_is_a_sidecar_next_to_the_app_db() -> None:
    """Job state lives in its own SQLite file — never inside the Alembic-managed schema."""
    assert jobs.job_queue_db_path("data/app.sqlite") == "data/app.sqlite.jobs"
    assert jobs.job_queue_db_path("/srv/nexus/app.sqlite") == "/srv/nexus/app.sqlite.jobs"


async def test_generate_story_job_renders_a_png(
    settings_override: Settings, tmp_path: Path
) -> None:
    output = tmp_path / "nested" / "story.png"
    result = await jobs.generate_story_job(
        {"user_id": 7, "text": "سلام دنیا — NEXUS", "output_path": str(output)}
    )
    assert result == {"output_path": str(output)}
    assert output.read_bytes().startswith(PNG_MAGIC)


async def test_process_pdf_job_extracts_text_from_a_real_pdf(
    rag_double: type[_RecordingRAGEngine],
) -> None:
    """D3: real extraction (pypdf) — page text reaches the RAG engine; result is inspectable."""
    result = await jobs.process_pdf_job(
        {"user_id": 42, "file_path": str(TWO_PAGE_PDF), "file_id": "abc"}
    )
    assert len(rag_double.calls) == 1
    user_id, text, metadata = rag_double.calls[0]
    assert user_id == 42 and metadata == {"file_id": "abc"}
    assert "NEXUS PDF fixture page one" in text
    assert "Second page: knowledge base" in text
    assert result == {
        "file_id": "abc",
        "pages": 2,
        "total_pages": 2,
        "characters": len(text),
        "truncated": False,
    }


async def test_pdf_job_without_the_extra_fails_with_an_actionable_error(
    rag_double: type[_RecordingRAGEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3: no ``[pdf]`` extra ⇒ explicit, actionable error — no UTF-8 fallback, no silence."""
    monkeypatch.setitem(sys.modules, "pypdf", None)  # makes ``import pypdf`` raise ImportError
    with pytest.raises(jobs.PdfSupportMissingError) as excinfo:
        await jobs.process_pdf_job({"user_id": 1, "file_path": str(TWO_PAGE_PDF), "file_id": "x"})
    assert "pip install" in str(excinfo.value) and "[pdf]" in str(excinfo.value)
    assert rag_double.calls == []


async def test_pdf_job_rejects_password_protected_pdf(
    rag_double: type[_RecordingRAGEngine], tmp_path: Path
) -> None:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(str(TWO_PAGE_PDF)))
    writer.encrypt(user_password="secret", algorithm="RC4-128")
    locked = tmp_path / "locked.pdf"
    with locked.open("wb") as handle:
        writer.write(handle)

    with pytest.raises(jobs.PdfExtractionError, match="password"):
        await jobs.process_pdf_job({"user_id": 1, "file_path": str(locked), "file_id": "x"})
    assert rag_double.calls == []


async def test_pdf_job_rejects_pdf_without_extractable_text(
    rag_double: type[_RecordingRAGEngine], tmp_path: Path
) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    blank = tmp_path / "blank.pdf"
    with blank.open("wb") as handle:
        writer.write(handle)

    with pytest.raises(jobs.PdfExtractionError, match="no extractable text"):
        await jobs.process_pdf_job({"user_id": 1, "file_path": str(blank), "file_id": "x"})
    assert rag_double.calls == []


async def test_pdf_job_rejects_non_pdf_bytes(
    rag_double: type[_RecordingRAGEngine], tmp_path: Path
) -> None:
    garbage = tmp_path / "garbage.pdf"
    garbage.write_bytes(b"%PDF-1.7\n\xff\xfe\x00binary")
    with pytest.raises(jobs.PdfExtractionError, match="not a readable PDF"):
        await jobs.process_pdf_job({"user_id": 1, "file_path": str(garbage), "file_id": "x"})
    assert rag_double.calls == []


async def test_pdf_extraction_is_bounded_by_pages(
    rag_double: type[_RecordingRAGEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3: bounded — only the first ``PDF_MAX_PAGES`` pages are read; truncation is reported."""
    monkeypatch.setattr(jobs, "PDF_MAX_PAGES", 1)
    result = await jobs.process_pdf_job(
        {"user_id": 1, "file_path": str(TWO_PAGE_PDF), "file_id": "x"}
    )
    _, text, _ = rag_double.calls[0]
    assert "page one" in text and "Second page" not in text
    assert result["pages"] == 1 and result["total_pages"] == 2 and result["truncated"] is True


async def test_pdf_extraction_is_bounded_by_characters(
    rag_double: type[_RecordingRAGEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap counts the page separators too, so the RAG engine never sees more."""
    monkeypatch.setattr(jobs, "PDF_MAX_CHARACTERS", 30)  # page one is 26 chars + "\n\n" + 2
    result = await jobs.process_pdf_job(
        {"user_id": 1, "file_path": str(TWO_PAGE_PDF), "file_id": "x"}
    )
    _, text, _ = rag_double.calls[0]
    assert text == "NEXUS PDF fixture page one\n\nSe"
    assert len(text) == 30 == result["characters"]
    assert result["pages"] == 2 and result["truncated"] is True

    rag_double.calls.clear()
    monkeypatch.setattr(jobs, "PDF_MAX_CHARACTERS", 26)  # exactly page one: page two never starts
    result = await jobs.process_pdf_job(
        {"user_id": 1, "file_path": str(TWO_PAGE_PDF), "file_id": "y"}
    )
    _, text, _ = rag_double.calls[0]
    assert text == "NEXUS PDF fixture page one"
    assert result["pages"] == 1 and result["truncated"] is True


def test_pdf_bounds_are_sane_defaults() -> None:
    assert jobs.PDF_MAX_PAGES == 100
    assert jobs.PDF_MAX_CHARACTERS == 250_000


async def test_process_pdf_job_raises_on_missing_file(
    rag_double: type[_RecordingRAGEngine], tmp_path: Path
) -> None:
    """Infrastructure errors propagate as-is (the queue records them); no 'Error:' strings."""
    with pytest.raises(FileNotFoundError):
        await jobs.process_pdf_job(
            {"user_id": 1, "file_path": str(tmp_path / "missing.pdf"), "file_id": "x"}
        )
    assert rag_double.calls == []


async def test_process_pdf_job_rejects_malformed_payload() -> None:
    with pytest.raises(KeyError):
        await jobs.process_pdf_job({"user_id": 1})


def test_build_job_queue_registers_both_handlers(settings_override: Settings) -> None:
    queue = jobs.build_job_queue(settings_override)
    assert isinstance(queue, InProcessJobQueue)
    assert str(queue.db_path) == jobs.job_queue_db_path(settings_override.db_path)
    assert set(queue.handlers) == {jobs.PDF_JOB, jobs.STORY_JOB}
    assert queue.handlers[jobs.PDF_JOB] is jobs.process_pdf_job
    assert queue.handlers[jobs.STORY_JOB] is jobs.generate_story_job


async def test_story_job_end_to_end_through_the_queue(
    settings_override: Settings, tmp_path: Path
) -> None:
    """``enqueue`` → in-process execution → durable ``succeeded`` + PNG on disk."""
    queue = jobs.build_job_queue(settings_override)
    output = tmp_path / "story.png"
    job_id = await queue.enqueue(
        job_type=jobs.STORY_JOB,
        idempotency_key="story:1:1",
        payload={"user_id": 1, "text": "پایان سلری", "output_path": str(output)},
    )
    assert await queue.wait(job_id, timeout=30) == JobStatus.SUCCEEDED
    assert await queue.get_result(job_id) == {"output_path": str(output)}
    assert output.read_bytes().startswith(PNG_MAGIC)
    await queue.close()


async def test_pdf_job_failure_is_recorded_not_swallowed(
    settings_override: Settings, rag_double: type[_RecordingRAGEngine], tmp_path: Path
) -> None:
    """An unreadable upload fails visibly with the extraction error persisted."""
    queue = jobs.build_job_queue(settings_override)
    doc = tmp_path / "binary.pdf"
    doc.write_bytes(b"%PDF-1.7\n\xff\xfe\x00binary")
    job_id = await queue.enqueue(
        job_type=jobs.PDF_JOB,
        idempotency_key="pdf:1:1",
        payload={"user_id": 1, "file_path": str(doc), "file_id": "bin"},
    )
    assert await queue.wait(job_id, timeout=30) == JobStatus.FAILED
    record = await queue.get_job(job_id)
    assert record is not None
    assert record.error is not None and record.error.startswith("PdfExtractionError:")
    assert rag_double.calls == []
    await queue.close()


async def test_heavy_work_runs_off_the_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch, rag_double: type[_RecordingRAGEngine], tmp_path: Path
) -> None:
    """CPU-bound engines run in a worker thread so the bot's loop stays responsive.

    This is the in-process equivalent of what the separate Celery worker
    process used to provide.
    """
    import nexus_ai_agent.features.story_gen as story_module

    seen_threads: list[int] = []

    class _ThreadRecordingGenerator:
        def __init__(self, gemini_engine: Any | None = None) -> None:
            pass

        async def generate_story_image(self, text: str, output_path: str) -> None:
            seen_threads.append(threading.get_ident())
            Path(output_path).write_bytes(PNG_MAGIC)

    monkeypatch.setattr(story_module, "AIStoryGenerator", _ThreadRecordingGenerator)

    await jobs.generate_story_job(
        {"user_id": 1, "text": "x", "output_path": str(tmp_path / "s.png")}
    )
    await jobs.process_pdf_job({"user_id": 1, "file_path": str(TWO_PAGE_PDF), "file_id": "d"})

    loop_thread = threading.get_ident()
    assert seen_threads and all(ident != loop_thread for ident in seen_threads)
    assert rag_double.threads and all(ident != loop_thread for ident in rag_double.threads)
