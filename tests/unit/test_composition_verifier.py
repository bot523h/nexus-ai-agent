"""Unit tests for the composition's queue-side verifier.

The verifier is the *independent* half: it re-measures the artifact the worker
produced.  These tests assert that it refuses the artifacts the mission names
— a missing file, an empty file, a tampered file, a file outside the
workspace, and a file whose **bytes are fine but whose content is wrong**.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

from nexus_ai_agent.creative.composition.verifier import (
    REASON_SEMANTIC_MISMATCH,
    composition_verifier,
)


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _result(path: Path, *, kind: str = "image", operation: str = "media.resize_image") -> dict:
    return {
        "success": True,
        "artifact_path": str(path),
        "artifact_kind": kind,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
        "operation": operation,
        "output_asset_id": "derived_1",
        "plan_fingerprint": "sha256:" + "1" * 64,
        "registry_fingerprint": "sha256:" + "2" * 64,
    }


def _png(path: Path, color=(10, 200, 30)) -> Path:
    Image.new("RGB", (32, 24), color).save(path)
    return path


def test_a_correct_artifact_passes_both_layers(tmp_path: Path) -> None:
    artifact = _png(tmp_path / "out.png")
    outcome = composition_verifier({"workspace_dir": str(tmp_path)}, _result(artifact))
    assert outcome.ok is True
    summary = outcome.summary
    assert summary["semantic"]["verified"] is True
    assert summary["spec_identity"]["plan_fingerprint"].startswith("sha256:")


def test_a_typed_worker_failure_is_reported_as_such() -> None:
    outcome = composition_verifier(
        {},
        {"success": False, "error_code": "invalid_request", "error_detail": "nope"},
    )
    assert outcome.ok is False
    assert outcome.reason_code == "typed_user_failure"


def test_a_missing_artifact_is_refused(tmp_path: Path) -> None:
    artifact = _png(tmp_path / "out.png")
    result = _result(artifact)
    artifact.unlink()
    outcome = composition_verifier({"workspace_dir": str(tmp_path)}, result)
    assert outcome.ok is False


def test_an_empty_artifact_is_refused(tmp_path: Path) -> None:
    artifact = tmp_path / "out.png"
    artifact.write_bytes(b"")
    outcome = composition_verifier({"workspace_dir": str(tmp_path)}, _result(artifact))
    assert outcome.ok is False


def test_a_tampered_artifact_is_refused(tmp_path: Path) -> None:
    artifact = _png(tmp_path / "out.png")
    result = _result(artifact)
    artifact.write_bytes(artifact.read_bytes() + b"tamper")
    outcome = composition_verifier({"workspace_dir": str(tmp_path)}, result)
    assert outcome.ok is False


def test_an_artifact_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "creative_ws"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    _png(outside)
    outcome = composition_verifier({"workspace_dir": str(workspace)}, _result(outside))
    assert outcome.ok is False


def test_a_coloured_image_claiming_to_be_grayscale_is_refused(tmp_path: Path) -> None:
    """Re-hashing is not semantic verification: the pixels are checked.

    The contract (from the durable row) says the output must be grayscale; the
    verifier measures the pixels itself and refuses the coloured file even
    though its bytes hash correctly.
    """
    artifact = _png(tmp_path / "out.png", color=(10, 200, 30))
    result = _result(artifact)
    result["artifact_kind"] = "image"
    result["media_contract"] = {
        "kind": "image",
        "container": "png",
        "codec": "png",
        "color_space": "gray",
    }
    outcome = composition_verifier({"workspace_dir": str(tmp_path)}, result)
    assert outcome.ok is False
    assert outcome.reason_code == "semantic_mismatch"


def test_a_real_grayscale_image_passes_the_contract_aware_check(tmp_path: Path) -> None:
    artifact = tmp_path / "out.png"
    Image.new("L", (32, 24), 128).save(artifact)
    result = _result(artifact)
    result["artifact_kind"] = "image"
    result["media_contract"] = {
        "kind": "image",
        "container": "png",
        "codec": "png",
        "color_space": "gray",
    }
    outcome = composition_verifier({"workspace_dir": str(tmp_path)}, result)
    assert outcome.ok is True
    assert outcome.summary["semantic"]["measurements"]["grayscale"] is True


def test_a_non_image_non_video_artifact_is_verified_for_integrity_only(tmp_path: Path) -> None:
    artifact = tmp_path / "out.bin"
    artifact.write_bytes(b"payload")
    outcome = composition_verifier(
        {"workspace_dir": str(tmp_path)}, _result(artifact, kind="binary")
    )
    assert outcome.ok is True
    assert outcome.summary["semantic"]["method"] == "not_applicable"


def test_a_success_without_an_artifact_claim_is_refused(tmp_path: Path) -> None:
    outcome = composition_verifier({"workspace_dir": str(tmp_path)}, {"success": True})
    assert outcome.ok is False
    assert outcome.reason_code == "success_without_artifact_claim"


def test_the_semantic_reason_code_is_stable() -> None:
    assert REASON_SEMANTIC_MISMATCH == "semantic_mismatch"
