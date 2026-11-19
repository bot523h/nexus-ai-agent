"""Unit tests for the storyboard materializer: determinism, atomicity, symlinks, TOCTOU."""

from __future__ import annotations

import hashlib
import math
import os
import struct
import wave
from pathlib import Path

import pytest

from nexus_ai_agent.creative import materializer as mz
from nexus_ai_agent.creative.storyboard_compiler import compile_storyboard

SCRIPT = (
    "INT. KITCHEN - DAY\n"
    "Sara: Hello, are you there?\n"
    "Ali: Yes, I am here.\n"
    "EXT. STREET - NIGHT\n"
    "سارا: سلام، حالت چطوره؟\n"
)
PROFILE = mz.RenderProfile.from_mapping(
    {"width": 320, "height": 180, "fps": 12, "sample_rate": 16000}
)


def _first_shot_id(manifest) -> str:  # type: ignore[no-untyped-def]
    event = manifest.editorial.events[0]
    return f"s{int(event.scene_index):03d}-sh{int(event.shot_index):03d}"


@pytest.fixture(scope="module")
def manifest():
    return compile_storyboard(SCRIPT)


# ----------------------------- Merkle (RFC 6962 style) -----------------------------


def test_merkle_empty_tree_is_sha256_of_empty_string() -> None:
    root, total = mz.merkle_root([])
    assert root == hashlib.sha256(b"").hexdigest()
    assert total == 0


def test_merkle_single_leaf_uses_leaf_prefix() -> None:
    root, total = mz.merkle_root([b"a"])
    assert root == hashlib.sha256(b"\x00a").hexdigest()
    assert total == 1


def test_merkle_two_leaves_use_node_prefix_not_plain_concatenation() -> None:
    root, _ = mz.merkle_root([b"a", b"b"])
    leaf_a = hashlib.sha256(b"\x00a").digest()
    leaf_b = hashlib.sha256(b"\x00b").digest()
    assert root == hashlib.sha256(b"\x01" + leaf_a + leaf_b).hexdigest()
    assert root != hashlib.sha256(b"\x00a\x00b").hexdigest()


def test_merkle_promotes_unpaired_leaf_without_duplicating_it() -> None:
    root, total = mz.merkle_root([b"a", b"b", b"c"])
    leaf = [hashlib.sha256(b"\x00" + x).digest() for x in (b"a", b"b", b"c")]
    n01 = hashlib.sha256(b"\x01" + leaf[0] + leaf[1]).digest()
    assert root == hashlib.sha256(b"\x01" + n01 + leaf[2]).hexdigest()
    assert root != mz.merkle_root([b"a", b"b", b"c", b"c"])[0]
    assert total == 3


# ----------------------------- numeric and identifier guards -----------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "12", None])
def test_profile_rejects_non_finite_bool_and_non_numeric_values(bad: object) -> None:
    with pytest.raises(mz.MaterializationError) as info:
        mz.RenderProfile.from_mapping(
            {"width": bad, "height": 180, "fps": 12, "sample_rate": 16000}
        )
    assert info.value.reason == "invalid_profile"


def test_profile_rejects_non_finite_fps_before_range_check() -> None:
    with pytest.raises(mz.MaterializationError, match="finite"):
        mz.RenderProfile.from_mapping({"width": 320, "height": 180, "fps": math.nan})


@pytest.mark.parametrize(
    "raw",
    [
        {"width": 321, "height": 180, "fps": 12, "sample_rate": 16000},
        {"width": 320, "height": 181, "fps": 12, "sample_rate": 16000},
        {"width": 15, "height": 180, "fps": 12, "sample_rate": 16000},
        {"width": 320, "height": 180, "fps": 0.5, "sample_rate": 16000},
        {"width": 320, "height": 180, "fps": 12, "sample_rate": 12345},
    ],
)
def test_profile_rejects_out_of_contract_values(raw: dict) -> None:
    with pytest.raises(mz.MaterializationError):
        mz.RenderProfile.from_mapping(raw)


@pytest.mark.parametrize("bad", ["../etc", "a/b", "..", "", "x" * 65, "-lead", ".hidden"])
def test_safe_identifier_rejects_traversal_and_malformed(bad: str) -> None:
    with pytest.raises(mz.MaterializationError) as info:
        mz.validate_safe_id(bad, "project_id")
    assert info.value.reason == "invalid_identifier"


# ----------------------------- path and symlink defense -----------------------------


def test_resolve_within_rejects_dotdot_and_absolute_outside(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(mz.MaterializationError) as info:
        mz.resolve_within(root, "../escape.png")
    assert info.value.reason == "path_traversal"
    with pytest.raises(mz.MaterializationError) as info:
        mz.resolve_within(root, tmp_path / "elsewhere.png")
    assert info.value.reason == "path_outside_workspace"


def test_resolve_within_refuses_symlink_component(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    real = root / "real"
    real.mkdir(parents=True)
    os.symlink(real, root / "link")
    with pytest.raises(mz.MaterializationError) as info:
        mz.resolve_within(root, "link/file.png")
    assert info.value.reason == "symlink_refused"


def test_stream_sha256_refuses_to_follow_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"payload")
    link = tmp_path / "link.bin"
    os.symlink(target, link)
    with pytest.raises(OSError):
        mz.stream_sha256(link)


def test_atomic_write_leaves_no_partial_or_temp_file_on_failure(tmp_path: Path) -> None:
    final = tmp_path / "out.bin"

    def _boom(handle: object) -> None:
        handle.write(b"half")  # type: ignore[attr-defined]
        raise RuntimeError("crash mid-write")

    with pytest.raises(RuntimeError, match="crash"):
        mz.atomic_write(final, _boom)
    assert not final.exists()
    assert list(tmp_path.glob(".out.bin.*.tmp")) == []


def test_atomic_write_replaces_only_on_success(tmp_path: Path) -> None:
    final = tmp_path / "out.bin"
    mz.atomic_write(final, lambda h: h.write(b"complete"))
    assert final.read_bytes() == b"complete"


# ----------------------------- generators -----------------------------


def test_slate_png_has_requested_dimensions_and_valid_signature(tmp_path: Path) -> None:
    png = tmp_path / "slate.png"
    mz.write_slate_png(png, width=64, height=32, seed=bytes(range(32)), label="t")
    data = png.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (64, 32)


def test_tone_wav_frame_count_is_exactly_round_duration_times_rate(tmp_path: Path) -> None:
    wav_path = tmp_path / "tone.wav"
    frames = mz.write_tone_wav(wav_path, duration_s=0.5, sample_rate=16000, seed=bytes(32))
    assert frames == 8000
    with wave.open(str(wav_path), "rb") as wav:
        assert wav.getnframes() == 8000
        assert wav.getframerate() == 16000
        assert wav.getsampwidth() == 2
        assert wav.getnchannels() == 1


@pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf, 10_000.0])
def test_tone_wav_refuses_non_finite_or_out_of_range_duration(tmp_path: Path, bad: float) -> None:
    with pytest.raises(mz.MaterializationError):
        mz.write_tone_wav(tmp_path / "t.wav", duration_s=bad, sample_rate=16000, seed=bytes(32))


def test_shot_seed_is_deterministic_and_sensitive_to_identity() -> None:
    a = mz.shot_seed("s000-sh000", "dialogue", 0, 0)
    assert a == mz.shot_seed("s000-sh000", "dialogue", 0, 0)
    assert a != mz.shot_seed("s000-sh001", "dialogue", 0, 0)


# ----------------------------- materialization and provenance seal -----------------------------


def test_materialize_is_byte_deterministic_across_separate_runs(tmp_path: Path, manifest) -> None:
    hashes = []
    for run in ("run-a", "run-b"):
        ws = tmp_path / run
        ws.mkdir()
        bundle = mz.StoryboardMediaMaterializer(ws, PROFILE).materialize(manifest, ws / "out")
        hashes.append(tuple(a.sha256 for a in bundle.assets))
    assert hashes[0] == hashes[1]
    assert len(hashes[0]) == 2 * manifest.total_shots


def test_provenance_seal_passes_on_untouched_bundle(tmp_path: Path, manifest) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    mat = mz.StoryboardMediaMaterializer(ws, PROFILE)
    bundle = mat.materialize(manifest, ws / "out")
    mat.verify_materialized_provenance(bundle)  # must not raise


def test_toctou_single_byte_flip_in_tone_is_detected(tmp_path: Path, manifest) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    mat = mz.StoryboardMediaMaterializer(ws, PROFILE)
    bundle = mat.materialize(manifest, ws / "out")
    wav = Path(next(a for a in bundle.assets if a.kind == "tone_wav").resolved_path)
    raw = bytearray(wav.read_bytes())
    raw[len(raw) // 2] ^= 0x01
    wav.write_bytes(bytes(raw))
    with pytest.raises(mz.MaterializationError) as info:
        mat.verify_materialized_provenance(bundle)
    assert info.value.reason == "provenance_violated"


def test_toctou_inode_replacement_with_identical_bytes_is_detected(
    tmp_path: Path, manifest
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    mat = mz.StoryboardMediaMaterializer(ws, PROFILE)
    bundle = mat.materialize(manifest, ws / "out")
    png = Path(next(a for a in bundle.assets if a.kind == "slate_png").resolved_path)
    same = png.read_bytes()
    replacement = png.with_name(png.name + ".swap")
    replacement.write_bytes(same)
    os.replace(replacement, png)  # same content, new inode
    with pytest.raises(mz.MaterializationError, match="inode"):
        mat.verify_materialized_provenance(bundle)


def test_toctou_symlink_swap_is_detected(tmp_path: Path, manifest) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    mat = mz.StoryboardMediaMaterializer(ws, PROFILE)
    bundle = mat.materialize(manifest, ws / "out")
    wav = Path(next(a for a in bundle.assets if a.kind == "tone_wav").resolved_path)
    decoy = tmp_path / "decoy.wav"
    decoy.write_bytes(wav.read_bytes())
    wav.unlink()
    os.symlink(decoy, wav)
    with pytest.raises(mz.MaterializationError) as info:
        mat.verify_materialized_provenance(bundle)
    assert info.value.reason == "provenance_violated"


def test_materialize_rejects_local_image_traversal(tmp_path: Path, manifest) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    shot = _first_shot_id(manifest)
    mat = mz.StoryboardMediaMaterializer(ws, PROFILE)
    with pytest.raises(mz.MaterializationError) as info:
        mat.materialize(manifest, ws / "out", local_images={shot: "../outside.png"})
    assert info.value.reason == "path_traversal"


def test_materialize_links_and_hashes_a_user_image_inside_workspace(
    tmp_path: Path, manifest
) -> None:
    ws = tmp_path / "ws"
    (ws / "assets").mkdir(parents=True)
    user_png = ws / "assets" / "frame.png"
    mz.write_slate_png(user_png, width=320, height=180, seed=bytes(range(32)), label="user")
    shot = _first_shot_id(manifest)
    mat = mz.StoryboardMediaMaterializer(ws, PROFILE)
    bundle = mat.materialize(manifest, ws / "out", local_images={shot: "assets/frame.png"})
    linked = [a for a in bundle.assets if a.kind == "local_image"]
    assert len(linked) == 1
    assert linked[0].sha256 == hashlib.sha256(user_png.read_bytes()).hexdigest()
    mat.verify_materialized_provenance(bundle)
