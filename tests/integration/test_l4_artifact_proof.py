"""Artifact-proof vertical slice (task-191) — real input to reopened, hashed artifact.

Drives ``scripts/l4_artifact_proof.py`` (loaded by path, like the board CLI):
a deterministic FFmpeg fixture goes through the SHIPPED chain —
``SURFACE_TO_CANONICAL`` → ``creative_render_job`` → packs runtime registry →
``CommandBus.dispatch`` → Lane IR → ``render_lane`` — and the artifact is then
verified by the queue's own verifier AND independently (stdlib hash, pure-Python
ISO-BMFF walk, full frame decode), reopened, and bound to its command/bus
provenance.  Undo is proven on the exact bus instance that executed.

The verifier is itself attacked (corrupted bytes, truncation, a fake artifact,
a wrong expectation, a foreign hash): a verifier that cannot fail is no
evidence.  The master/L4 boundary is pinned from the negative side:
``delivery.render_master_4k`` produces no file, and the harness never emits L4.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "l4_artifact_proof.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("l4_artifact_proof_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


harness = _load()


@pytest.fixture(scope="module")
def proof(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    workdir = tmp_path_factory.mktemp("l4proof")
    result: dict[str, Any] = harness.run_slice(workdir)
    # keep the trim artifact for the adversarial tests (the workspace is ours)
    result["_artifact_path"] = str(workdir / "creative_tmp" / "creative_l4proof" / "output.mp4")
    return result


@pytest.fixture(scope="module")
def binary() -> str:
    return resolve_ffmpeg_bin()


# ------------------------------------------------------------ the chain ----


def test_real_input_is_deterministic_media(proof: dict[str, Any]) -> None:
    assert proof["input"]["deterministic"] is True
    assert proof["input"]["size_bytes"] > 0
    assert proof["input"]["sha256"].startswith("sha256:")


def test_discovery_advertises_the_operation_from_runtime_truth(proof: dict[str, Any]) -> None:
    view = proof["discovery"]
    assert view["operation_id"] == "timeline.trim"
    assert view["available"] is True
    assert set(view["pack_states"].values()) == {"available"}


def test_typed_command_went_through_the_real_bus(proof: dict[str, Any]) -> None:
    command, result = proof["command"], proof["bus_result"]
    assert command["operation"] == "timeline.trim"
    assert command["protocol_version"] == "nagar.command.v1"
    assert command["execution_mode"] == "local"
    assert command["input"] == {
        "clip_asset_id": "src",
        "in_point_us": 500_000,
        "out_point_us": 2_500_000,
    }
    assert result["status"] == "applied"
    assert result["state_revision"] == 1
    assert result["transaction_id"].startswith("tx_")


def test_artifact_is_verified_by_the_queue_and_independently(proof: dict[str, Any]) -> None:
    assert proof["queue_verifier"] == {"ok": True, "reason_code": None}
    checks = proof["verification"]["checks"]
    assert checks == {
        "exists": True,
        "non_zero": True,
        "sha256_matches_claim": True,
        "iso_bmff_container": True,
        "streams_video_and_audio": True,
        "resolution": True,
        "duration": True,
        "full_decode_clean": True,
        "frame_count": True,
        "probe_agrees": True,
    }
    # expectations come from the shipped LaneProfile, not from the source media
    assert tuple(proof["expected"]["size"]) == (1280, 720)
    assert proof["verification"]["decode"]["video_frames"] == proof["expected"]["frames"]


def test_artifact_reopens_identically_and_is_hashed(proof: dict[str, Any]) -> None:
    assert proof["reopen"] == {"identical": True, "ok": True}
    assert proof["artifact"]["sha256"] == proof["verification"]["sha256"]
    assert proof["artifact"]["lane_ir_hash"].startswith("sha256:")
    assert proof["status"] == "ARTIFACT_PROOF_VERIFIED"


def test_undo_is_traceable_on_the_executing_bus(proof: dict[str, Any]) -> None:
    undo = proof["undo"]
    assert undo["undone_operation"] == "timeline.trim"
    assert undo["state_revision"] == proof["bus_result"]["state_revision"] + 1
    assert undo["state_hash_after_undo"] != proof["bus_result"]["state_hash"]
    # honest boundary: undo rewinds state, it does not retract published bytes
    assert undo["artifact_untouched_by_undo"] is True


def test_preview_proxy_is_a_real_verified_render(proof: dict[str, Any]) -> None:
    preview = proof["preview"]
    assert preview["operation"] == "delivery.make_proxy_480p"
    assert preview["success"] is True
    assert preview["queue_verifier_ok"] is True
    assert preview["ok"] is True
    assert preview["checks"]["resolution"] is True


# ------------------------------------------------- master / L4 boundary ----


def test_master_4k_is_simulated_and_never_claimed(proof: dict[str, Any]) -> None:
    master = proof["master"]
    assert master["dispatch_status"] == "applied"
    assert master["record_claims_master"] is True  # the record says so...
    assert master["artifact_path_in_output"] is False  # ...but no file exists
    assert master["verdict"].startswith("SIMULATED")


def test_harness_never_emits_l4(proof: dict[str, Any]) -> None:
    assert proof["l4"]["emitted"] is False
    candidate = proof["proof_registry_candidate"]
    assert candidate["kind"] == "artifact_evidence"  # never "production_like"
    assert set(candidate) == {"operation_id", "kind", "method", "recorded_at", "source_revision"}


# --------------------------------------------- the verifier can fail ----


def _expect(proof: dict[str, Any]) -> dict[str, Any]:
    e = proof["expected"]
    return {"size": tuple(e["size"]), "seconds": e["seconds"], "frames": e["frames"]}


def test_verifier_refuses_corrupted_bytes(
    proof: dict[str, Any], binary: str, tmp_path: Path
) -> None:
    bad = tmp_path / "corrupt.mp4"
    data = bytearray(Path(proof["_artifact_path"]).read_bytes())
    for offset in range(len(data) // 2, len(data) // 2 + 4096):
        data[offset] ^= 0xFF  # inside mdat: container intact, payload broken
    bad.write_bytes(bytes(data))
    outcome = harness.verify_artifact_independently(
        bad, binary, claimed_sha256=proof["artifact"]["sha256"], expect=_expect(proof)
    )
    assert outcome["ok"] is False
    assert outcome["checks"]["sha256_matches_claim"] is False


def test_verifier_refuses_a_truncated_file(proof: dict[str, Any], tmp_path: Path) -> None:
    bad = tmp_path / "truncated.mp4"
    data = Path(proof["_artifact_path"]).read_bytes()
    bad.write_bytes(data[: len(data) // 3])
    with pytest.raises(ValueError):
        harness.parse_isobmff(bad)


def test_verifier_refuses_a_fake_artifact(tmp_path: Path) -> None:
    fake = tmp_path / "output.mp4"
    fake.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    with pytest.raises(ValueError):
        harness.parse_isobmff(fake)


def test_verifier_refuses_a_wrong_expectation(proof: dict[str, Any], binary: str) -> None:
    wrong = {**_expect(proof), "size": (640, 360), "frames": 50}  # the SOURCE's shape
    outcome = harness.verify_artifact_independently(
        Path(proof["_artifact_path"]),
        binary,
        claimed_sha256=proof["artifact"]["sha256"],
        expect=wrong,
    )
    assert outcome["ok"] is False
    assert outcome["checks"]["resolution"] is False
    assert outcome["checks"]["frame_count"] is False


def test_verifier_refuses_a_foreign_hash_claim(
    proof: dict[str, Any], binary: str, tmp_path: Path
) -> None:
    copy = tmp_path / "copy.mp4"
    shutil.copyfile(proof["_artifact_path"], copy)
    outcome = harness.verify_artifact_independently(
        copy, binary, claimed_sha256=proof["input"]["sha256"], expect=_expect(proof)
    )
    assert outcome["checks"]["sha256_matches_claim"] is False
    assert outcome["ok"] is False


def test_verifier_refuses_a_well_formed_box_stream_that_is_not_mp4(tmp_path: Path) -> None:
    """Valid box framing with a movie box but no ``ftyp`` brand: only the
    brand rule can catch it (the PNG fake above dies earlier on framing)."""
    import struct

    def box(kind: bytes, payload: bytes = b"") -> bytes:
        return struct.pack(">I4s", 8 + len(payload), kind) + payload

    fake = tmp_path / "output.mp4"
    fake.write_bytes(box(b"free") + box(b"moov") + box(b"mdat", b"\x00" * 16))
    with pytest.raises(ValueError, match="ftyp"):
        harness.parse_isobmff(fake)
