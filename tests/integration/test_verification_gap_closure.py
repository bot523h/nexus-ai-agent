"""Task-180 integration proofs: the three verification GAPs, closed end-to-end.

Real ``InProcessJobQueue`` + real handlers + real bytes throughout:

* GAP-A  ``slideshow.render → job → artifact → verifier → completed`` on the
  real Wave 2.5 worker handler with a REAL FFmpeg encode (the allow-listed
  binary resolves through ``imageio-ffmpeg`` in dev/CI — the same policy as
  ``test_creative_chain_e2e``).  The encoder is never faked in the happy
  path; lying-handler attacks use explicitly registered fake handlers, which
  is the point of the attack, not a production stand-in.
* GAP-B  ``pdf_extract`` with a real pypdf-extractable PDF.  The RAG engine
  (an external LLM service) is the ONLY double — the artifact under test is
  the persisted extracted text, measured for real.
* GAP-C  ``story`` with the real Pillow generator (deterministic local
  render, no network, no mocks).

Adversarial attacks A–H from the task-180 mission run here against the real
queue and the DEFAULT verifier registry (nothing is unregistered to make an
attack pass).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from nexus_ai_agent.adapters.in_process_job_queue import (
    VERIFICATION_RESULT_KEY,
    InProcessJobQueue,
    default_artifact_verifiers,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative.slideshow.ffmpeg import sha256_file
from nexus_ai_agent.jobs.lifecycle import is_failure
from nexus_ai_agent.worker import default_job_handlers

pytestmark = pytest.mark.integration


def make_pdf_with_text(dest: Path, text: str = "Hello PDF world") -> Path:
    """A real one-page PDF with extractable text and correct xref offsets.

    Kept local to this file (no cross-directory test imports): CI invokes the
    bare ``pytest`` console script, where ``tests`` is not an importable
    package — every test module must be self-contained or use its own
    directory's helpers (the ``slideshow_media``/``surface_fakes`` pattern).
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


# ---------------------------------------------------------------------------
# helpers (same policy as tests/integration/test_job_lifecycle_queue.py)
# ---------------------------------------------------------------------------
def _row(db_path: Path, job_id: str, column: str) -> Any:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            f"SELECT {column} FROM nexus_job_queue WHERE id = ?",
            (job_id,),  # noqa: S608
        ).fetchone()
    assert row is not None
    return row[0]


def _queue(db: Path) -> InProcessJobQueue:
    queue = InProcessJobQueue(db)  # artifact_verifiers=None → DEFAULT registry
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    return queue


async def _drain(queue: InProcessJobQueue, job_id: str) -> JobStatus:
    for _ in range(400):
        status = await queue.get_status(job_id)
        if status in {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}:
            return status
        await asyncio.sleep(0.05)
    raise AssertionError("job never reached a terminal state")


def _jpeg(path: Path, size: tuple[int, int] = (160, 120)) -> Path:
    Image.new("RGB", size, "#336699").save(path, format="JPEG")
    return path


class _FakeRag:
    """The ONLY double in this file: the RAG engine is an external service."""

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


# ---------------------------------------------------------------------------
# GAP-A — slideshow_render: the full chain, real encode, default registry
# ---------------------------------------------------------------------------
# Real 30 s slideshow at 320x240 encodes in ~2 s with the bundled static
# FFmpeg, so this proof stays in the default (non-slow) CI suite.
@pytest.mark.asyncio
async def test_gap_a_slideshow_chain_completes_only_with_verified_artifact(
    tmp_path: Path,
) -> None:
    """slideshow.render → job → artifact → verifier → completed (all real).

    The registered verifier re-measures the master: path, containment,
    size, recomputed sha256 and a REAL media probe of the encoded bytes.
    """
    db = tmp_path / "jobs.sqlite3"
    queue = _queue(db)

    workspace = tmp_path / "slideshow_ws"
    workspace.mkdir()
    image = _jpeg(workspace / "slide_00.jpg")
    output = workspace / "master.mp4"

    job_id = await queue.enqueue(
        job_type="slideshow_render",
        idempotency_key="slideshow:gap-a:1",
        payload={
            "image_paths": [str(image)],
            "output_path": str(output),
            "workspace_dir": str(workspace),
            "target_duration_us": 30_000_000,
            "resolution": "320x240",
            "project_name": "gap-a-proof",
        },
    )
    assert await _drain(queue, job_id) is JobStatus.COMPLETED

    result = await queue.get_result(job_id)
    assert result is not None
    verification = result[VERIFICATION_RESULT_KEY]
    assert verification["status"] == "verified"
    physical = verification["physical_identity"]
    assert physical["path"] == str(output)
    assert physical["workspace"] == str(workspace.resolve())
    assert int(physical["size_bytes"]) > 0
    assert physical["sha256"] == sha256_file(output)
    probe = verification["probe"]
    assert isinstance(probe, dict) and probe["duration_us"] > 0  # real ffprobe facts
    assert verification["spec_identity"]["operation"] == "slideshow_render"

    chain = await queue.get_result_chain(job_id)
    assert chain["verification_status"] == "verified"
    assert chain["sha256"] == sha256_file(output)


@pytest.mark.asyncio
async def test_attack_e_slideshow_success_without_artifact_fails(tmp_path: Path) -> None:
    """Attack E: a slideshow handler claiming success with no artifact → FAILED.

    The attack runs against the DEFAULT registry — the historical hole
    ("job types without a verifier keep unverified semantics") is closed for
    ``slideshow_render`` because it now HAS one.
    """
    assert "slideshow_render" in default_artifact_verifiers()
    db = tmp_path / "jobs.sqlite3"
    queue = _queue(db)

    async def _lying_handler(payload: dict[str, object]) -> dict[str, object]:
        return {"success": True, "output_path": payload["output_path"]}

    queue.register_handler("slideshow_render", _lying_handler)
    job_id = await queue.enqueue(
        job_type="slideshow_render",
        idempotency_key="slideshow:attack-e:1",
        payload={
            "image_paths": [str(tmp_path / "x.jpg")],
            "output_path": str(tmp_path / "master.mp4"),
            "workspace_dir": str(tmp_path),
            "target_duration_us": 30_000_000,
        },
    )
    assert is_failure(await _drain(queue, job_id))
    assert _row(db, job_id, "error") == "verification_failed:success_without_artifact_claim"
    assert _row(db, job_id, "result_json") is None


# ---------------------------------------------------------------------------
# GAP-C — story: real Pillow render, no doubles at all
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gap_c_story_completes_with_verified_png(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = _queue(db)
    out_dir = tmp_path / "temp"
    out_dir.mkdir()
    output = out_dir / "story_9_z.png"

    job_id = await queue.enqueue(
        job_type="story",
        idempotency_key="story:gap-c:1",
        payload={"user_id": 9, "text": "سلام دنیا", "output_path": str(output)},
    )
    assert await _drain(queue, job_id) is JobStatus.COMPLETED

    result = await queue.get_result(job_id)
    assert result is not None
    verification = result[VERIFICATION_RESULT_KEY]
    assert verification["status"] == "verified"
    probe = verification["probe"]
    assert probe["format"] == "PNG" and probe["width"] == 1080 and probe["height"] == 1920
    assert verification["physical_identity"]["sha256"] == sha256_file(output)
    assert verification["spec_identity"] == {"operation": "story"}


@pytest.mark.asyncio
async def test_attack_a_success_without_artifact_fails(tmp_path: Path) -> None:
    """Attack A: handler returns success=True but the artifact does not exist."""
    db = tmp_path / "jobs.sqlite3"
    queue = _queue(db)
    ghost = tmp_path / "temp" / "story_1_a.png"

    async def _lying_handler(payload: dict[str, object]) -> dict[str, object]:
        return {
            "output_path": str(ghost),
            "artifact_kind": "image",
            "content_sha256": "sha256:" + "1" * 64,
            "size_bytes": 1234,
        }

    queue.register_handler("story", _lying_handler)
    job_id = await queue.enqueue(
        job_type="story",
        idempotency_key="story:attack-a:1",
        payload={"user_id": 1, "text": "x", "output_path": str(ghost)},
    )
    assert is_failure(await _drain(queue, job_id))
    assert _row(db, job_id, "error") == "verification_failed:missing_artifact"


@pytest.mark.asyncio
async def test_attack_b_zero_byte_artifact_fails(tmp_path: Path) -> None:
    """Attack B: the artifact exists but is zero bytes."""
    db = tmp_path / "jobs.sqlite3"
    queue = _queue(db)
    out_dir = tmp_path / "temp"
    out_dir.mkdir()
    zero = out_dir / "story_2_b.png"

    async def _zero_byte_handler(payload: dict[str, object]) -> dict[str, object]:
        zero.write_bytes(b"")
        return {
            "output_path": str(zero),
            "artifact_kind": "image",
            "content_sha256": sha256_file(zero),
            "size_bytes": 0,
        }

    queue.register_handler("story", _zero_byte_handler)
    job_id = await queue.enqueue(
        job_type="story",
        idempotency_key="story:attack-b:1",
        payload={"user_id": 2, "text": "x", "output_path": str(zero)},
    )
    assert is_failure(await _drain(queue, job_id))
    assert _row(db, job_id, "error") == "verification_failed:empty_artifact"


@pytest.mark.asyncio
async def test_attack_c_artifact_outside_expected_location_fails(tmp_path: Path) -> None:
    """Attack C: a VALID artifact at a location other than the dispatched one."""
    db = tmp_path / "jobs.sqlite3"
    queue = _queue(db)
    dispatched = tmp_path / "temp" / "story_3_c.png"
    dispatched.parent.mkdir()
    smuggled = tmp_path / "smuggled" / "story_3_c.png"
    smuggled.parent.mkdir()

    async def _relocating_handler(payload: dict[str, object]) -> dict[str, object]:
        Image.new("RGB", (64, 48), "#010101").save(smuggled)
        return {
            "output_path": str(smuggled),
            "artifact_kind": "image",
            "content_sha256": sha256_file(smuggled),
            "size_bytes": smuggled.stat().st_size,
        }

    queue.register_handler("story", _relocating_handler)
    job_id = await queue.enqueue(
        job_type="story",
        idempotency_key="story:attack-c:1",
        payload={"user_id": 3, "text": "x", "output_path": str(dispatched)},
    )
    assert is_failure(await _drain(queue, job_id))
    assert _row(db, job_id, "error") == "verification_failed:unexpected_artifact_path"


@pytest.mark.asyncio
async def test_attack_d_sha_changed_after_write_fails(tmp_path: Path) -> None:
    """Attack D: bytes change between the handler's claim and verification.

    The handler writes a valid PNG, claims its digest, then the bytes are
    corrupted BEFORE the handler returns — exactly the tamper window the
    independent re-measurement exists to catch.
    """
    db = tmp_path / "jobs.sqlite3"
    queue = _queue(db)
    out_dir = tmp_path / "temp"
    out_dir.mkdir()
    target = out_dir / "story_4_d.png"

    async def _tampered_handler(payload: dict[str, object]) -> dict[str, object]:
        Image.new("RGB", (64, 48), "#0a0a0a").save(target)
        claim_sha = sha256_file(target)
        # tamper AFTER the claim: same path, same size-ish, different bytes
        target.write_bytes(target.read_bytes() + b"tampered-tail")
        return {
            "output_path": str(target),
            "artifact_kind": "image",
            "content_sha256": claim_sha,
            "size_bytes": target.stat().st_size - len(b"tampered-tail"),
        }

    queue.register_handler("story", _tampered_handler)
    job_id = await queue.enqueue(
        job_type="story",
        idempotency_key="story:attack-d:1",
        payload={"user_id": 4, "text": "x", "output_path": str(target)},
    )
    assert is_failure(await _drain(queue, job_id))
    # size was claimed honestly-then-staled → the first mismatch that fires
    # may be size or sha; both are fail-closed typed rejections.
    error = str(_row(db, job_id, "error"))
    assert error in {
        "verification_failed:size_mismatch",
        "verification_failed:sha256_mismatch",
    }


# ---------------------------------------------------------------------------
# Attacks F/G — idempotency (queue-level, job type under the verifier)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_attack_f_same_key_same_payload_is_one_job_one_effect(
    tmp_path: Path,
) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = _queue(db)
    calls: list[dict[str, object]] = []
    out_dir = tmp_path / "temp"
    out_dir.mkdir()
    target = out_dir / "story_5_f.png"

    async def _counting_handler(payload: dict[str, object]) -> dict[str, object]:
        calls.append(payload)
        Image.new("RGB", (32, 32), "#0b0b0b").save(target)
        return {
            "output_path": str(target),
            "artifact_kind": "image",
            "content_sha256": sha256_file(target),
            "size_bytes": target.stat().st_size,
        }

    queue.register_handler("story", _counting_handler)
    payload = {"user_id": 5, "text": "same", "output_path": str(target)}
    first = await queue.enqueue(
        job_type="story", idempotency_key="story:attack-f:1", payload=payload
    )
    second = await queue.enqueue(
        job_type="story", idempotency_key="story:attack-f:1", payload=dict(payload)
    )
    assert first == second, "same key + same payload collapses to one job"
    assert await _drain(queue, first) is JobStatus.COMPLETED
    assert await _drain(queue, second) is JobStatus.COMPLETED
    assert len(calls) == 1, "no duplicate side effect"


@pytest.mark.asyncio
async def test_attack_g_same_key_different_payload_conflicts_first_wins(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = _queue(db)
    seen: list[str] = []
    out_dir = tmp_path / "temp"
    out_dir.mkdir()
    target = out_dir / "story_6_g.png"

    async def _recording_handler(payload: dict[str, object]) -> dict[str, object]:
        seen.append(str(payload["text"]))
        Image.new("RGB", (32, 32), "#0c0c0c").save(target)
        return {
            "output_path": str(target),
            "artifact_kind": "image",
            "content_sha256": sha256_file(target),
            "size_bytes": target.stat().st_size,
        }

    queue.register_handler("story", _recording_handler)
    first = await queue.enqueue(
        job_type="story",
        idempotency_key="story:attack-g:1",
        payload={"user_id": 6, "text": "ORIGINAL", "output_path": str(target)},
    )
    second = await queue.enqueue(
        job_type="story",
        idempotency_key="story:attack-g:1",
        payload={"user_id": 6, "text": "SMUGGLED", "output_path": str(target)},
    )
    assert first == second, "one key is one job — no second row"
    assert await _drain(queue, first) is JobStatus.COMPLETED
    assert seen == ["ORIGINAL"], "first payload wins; a retry cannot smuggle a new effect"
    conflict_events = [
        record
        for record in caplog.records
        if "idempotency" in record.getMessage().lower()
        and "conflict" in record.getMessage().lower()
    ]
    assert conflict_events, "the payload conflict must be observable (structured log event)"


# ---------------------------------------------------------------------------
# Attack H — re-verification is deterministic and non-destructive
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_attack_h_reverification_is_deterministic_and_read_only(
    tmp_path: Path,
) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = _queue(db)
    out_dir = tmp_path / "temp"
    out_dir.mkdir()
    target = out_dir / "story_7_h.png"

    job_id = await queue.enqueue(
        job_type="story",
        idempotency_key="story:attack-h:1",
        payload={"user_id": 7, "text": "determinism", "output_path": str(target)},
    )
    assert await _drain(queue, job_id) is JobStatus.COMPLETED
    result = await queue.get_result(job_id)
    assert result is not None
    payload = {"user_id": 7, "text": "determinism", "output_path": str(target)}

    verifier = default_artifact_verifiers()["story"]
    sha_before = sha256_file(target)
    handler_result = {k: v for k, v in result.items() if k != VERIFICATION_RESULT_KEY}
    first = verifier(payload, handler_result)
    second = verifier(payload, handler_result)
    assert first.ok and second.ok
    assert first.summary == second.summary, "same input → same deterministic verdict"
    assert sha256_file(target) == sha_before, "verification never mutates the artifact"


# ---------------------------------------------------------------------------
# GAP-B — pdf_extract: real extraction, real artifact, RAG doubled
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gap_b_pdf_extract_completes_with_verified_text_artifact(
    tmp_path: Path, fake_rag: _FakeRag
) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = _queue(db)
    source = make_pdf_with_text(tmp_path / "doc.pdf", "Hello PDF world")
    expected_artifact = tmp_path / "doc.extracted.txt"

    job_id = await queue.enqueue(
        job_type="pdf_extract",
        idempotency_key="pdf:gap-b:1",
        payload={"user_id": 11, "file_path": str(source), "file_id": "FID-1"},
    )
    assert await _drain(queue, job_id) is JobStatus.COMPLETED

    # the artifact is real, deterministic, and independently re-measured
    assert expected_artifact.read_text(encoding="utf-8") == "Hello PDF world"
    result = await queue.get_result(job_id)
    assert result is not None
    verification = result[VERIFICATION_RESULT_KEY]
    assert verification["status"] == "verified"
    assert verification["physical_identity"]["path"] == str(expected_artifact)
    assert verification["physical_identity"]["sha256"] == sha256_file(expected_artifact)
    assert verification["probe"] == {"encoding": "utf-8", "characters": 15}
    assert verification["logical_identity"]["file_id"] == "FID-1"
    # the (doubled) RAG ingestion still happened — behavior preserved
    assert fake_rag.added and fake_rag.added[0][1] == "Hello PDF world"


@pytest.mark.asyncio
async def test_pdf_extract_rag_failure_still_fails_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing the gap did not weaken the historical failure semantics."""
    import nexus_ai_agent.features.rag as rag_module

    class _BrokenRag:
        async def add_document(self, *_: object, **__: object) -> None:
            raise RuntimeError("embedding service down")

    monkeypatch.setattr(rag_module, "AdvancedRAGEngine", lambda: _BrokenRag())

    db = tmp_path / "jobs.sqlite3"
    queue = _queue(db)
    source = make_pdf_with_text(tmp_path / "doc.pdf")
    job_id = await queue.enqueue(
        job_type="pdf_extract",
        idempotency_key="pdf:rag-fail:1",
        payload={"user_id": 12, "file_path": str(source), "file_id": "FID-2"},
    )
    assert is_failure(await _drain(queue, job_id))
    assert "embedding service down" in str(_row(db, job_id, "error"))


@pytest.mark.asyncio
async def test_pdf_extract_success_claim_without_text_artifact_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attack A (pdf form through the queue): message-only success is FAILED."""
    import nexus_ai_agent.features.rag as rag_module

    monkeypatch.setattr(rag_module, "AdvancedRAGEngine", lambda: _FakeRag())

    db = tmp_path / "jobs.sqlite3"
    queue = _queue(db)

    async def _message_only_handler(payload: dict[str, object]) -> dict[str, object]:
        return {"message": f"Successfully processed {payload['file_id']}"}

    queue.register_handler("pdf_extract", _message_only_handler)
    source = make_pdf_with_text(tmp_path / "doc.pdf")
    job_id = await queue.enqueue(
        job_type="pdf_extract",
        idempotency_key="pdf:attack-a:1",
        payload={"user_id": 13, "file_path": str(source), "file_id": "FID-3"},
    )
    assert is_failure(await _drain(queue, job_id))
    assert _row(db, job_id, "error") == "verification_failed:success_without_artifact_claim"


# ---------------------------------------------------------------------------
# wiring proof — the DEFAULT registry covers every handler
# ---------------------------------------------------------------------------
def test_default_registry_verifies_every_registered_handler() -> None:
    verifiers = default_artifact_verifiers()
    handlers = default_job_handlers()
    assert set(handlers) <= set(verifiers)
    assert json.dumps(sorted(verifiers))  # registry is serializable/inspectable
