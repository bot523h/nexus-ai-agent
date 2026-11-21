"""Coverage closure for the storyboard materializer's refusal and edge paths.

Each test pins one fail-closed branch: an invalid profile field, an unsafe path,
a Merkle proof edge, or one provenance tamper. These are the paths the happy-path
suite in ``test_creative_materializer.py`` does not reach.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from nexus_ai_agent.creative import materializer as mz
from nexus_ai_agent.creative.storyboard_compiler import compile_storyboard

SCRIPT = (
    "INT. KITCHEN - DAY\nSara: Hello, are you there?\nEXT. STREET - NIGHT\nAli: Yes, I am here.\n"
)
PROFILE = mz.RenderProfile.from_mapping(
    {"width": 320, "height": 180, "fps": 12, "sample_rate": 16000}
)


def _reason(exc: pytest.ExceptionInfo[mz.MaterializationError]) -> str:
    return exc.value.reason


# ----------------------------- profile and scalar validators -----------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"width": 320, "height": 180, "fps": 12, "sample_rate": 22050},
        {"width": 320, "height": 181, "fps": 12, "sample_rate": 16000},
        {"width": 320, "height": 180, "fps": float("inf"), "sample_rate": 16000},
        {"width": 320, "height": 180, "fps": "12", "sample_rate": 16000},
        {"width": 320, "height": 180, "fps": 999, "sample_rate": 16000},
        {"width": True, "height": 180, "fps": 12, "sample_rate": 16000},
        {"width": 320, "height": 180, "fps": 12, "sample_rate": 8000},
    ],
)
def test_profile_refuses_every_out_of_contract_field(raw: dict[str, object]) -> None:
    with pytest.raises(mz.MaterializationError) as info:
        mz.RenderProfile.from_mapping(raw)
    assert _reason(info) == "invalid_profile"


def test_safe_identifier_refuses_traversal_and_non_strings() -> None:
    with pytest.raises(mz.MaterializationError) as info:
        mz.validate_safe_id("../escape", "job")
    assert _reason(info) == "invalid_identifier"
    with pytest.raises(mz.MaterializationError):
        mz.validate_safe_id(42, "job")
    assert mz.validate_safe_id("job-1", "job") == "job-1"


def test_shot_duration_refuses_non_numbers_and_non_finite() -> None:
    with pytest.raises(mz.MaterializationError) as info:
        mz._finite_shot_duration("1.0")
    assert _reason(info) == "invalid_duration"
    with pytest.raises(mz.MaterializationError):
        mz._finite_shot_duration(float("nan"))
    assert mz._finite_shot_duration(1) == 1.0


# ----------------------------- filesystem path refusals -----------------------------


def test_stream_sha256_refuses_a_directory(tmp_path: Path) -> None:
    with pytest.raises(mz.MaterializationError) as info:
        mz.stream_sha256(tmp_path)
    assert _reason(info) == "not_regular_file"


def test_resolve_within_refuses_a_path_escaping_the_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(mz.MaterializationError) as info:
        mz.resolve_within(root, "../outside.mp4")
    assert _reason(info) == "path_traversal"  # ".." is refused before any resolution


def test_resolve_within_refuses_a_resolved_path_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    os.symlink(outside, link)
    with pytest.raises(mz.MaterializationError):
        mz.resolve_within(root, link / "x.mp4")


# ----------------------------- Merkle proof edges -----------------------------


def test_inclusion_proof_refuses_an_index_out_of_range() -> None:
    # RFC 6962 API: an out-of-range index is a plain ValueError (the typed
    # MaterializationError spelling belonged to the pre-RFC proof helper).
    with pytest.raises(ValueError, match="leaf index outside tree"):
        mz.merkle_inclusion_proof([b"a", b"b"], 2)


@pytest.mark.parametrize("total", [1, 2, 3, 5, 8, 13])
def test_every_leaf_of_every_size_verifies_against_the_root(total: int) -> None:
    payloads = [f"leaf-{i}".encode() for i in range(total)]
    root, _ = mz.merkle_root(payloads)
    for index in range(total):
        proof = mz.merkle_inclusion_proof(payloads, index)
        assert mz.verify_merkle_inclusion_proof(
            payloads[index],
            index=index,
            total_leaves=total,
            proof=proof,
            expected_root=root,
        )


def test_verify_merkle_proof_fails_closed_on_every_malformed_input() -> None:
    payloads = [b"a", b"b", b"c"]
    root, _ = mz.merkle_root(payloads)
    proof = mz.merkle_inclusion_proof(payloads, 1)
    assert not mz.verify_merkle_inclusion_proof(
        b"a", index=1, total_leaves=0, proof=proof, expected_root=root
    )
    assert not mz.verify_merkle_inclusion_proof(
        b"b", index=5, total_leaves=3, proof=proof, expected_root=root
    )
    assert not mz.verify_merkle_inclusion_proof(
        b"b", index=1, total_leaves=3, proof=(*proof, "00" * 32), expected_root=root
    )
    assert not mz.verify_merkle_inclusion_proof(
        b"b", index=1, total_leaves=3, proof=proof[:1], expected_root=root
    )
    assert not mz.verify_merkle_inclusion_proof(
        b"tampered", index=1, total_leaves=3, proof=proof, expected_root=root
    )


def test_verify_merkle_proof_rejects_a_proof_replayed_against_a_longer_tree() -> None:
    short = [b"a", b"b"]
    root_short, _ = mz.merkle_root(short)
    proof = mz.merkle_inclusion_proof(short, 0)
    assert not mz.verify_merkle_inclusion_proof(
        b"a", index=0, total_leaves=4, proof=proof, expected_root=root_short
    )


# ----------------------------- durability and media helpers -----------------------------


def test_fsync_directory_tolerates_a_missing_directory(tmp_path: Path) -> None:
    mz._fsync_directory(tmp_path / "does-not-exist")  # must not raise


def test_fsync_directory_tolerates_a_failing_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_fd: int) -> None:
        raise OSError("fsync refused")

    monkeypatch.setattr(mz.os, "fsync", _boom)
    mz._fsync_directory(tmp_path)  # must not raise


def test_slate_png_refuses_a_bad_seed(tmp_path: Path) -> None:
    with pytest.raises(mz.MaterializationError) as info:
        mz.write_slate_png(tmp_path / "s.png", width=320, height=180, seed=b"short", label="label")
    assert _reason(info) == "invalid_seed"


def test_tone_wav_refuses_out_of_range_duration(tmp_path: Path) -> None:
    seed = b"\x01" * 32
    with pytest.raises(mz.MaterializationError) as info:
        mz.write_tone_wav(tmp_path / "t.wav", duration_s=float("nan"), sample_rate=16000, seed=seed)
    assert _reason(info) == "invalid_duration"
    with pytest.raises(mz.MaterializationError):
        mz.write_tone_wav(tmp_path / "t.wav", duration_s=0.001, sample_rate=16000, seed=seed)


# ----------------------------- provenance tampers -----------------------------


@pytest.fixture()
def sealed(tmp_path: Path) -> tuple[mz.StoryboardMediaMaterializer, mz.MaterializedBundle, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    mat = mz.StoryboardMediaMaterializer(ws, PROFILE)
    bundle = mat.materialize(compile_storyboard(SCRIPT), ws / "out")
    return mat, bundle, ws


def _tone(bundle: mz.MaterializedBundle) -> Path:
    return Path(next(a for a in bundle.assets if a.kind == "tone_wav").resolved_path)


def test_provenance_refuses_an_asset_that_went_missing(
    sealed: tuple[mz.StoryboardMediaMaterializer, mz.MaterializedBundle, Path],
) -> None:
    mat, bundle, _ = sealed
    _tone(bundle).unlink()
    with pytest.raises(mz.MaterializationError, match="missing"):
        mat.verify_materialized_provenance(bundle)


def test_provenance_refuses_an_asset_replaced_by_a_directory(
    sealed: tuple[mz.StoryboardMediaMaterializer, mz.MaterializedBundle, Path],
) -> None:
    mat, bundle, _ = sealed
    tone = _tone(bundle)
    tone.unlink()
    tone.mkdir()
    with pytest.raises(mz.MaterializationError, match="not a regular file"):
        mat.verify_materialized_provenance(bundle)


def test_provenance_refuses_an_asset_whose_size_changed(
    sealed: tuple[mz.StoryboardMediaMaterializer, mz.MaterializedBundle, Path],
) -> None:
    mat, bundle, _ = sealed
    tone = _tone(bundle)
    tone.write_bytes(tone.read_bytes() + b"x")
    with pytest.raises(mz.MaterializationError, match="size changed"):
        mat.verify_materialized_provenance(bundle)


def test_provenance_refuses_an_asset_outside_the_workspace(
    sealed: tuple[mz.StoryboardMediaMaterializer, mz.MaterializedBundle, Path],
    tmp_path: Path,
) -> None:
    mat, bundle, _ = sealed
    outside = tmp_path / "elsewhere.wav"
    outside.write_bytes(_tone(bundle).read_bytes())
    records = list(bundle.assets)
    idx = next(i for i, a in enumerate(records) if a.kind == "tone_wav")
    records[idx] = dataclasses.replace(records[idx], resolved_path=str(outside))
    tampered = dataclasses.replace(bundle, assets=tuple(records))
    with pytest.raises(mz.MaterializationError, match="outside workspace"):
        mat.verify_materialized_provenance(tampered)


def test_provenance_refuses_a_manifest_whose_root_no_longer_matches(
    sealed: tuple[mz.StoryboardMediaMaterializer, mz.MaterializedBundle, Path],
) -> None:
    mat, bundle, _ = sealed
    tampered = dataclasses.replace(bundle, asset_merkle_root="0" * 64)
    with pytest.raises(mz.MaterializationError, match="merkle root mismatch"):
        mat.verify_materialized_provenance(tampered)


def test_profile_hash_is_stable_and_binds_every_field() -> None:
    base = PROFILE.profile_hash()
    assert (
        base
        == mz.RenderProfile.from_mapping(
            {"width": 320, "height": 180, "fps": 12, "sample_rate": 16000}
        ).profile_hash()
    )
    assert PROFILE.canonical_dict()["fps"] == repr(12.0)
    other = mz.RenderProfile.from_mapping(
        {"width": 320, "height": 180, "fps": 24, "sample_rate": 16000}
    )
    assert other.profile_hash() != base


def test_total_duration_is_the_exact_sum_of_shots(sealed: tuple[object, object, Path]) -> None:
    bundle = sealed[1]
    assert isinstance(bundle, mz.MaterializedBundle)
    assert bundle.total_duration_s == pytest.approx(sum(bundle.shot_durations_s))


def test_resolve_within_defense_in_depth_refuses_a_symlink_that_escapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    os.symlink(outside, root / "link")
    # Disable the first-line symlink walk so only the post-resolve containment
    # check stands between the caller and the escape.
    monkeypatch.setattr(mz, "refuse_symlink_chain", lambda _root, _candidate: None)
    with pytest.raises(mz.MaterializationError) as info:
        mz.resolve_within(root, root / "link" / "x.wav")
    assert _reason(info) == "path_outside_workspace"
