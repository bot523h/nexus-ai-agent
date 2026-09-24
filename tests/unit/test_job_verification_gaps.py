"""Unit tests for the task-180 verification dialects and verifiers (GAP-A/B/C).

Everything here runs against real bytes: Pillow writes the images, pypdf
reads a real (offset-correct) PDF, and the digests are recomputed by the
runtime's own ``sha256_file``. No verifier is ever mocked — only external
*services* (RAG) are doubles, and never in this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from nexus_ai_agent.creative.slideshow.ffmpeg import sha256_file
from nexus_ai_agent.jobs.creative_verification import slideshow_render_verifier
from nexus_ai_agent.jobs.feature_verification import (
    pdf_extract_verifier,
    pdf_text_artifact_path,
    publish_text_artifact,
    story_verifier,
)
from nexus_ai_agent.jobs.verification import (
    REASON_EMPTY,
    REASON_NO_CLAIM,
    REASON_PROBE_FAILED,
    REASON_SHA_MISMATCH,
    REASON_UNEXPECTED_PATH,
    ArtifactClaim,
    VerificationOutcome,
    verify_artifact,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _png(path: Path, size: tuple[int, int] = (64, 48)) -> Path:
    Image.new("RGB", size, "#123456").save(path)
    return path


def _story_claim(artifact: Path) -> dict[str, object]:
    return {
        "output_path": str(artifact),
        "artifact_kind": "image",
        "content_sha256": sha256_file(artifact),
        "size_bytes": artifact.stat().st_size,
    }


def _text_claim(artifact: Path) -> dict[str, object]:
    return {
        "artifact_path": str(artifact),
        "artifact_kind": "text",
        "content_sha256": sha256_file(artifact),
        "size_bytes": artifact.stat().st_size,
    }


# ---------------------------------------------------------------------------
# image dialect (GAP-C foundation)
# ---------------------------------------------------------------------------
def test_image_dialect_verifies_a_real_png(tmp_path: Path) -> None:
    artifact = _png(tmp_path / "s.png")
    claim = ArtifactClaim.from_handler_result(_story_claim(artifact))
    outcome = verify_artifact(claim, expected_path=artifact, expected_root=tmp_path)
    assert outcome.ok, outcome.reason_code
    probe = outcome.summary["probe"]
    assert probe == {"format": "PNG", "width": 64, "height": 48, "mode": "RGB"}


def test_image_dialect_rejects_garbage_bytes_with_png_name(tmp_path: Path) -> None:
    artifact = tmp_path / "s.png"
    artifact.write_bytes(b"this is not an image at all, just bytes")
    claim = ArtifactClaim.from_handler_result(_story_claim(artifact))
    outcome = verify_artifact(claim, expected_path=artifact, expected_root=tmp_path)
    assert not outcome.ok
    assert outcome.reason_code == REASON_PROBE_FAILED


def test_image_dialect_rejects_zero_byte_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "s.png"
    artifact.write_bytes(b"")
    claim = ArtifactClaim.from_handler_result(_story_claim(artifact))
    outcome = verify_artifact(claim, expected_path=artifact, expected_root=tmp_path)
    assert not outcome.ok
    assert outcome.reason_code == REASON_EMPTY


# ---------------------------------------------------------------------------
# text dialect (GAP-B foundation)
# ---------------------------------------------------------------------------
def test_text_dialect_verifies_utf8(tmp_path: Path) -> None:
    text = "متن استخراج‌شده\nline two"
    artifact = tmp_path / "d.extracted.txt"
    artifact.write_text(text, encoding="utf-8")
    claim = ArtifactClaim.from_handler_result(_text_claim(artifact))
    outcome = verify_artifact(claim, expected_path=artifact, expected_root=tmp_path)
    assert outcome.ok, outcome.reason_code
    assert outcome.summary["probe"] == {"encoding": "utf-8", "characters": len(text)}


def test_text_dialect_rejects_non_utf8_bytes(tmp_path: Path) -> None:
    artifact = tmp_path / "d.extracted.txt"
    artifact.write_bytes(b"\xff\xfe\x00binary garbage \x81\x82")
    claim = ArtifactClaim.from_handler_result(_text_claim(artifact))
    outcome = verify_artifact(claim, expected_path=artifact, expected_root=tmp_path)
    assert not outcome.ok
    assert outcome.reason_code == REASON_PROBE_FAILED


# ---------------------------------------------------------------------------
# story_verifier (GAP-C)
# ---------------------------------------------------------------------------
def _story_payload(tmp_path: Path) -> dict[str, object]:
    return {"user_id": 42, "text": "hi", "output_path": str(tmp_path / "story_42_x.png")}


def test_story_verifier_happy_path(tmp_path: Path) -> None:
    payload = _story_payload(tmp_path)
    artifact = _png(Path(str(payload["output_path"])))
    outcome = story_verifier(payload, _story_claim(artifact))
    assert outcome.ok, outcome.reason_code
    assert outcome.summary["status"] == "verified"
    assert outcome.summary["spec_identity"] == {"operation": "story"}
    assert outcome.summary["logical_identity"]["user_id"] == 42


def test_story_verifier_missing_artifact_fails(tmp_path: Path) -> None:
    payload = _story_payload(tmp_path)
    ghost = tmp_path / "story_42_x.png"  # never written
    claim = {
        "output_path": str(ghost),
        "artifact_kind": "image",
        "content_sha256": "sha256:" + "a" * 64,
        "size_bytes": 10,
    }
    outcome = story_verifier(payload, claim)
    assert not outcome.ok
    assert outcome.reason_code == "missing_artifact"


def test_story_verifier_relocated_artifact_fails(tmp_path: Path) -> None:
    """Attack C (unit form): a valid image at the WRONG path cannot pass."""
    payload = _story_payload(tmp_path)
    elsewhere = tmp_path / "elsewhere.png"
    _png(elsewhere)
    outcome = story_verifier(payload, _story_claim(elsewhere))
    assert not outcome.ok
    assert outcome.reason_code == REASON_UNEXPECTED_PATH


def test_story_verifier_tampered_sha_fails(tmp_path: Path) -> None:
    """Attack D (unit form): the recomputed digest must equal the claim."""
    payload = _story_payload(tmp_path)
    artifact = _png(Path(str(payload["output_path"])))
    claim = _story_claim(artifact)
    claim["content_sha256"] = "sha256:" + "b" * 64
    outcome = story_verifier(payload, claim)
    assert not outcome.ok
    assert outcome.reason_code == REASON_SHA_MISMATCH


def test_story_verifier_success_without_claim_fails(tmp_path: Path) -> None:
    """Attack A (unit form): success=True with no artifact claim is FAILED."""
    outcome = story_verifier(_story_payload(tmp_path), {"success": True})
    assert not outcome.ok
    assert outcome.reason_code == REASON_NO_CLAIM


# ---------------------------------------------------------------------------
# pdf_extract_verifier (GAP-B)
# ---------------------------------------------------------------------------
def _pdf_payload(tmp_path: Path) -> dict[str, object]:
    return {"user_id": 7, "file_path": str(tmp_path / "doc.pdf"), "file_id": "FID"}


def test_pdf_artifact_path_is_deterministic_from_payload(tmp_path: Path) -> None:
    assert pdf_text_artifact_path(tmp_path / "doc.pdf") == tmp_path / "doc.extracted.txt"
    # idempotent derivation, independent of any handler output
    assert pdf_text_artifact_path(tmp_path / "doc.pdf") == pdf_text_artifact_path(
        tmp_path / "doc.pdf"
    )


def test_pdf_verifier_happy_path(tmp_path: Path) -> None:
    payload = _pdf_payload(tmp_path)
    artifact = pdf_text_artifact_path(Path(str(payload["file_path"])))
    publish_text_artifact(artifact, "Hello PDF world")
    outcome = pdf_extract_verifier(payload, _text_claim(artifact))
    assert outcome.ok, outcome.reason_code
    assert outcome.summary["spec_identity"] == {"operation": "pdf_extract"}
    assert outcome.summary["logical_identity"]["file_id"] == "FID"


def test_pdf_verifier_success_without_persisted_text_fails(tmp_path: Path) -> None:
    """Attack A (pdf form): no persisted extraction, no completion."""
    outcome = pdf_extract_verifier(_pdf_payload(tmp_path), {"message": "Successfully processed"})
    assert not outcome.ok
    assert outcome.reason_code == REASON_NO_CLAIM


def test_pdf_verifier_zero_byte_extraction_fails(tmp_path: Path) -> None:
    """Attack B (pdf form): an empty text layer is not a verified artifact."""
    payload = _pdf_payload(tmp_path)
    artifact = pdf_text_artifact_path(Path(str(payload["file_path"])))
    artifact.write_bytes(b"")
    claim = {
        "artifact_path": str(artifact),
        "artifact_kind": "text",
        "content_sha256": "sha256:" + "c" * 64,
        "size_bytes": 0,
    }
    outcome = pdf_extract_verifier(payload, claim)
    assert not outcome.ok
    assert outcome.reason_code == REASON_EMPTY


# ---------------------------------------------------------------------------
# slideshow_render_verifier (GAP-A)
# ---------------------------------------------------------------------------
def _slideshow_payload(tmp_path: Path) -> dict[str, object]:
    return {
        "image_paths": [str(tmp_path / "a.jpg")],
        "output_path": str(tmp_path / "master.mp4"),
        "workspace_dir": str(tmp_path),
        "target_duration_us": 30_000_000,
        "project_name": "unit-slideshow",
    }


def test_slideshow_verifier_typed_user_failure_is_refused_fail_closed(
    tmp_path: Path,
) -> None:
    # task-181 (GAP-A) contract change (strengthening): a typed user failure
    # can never verify OK — refusal fail-closed, matching the creative
    # verifier's answer.
    outcome = slideshow_render_verifier(
        _slideshow_payload(tmp_path), {"success": False, "error_code": "ffmpeg_unavailable"}
    )
    assert not outcome.ok
    assert outcome.reason_code == "typed_user_failure"
    assert outcome.summary["reason_code"] == "typed_user_failure"


def test_slideshow_verifier_success_without_claim_fails(tmp_path: Path) -> None:
    """Attack E (unit form): the historical hole — success with no artifact."""
    outcome = slideshow_render_verifier(_slideshow_payload(tmp_path), {"success": True})
    assert not outcome.ok
    assert outcome.reason_code == REASON_NO_CLAIM


def test_slideshow_verifier_zero_byte_master_fails(tmp_path: Path) -> None:
    """Attack B (slideshow form)."""
    payload = _slideshow_payload(tmp_path)
    master = tmp_path / "master.mp4"
    master.write_bytes(b"")
    claim = {
        "success": True,
        "output_path": str(master),
        "content_sha256": "sha256:" + "d" * 64,
        "size_bytes": 0,
        "duration_us": 30_000_000,
    }
    outcome = slideshow_render_verifier(payload, claim)
    assert not outcome.ok
    assert outcome.reason_code == REASON_EMPTY


def test_slideshow_verifier_relocated_master_fails(tmp_path: Path) -> None:
    """Attack C (slideshow form): the master must be the dispatched path."""
    inside = tmp_path / "ws"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = _slideshow_payload(inside)
    payload["output_path"] = str(inside / "master.mp4")
    # A structurally valid claim that points somewhere else entirely.
    claim = {
        "success": True,
        "output_path": str(outside / "master.mp4"),
        "content_sha256": "sha256:" + "e" * 64,
        "size_bytes": 5,
    }
    outcome = slideshow_render_verifier(payload, claim)
    assert not outcome.ok
    assert outcome.reason_code == REASON_UNEXPECTED_PATH


# ---------------------------------------------------------------------------
# publish_text_artifact — atomic publication contract
# ---------------------------------------------------------------------------
def test_publish_text_artifact_is_atomic_and_leaves_no_temp(tmp_path: Path) -> None:
    destination = tmp_path / "sub" / "doc.extracted.txt"
    publish_text_artifact(destination, "payload text")
    assert destination.read_text(encoding="utf-8") == "payload text"
    leftovers = [p.name for p in destination.parent.iterdir() if ".part-" in p.name]
    assert leftovers == []


def test_publish_text_artifact_failure_leaves_no_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed publication must leave neither a partial artifact nor a temp."""
    from nexus_ai_agent.jobs import feature_verification

    destination = tmp_path / "doc.extracted.txt"

    def _boom(src: str | int, dst: str | int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(feature_verification.os, "replace", _boom)
    with pytest.raises(OSError):
        publish_text_artifact(destination, "payload text")
    assert not destination.exists()
    assert [p.name for p in tmp_path.iterdir() if ".part-" in p.name] == []


# ---------------------------------------------------------------------------
# re-verification determinism (Attack H, unit form)
# ---------------------------------------------------------------------------
def test_reverification_is_deterministic_and_read_only(tmp_path: Path) -> None:
    payload = _story_payload(tmp_path)
    artifact = _png(Path(str(payload["output_path"])))
    claim = _story_claim(artifact)
    before_sha = sha256_file(artifact)
    first: VerificationOutcome = story_verifier(payload, claim)
    second: VerificationOutcome = story_verifier(payload, claim)
    assert first.ok and second.ok
    assert first.summary == second.summary, "verification must be deterministic"
    assert sha256_file(artifact) == before_sha, "verification must not touch the artifact"
