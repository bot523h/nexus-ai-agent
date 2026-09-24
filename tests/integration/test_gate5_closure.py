"""Gate 5 closure (task-181) — regression + scenario suite.

Every test here closes a Gap that was reproduced against the pre-task-181
tree (see ``docs/audits/GATE5_CLOSURE_2026-09-24.md``):

* **GAP-A** — a typed user failure (``{"success": False, "error_code": ...}``)
  reached ``COMPLETED``.  Now: failure status, never COMPLETED, notifier never
  emits success.
* **GAP-B** — one undifferentiated ``failed`` state.  Now: ``FAILED_RETRYABLE``
  / ``FAILED_TERMINAL`` per ``jobs/failure_semantics``.
* **Publication order** — a refused publication replaced (and destroyed) the
  previous valid artifact and left the refused bytes at the destination.
  Now: stage → verify → publish → re-probe; refusal retracts the staged temp
  and preserves the previous artifact.
* **Trace** — worker/queue events carried no ``job_id`` (job_id=null in the
  trace).  Now: every event emitted under a job carries its ``job_id``.

Honest limits (NOT VERIFIED here, documented as such):
* cross-process duplicate-worker races and concurrent destination writers —
  the queue is single-process by design (in-process asyncio tasks + one
  SQLite sidecar).  The crash *windows* are exercised via injection below;
  process-kill races are NOT simulated and are not claimed as proven.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import (
    VERIFICATION_RESULT_KEY,
    InProcessJobQueue,
    JobCompletion,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus

# Module-level notifier imports: loading telegram.ext before any
# ``telegram.Bot`` patching avoids the ExtBot metaclass conflict (the same
# pattern as test_creative_notify).
from nexus_ai_agent.bot.app import _build_job_completion_notifier  # noqa: E402
from nexus_ai_agent.bot.slideshow_notify import notify_slideshow_completion  # noqa: E402

pytestmark = pytest.mark.integration

TERMINAL_STATES = {
    JobStatus.COMPLETED,
    JobStatus.FAILED_RETRYABLE,
    JobStatus.FAILED_TERMINAL,
}


def make_pdf_with_text(dest: Path, text: str = "Hello PDF world") -> Path:
    """A real one-page PDF with extractable text and correct xref offsets.

    Same self-contained helper pattern as ``test_verification_gap_closure``
    (CI runs the bare pytest console script — no cross-directory imports).
    """
    content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        (
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>"
        ),
        b"<</Length " + str(len(content)).encode() + b">>stream\n" + content + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    dest.write_bytes(bytes(out))
    return dest


def make_blank_pdf(dest: Path) -> Path:
    """A valid PDF with NO text layer (image-only) — extraction yields ''."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with dest.open("wb") as handle:
        writer.write(handle)
    return dest


async def _drain(queue: InProcessJobQueue, job_id: str, timeout: float = 30.0) -> JobStatus:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = await queue.get_status(job_id)
        if status in TERMINAL_STATES:
            return status
        await asyncio.sleep(0.02)
    raise TimeoutError(f"job {job_id} never reached a terminal state")


class _FakeTelegram:
    """Records sends at the ``telegram.Bot`` boundary (no network)."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.documents: list[int] = []
        self.videos: list[int] = []

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))

    async def send_document(self, **kwargs: Any) -> None:  # noqa: ANN401
        self.documents.append(int(str(kwargs.get("chat_id") or 0)))

    async def send_video(self, **kwargs: Any) -> None:  # noqa: ANN401
        self.videos.append(int(str(kwargs.get("chat_id") or 0)))


class _FakeRag:
    """The only double on the pdf lane: the RAG engine is an external service
    (same pattern as test_verification_gap_closure)."""

    def __init__(self) -> None:
        self.added: list[tuple[int, str, dict[str, object]]] = []

    async def add_document(self, user_id: int, text: str, meta: dict[str, object]) -> None:
        self.added.append((user_id, text, meta))


@pytest.fixture()
def fake_rag(monkeypatch: pytest.MonkeyPatch) -> _FakeRag:
    import nexus_ai_agent.features.rag as rag_module

    fake = _FakeRag()
    monkeypatch.setattr(rag_module, "AdvancedRAGEngine", lambda: fake)
    return fake


@pytest.fixture()
def fake_telegram(monkeypatch: pytest.MonkeyPatch) -> _FakeTelegram:
    import telegram

    fake = _FakeTelegram()
    monkeypatch.setattr(telegram, "Bot", lambda token: fake)
    return fake


# ===========================================================================
# GAP-A — typed failure semantics: failure status, never COMPLETED
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("render_failed", JobStatus.FAILED_TERMINAL),
        ("unsupported_operation", JobStatus.FAILED_TERMINAL),
        ("invalid_request", JobStatus.FAILED_TERMINAL),
        ("caption_profile_unavailable", JobStatus.FAILED_RETRYABLE),
        ("ffmpeg_unavailable", JobStatus.FAILED_RETRYABLE),
    ],
)
async def test_typed_render_failure_lands_in_a_failure_status_never_completed(
    tmp_path: Path, code: str, expected: JobStatus
) -> None:
    """Mission regression: typed failure → failure status → never COMPLETED."""
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"success": False, "error_code": code, "error_detail": "synthetic"}

    queue.register_handler("creative_render", handler)
    job_id = await queue.enqueue(
        job_type="creative_render", idempotency_key=f"typed-{code}", payload={"chat_id": 1}
    )
    status = await _drain(queue, job_id)
    assert status is expected, f"{code} must classify to {expected.value}, got {status.value}"
    assert status is not JobStatus.COMPLETED

    chain = await queue.get_result_chain(job_id)
    assert chain["failure_reason"] == f"typed_failure:{code}"
    assert chain["execution_status"] in {"failed_retryable", "failed_terminal"}
    # the typed result survives for the notifier/audit; nothing verified
    result = await queue.get_result(job_id)
    assert result is not None and result["success"] is False
    assert VERIFICATION_RESULT_KEY not in result


@pytest.mark.asyncio
async def test_typed_failure_reaching_a_verifier_still_cannot_complete(
    tmp_path: Path,
) -> None:
    """Defense in depth: even if the queue check is bypassed, the verifier
    refuses a typed failure — the mutation that forces COMPLETED on typed
    failures must have no single point of failure (see mutation probe #2)."""
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"success": False, "error_code": "render_failed"}

    queue.register_handler("creative_render", handler)
    # Simulate a mutated queue that forgot the typed short-circuit by
    # monkeypatching is_typed_user_failure at the queue module.
    import nexus_ai_agent.adapters.in_process_job_queue as queue_module

    original = queue_module.is_typed_user_failure
    queue_module.is_typed_user_failure = lambda result: False  # type: ignore[assignment]
    try:
        job_id = await queue.enqueue(
            job_type="creative_render", idempotency_key="defense", payload={"chat_id": 1}
        )
        status = await _drain(queue, job_id)
    finally:
        queue_module.is_typed_user_failure = original  # type: ignore[assignment]
    assert status in {JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}


# ===========================================================================
# Notification matrix (mission §14) — lifecycle state is the source of truth
# ===========================================================================


def _completion(status: JobStatus, **overrides: Any) -> JobCompletion:
    base: dict[str, Any] = {
        "job_id": "job-42",
        "job_type": "pdf_extract",
        "status": status,
        "result": None,
        "error": None,
        "payload": {"chat_id": 4242},
    }
    base.update(overrides)
    return JobCompletion(**base)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_marker", "forbidden"),
    [
        (JobStatus.COMPLETED, "✅", None),
        (JobStatus.FAILED_RETRYABLE, "⚠️", "✅"),
        (JobStatus.FAILED_TERMINAL, "❌", "✅"),
    ],
)
async def test_notification_matrix_status_drives_copy(
    fake_telegram: _FakeTelegram,
    status: JobStatus,
    expected_marker: str,
    forbidden: str | None,
) -> None:
    """COMPLETED→success, FAILED_RETRYABLE→failure, FAILED_TERMINAL→terminal
    failure.  The three copies are distinct; failures never read as success."""
    notify = _build_job_completion_notifier("dummy-token")
    await notify(_completion(status))
    assert len(fake_telegram.messages) == 1
    text = fake_telegram.messages[0][1]
    assert text.startswith(expected_marker), text
    assert "job-42" in text, "the durable job id must appear in the notification"
    if forbidden:
        assert forbidden not in text
    if status is not JobStatus.COMPLETED:
        assert "کامل شد" not in text


@pytest.mark.asyncio
async def test_verification_state_never_produces_a_success_notification(
    fake_telegram: _FakeTelegram, caplog: pytest.LogCaptureFixture
) -> None:
    """VERIFYING → no success notification (the notifier stays silent)."""
    notify = _build_job_completion_notifier("dummy-token")
    with caplog.at_level(logging.INFO):
        await notify(_completion(JobStatus.VERIFYING))
    assert fake_telegram.messages == [], "VERIFYING must not notify at all"
    assert fake_telegram.documents == [] and fake_telegram.videos == []


@pytest.mark.asyncio
async def test_lying_success_result_cannot_turn_failure_into_success(
    fake_telegram: _FakeTelegram, tmp_path: Path
) -> None:
    """``result.success == true`` under a failure status is still a failure —
    the durable state is the notifier's only source of truth (mutation #5)."""
    notify = _build_job_completion_notifier("dummy-token")
    lying = {
        "success": True,
        "output_path": str(tmp_path / "master.mp4"),
        "artifact_path": str(tmp_path / "output.mp4"),
        "artifact_kind": "video",
        "sha256": "sha256:" + "0" * 64,
        "size_bytes": 12,
    }
    await notify(
        _completion(
            JobStatus.FAILED_TERMINAL,
            job_type="creative_render",
            result=lying,
            payload={"chat_id": 4242, "command": "edit", "operation": "trim", "lang": "en"},
        )
    )
    texts = [text for _, text in fake_telegram.messages]
    assert texts, "the failure must still be reported"
    assert all("✅" not in text for text in texts), texts
    assert not fake_telegram.videos and not fake_telegram.documents

    fake_telegram.messages.clear()
    await notify_slideshow_completion(
        _completion(
            JobStatus.FAILED_RETRYABLE,
            job_type="slideshow_render",
            result=lying,
            payload={"chat_id": 7, "workspace_dir": str(tmp_path / "nowhere")},
        ),
        "dummy",
    )
    texts = [text for _, text in fake_telegram.messages]
    assert texts and all("✅" not in text for text in texts), texts
    assert not fake_telegram.documents


@pytest.mark.asyncio
async def test_verification_failure_notification_is_never_success(
    fake_telegram: _FakeTelegram, tmp_path: Path
) -> None:
    """End-to-end: a lying success claim that fails verification produces a
    failure notification — never a success one, never a delivery."""
    from nexus_ai_agent.bot.app import _build_job_completion_notifier

    queue = InProcessJobQueue(
        tmp_path / "jobs.sqlite3",
        on_job_finished=_build_job_completion_notifier("dummy-token"),
    )

    async def liar(payload: dict[str, object]) -> dict[str, object]:
        # claims a measured artifact that does not exist
        return {
            "success": True,
            "artifact_path": str(tmp_path / "missing.mp4"),
            "artifact_kind": "video",
            "sha256": "sha256:" + "1" * 64,
            "size_bytes": 99,
            "workspace_dir": str(tmp_path),
            "operation": "timeline.trim",
        }

    queue.register_handler("creative_render", liar)
    job_id = await queue.enqueue(
        job_type="creative_render", idempotency_key="lie", payload={"chat_id": 4242, "lang": "en"}
    )
    status = await _drain(queue, job_id)
    assert status in {JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
    await asyncio.sleep(0.05)
    texts = [text for _, text in fake_telegram.messages]
    assert texts, "the refusal must be reported"
    assert all("✅" not in text for text in texts), texts
    assert not fake_telegram.videos and not fake_telegram.documents


# ===========================================================================
# Publication order (§7) + staging leak (§8) + atomic publication (§12)
# ===========================================================================


@pytest.mark.asyncio
async def test_refused_publication_preserves_old_artifact_and_cleans_staging(
    tmp_path: Path, fake_rag: _FakeRag
) -> None:
    """R2 regression (reproduced on the pre-task-181 tree): verification
    refusal (empty extraction) must NOT publish, must retract its staged
    temp, must leave the previous valid artifact untouched, and must persist
    the failure reason."""
    from nexus_ai_agent.jobs.feature_verification import (
        pdf_text_artifact_path,
        pdf_text_staged_path,
    )
    from nexus_ai_agent.worker import process_pdf_job

    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    source = make_blank_pdf(pdf_dir / "doc.pdf")  # extraction → ""
    final = pdf_text_artifact_path(source)
    final.write_text("OLD VALID EXTRACTION", encoding="utf-8")

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    queue.register_handler("pdf_extract", process_pdf_job)
    job_id = await queue.enqueue(
        job_type="pdf_extract",
        idempotency_key="refuse",
        payload={"user_id": 1, "file_path": str(source), "file_id": "f1"},
    )
    status = await _drain(queue, job_id)

    assert status is not JobStatus.COMPLETED
    assert status is JobStatus.FAILED_RETRYABLE  # empty_artifact ⇒ RETRYABLE
    chain = await queue.get_result_chain(job_id)
    assert chain["failure_reason"] == "verification_failed:empty_artifact"
    # §12: the previous valid artifact survives byte-for-byte
    assert final.read_text(encoding="utf-8") == "OLD VALID EXTRACTION"
    # §8: no staged temp, no .part residue — the refused bytes are gone
    assert not pdf_text_staged_path(source).exists()
    leftovers = sorted(p.name for p in pdf_dir.iterdir())
    assert leftovers == ["doc.extracted.txt", "doc.pdf"], leftovers


@pytest.mark.asyncio
async def test_first_refusal_without_previous_artifact_publishes_nothing(
    tmp_path: Path, fake_rag: _FakeRag
) -> None:
    """Same refusal with no prior artifact: the destination is never born."""
    from nexus_ai_agent.jobs.feature_verification import (
        pdf_text_artifact_path,
        pdf_text_staged_path,
    )
    from nexus_ai_agent.worker import process_pdf_job

    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    source = make_blank_pdf(pdf_dir / "doc.pdf")

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    queue.register_handler("pdf_extract", process_pdf_job)
    job_id = await queue.enqueue(
        job_type="pdf_extract",
        idempotency_key="refuse2",
        payload={"user_id": 1, "file_path": str(source), "file_id": "f2"},
    )
    status = await _drain(queue, job_id)
    assert status in {JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
    assert not pdf_text_artifact_path(source).exists()
    assert not pdf_text_staged_path(source).exists()
    leftovers = sorted(p.name for p in pdf_dir.iterdir())
    assert leftovers == ["doc.pdf"], leftovers


@pytest.mark.asyncio
async def test_happy_path_publishes_then_reprobes_then_completes(
    tmp_path: Path, fake_rag: _FakeRag
) -> None:
    """§7 order on the real lane: stage → verify → publish → re-probe →
    COMPLETED, with the published artifact the verified one."""
    from nexus_ai_agent.jobs.feature_verification import (
        pdf_text_artifact_path,
        pdf_text_staged_path,
    )
    from nexus_ai_agent.worker import process_pdf_job

    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    source = make_pdf_with_text(pdf_dir / "doc.pdf", "Gate five closure")

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    queue.register_handler("pdf_extract", process_pdf_job)
    job_id = await queue.enqueue(
        job_type="pdf_extract",
        idempotency_key="publish",
        payload={"user_id": 1, "file_path": str(source), "file_id": "f3"},
    )
    status = await _drain(queue, job_id)
    assert status is JobStatus.COMPLETED

    final = pdf_text_artifact_path(source)
    assert final.is_file() and "Gate five closure" in final.read_text(encoding="utf-8")
    assert not pdf_text_staged_path(source).exists(), "staged temp must not survive publish"
    result = await queue.get_result(job_id)
    assert result is not None
    assert result["artifact_path"] == str(final), "the claim must record the PUBLISHED path"
    verification = result[VERIFICATION_RESULT_KEY]
    assert verification["status"] == "verified"


@pytest.mark.asyncio
async def test_publish_failure_retracts_staging_and_preserves_old_artifact(
    tmp_path: Path, fake_rag: _FakeRag
) -> None:
    """Crash during publication (§13 window): the staged temp is retracted,
    the previous published artifact is untouched, and the job fails
    classifiable — a partial artifact never occupies the final name."""
    from nexus_ai_agent.adapters.in_process_job_queue import ArtifactPublication
    from nexus_ai_agent.jobs.feature_verification import (
        pdf_text_artifact_path,
        pdf_text_staged_path,
    )
    from nexus_ai_agent.worker import process_pdf_job

    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    source = make_pdf_with_text(pdf_dir / "doc.pdf", "fresh extraction")
    final = pdf_text_artifact_path(source)
    final.write_text("OLD VALID EXTRACTION", encoding="utf-8")

    def _broken_publish(payload: dict[str, object], result: dict[str, object]) -> dict[str, object]:
        raise OSError(28, "no space left on device")

    queue = InProcessJobQueue(
        tmp_path / "jobs.sqlite3",
        artifact_publications={
            "pdf_extract": ArtifactPublication(
                publish=_broken_publish,
                retract=lambda payload, result: pdf_text_staged_path(
                    Path(str(payload["file_path"]))
                ).unlink(missing_ok=True),
            ),
        },
    )
    queue.register_handler("pdf_extract", process_pdf_job)
    job_id = await queue.enqueue(
        job_type="pdf_extract",
        idempotency_key="pubfail",
        payload={"user_id": 1, "file_path": str(source), "file_id": "f4"},
    )
    status = await _drain(queue, job_id)
    assert status is JobStatus.FAILED_RETRYABLE  # ENOSPC ⇒ temporary IO ⇒ RETRYABLE
    chain = await queue.get_result_chain(job_id)
    assert chain["failure_reason"] == "verification_failed:publish_failed"
    assert final.read_text(encoding="utf-8") == "OLD VALID EXTRACTION"
    assert not pdf_text_staged_path(source).exists(), "staging must be retracted"
    leftovers = sorted(p.name for p in pdf_dir.iterdir())
    assert leftovers == ["doc.extracted.txt", "doc.pdf"], leftovers


# ===========================================================================
# Trace (§9) — job_id at every lifecycle point
# ===========================================================================


@pytest.mark.asyncio
async def test_every_lifecycle_trace_event_carries_job_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R3 regression (reproduced: ``creative_render_start`` had no job_id):
    every structlog event emitted while a job runs carries its durable
    ``job_id``, and the queue's own lifecycle lines name it explicitly."""
    import structlog

    from nexus_ai_agent.observability.logging import get_logger

    captured: list[dict[str, Any]] = []

    def _capture(logger: Any, method: str, event_dict: dict[str, Any]) -> str:
        captured.append(dict(event_dict))
        return "captured"

    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, _capture],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        logger_factory=structlog.PrintLoggerFactory(open("/dev/null", "w")),  # noqa: SIM115
        cache_logger_on_first_use=False,
    )
    worker_log = get_logger("tests.worker")

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", artifact_verifiers={})

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        worker_log.info("creative_render_start", idempotency_key="trace")  # production shape
        return {"success": True, "artifact_path": "/dev/null", "sha256": "x", "size_bytes": 1}

    queue.register_handler("creative_render", handler)  # no verifier: trace-only
    with caplog.at_level(logging.INFO):
        job_id = await queue.enqueue(
            job_type="creative_render", idempotency_key="trace", payload={"chat_id": 1}
        )
        status = await _drain(queue, job_id)
        await asyncio.sleep(0.05)

    assert status is JobStatus.COMPLETED
    assert captured, "the worker event must be captured"
    missing = [event for event in captured if event.get("job_id") != job_id]
    assert not missing, f"trace events without the durable job_id: {missing}"

    # the queue's own lifecycle lines name the job explicitly (stdlib side)
    lifecycle_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("nexus_ai_agent.adapters.in_process_job_queue")
        and "job_" in record.getMessage()
    ]
    assert any("job_completed" in line for line in lifecycle_lines)
    assert all(job_id in line for line in lifecycle_lines), lifecycle_lines


@pytest.mark.asyncio
async def test_result_and_completion_carry_the_durable_job_id(tmp_path: Path) -> None:
    """queue → worker → result → trace/notifier: the id never drops to null."""
    completions: list[JobCompletion] = []

    async def hook(completion: JobCompletion) -> None:
        completions.append(completion)

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", on_job_finished=hook)

    async def ok(payload: dict[str, object]) -> dict[str, object]:
        return {"echo": 1}

    queue.register_handler("ok", ok)
    job_id = await queue.enqueue(job_type="ok", idempotency_key="chain", payload={})
    assert await _drain(queue, job_id) is JobStatus.COMPLETED

    chain = await queue.get_result_chain(job_id)
    assert chain["job_id"] == job_id and job_id
    assert completions and completions[0].job_id == job_id


# ===========================================================================
# Idempotency (§11) — explicit, deterministic scenario matrix
# ===========================================================================


@pytest.mark.asyncio
async def test_idempotency_matrix(tmp_path: Path) -> None:
    """same key+payload ⇒ one job; different payload ⇒ conflict logged and
    first payload wins; duplicates during PROCESSING / after COMPLETED /
    after terminal failure never re-execute."""
    calls: list[dict[str, object]] = []
    gate = asyncio.Event()

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        calls.append(dict(payload))
        if payload.get("slow"):
            await gate.wait()
        if payload.get("fail"):
            return {"success": False, "error_code": "invalid_request"}
        return {"echo": payload.get("value")}

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    queue.register_handler("work", handler)

    # 1 — same key + same payload ⇒ same job, one logical effect
    payload = {"value": 1}
    first = await queue.enqueue(job_type="work", idempotency_key="k", payload=payload)
    second = await queue.enqueue(job_type="work", idempotency_key="k", payload=payload)
    assert first == second
    assert await _drain(queue, first) is JobStatus.COMPLETED
    assert len(calls) == 1

    # 2 — duplicate after COMPLETED ⇒ same logical result, no re-execution
    result_before = await queue.get_result(first)
    third = await queue.enqueue(job_type="work", idempotency_key="k", payload=payload)
    assert third == first
    await asyncio.sleep(0.1)
    assert await queue.get_result(first) == result_before
    assert len(calls) == 1

    # 3 — same key + different payload ⇒ conflict observed, first payload wins
    with _log_messages() as messages:
        fourth = await queue.enqueue(job_type="work", idempotency_key="k", payload={"value": 999})
    assert fourth == first
    assert any("job_idempotency_payload_conflict" in message for message in messages), messages
    await asyncio.sleep(0.1)
    assert (await queue.get_result(first))["echo"] == 1
    assert len(calls) == 1

    # 4 — duplicate submission during PROCESSING ⇒ no duplicate execution
    slow_payload = {"value": 2, "slow": True}
    slow_id = await queue.enqueue(job_type="work", idempotency_key="slow", payload=slow_payload)
    duplicate = await queue.enqueue(job_type="work", idempotency_key="slow", payload=slow_payload)
    assert duplicate == slow_id
    await asyncio.sleep(0.1)
    gate.set()
    assert await _drain(queue, slow_id) is JobStatus.COMPLETED
    assert sum(1 for call in calls if call.get("slow")) == 1

    # 5 — duplicate after terminal failure ⇒ same job, same failure, no rerun
    fail_payload = {"value": 3, "fail": True}
    failed_id = await queue.enqueue(job_type="work", idempotency_key="bad", payload=fail_payload)
    assert await _drain(queue, failed_id) is JobStatus.FAILED_TERMINAL  # invalid_request
    again = await queue.enqueue(job_type="work", idempotency_key="bad", payload=fail_payload)
    assert again == failed_id
    await asyncio.sleep(0.1)
    assert sum(1 for call in calls if call.get("fail")) == 1
    assert (await queue.get_status(failed_id)) is JobStatus.FAILED_TERMINAL


class _log_messages:
    """Collect stdlib log messages across a block (root-logger handler)."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def __enter__(self) -> list[str]:
        sink = self.messages

        class _ListHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                sink.append(record.getMessage())

        self._handler = _ListHandler()
        logging.getLogger().addHandler(self._handler)
        return self.messages

    def __exit__(self, *exc: object) -> None:
        logging.getLogger().removeHandler(self._handler)


# ===========================================================================
# Crash windows (§13) — injected; process-kill races NOT VERIFIED by design
# ===========================================================================


@pytest.mark.asyncio
async def test_crash_after_publish_before_persist_recovers_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_rag: _FakeRag
) -> None:
    """Crash immediately after the atomic rename (§13 window): the row is
    still VERIFYING when the process dies; ``resume_pending`` re-runs the
    job, which republishes only its own key-scoped attempt and completes."""
    from nexus_ai_agent.jobs.feature_verification import pdf_text_artifact_path
    from nexus_ai_agent.worker import process_pdf_job

    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    source = make_pdf_with_text(pdf_dir / "doc.pdf", "survives a crash")

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    queue.register_handler("pdf_extract", process_pdf_job)

    import nexus_ai_agent.adapters.in_process_job_queue as queue_module

    original_mark_completed = queue_module.InProcessJobQueue._mark_completed
    crashed = False

    def _crash_once(self: Any, job_id: str, result: dict[str, object], token: Any) -> Any:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("process died after atomic rename")
        return original_mark_completed(self, job_id, result, token)

    monkeypatch.setattr(queue_module.InProcessJobQueue, "_mark_completed", _crash_once)
    job_id = await queue.enqueue(
        job_type="pdf_extract",
        idempotency_key="crashy",
        payload={"user_id": 1, "file_path": str(source), "file_id": "f5"},
    )
    for _ in range(200):
        if crashed:
            break
        await asyncio.sleep(0.02)
    assert crashed, "the injected crash must fire"

    # The crash killed the owning process: under the execution-ownership
    # contract a fresh process may only take over after the dead owner's
    # lease expires (fresh-lease rows are never reclaimed).  Backdate
    # started_at to express the expired lease of the dead generation.
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        connection.execute(
            "UPDATE nexus_job_queue SET started_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", job_id),
        )

    # recovery: a fresh process reclaims the lease-expired row
    monkeypatch.setattr(queue_module.InProcessJobQueue, "_mark_completed", original_mark_completed)
    reclaimed = await queue.resume_pending()
    assert job_id in reclaimed
    status = await _drain(queue, job_id)
    assert status is JobStatus.COMPLETED
    final = pdf_text_artifact_path(source)
    assert "survives a crash" in final.read_text(encoding="utf-8")
