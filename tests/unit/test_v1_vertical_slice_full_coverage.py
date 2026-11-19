"""Line + branch closure for the free vertical slice, plus an AST mutation gate.

Covered modules (the whole "free vertical slice" of the creative package):

* ``materializer.py`` - validation, RFC 6962 tree/proofs, atomic IO, provenance.
* ``artifact_passport.py`` - seal building, per-shot Merkle verification, IO refusals.
* ``artifact_verifier.py`` - ISO-BMFF walker, ffprobe parsing, verdict branches.
* ``artifact_download.py`` - every refusal the download gate can produce.
* ``studio_cli.py`` - ``init``/``doctor``/``render`` argument and error branches.
* ``studio_cas_vault_bridge.py`` - CAS index, memory meter, FFmpeg/vault refusals.

Every branch is driven by real inputs (real files, real FIFOs, real symlinks, real
FFmpeg output); where a refusal can only be provoked by a fault in the
environment, the fault is injected at the narrowest seam (``os.open``,
``subprocess.run``, ``sqlite3.connect``, ``verify_mp4``) rather than by faking the
logic under test.

The module ends with a genuine AST mutation gate: four mutants are parsed from
the real sources with :func:`ast.parse`, compiled, executed in their own module
objects and each must be **KILLED** by the very assertions that pass on the
pristine module. Nothing is removed from :data:`sys.modules`, so C-extension
modules (``numpy``, ``PIL``, ``sqlalchemy``, …) stay bound to their original
instances while the mutants run.
"""

from __future__ import annotations

import ast
import dataclasses
import gc
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import stat
import struct
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from nexus_ai_agent.creative import artifact_download as ad
from nexus_ai_agent.creative import artifact_passport as ap
from nexus_ai_agent.creative import artifact_verifier as av
from nexus_ai_agent.creative import materializer as mz
from nexus_ai_agent.creative import studio_cas_vault_bridge as bridge
from nexus_ai_agent.creative import studio_cli
from nexus_ai_agent.creative.storyboard_compiler import compile_storyboard
from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
    derive_vault_keys as _derive_vault_keys,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY = "k" * 40
FPS = 12
PROFILE = {"width": 320, "height": 180, "fps": FPS, "sample_rate": 16_000}


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def real_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A genuine 1 s H.264/AAC MP4 produced by FFmpeg (no fixtures on disk)."""
    out = tmp_path_factory.mktemp("media") / "clip.mp4"
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x90:rate=10:duration=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=16000:duration=1",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(out),
            ],
            check=True,
            timeout=120,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment gate
        pytest.skip("real ffmpeg is required for media-backed coverage tests")
    return out


@pytest.fixture()
def copied_mp4(tmp_path: Path, real_mp4: Path) -> Path:
    target = tmp_path / "storyboard.mp4"
    target.write_bytes(real_mp4.read_bytes())
    return target


def _passport(mp4: Path, **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "job_id": "job-1",
        "project_id": "proj-1",
        "source_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "render_profile_hash": "c" * 64,
        "asset_merkle_root": "d" * 64,
        "total_asset_leaves": 6,
        "output_rel_path": "projects/proj-1/renders/job-1/storyboard.mp4",
        "output_size_bytes": mp4.stat().st_size,
        "output_sha256": hashlib.sha256(mp4.read_bytes()).hexdigest(),
        "probed_duration_s": 1.0,
        "verifier_status": "PASS",
        "created_at_unix_s": 1_700_000_000,
        "signing_key": KEY,
    }
    fields.update(overrides)
    return ap.build_passport(**fields)


def _sealed(data: dict[str, Any], key: str = KEY) -> dict[str, Any]:
    data = dict(data)
    data.pop("hmac_sha256", None)
    data["hmac_sha256"] = ap._seal(data, key.encode())
    return data


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _shot_hashes(count: int = 3) -> tuple[str, ...]:
    return tuple(hashlib.sha256(f"shot-{index}".encode()).hexdigest() for index in range(count))


# --------------------------------------------------------------------------- #
# materializer.py - profile, id and path validation
# --------------------------------------------------------------------------- #


def test_render_profile_refuses_odd_dimensions_and_unknown_sample_rate() -> None:
    with pytest.raises(mz.MaterializationError, match="invalid_profile"):
        mz.RenderProfile.from_mapping({**PROFILE, "width": 321})
    with pytest.raises(mz.MaterializationError, match="unsupported sample_rate"):
        mz.RenderProfile.from_mapping({**PROFILE, "sample_rate": 22_050})
    profile = mz.RenderProfile.from_mapping(PROFILE)
    assert profile.canonical_dict()["fps"] == repr(12.0)
    assert len(profile.profile_hash()) == 64


@pytest.mark.parametrize(
    "value,reason",
    [
        ("wide", "must be a number"),
        (True, "must be a number"),
        (math.nan, "must be finite"),
        (math.inf, "must be finite"),
        (0, "must be in"),
        (999, "must be in"),
    ],
)
def test_finite_rejects_every_out_of_contract_number(value: object, reason: str) -> None:
    with pytest.raises(mz.MaterializationError, match=reason):
        mz._finite(value, "fps", 1.0, 60.0)


@pytest.mark.parametrize(
    "value,reason",
    [(12.5, "must be an integer"), (False, "must be an integer"), (8, "must be in")],
)
def test_integer_rejects_every_out_of_contract_number(value: object, reason: str) -> None:
    with pytest.raises(mz.MaterializationError, match=reason):
        mz._integer(value, "width", 16, 1920)


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "-lead", "x" * 65, 42, None])
def test_validate_safe_id_refuses_unsafe_identifiers(bad: object) -> None:
    with pytest.raises(mz.MaterializationError, match="invalid_identifier"):
        mz.validate_safe_id(bad, "shot_id")
    assert mz.validate_safe_id("shot_000-1", "shot_id") == "shot_000-1"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_stream_sha256_refuses_a_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(mz.MaterializationError, match="not_regular_file"):
        mz.stream_sha256(fifo)


def test_resolve_within_refuses_traversal_and_absolute_escapes(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(mz.MaterializationError, match="path_traversal"):
        mz.resolve_within(root, "../etc/passwd")
    with pytest.raises(mz.MaterializationError, match="path_outside_workspace"):
        mz.resolve_within(root, tmp_path / "elsewhere")
    assert mz.resolve_within(root, "inner/file.txt") == root.resolve() / "inner/file.txt"
    assert mz.resolve_within(root, "not-yet-created/leaf.bin").name == "leaf.bin"


def test_refuse_symlink_chain_refuses_a_linked_component(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "real").mkdir(parents=True)
    link = root / "link"
    link.symlink_to(root / "real", target_is_directory=True)
    with pytest.raises(mz.MaterializationError, match="symlink_refused"):
        mz.refuse_symlink_chain(root.resolve(), root / "link" / "file.bin")


# --------------------------------------------------------------------------- #
# materializer.py - RFC 6962 primitives
# --------------------------------------------------------------------------- #


def test_merkle_inclusion_proof_rejects_bad_indexes() -> None:
    leaves = [b"a", b"b"]
    for bad in (-1, 2, True, "0", 1.0):
        with pytest.raises(ValueError, match="leaf index outside tree"):
            mz.merkle_inclusion_proof(leaves, bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="leaf index outside tree"):
        mz.merkle_inclusion_proof([], 0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"total_leaves": 0},
        {"index": -1},
        {"index": 4},
        {"expected_root": "NOT-HEX"},
        {"proof": "deadbeef"},
        {"index": True},
        {"total_leaves": True},
        {"leaf": "not-bytes"},
    ],
)
def test_verify_merkle_inclusion_proof_refuses_malformed_arguments(kwargs: dict[str, Any]) -> None:
    arguments: dict[str, Any] = {
        "leaf": b"a",
        "index": 0,
        "total_leaves": 2,
        "proof": mz.merkle_inclusion_proof([b"a", b"b"], 0),
        "expected_root": mz.merkle_root_sha256([b"a", b"b"]),
    }
    arguments.update(kwargs)
    assert (
        mz.verify_merkle_inclusion_proof(
            arguments["leaf"],
            index=arguments["index"],
            total_leaves=arguments["total_leaves"],
            proof=arguments["proof"],
            expected_root=arguments["expected_root"],
        )
        is False
    )


def test_verify_merkle_inclusion_proof_rejects_surplus_and_invalid_siblings() -> None:
    leaves = [b"a", b"b", b"c", b"d"]
    root = mz.merkle_root_sha256(leaves)
    proof = list(mz.merkle_inclusion_proof(leaves, 0))
    assert (
        mz.verify_merkle_inclusion_proof(
            b"a", index=0, total_leaves=4, proof=proof + ["00" * 32], expected_root=root
        )
        is False
    )
    tampered = list(proof)
    tampered[0] = "zz" * 32
    assert (
        mz.verify_merkle_inclusion_proof(
            b"a", index=0, total_leaves=4, proof=tampered, expected_root=root
        )
        is False
    )
    assert (
        mz.verify_merkle_inclusion_proof(
            b"a", index=0, total_leaves=4, proof=proof, expected_root="e" * 64
        )
        is False
    )


def test_merkle_roots_are_rfc6962_domain_separated() -> None:
    """Pin the RFC 6962 construction: leaf = H(0x00||d), node = H(0x01||l||r)."""
    leaves = [b"a", b"b", b"c"]
    left = hashlib.sha256(b"\x00" + b"a").digest()
    right = hashlib.sha256(b"\x00" + b"b").digest()
    pair = hashlib.sha256(b"\x01" + left + right).digest()
    leaf_c = hashlib.sha256(b"\x00" + b"c").digest()
    expected = hashlib.sha256(b"\x01" + pair + leaf_c).hexdigest()
    assert mz.merkle_root_sha256(leaves) == expected
    assert mz.merkle_root(leaves) == (expected, 3)
    assert mz.merkle_root_sha256([]) == mz.EMPTY_MERKLE_ROOT
    assert mz._merkle_hash([], 0, 0).hex() == mz.EMPTY_MERKLE_ROOT


def test_shot_content_hash_and_leaf_validation() -> None:
    digest = hashlib.sha256(b"image").hexdigest()
    tone = hashlib.sha256(b"tone").hexdigest()
    hashed = mz.shot_content_hash(
        shot_id="s000-sh000", image_sha256=digest, wav_sha256=tone, duration_s=1.5
    )
    assert len(hashed) == 64
    assert hashed == mz.shot_content_hash(
        shot_id="s000-sh000", image_sha256=digest, wav_sha256=tone, duration_s=1.5
    )
    with pytest.raises(mz.MaterializationError, match="invalid_digest"):
        mz.shot_content_hash(
            shot_id="s000-sh000", image_sha256="nope", wav_sha256=tone, duration_s=1.0
        )
    with pytest.raises(mz.MaterializationError, match="invalid_digest"):
        mz.shot_leaf_payloads([hashed, "XYZ"])
    with pytest.raises(mz.MaterializationError, match="invalid_digest"):
        mz.shot_leaf_payloads([12])  # type: ignore[list-item]


def test_merkle_inclusion_proof_record_rejects_bad_inputs() -> None:
    payloads = mz.shot_leaf_payloads(_shot_hashes(2))
    for bad in (-1, 2, True):
        with pytest.raises(mz.MaterializationError, match="invalid_leaf_index"):
            mz.merkle_inclusion_proof_record(payloads, bad)  # type: ignore[arg-type]
    with pytest.raises(mz.MaterializationError, match="invalid_leaf_payload"):
        mz.merkle_inclusion_proof_record([b"not-hex"], 0)


def test_proof_record_verification_rejects_every_tampered_field() -> None:
    hashes = _shot_hashes(3)
    payloads = mz.shot_leaf_payloads(hashes)
    root = mz.merkle_root_sha256(payloads)
    proofs = [mz.merkle_inclusion_proof_record(payloads, index) for index in range(3)]
    assert (
        mz.verify_shot_inclusion_proofs(shot_hashes=hashes, merkle_root=root, proofs=proofs) == ()
    )

    def _violations(mutated: list[Any]) -> tuple[str, ...]:
        return mz.verify_shot_inclusion_proofs(shot_hashes=hashes, merkle_root=root, proofs=mutated)

    inner = [dict(proof) for proof in proofs]
    inner[0]["leaf_index"] = 1
    assert "shot_inclusion_proof_invalid" in _violations(inner)

    inner = [dict(proof) for proof in proofs]
    inner[0]["leaf_hash"] = "f" * 64
    assert "shot_inclusion_proof_invalid" in _violations(inner)

    inner = [dict(proof) for proof in proofs]
    inner[0]["root_hash"] = "f" * 64
    assert "shot_inclusion_proof_invalid" in _violations(inner)

    inner = [dict(proof) for proof in proofs]
    inner[0]["total_leaves"] = 99
    assert "shot_inclusion_proof_invalid" in _violations(inner)

    inner = [dict(proof) for proof in proofs]
    inner[0]["sibling_hashes"] = "not-a-list"
    assert "shot_inclusion_proof_invalid" in _violations(inner)

    inner = [dict(proof) for proof in proofs]
    inner[0]["sibling_hashes"] = ["0" * 64 for _ in inner[0]["sibling_hashes"]]
    assert "shot_inclusion_proof_invalid" in _violations(inner)

    assert "shot_inclusion_proof_invalid" in _violations([object(), *proofs[1:]])  # type: ignore[list-item]


def test_shot_tree_violation_codes_are_typed_and_ordered() -> None:
    hashes = _shot_hashes(2)
    payloads = mz.shot_leaf_payloads(hashes)
    root = mz.merkle_root_sha256(payloads)
    proofs = [mz.merkle_inclusion_proof_record(payloads, index) for index in range(2)]
    assert mz.verify_shot_inclusion_proofs(shot_hashes=hashes, merkle_root="", proofs=proofs) == (
        "shot_merkle_root_mismatch",
    )
    assert mz.verify_shot_inclusion_proofs(shot_hashes=["zz"], merkle_root=root, proofs=proofs) == (
        "shot_inclusion_proof_invalid",
    )
    assert (
        mz.verify_shot_inclusion_proofs(shot_hashes=hashes, merkle_root="f" * 64, proofs=proofs)[0]
        == "shot_merkle_root_mismatch"
    )
    assert mz.verify_shot_inclusion_proofs(shot_hashes=hashes, merkle_root=root, proofs=[]) == (
        "shot_inclusion_proof_count_mismatch",
    )
    # An empty tree with no proofs is the only proof-free shape that verifies.
    assert (
        mz.verify_shot_inclusion_proofs(shot_hashes=(), merkle_root=mz.EMPTY_MERKLE_ROOT, proofs=[])
        == ()
    )


def test_shot_content_hashes_from_assets_uses_tone_records(tmp_path: Path) -> None:
    records = [
        mz.AssetRecord("s0", "slate_png", "a.png", "/a.png", 1, "a" * 64, 1, 0.0),
        mz.AssetRecord("s0", "tone_wav", "a.wav", "/a.wav", 2, "b" * 64, 2, 0.5),
        mz.AssetRecord("s1", "local_image", "b.png", "/b.png", 3, "c" * 64, 3, 0.0),
        mz.AssetRecord("s1", "tone_wav", "b.wav", "/b.wav", 4, "d" * 64, 4, 0.25),
        mz.AssetRecord("manifest", "manifest_json", "m.json", "/m.json", 5, "e" * 64, 5, 0.0),
    ]
    hashes = mz.shot_content_hashes_from_assets(records)
    assert hashes == (
        mz.shot_content_hash(
            shot_id="s0", image_sha256="a" * 64, wav_sha256="b" * 64, duration_s=0.5
        ),
        mz.shot_content_hash(
            shot_id="s1", image_sha256="c" * 64, wav_sha256="d" * 64, duration_s=0.25
        ),
    )


def test_planned_shot_durations_refuses_unknown_sample_rate() -> None:
    manifest = compile_storyboard("INT. ROOM - DAY\nA: Hello there.\n")
    assert mz.planned_shot_durations(manifest)[0] > 0
    with pytest.raises(mz.MaterializationError, match="unsupported sample rate"):
        mz.planned_shot_durations(manifest, sample_rate_hz=22_050)
    with pytest.raises(mz.MaterializationError, match="unsupported sample rate"):
        mz.planned_shot_durations(manifest, sample_rate_hz=True)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# materializer.py - atomic IO and deterministic generators
# --------------------------------------------------------------------------- #


def test_atomic_write_cleans_up_the_temporary_file_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"

    def _boom(_handle: Any) -> None:
        raise RuntimeError("writer exploded")

    with pytest.raises(RuntimeError, match="writer exploded"):
        mz.atomic_write(target, _boom)
    assert not target.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".out.bin")] == []


def test_fsync_directory_survives_open_and_fsync_failures(tmp_path: Path, monkeypatch: Any) -> None:
    real_open = os.open

    def _open(directory: Any, *args: Any, **kwargs: Any) -> int:
        if str(directory) == str(tmp_path):
            raise OSError("no descriptor for you")
        return real_open(directory, *args, **kwargs)

    monkeypatch.setattr(mz.os, "open", _open)
    mz._fsync_directory(tmp_path)  # refused open: silently tolerated

    monkeypatch.setattr(mz.os, "open", real_open)

    def _fsync(_fd: int) -> None:
        raise OSError("fsync refused")

    monkeypatch.setattr(mz.os, "fsync", _fsync)
    mz._fsync_directory(tmp_path)  # refused fsync: silently tolerated


def test_write_slate_png_requires_a_32_byte_seed(tmp_path: Path) -> None:
    with pytest.raises(mz.MaterializationError, match="invalid_seed"):
        mz.write_slate_png(tmp_path / "x.png", width=32, height=32, seed=b"short", label="x")
    with pytest.raises(mz.MaterializationError, match="invalid_seed"):
        mz.write_slate_png(tmp_path / "x.png", width=32, height=32, seed="t" * 32, label="x")  # type: ignore[arg-type]


def test_write_slate_png_is_deterministic_and_streams_rows(tmp_path: Path) -> None:
    seed = hashlib.sha256(b"slate").digest()
    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    mz.write_slate_png(first, width=128, height=160, seed=seed, label="shot|kind")
    mz.write_slate_png(second, width=128, height=160, seed=seed, label="shot|kind")
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.parametrize("bad", [math.nan, math.inf, 0.0, 601.0])
def test_write_tone_wav_refuses_invalid_duration(tmp_path: Path, bad: object) -> None:
    with pytest.raises(mz.MaterializationError, match="invalid_duration"):
        mz.write_tone_wav(
            tmp_path / "t.wav",
            duration_s=bad,  # type: ignore[arg-type]
            sample_rate=16_000,
            seed=hashlib.sha256(b"t").digest(),
        )


@pytest.mark.parametrize(
    "value,reason",
    [
        ("1.0", "must be a number"),
        (True, "must be a number"),
        (math.nan, "out of range"),
        (0.0, "out of range"),
    ],
)
def test_finite_shot_duration_refuses_invalid_values(value: object, reason: str) -> None:
    with pytest.raises(mz.MaterializationError, match=reason):
        mz._finite_shot_duration(value)


# --------------------------------------------------------------------------- #
# materializer.py - materialize() and provenance
# --------------------------------------------------------------------------- #


@pytest.fixture()
def small_bundle(
    tmp_path: Path,
) -> tuple[mz.StoryboardMediaMaterializer, mz.MaterializedBundle, Any]:
    """A real materialized bundle for a one-shot screenplay."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    manifest = compile_storyboard("INT. ROOM - DAY\nA: Hello there.\n")
    materializer = mz.StoryboardMediaMaterializer(workspace, mz.RenderProfile.from_mapping(PROFILE))
    bundle = materializer.materialize(manifest, Path("render"))
    return materializer, bundle, manifest


def test_materialize_seals_one_inclusion_proof_per_shot(
    small_bundle: tuple[mz.StoryboardMediaMaterializer, mz.MaterializedBundle, Any],
) -> None:
    materializer, bundle, _manifest = small_bundle
    assert len(bundle.shot_hashes) == 1
    assert len(bundle.shot_inclusion_proofs) == 1
    assert bundle.shot_inclusion_proofs[0]["leaf_hash"] == bundle.shot_hashes[0]
    assert (
        mz.verify_shot_inclusion_proofs(
            shot_hashes=bundle.shot_hashes,
            merkle_root=bundle.shot_merkle_root,
            proofs=bundle.shot_inclusion_proofs,
        )
        == ()
    )
    materializer.verify_materialized_provenance(bundle)


def test_materialize_accepts_a_local_image_and_binds_it_into_the_shot_leaf(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    plate = workspace / "plates" / "plate.png"
    plate.parent.mkdir()
    mz.write_slate_png(plate, width=320, height=180, seed=b"\x05" * 32, label="plate")
    manifest = compile_storyboard("INT. ROOM - DAY\nA: Hello there.\n")
    materializer = mz.StoryboardMediaMaterializer(workspace, mz.RenderProfile.from_mapping(PROFILE))
    shot_id = "s000-sh000"
    bundle = materializer.materialize(
        manifest, Path("render"), local_images={shot_id: "plates/plate.png"}
    )
    assert bundle.assets[0].kind == "local_image"
    assert bundle.assets[0].sha256 == hashlib.sha256(plate.read_bytes()).hexdigest()
    generated = materializer.materialize(manifest, Path("render2"))
    assert generated.shot_hashes != bundle.shot_hashes
    materializer.verify_materialized_provenance(bundle)


def _patch_asset(
    bundle: mz.MaterializedBundle, index: int, **changes: Any
) -> mz.MaterializedBundle:
    assets = list(bundle.assets)
    assets[index] = dataclasses.replace(assets[index], **changes)
    return dataclasses.replace(bundle, assets=tuple(assets))


def test_provenance_refuses_assets_outside_the_workspace(
    small_bundle: tuple[mz.StoryboardMediaMaterializer, mz.MaterializedBundle, Any], tmp_path: Path
) -> None:
    materializer, bundle, _ = small_bundle
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")
    with pytest.raises(mz.MaterializationError, match="outside workspace"):
        materializer.verify_materialized_provenance(
            _patch_asset(bundle, 0, resolved_path=str(outside))
        )


def test_provenance_refuses_deleted_replaced_and_resized_assets(
    small_bundle: tuple[mz.StoryboardMediaMaterializer, mz.MaterializedBundle, Any], tmp_path: Path
) -> None:
    materializer, bundle, _ = small_bundle
    missing = Path(bundle.out_dir) / "gone.png"
    with pytest.raises(mz.MaterializationError, match="missing"):
        materializer.verify_materialized_provenance(
            _patch_asset(bundle, 0, resolved_path=str(missing))
        )
    directory = Path(bundle.out_dir) / "dir.png"
    directory.mkdir()
    with pytest.raises(mz.MaterializationError, match="not a regular file"):
        materializer.verify_materialized_provenance(
            _patch_asset(bundle, 0, resolved_path=str(directory))
        )
    with pytest.raises(mz.MaterializationError, match="inode replaced"):
        materializer.verify_materialized_provenance(_patch_asset(bundle, 0, inode=1))
    with pytest.raises(mz.MaterializationError, match="size changed"):
        materializer.verify_materialized_provenance(_patch_asset(bundle, 0, size_bytes=1))


def test_provenance_refuses_a_broken_asset_root_and_shot_seal(
    small_bundle: tuple[mz.StoryboardMediaMaterializer, mz.MaterializedBundle, Any], tmp_path: Path
) -> None:
    materializer, bundle, _ = small_bundle
    with pytest.raises(mz.MaterializationError, match="asset merkle root mismatch"):
        materializer.verify_materialized_provenance(
            dataclasses.replace(bundle, asset_merkle_root="f" * 64)
        )
    with pytest.raises(mz.MaterializationError, match="shot proof without shots"):
        materializer.verify_materialized_provenance(dataclasses.replace(bundle, shot_hashes=()))
    with pytest.raises(mz.MaterializationError, match="shot content hash mismatch"):
        materializer.verify_materialized_provenance(
            dataclasses.replace(bundle, shot_hashes=("a" * 64,))
        )
    # A bundle with no shots and no proof material is simply provenance-empty.
    materializer.verify_materialized_provenance(
        dataclasses.replace(bundle, shot_hashes=(), shot_inclusion_proofs=(), shot_merkle_root="")
    )
    tampered = [dict(proof) for proof in bundle.shot_inclusion_proofs]
    tampered[0]["leaf_hash"] = "f" * 64
    with pytest.raises(mz.MaterializationError, match="shot inclusion proof"):
        materializer.verify_materialized_provenance(
            dataclasses.replace(bundle, shot_inclusion_proofs=tuple(tampered))
        )


# --------------------------------------------------------------------------- #
# artifact_passport.py
# --------------------------------------------------------------------------- #


def test_signing_key_accepts_bytes_and_refuses_other_types() -> None:
    assert ap._signing_key(bytearray(b"z" * 40)) == b"z" * 40
    with pytest.raises(ap.PassportError, match="invalid_key"):
        ap._signing_key(42)
    with pytest.raises(ap.PassportError, match="invalid_key"):
        ap._signing_key("short")


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"verifier_status": "VIOLATED"}, "verifier_not_pass"),
        ({"source_sha256": "zz"}, "invalid_digest"),
        ({"probed_duration_s": "1.0"}, "invalid_number"),
        ({"probed_duration_s": math.nan}, "must be finite"),
        ({"probed_duration_s": 0.0}, "must be positive"),
        ({"output_size_bytes": 0}, "output_size_bytes must be positive"),
    ],
)
def test_build_passport_refuses_invalid_fields(
    copied_mp4: Path, changes: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ap.PassportError, match=reason):
        _passport(copied_mp4, **changes)


def test_build_passport_seals_the_per_shot_tree(copied_mp4: Path) -> None:
    hashes = _shot_hashes(3)
    passport = _passport(copied_mp4, shot_hashes=list(hashes))
    assert passport["shot_hashes"] == list(hashes)
    assert len(passport["shot_inclusion_proofs"]) == 3
    assert ap.passport_shot_proof_violations(passport) == ()
    assert set(ap.REQUIRED_FIELDS) <= set(passport)


def test_build_passport_refuses_a_bad_shot_hash_sequence(copied_mp4: Path) -> None:
    with pytest.raises(ap.PassportError, match="invalid_digest"):
        _passport(copied_mp4, shot_hashes="a" * 64)
    with pytest.raises(ap.PassportError, match="invalid_digest"):
        _passport(copied_mp4, shot_hashes=["not-a-digest"])


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"shot_merkle_root": 7}, ("shot_merkle_root_mismatch",)),
        ({"shot_hashes": {"a": 1}}, ("shot_inclusion_proof_invalid",)),
        ({"shot_hashes": [1, 2]}, ("shot_inclusion_proof_invalid",)),
        ({"shot_inclusion_proofs": "nope"}, ("shot_inclusion_proof_invalid",)),
    ],
)
def test_passport_proof_violations_report_structural_damage(
    copied_mp4: Path, changes: dict[str, Any], expected: tuple[str, ...]
) -> None:
    passport = _passport(copied_mp4, shot_hashes=list(_shot_hashes(2)))
    passport.update(changes)
    assert ap.passport_shot_proof_violations(passport) == expected


def test_verify_passport_refuses_every_merkle_violation(copied_mp4: Path, tmp_path: Path) -> None:
    good = _passport(copied_mp4, shot_hashes=list(_shot_hashes(3)))
    path = tmp_path / "storyboard.passport.json"

    broken_root = dict(good, shot_merkle_root="f" * 64)
    with pytest.raises(ap.PassportError, match="shot_merkle_root_mismatch"):
        ap.verify_artifact_passport(_write(path, _sealed(broken_root)), copied_mp4, KEY)

    short = dict(good, shot_inclusion_proofs=good["shot_inclusion_proofs"][:2])
    with pytest.raises(ap.PassportError, match="shot_inclusion_proof_count_mismatch"):
        ap.verify_artifact_passport(_write(path, _sealed(short)), copied_mp4, KEY)

    proofs = [dict(proof) for proof in good["shot_inclusion_proofs"]]
    proofs[1]["sibling_hashes"] = ["0" * 64 for _ in proofs[1]["sibling_hashes"]]
    sibling_flip = dict(good, shot_inclusion_proofs=proofs)
    with pytest.raises(ap.PassportError, match="shot_inclusion_proof_invalid"):
        ap.verify_artifact_passport(_write(path, _sealed(sibling_flip)), copied_mp4, KEY)

    leaf_flip = dict(good, shot_hashes=[good["shot_hashes"][0], "e" * 64, good["shot_hashes"][2]])
    with pytest.raises(ap.PassportError):
        ap.verify_artifact_passport(_write(path, _sealed(leaf_flip)), copied_mp4, KEY)


def test_verify_passport_refuses_oversized_malformed_and_unreadable_files(
    tmp_path: Path, copied_mp4: Path
) -> None:
    oversized = tmp_path / "huge.passport.json"
    oversized.write_bytes(b"{" + b" " * (ap.MAX_PASSPORT_BYTES + 8) + b"}")
    with pytest.raises(ap.PassportError, match="passport_size"):
        ap.verify_artifact_passport(oversized, copied_mp4, KEY)
    assert _rejects(tmp_path / "not-object.json", b"[]", "passport_malformed", copied_mp4)
    assert _rejects(tmp_path / "bad-utf8.json", b"\xff", "passport_unreadable", copied_mp4)
    assert _rejects(tmp_path / "truncated.json", b"{", "passport_unreadable", copied_mp4)
    assert _rejects(tmp_path / "empty.json", b"", "passport_size", copied_mp4)


def _rejects(path: Path, raw: bytes, reason: str, mp4: Path) -> bool:
    path.write_bytes(raw)
    with pytest.raises(ap.PassportError, match=reason):
        ap.verify_artifact_passport(path, mp4, KEY)
    return True


def test_verify_passport_refuses_missing_fields_version_and_verdict(
    tmp_path: Path, copied_mp4: Path
) -> None:
    good = _passport(copied_mp4)
    path = tmp_path / "storyboard.passport.json"
    missing = dict(good)
    del missing["shot_hashes"]
    with pytest.raises(ap.PassportError, match="passport_malformed"):
        ap.verify_artifact_passport(_write(path, _sealed(missing)), copied_mp4, KEY)
    with pytest.raises(ap.PassportError, match="passport_version"):
        ap.verify_artifact_passport(
            _write(path, _sealed(dict(good, passport_version="2"))), copied_mp4, KEY
        )
    with pytest.raises(ap.PassportError, match="verifier_not_pass"):
        ap.verify_artifact_passport(
            _write(path, _sealed(dict(good, verifier_status="VIOLATED"))), copied_mp4, KEY
        )
    with pytest.raises(ap.PassportError, match="artifact_mismatch"):
        ap.verify_artifact_passport(
            _write(path, _sealed(dict(good, output_size_bytes=1))), copied_mp4, KEY
        )


def test_verify_passport_refuses_a_symlinked_or_non_regular_passport(
    tmp_path: Path, copied_mp4: Path
) -> None:
    real = tmp_path / "real.passport.json"
    real.write_text(json.dumps(_passport(copied_mp4)), encoding="utf-8")
    link = tmp_path / "link.passport.json"
    link.symlink_to(real)
    with pytest.raises(ap.PassportError, match="passport_symlink_refused"):
        ap.verify_artifact_passport(link, copied_mp4, KEY)
    directory = tmp_path / "dir.passport.json"
    directory.mkdir()
    with pytest.raises(ap.PassportError, match="passport_not_regular"):
        ap.verify_artifact_passport(directory, copied_mp4, KEY)
    with pytest.raises(ap.PassportError, match="passport_missing"):
        ap.verify_artifact_passport(tmp_path / "nope.json", copied_mp4, KEY)


def test_verify_passport_refuses_a_symlinked_or_missing_artifact(
    tmp_path: Path, copied_mp4: Path
) -> None:
    passport_path = tmp_path / "storyboard.passport.json"
    ap.write_passport(passport_path, _passport(copied_mp4))
    link = tmp_path / "link.mp4"
    link.symlink_to(copied_mp4)
    with pytest.raises(ap.PassportError, match="artifact_symlink_refused"):
        ap.verify_artifact_passport(passport_path, link, KEY)
    directory = tmp_path / "dir.mp4"
    directory.mkdir()
    with pytest.raises(ap.PassportError, match="artifact_not_regular"):
        ap.verify_artifact_passport(passport_path, directory, KEY)
    with pytest.raises(ap.PassportError, match="artifact_missing"):
        ap.verify_artifact_passport(passport_path, tmp_path / "gone.mp4", KEY)


# --------------------------------------------------------------------------- #
# artifact_verifier.py
# --------------------------------------------------------------------------- #


def test_box_walker_rejects_a_truncated_header(tmp_path: Path) -> None:
    path = tmp_path / "short.mp4"
    path.write_bytes(b"\x00\x00\x00\x08fty")
    _types, problems = av.read_top_level_boxes(path)
    assert any("truncated box header" in problem for problem in problems)


def test_box_walker_rejects_a_truncated_largesize(tmp_path: Path) -> None:
    path = tmp_path / "large.mp4"
    path.write_bytes(struct.pack(">I4s", 1, b"ftyp") + b"\x00\x00")
    _types, problems = av.read_top_level_boxes(path)
    assert any("truncated largesize" in problem for problem in problems)


def test_box_walker_accepts_largesize_and_zero_size_boxes(tmp_path: Path) -> None:
    path = tmp_path / "boxes.mp4"
    path.write_bytes(struct.pack(">I4sQ", 1, b"ftyp", 16) + struct.pack(">I4s", 0, b"moov"))
    types, problems = av.read_top_level_boxes(path)
    assert types == ("ftyp", "moov")
    assert any("mdat" in problem for problem in problems)


def test_box_walker_reports_untiled_files_and_missing_ftyp(tmp_path: Path) -> None:
    path = tmp_path / "moov-first.mp4"
    path.write_bytes(struct.pack(">I4s", 8, b"moov"))
    _types, problems = av.read_top_level_boxes(path)
    assert "first box is not ftyp" in problems
    tiling = tmp_path / "tiling.mp4"
    tiling.write_bytes(struct.pack(">I4s", 16, b"ftyp") + b"\x00" * 4)
    _types, problems = av.read_top_level_boxes(tiling)
    assert any("overruns" in problem for problem in problems)


def test_probe_is_unobserved_when_ffprobe_is_missing(real_mp4: Path) -> None:
    verdict = av.verify_mp4(
        real_mp4,
        expected_duration_s=1.0,
        tolerance_s=0.2,
        ffprobe="/nonexistent/ffprobe-binary",
    )
    assert verdict.status == av.UNOBSERVED
    assert verdict.reasons == ("ffprobe_unavailable_or_unparseable",)


def test_probe_is_unobserved_when_ffprobe_fails_or_floods(real_mp4: Path, monkeypatch: Any) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        av.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"{}")
    )
    assert av._probe(real_mp4, "ffprobe", 1.0) is None
    monkeypatch.setattr(
        av.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"{" + b" " * 2_000_000 + b"}"),
    )
    assert av._probe(real_mp4, "ffprobe", 1.0) is None


def test_probe_is_unobserved_for_undecodable_output(real_mp4: Path, monkeypatch: Any) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        av.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"\xff\xfe")
    )
    assert av._probe(real_mp4, "ffprobe", 1.0) is None


def test_verify_mp4_refuses_bad_expectations(tmp_path: Path, real_mp4: Path) -> None:
    verdict = av.verify_mp4(real_mp4, expected_duration_s=math.inf, tolerance_s=0.2)
    assert verdict.reasons == ("expected_duration_not_positive_finite",)
    verdict = av.verify_mp4(real_mp4, expected_duration_s=1.0, tolerance_s=math.nan)
    assert verdict.reasons == ("tolerance_not_positive_finite",)


def test_verify_mp4_refuses_symlinks_directories_and_empty_files(
    tmp_path: Path, real_mp4: Path
) -> None:
    link = tmp_path / "link.mp4"
    link.symlink_to(real_mp4)
    assert av.verify_mp4(link, expected_duration_s=1.0, tolerance_s=0.2).status == av.VIOLATED
    assert av.verify_mp4(tmp_path, expected_duration_s=1.0, tolerance_s=0.2).reasons == (
        "not_regular_file",
    )
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert av.verify_mp4(empty, expected_duration_s=1.0, tolerance_s=0.2).reasons == ("zero_bytes",)


def test_verify_mp4_reports_box_layout_problems(tmp_path: Path) -> None:
    bad = tmp_path / "bad-layout.mp4"
    bad.write_bytes((1000).to_bytes(4, "big") + b"ftyp" + b"\x00" * 8)
    verdict = av.verify_mp4(bad, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED
    assert verdict.reasons[0].startswith("box_layout:")


@pytest.mark.parametrize(
    "probe,expected",
    [
        # A probe that enumerates no stream at all is already refused as
        # ``no_video_stream``; the audio gate speaks only about listed streams.
        (
            {"streams": "not-a-list", "format": {"duration": "1.0"}},
            {"no_video_stream"},
        ),
        (
            {"streams": [], "format": {"duration": "1.0"}},
            {"no_video_stream"},
        ),
        (
            {"streams": [{"codec_type": "video"}], "format": {"duration": "bad"}},
            {"missing_audio_stream", "probed_duration_not_positive_finite"},
        ),
        (
            {"streams": [{"codec_type": "video"}], "format": {"duration": None}},
            {"missing_audio_stream", "probed_duration_not_positive_finite"},
        ),
        (
            {"streams": [{"codec_type": "video"}], "format": {"duration": "-2"}},
            {"missing_audio_stream", "probed_duration_not_positive_finite"},
        ),
        (
            {"streams": [{"codec_type": "video"}], "format": {"duration": "99.0"}},
            {
                "missing_audio_stream",
                "duration_mismatch: probed=99.000000s expected=1.000000s",
            },
        ),
        (
            {"streams": [{"codec_type": "video"}], "format": "not-a-dict"},
            {"missing_audio_stream", "probed_duration_not_positive_finite"},
        ),
        # A-08: an AAC audio stream the demuxer reports satisfies the gate ...
        (
            {
                "streams": [
                    {"codec_type": "video"},
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"duration": "99.0"},
            },
            {"duration_mismatch: probed=99.000000s expected=1.000000s"},
        ),
        # ... a stream that omits its codec name is believed to be AAC ...
        (
            {
                "streams": [
                    {"codec_type": "video"},
                    {"codec_type": "audio"},
                ],
                "format": {"duration": "99.0"},
            },
            {"duration_mismatch: probed=99.000000s expected=1.000000s"},
        ),
        # ... but a non-AAC audio stream is refused as if it were silent ...
        (
            {
                "streams": [
                    {"codec_type": "video"},
                    {"codec_type": "audio", "codec_name": "opus"},
                ],
                "format": {"duration": "1.0"},
            },
            {"missing_audio_stream"},
        ),
        # ... and non-dict stream entries cannot vouch for anything.
        (
            {"streams": ["video", 7], "format": {"duration": "1.0"}},
            {"no_video_stream"},
        ),
    ],
)
def test_verify_mp4_reads_every_probe_shape(
    real_mp4: Path, monkeypatch: Any, probe: dict[str, Any], expected: set[str]
) -> None:
    monkeypatch.setattr(av, "_probe", lambda *a, **k: probe)
    verdict = av.verify_mp4(real_mp4, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED
    assert set(verdict.reasons) == expected


def test_verify_mp4_passes_a_real_file(real_mp4: Path) -> None:
    verdict = av.verify_mp4(real_mp4, expected_duration_s=1.0, tolerance_s=0.3)
    assert verdict.status == av.PASS
    assert verdict.passed is True
    assert verdict.has_video is True
    assert verdict.output_sha256 == hashlib.sha256(real_mp4.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# artifact_download.py
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "rel_path",
    [
        "",
        "/abs/path.mp4",
        "a\\b.mp4",
        "nul\x00byte.mp4",
        "x" * 300 + ".mp4",
        "notes.txt",
        42,
        None,
    ],
)
def test_download_gate_refuses_malformed_paths(tmp_path: Path, rel_path: object) -> None:
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(tmp_path, rel_path, KEY)  # type: ignore[arg-type]
    assert info.value.status_code == 422
    assert info.value.reason == "malformed_path"


def test_uniterated_download_stream_and_discarded_authorization_never_leak_fd(
    real_mp4: Path,
) -> None:
    """CP4: a stream never iterated, and an authorization never streamed.

    A generator body never runs before its first ``next()``, so the old
    ``yield``-based ``iter_authorized_chunks`` leaked the verified descriptor
    whenever the response object was discarded uniterated. The new
    plain-function/iterator design must close the descriptor in EVERY case:
    dropped stream, dropped authorization, EOF, and explicit close — proven
    here with ``os.fstat(fd)`` returning ``EBADF`` after ``gc.collect()``.
    """
    fd = os.open(real_mp4, os.O_RDONLY)
    stream = ad.iter_authorized_chunks(fd)  # type: ignore[arg-type]
    del stream
    gc.collect()
    with pytest.raises(OSError):
        os.fstat(fd)  # closed by weakref.finalize even though never iterated

    # (b) an authorization whose stream is never built at all
    ad2 = ad.AuthorizedDownload(path=real_mp4, size_bytes=1, sha256="0" * 64, _lease=None)
    ad2.close()  # hand-built object: close() is still safe and idempotent
    ad2.close()

    # Belt and braces: the lease path itself, with double close
    lease_fd = os.open(real_mp4, os.O_RDONLY)
    lease = ad._FdLease(lease_fd)
    assert lease.fd == lease_fd
    lease.close()
    assert lease.fd is None
    lease.close()  # second close must NOT close an unrelated descriptor
    with pytest.raises(OSError):
        os.fstat(lease_fd)


def test_download_gate_refuses_traversal_and_symlinks(tmp_path: Path, real_mp4: Path) -> None:
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(tmp_path, "..%2fetc.mp4", KEY)
    assert info.value.status_code in {403, 422}
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    link = root / "sub" / "link.mp4"
    link.symlink_to(real_mp4)
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(root, "sub/link.mp4", KEY)
    assert info.value.status_code == 403
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(root, "real/../../escape.mp4", KEY)
    assert info.value.status_code in {403, 422}


def test_download_gate_refuses_missing_and_non_regular_artifacts(
    tmp_path: Path, real_mp4: Path
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(root, "missing.mp4", KEY)
    assert (info.value.status_code, info.value.reason) == (404, "artifact_not_found")
    (root / "directory.mp4").mkdir()
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(root, "directory.mp4", KEY)
    assert info.value.status_code == 404


def test_download_gate_refuses_missing_or_broken_passports(tmp_path: Path, real_mp4: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    mp4 = root / "storyboard.mp4"
    mp4.write_bytes(real_mp4.read_bytes())
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(root, "storyboard.mp4", KEY)
    assert (info.value.status_code, info.value.reason) == (409, "passport_missing")

    passport_path = root / f"storyboard{ap.PASSPORT_SUFFIX}"
    ap.write_passport(passport_path, _passport(mp4, output_rel_path="other/storyboard.mp4"))
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(root, "storyboard.mp4", KEY)
    assert (info.value.status_code, info.value.reason) == (409, "passport_for_other_artifact")

    ap.write_passport(passport_path, _passport(mp4, output_rel_path="storyboard.mp4"))
    authorized = ad.authorize_artifact_download(root, "storyboard.mp4", KEY)
    assert authorized.path == mp4.resolve()
    assert authorized.size_bytes == mp4.stat().st_size

    raw = bytearray(mp4.read_bytes())
    raw[-16] ^= 0xFF
    mp4.write_bytes(bytes(raw))
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(root, "storyboard.mp4", KEY)
    assert info.value.status_code == 409
    assert info.value.reason.startswith("passport_artifact_mismatch")


def test_download_gate_refuses_a_passport_sealed_with_another_key(
    tmp_path: Path, real_mp4: Path
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    mp4 = root / "storyboard.mp4"
    mp4.write_bytes(real_mp4.read_bytes())
    ap.write_passport(
        root / f"storyboard{ap.PASSPORT_SUFFIX}",
        _passport(mp4, output_rel_path="storyboard.mp4", signing_key="z" * 40),
    )
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(root, "storyboard.mp4", KEY)
    assert (info.value.status_code, info.value.reason) == (409, "passport_seal_mismatch")


# --------------------------------------------------------------------------- #
# studio_cli.py - init / doctor branches
# --------------------------------------------------------------------------- #


def test_studio_init_refuses_a_symlinked_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(studio_cli.StudioCliError, match="workspace root is a symlink"):
        studio_cli.studio_init(link)


def test_studio_init_refuses_a_symlinked_or_non_directory_component(tmp_path: Path) -> None:
    root = tmp_path / "studio"
    root.mkdir()
    (root / "projects").write_text("not a directory", encoding="utf-8")
    with pytest.raises(studio_cli.StudioCliError, match="not a directory"):
        studio_cli.studio_init(root)

    other = tmp_path / "studio2"
    other.mkdir()
    (other / "assets").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(studio_cli.StudioCliError, match="is a symlink"):
        studio_cli.studio_init(other)


def test_studio_doctor_reports_missing_binaries_and_layout(tmp_path: Path) -> None:
    report = studio_cli.studio_doctor(
        tmp_path / "empty", ffmpeg="/nonexistent/ffmpeg", ffprobe="/nonexistent/ffprobe"
    )
    assert report.healthy is False
    names = {check.name: check.ok for check in report.checks}
    assert names["ffmpeg_runs"] is False
    assert names["ffprobe_runs"] is False
    assert names["workspace_layout"] is False
    assert names["atomic_write"] is False
    assert names["queue_sqlite"] is False
    assert report.exit_code == 1


def test_studio_doctor_reports_sqlite_and_compiler_failures(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root = tmp_path / "studio"
    studio_cli.studio_init(root)

    def _broken_connect(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("db refused")

    monkeypatch.setattr(studio_cli.sqlite3, "connect", _broken_connect)
    real_compile = studio_cli._compiler_ok
    monkeypatch.setattr(
        studio_cli,
        "_compiler_ok",
        lambda: (False, "CompilerBoom"),
    )
    report = studio_cli.studio_doctor(root)
    checks = {check.name: check.ok for check in report.checks}
    assert checks["queue_sqlite"] is False
    assert checks["storyboard_compiler"] is False
    monkeypatch.setattr(studio_cli, "_compiler_ok", real_compile)


def test_binary_probe_reports_oserror(monkeypatch: Any) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("cannot fork")

    monkeypatch.setattr(studio_cli.subprocess, "run", _boom)
    assert studio_cli._binary_ok("ffmpeg") == (False, "OSError")


# --------------------------------------------------------------------------- #
# studio_cli.py - render argument handling
# --------------------------------------------------------------------------- #


def _cli(argv: list[str]) -> tuple[int, dict[str, Any]]:
    buffer = io.StringIO()
    code = studio_cli.run_studio_cli(argv, out=buffer)
    text = buffer.getvalue().strip()
    payload = json.loads(text) if text else {}
    assert text.count("\n") == 0
    return code, payload


def test_cli_build_parser_exposes_the_type4_flags() -> None:
    parser = studio_cli.build_parser()
    namespace = parser.parse_args(
        [
            "render",
            "--script",
            "INT. ROOM - DAY\nA: Hi.\n",
            "--output-dir",
            "out",
            "--cas-dir",
            "cas",
            "--vault-dir",
            "vault",
            "--image",
            "s000-sh000=plate.png",
        ]
    )
    assert namespace.cas_dir == Path("cas")
    assert namespace.vault_dir == Path("vault")
    assert namespace.image == ["s000-sh000=plate.png"]
    assert namespace.command == "render"


def test_cli_reads_secrets_from_file_flag_and_environment(tmp_path: Path) -> None:
    secret_file = tmp_path / "key.txt"
    secret_file.write_text("  file-secret  \n", encoding="utf-8")
    assert studio_cli._read_secret(inline=None, path=secret_file, env_name="X") == "file-secret"
    assert studio_cli._read_secret(inline="inline", path=None, env_name="X") == "inline"
    assert studio_cli._read_secret(inline=None, path=None, env_name="MISSING_ENV_VAR_XYZ") is None
    with pytest.raises(studio_cli.StudioCliError, match="secret file missing"):
        studio_cli._read_secret(
            inline=None, path=tmp_path / "nope.txt", env_name="MISSING_ENV_VAR_XYZ"
        )


def test_cli_reads_secret_from_environment(monkeypatch: Any) -> None:
    monkeypatch.setenv("NEXUS_STUDIO_TEST_KEY", "  env-secret ")
    assert (
        studio_cli._read_secret(inline=None, path=None, env_name="NEXUS_STUDIO_TEST_KEY")
        == "env-secret"
    )


def test_cli_refuses_missing_or_empty_script_inputs(tmp_path: Path) -> None:
    code, payload = _cli(["render", "--output-dir", str(tmp_path / "out")])
    assert (code, payload["status"]) == (2, "refused")
    assert "one of --script or --script-file" in payload["reason"]

    code, payload = _cli(["render", "--script", "   ", "--output-dir", str(tmp_path / "out")])
    assert "script text is empty" in payload["reason"]

    code, payload = _cli(
        ["render", "--script-file", str(tmp_path / "missing.txt"), "--output-dir", "out"]
    )
    assert "script file missing" in payload["reason"]

    empty = tmp_path / "empty.txt"
    empty.write_text("   \n", encoding="utf-8")
    code, payload = _cli(["render", "--script-file", str(empty), "--output-dir", "out"])
    assert "script file is empty" in payload["reason"]

    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (studio_cli.MAX_SCRIPT_BYTES + 1))
    code, payload = _cli(["render", "--script-file", str(big), "--output-dir", "out"])
    assert "size out of range" in payload["reason"]

    directory = tmp_path / "dir.txt"
    directory.mkdir()
    code, payload = _cli(["render", "--script-file", str(directory), "--output-dir", "out"])
    assert "must be a regular, non-symlink file" in payload["reason"]

    target = tmp_path / "target.txt"
    target.write_text("INT. ROOM - DAY\nA: Hi.\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    code, payload = _cli(["render", "--script-file", str(link), "--output-dir", "out"])
    assert "must be a regular, non-symlink file" in payload["reason"]


def test_cli_parses_image_specs_and_refuses_bad_ones(tmp_path: Path) -> None:
    plate = tmp_path / "plate.png"
    plate.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert studio_cli._parse_image_specs([f"s000-sh000={plate}"]) == {"s000-sh000": plate}
    with pytest.raises(studio_cli.StudioCliError, match="SHOT_ID=PATH"):
        studio_cli._parse_image_specs(["no-equals-sign"])
    with pytest.raises(studio_cli.StudioCliError, match="SHOT_ID=PATH"):
        studio_cli._parse_image_specs(["=missing-shot.png"])
    with pytest.raises(studio_cli.StudioCliError, match="not a regular file"):
        studio_cli._parse_image_specs([f"s000-sh000={tmp_path / 'nope.png'}"])
    with pytest.raises(studio_cli.StudioCliError, match="not a regular file"):
        studio_cli._parse_image_specs([f"s000-sh000={tmp_path}"])


def test_cli_refuses_a_render_without_signing_key(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv(studio_cli.SIGNING_KEY_ENV, raising=False)
    code, payload = _cli(
        [
            "render",
            "--script",
            "INT. ROOM - DAY\nA: Hi.\n",
            "--output-dir",
            str(tmp_path / "out"),
            "--cas-dir",
            str(tmp_path / "cas"),
        ]
    )
    assert (code, payload["status"]) == (2, "refused")
    assert "signing key is required" in payload["reason"]


def test_cli_refuses_a_vault_backup_without_secret(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv(studio_cli.VAULT_SECRET_ENV, raising=False)
    code, payload = _cli(
        [
            "render",
            "--script",
            "INT. ROOM - DAY\nA: Hi.\n",
            "--output-dir",
            str(tmp_path / "out"),
            "--cas-dir",
            str(tmp_path / "cas"),
            "--vault-dir",
            str(tmp_path / "vault"),
            "--signing-key",
            KEY,
        ]
    )
    assert (code, payload["status"]) == (2, "refused")
    assert "vault secret" in payload["reason"]


def test_cli_refuses_approval_flags_without_a_ledger(tmp_path: Path) -> None:
    base = [
        "render",
        "--script",
        "INT. ROOM - DAY\nA: Hi.\n",
        "--output-dir",
        str(tmp_path / "out"),
        "--cas-dir",
        str(tmp_path / "cas"),
        "--signing-key",
        KEY,
        "--approval-id",
        "approval-1",
        "--principal-id",
        "owner-1",
    ]
    code, payload = _cli(base)
    assert (code, payload["status"]) == (2, "refused")
    assert "approval ledger is required" in payload["reason"]


def test_cli_consumes_a_real_approval_and_renders(tmp_path: Path) -> None:
    """The approval is consumed exactly once, against the sealed manifest digests."""
    from nexus_ai_agent.creative.storyboard_render import issue_render_approval
    from nexus_ai_agent.nagar.ibpc.approval import ApprovalLedger

    script = "INT. ROOM - DAY\nA: Hello there, friend.\n"
    compiled = compile_storyboard(script)
    profile_hash = mz.RenderProfile.from_mapping(PROFILE).profile_hash()
    ledger = ApprovalLedger(now=lambda: "2026-11-18T00:00:00Z")
    approval_id = issue_render_approval(
        ledger,
        project_id="proj-cli",
        job_id="job-cli",
        principal_id="owner-1",
        source_sha256=hashlib.sha256(script.encode("utf-8")).hexdigest(),
        script_sha256=compiled.script_sha256,
        manifest_sha256=compiled.manifest_sha256,
        render_profile_hash=profile_hash,
    )
    argv = [
        "render",
        "--script",
        script,
        "--title",
        "approval path",
        "--job-id",
        "job-cli",
        "--project-id",
        "proj-cli",
        "--output-dir",
        str(tmp_path / "out"),
        "--cas-dir",
        str(tmp_path / "cas"),
        "--fps",
        str(PROFILE["fps"]),
        "--width",
        str(PROFILE["width"]),
        "--height",
        str(PROFILE["height"]),
        "--signing-key",
        KEY,
        "--approval-id",
        approval_id,
        "--principal-id",
        "owner-1",
    ]
    buffer = io.StringIO()
    code = studio_cli.run_studio_cli(argv, ledger=ledger, out=buffer)
    payload = json.loads(buffer.getvalue())
    if code != 0:  # pragma: no cover - ffmpeg-less environments
        pytest.skip(f"render requires real ffmpeg: {payload}")
    assert payload["status"] == "rendered"
    receipt = json.loads((Path(payload["output_path"]).parent / "job-cli.receipt.json").read_text())
    assert receipt["approval_id"] == approval_id
    assert receipt["approved_by"] == "owner-1"

    # Replaying the same approval is refused (single-use ledger).
    replay = io.StringIO()
    assert studio_cli.run_studio_cli(argv, ledger=ledger, out=replay) == 2
    assert "approval_" in json.loads(replay.getvalue())["reason"]


def test_cli_reports_bridge_refusals_as_json(tmp_path: Path) -> None:
    code, payload = _cli(
        [
            "render",
            "--script",
            "INT. ROOM - DAY\nA: Hi.\n",
            "--output-dir",
            str(tmp_path / "out"),
            "--cas-dir",
            str(tmp_path / "cas"),
            "--signing-key",
            KEY,
            "--width",
            "321",
        ]
    )
    assert (code, payload["status"]) == (1, "refused")
    assert payload["reason"] == "invalid_profile"


def test_cli_reports_a_failed_vault_backup_as_json(tmp_path: Path, monkeypatch: Any) -> None:
    def _refuse(**_kwargs: Any) -> Any:
        raise bridge.StudioBridgeError("vault_push_failed", "QuotaExceeded")

    monkeypatch.setattr(bridge, "sync_and_verify_studio_bundle_in_vault", _refuse, raising=True)
    monkeypatch.setitem(sys.modules, "nexus_ai_agent.creative.studio_cas_vault_bridge", bridge)
    code, payload = _cli(
        [
            "render",
            "--script",
            "INT. ROOM - DAY\nA: Hi, hello there.\n",
            "--output-dir",
            str(tmp_path / "out"),
            "--cas-dir",
            str(tmp_path / "cas"),
            "--vault-dir",
            str(tmp_path / "vault"),
            "--vault-secret",
            "vault-secret",
            "--signing-key",
            KEY,
        ]
    )
    if code == 2:  # pragma: no cover - ffmpeg-less environments
        pytest.skip(payload["reason"])
    assert (code, payload["status"]) == (1, "vault_failed")
    assert payload["vault_reason"] == "vault_push_failed"


def test_cli_init_and_doctor_commands_print_json(tmp_path: Path) -> None:
    code, payload = _cli(["init", "--workspace", str(tmp_path / "studio")])
    assert (code, payload["status"]) == (0, "initialized")
    assert payload["created"]

    code, payload = _cli(
        [
            "doctor",
            "--workspace",
            str(tmp_path / "studio"),
            "--ffmpeg",
            "/nonexistent/ffmpeg",
            "--ffprobe",
            "/nonexistent/ffprobe",
        ]
    )
    assert (code, payload["status"]) == (1, "unhealthy")
    assert {check["name"] for check in payload["checks"]} >= {"ffmpeg_runs", "queue_sqlite"}


def test_cli_returns_argparse_exit_codes() -> None:
    buffer = io.StringIO()
    assert studio_cli.run_studio_cli(["--help"], out=buffer) == 0
    assert studio_cli.run_studio_cli([], out=buffer) == 2
    assert studio_cli.run_studio_cli(["--not-a-flag"], out=buffer) == 2


def test_cli_main_is_the_same_entry_point(tmp_path: Path) -> None:
    assert studio_cli.main(["init", "--workspace", str(tmp_path / "ws")]) == 0


# --------------------------------------------------------------------------- #
# studio_cas_vault_bridge.py - meter, paths, index, FFmpeg and vault refusals
# --------------------------------------------------------------------------- #


def test_memory_meter_enforces_its_ceiling() -> None:
    with pytest.raises(bridge.StudioBridgeError, match="invalid_memory_ceiling"):
        bridge._MemoryMeter(0)
    with pytest.raises(bridge.StudioBridgeError, match="invalid_memory_ceiling"):
        bridge._MemoryMeter(True)  # type: ignore[arg-type]
    meter = bridge._MemoryMeter(10)
    meter.charge(4)
    assert meter.live_bytes == 4
    with pytest.raises(bridge.StudioBridgeError, match="invalid_memory_charge"):
        meter.charge(-1)
    with pytest.raises(bridge.StudioBridgeError, match="memory_ceiling_exceeded"):
        meter.charge(11)
    meter.release(99)
    assert meter.live_bytes == 0
    assert meter.peak_bytes == 4


def test_prepare_directory_and_safe_child_guards(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(bridge.StudioBridgeError, match="symlink_refused"):
        bridge._prepare_directory(link, label="out")
    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(bridge.StudioBridgeError, match="not_a_directory"):
        bridge._prepare_directory(file_path, label="out")
    assert bridge._prepare_directory(real, label="out") == real.resolve()

    with pytest.raises(bridge.StudioBridgeError, match="unsafe_path"):
        bridge._safe_child(real, "../escape", label="child")
    with pytest.raises(bridge.StudioBridgeError, match="unsafe_path"):
        bridge._safe_child(real, "a/b", label="child")
    with pytest.raises(bridge.StudioBridgeError, match="unsafe_path"):
        bridge._safe_child(real, "", label="child")
    inner = real / "inner"
    inner.mkdir()
    target = real / "target.txt"
    target.write_text("x", encoding="utf-8")
    (inner / "link.txt").symlink_to(target)
    with pytest.raises(bridge.StudioBridgeError, match="symlink_refused"):
        bridge._safe_child(inner, "link.txt", label="child")


def test_read_small_json_and_copy_streaming_guards(tmp_path: Path) -> None:
    meter = bridge._MemoryMeter(1024 * 1024)
    directory = tmp_path / "dir"
    directory.mkdir()
    with pytest.raises(bridge.StudioBridgeError, match="not_regular_file"):
        bridge._read_small_json(directory, limit=1024, meter=meter)
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(bridge.StudioBridgeError, match="invalid_json_size"):
        bridge._read_small_json(empty, limit=1024, meter=meter)
    big = tmp_path / "big.json"
    big.write_bytes(b"{}" + b" " * 2048)
    with pytest.raises(bridge.StudioBridgeError, match="invalid_json_size"):
        bridge._read_small_json(big, limit=1024, meter=meter)
    good = tmp_path / "good.json"
    good.write_text('{"a": 1}', encoding="utf-8")
    assert bridge._read_small_json(good, limit=1024, meter=meter) == {"a": 1}

    with pytest.raises(bridge.StudioBridgeError, match="not_regular_file"):
        bridge._copy_streaming(directory, tmp_path / "out.bin", meter=meter)
    digest, size = bridge._copy_streaming(good, tmp_path / "copy.json", meter=meter)
    assert (digest, size) == (hashlib.sha256(good.read_bytes()).hexdigest(), good.stat().st_size)


def test_copy_streaming_cleans_up_when_the_copy_is_interrupted(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    meter = bridge._MemoryMeter(1024 * 1024)
    real_replace = os.replace

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("replace refused")

    monkeypatch.setattr(bridge.os, "replace", _boom)
    with pytest.raises(OSError, match="replace refused"):
        bridge._copy_streaming(source, tmp_path / "target.bin", meter=meter)
    monkeypatch.setattr(bridge.os, "replace", real_replace)
    assert not (tmp_path / "target.bin").exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".target.bin")] == []


def test_shot_index_path_and_reader_guards(tmp_path: Path) -> None:
    meter = bridge._MemoryMeter(1024 * 1024)
    with pytest.raises(bridge.StudioBridgeError, match="invalid_cache_key"):
        bridge._shot_index_path(tmp_path, "not-hex")
    index_root = tmp_path / bridge.SHOT_INDEX_DIRNAME
    index_root.mkdir()
    key = "a" * 64
    assert (
        bridge._read_shot_index(index_root / f"{key}.json", segment_key_hash=key, meter=meter)
        is None
    )
    assert (
        bridge._read_shot_index(tmp_path / "absent.json", segment_key_hash=key, meter=meter) is None
    )
    path = index_root / f"{key}.json"
    for payload in (
        b"not json",
        b'{"schema": "other"}',
        json.dumps({"schema": bridge.SHOT_SEGMENT_SCHEMA, "segment_key_hash": "b" * 64}).encode(),
        json.dumps(
            {"schema": bridge.SHOT_SEGMENT_SCHEMA, "segment_key_hash": key, "cas_key": "zz"}
        ).encode(),
        json.dumps(
            {
                "schema": bridge.SHOT_SEGMENT_SCHEMA,
                "segment_key_hash": key,
                "cas_key": "c" * 64,
                "content_sha": "short",
            }
        ).encode(),
        b"[]",
    ):
        path.write_bytes(payload)
        assert bridge._read_shot_index(path, segment_key_hash=key, meter=meter) is None
    path.write_bytes(
        json.dumps(
            {
                "schema": bridge.SHOT_SEGMENT_SCHEMA,
                "segment_key_hash": key,
                "cas_key": "c" * 64,
                "content_sha": "d" * 64,
            }
        ).encode()
    )
    entry = bridge._read_shot_index(path, segment_key_hash=key, meter=meter)
    assert entry is not None and entry["cas_key"] == "c" * 64


def test_shot_index_path_refuses_a_symlinked_index_root(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / bridge.SHOT_INDEX_DIRNAME).symlink_to(
        tmp_path / "elsewhere", target_is_directory=True
    )
    with pytest.raises(bridge.StudioBridgeError, match="symlink_refused"):
        bridge._shot_index_path(tmp_path, "a" * 64)


def test_run_ffmpeg_reports_timeout_unavailable_and_failure(monkeypatch: Any) -> None:
    def _timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

    monkeypatch.setattr(bridge.subprocess, "run", _timeout)
    with pytest.raises(bridge.StudioBridgeError, match="ffmpeg_timeout"):
        bridge._run_ffmpeg(["ffmpeg"], timeout_s=1, label="probe")

    def _missing(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("no ffmpeg")

    monkeypatch.setattr(bridge.subprocess, "run", _missing)
    with pytest.raises(bridge.StudioBridgeError, match="ffmpeg_unavailable"):
        bridge._run_ffmpeg(["ffmpeg"], timeout_s=1, label="probe")

    from types import SimpleNamespace

    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=3, stderr=b"boom"),
    )
    with pytest.raises(bridge.StudioBridgeError, match="ffmpeg_failed"):
        bridge._run_ffmpeg(["ffmpeg"], timeout_s=1, label="probe")


def test_concat_list_refuses_unsafe_entries(tmp_path: Path) -> None:
    meter = bridge._MemoryMeter(1024 * 1024)
    unsafe = tmp_path / "has'quote.mp4"
    unsafe.write_bytes(b"x")
    with pytest.raises(bridge.StudioBridgeError, match="unsafe_path"):
        bridge._write_concat_list(tmp_path / "list.txt", [unsafe], meter=meter)


def test_build_argv_helpers_are_deterministic(tmp_path: Path) -> None:
    argv = bridge.build_shot_ffmpeg_argv(
        ffmpeg="ffmpeg",
        image_path=tmp_path / "i.png",
        wav_path=tmp_path / "a.wav",
        output_path=tmp_path / "o.mp4",
        duration_s=1.5,
        fps=12.0,
        width=320,
        height=180,
        sample_rate=16_000,
    )
    assert argv[0] == "ffmpeg"
    assert "-pix_fmt" in argv and argv[argv.index("-pix_fmt") + 1] == "yuv420p"
    assert argv[argv.index("-s") + 1] == "320x180"
    assert argv[argv.index("-r") + 1] == "12"
    concat = bridge.build_concat_argv(
        ffmpeg="ffmpeg", concat_list=tmp_path / "l.txt", output_path=tmp_path / "o.mp4"
    )
    assert concat[concat.index("-c") + 1] == "copy"
    assert "-safe" in concat


# --------------------------------------------------------------------------- #
# studio_cas_vault_bridge.py - render refusals
# --------------------------------------------------------------------------- #


SCRIPT = "INT. ROOM - DAY\nA: Hello there, friend.\n"
FAST = {"fps": 8, "width": 64, "height": 64}


def _ffmpeg_available() -> bool:
    import shutil

    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _ffmpeg_bin() -> str:
    """Path of the real FFmpeg binary under test (``NEXUS_FFMPEG`` overrides)."""
    import shutil

    return os.environ.get("NEXUS_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe_bin() -> str:
    """Path of the real ffprobe binary under test (``NEXUS_FFPROBE`` overrides)."""
    import shutil

    return os.environ.get("NEXUS_FFPROBE") or shutil.which("ffprobe") or "ffprobe"


needs_media = pytest.mark.skipif(
    not _ffmpeg_available(), reason="real ffmpeg and ffprobe are required"
)


@needs_media
def test_render_refuses_an_invalid_script_and_profile(tmp_path: Path) -> None:
    with pytest.raises(bridge.StudioBridgeError, match="invalid_script"):
        bridge.render_storyboard_with_segment_cas(
            "   ",
            "t",
            tmp_path / "out",
            tmp_path / "cas",
            "job",
            signing_key=KEY,
            **FAST,
        )
    with pytest.raises(bridge.StudioBridgeError, match="invalid_profile"):
        bridge.render_storyboard_with_segment_cas(
            SCRIPT,
            "t",
            tmp_path / "out",
            tmp_path / "cas",
            "job",
            signing_key=KEY,
            fps=8,
            width=65,
            height=64,
        )


@needs_media
def test_render_refuses_a_missing_local_image(tmp_path: Path) -> None:
    with pytest.raises(bridge.StudioBridgeError, match="local_image_missing"):
        bridge.render_storyboard_with_segment_cas(
            SCRIPT,
            "t",
            tmp_path / "out",
            tmp_path / "cas",
            "job",
            signing_key=KEY,
            local_images={"s000-sh000": tmp_path / "nope.png"},
            **FAST,
        )


@needs_media
def test_render_surfaces_compiler_failures_and_calls_the_hook(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls: list[str] = []
    result = bridge.render_storyboard_with_segment_cas(
        SCRIPT,
        "t",
        tmp_path / "out",
        tmp_path / "cas",
        "job",
        signing_key=KEY,
        before_render=lambda compiled: calls.append(compiled.manifest_sha256),
        **FAST,
    )
    assert len(calls) == 1
    assert calls[0] == json.loads(result.manifest_path.read_text())["manifest_sha256"]

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("parser refused")

    monkeypatch.setattr(bridge, "compile_storyboard", _explode)
    with pytest.raises(bridge.StudioBridgeError, match="compile_failed"):
        bridge.render_storyboard_with_segment_cas(
            SCRIPT, "t", tmp_path / "out2", tmp_path / "cas", "job", signing_key=KEY, **FAST
        )


@needs_media
def test_render_recovers_from_a_corrupt_cas_entry(tmp_path: Path) -> None:
    """A damaged index/cache entry must degrade to a re-render, never to a bad film."""
    first = bridge.render_storyboard_with_segment_cas(
        SCRIPT, "t", tmp_path / "out", tmp_path / "cas", "job", signing_key=KEY, **FAST
    )
    assert (first.cas_hits, first.cas_misses) == (0, 1)
    for entry in (tmp_path / "cas" / bridge.SHOT_INDEX_DIRNAME).glob("*.json"):
        payload = json.loads(entry.read_text())
        payload["content_sha"] = "0" * 64
        entry.write_text(json.dumps(payload), encoding="utf-8")
    second = bridge.render_storyboard_with_segment_cas(
        SCRIPT, "t", tmp_path / "out2", tmp_path / "cas", "job", signing_key=KEY, **FAST
    )
    assert (second.cas_hits, second.cas_misses) == (0, 1)


@needs_media
def test_render_reports_an_invalid_segment_from_a_lying_ffmpeg(
    tmp_path: Path, monkeypatch: Any
) -> None:
    def _broken(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(bridge, "_run_ffmpeg", _broken)
    with pytest.raises(bridge.StudioBridgeError):
        bridge.render_storyboard_with_segment_cas(
            SCRIPT,
            "t",
            tmp_path / "out",
            tmp_path / "cas",
            "job",
            signing_key=KEY,
            **FAST,
        )


@needs_media
def test_render_fails_closed_when_the_final_verifier_or_passport_refuses(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        bridge,
        "verify_mp4",
        lambda *a, **k: av.Mp4Verdict(status=av.VIOLATED, reasons=("forged",)),
    )
    with pytest.raises(bridge.StudioBridgeError):
        bridge.render_storyboard_with_segment_cas(
            SCRIPT, "t", tmp_path / "out", tmp_path / "cas", "job", signing_key=KEY, **FAST
        )


@needs_media
def test_render_fails_closed_when_provenance_is_violated(tmp_path: Path, monkeypatch: Any) -> None:
    def _refuse(_self: Any, _bundle: Any) -> None:
        raise mz.MaterializationError("provenance_violated", "simulated drift")

    monkeypatch.setattr(mz.StoryboardMediaMaterializer, "verify_materialized_provenance", _refuse)
    with pytest.raises(bridge.StudioBridgeError, match="provenance_violated"):
        bridge.render_storyboard_with_segment_cas(
            SCRIPT, "t", tmp_path / "out", tmp_path / "cas", "job", signing_key=KEY, **FAST
        )


@needs_media
def test_render_fails_closed_when_the_passport_does_not_verify(
    tmp_path: Path, monkeypatch: Any
) -> None:
    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise ap.PassportError("seal_mismatch", "simulated")

    monkeypatch.setattr(bridge, "verify_artifact_passport", _refuse)
    with pytest.raises(bridge.StudioBridgeError, match="passport_verification_failed"):
        bridge.render_storyboard_with_segment_cas(
            SCRIPT, "t", tmp_path / "out", tmp_path / "cas", "job", signing_key=KEY, **FAST
        )


# --------------------------------------------------------------------------- #
# studio_cas_vault_bridge.py - vault refusals
# --------------------------------------------------------------------------- #


def test_journal_reader_refuses_corrupt_files(tmp_path: Path) -> None:
    meter = bridge._MemoryMeter(1024 * 1024)
    assert bridge._read_journal(tmp_path / "absent.jsonl", meter=meter) == []
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    assert bridge._read_journal(empty, meter=meter) == []
    directory = tmp_path / "dir.jsonl"
    directory.mkdir()
    with pytest.raises(bridge.StudioBridgeError, match="journal_invalid"):
        bridge._read_journal(directory, meter=meter)
    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(bridge.StudioBridgeError, match="journal_invalid"):
        bridge._read_journal(corrupt, meter=meter)
    non_object = tmp_path / "list.jsonl"
    non_object.write_text("[]\n", encoding="utf-8")
    with pytest.raises(bridge.StudioBridgeError, match="journal_invalid"):
        bridge._read_journal(non_object, meter=meter)
    blank = tmp_path / "blank.jsonl"
    blank.write_text('\n{"project_id": "p"}\n\n', encoding="utf-8")
    assert bridge._read_journal(blank, meter=meter) == [{"project_id": "p"}]


def test_vault_sync_refuses_an_empty_project_id(tmp_path: Path) -> None:
    with pytest.raises(bridge.StudioBridgeError, match="invalid_project_id"):
        bridge.sync_and_verify_studio_bundle_in_vault(
            render_result=None,  # type: ignore[arg-type]
            vault_storage_dir=tmp_path / "vault",
            vault_journal_db_path=tmp_path / "journal.jsonl",
            restore_dir=tmp_path / "restore",
            master_secret=b"secret",
            project_id="   ",
            signing_key=KEY,
        )


def test_vault_head_resolution_reports_ambiguous_and_invalid_manifests(tmp_path: Path) -> None:
    from nexus_ai_agent.storage.personal_cloud_vault import LocalDeviceVaultDriver

    driver = LocalDeviceVaultDriver(tmp_path / "vault")
    assert bridge._current_vault_head(driver, "proj") is None

    driver.put_manifest("snap-a", b"not-a-manifest")
    with pytest.raises(bridge.StudioBridgeError, match="vault_manifest_invalid"):
        bridge._current_vault_head(driver, "proj")


def test_vault_sync_fails_closed_when_the_push_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    class _RefusingDriver:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def list_manifests(self) -> list[str]:
            return []

        def put_chunk(self, *_args: Any, **_kwargs: Any) -> None:
            from nexus_ai_agent.storage.personal_cloud_vault import VaultQuotaExceededError

            raise VaultQuotaExceededError("quota")

    monkeypatch.setattr(bridge, "LocalDeviceVaultDriver", _RefusingDriver)
    with pytest.raises(bridge.StudioBridgeError):
        bridge.sync_and_verify_studio_bundle_in_vault(
            render_result=_FakeRenderResult(tmp_path),
            vault_storage_dir=tmp_path / "vault",
            vault_journal_db_path=tmp_path / "journal.jsonl",
            restore_dir=tmp_path / "restore",
            master_secret=b"secret",
            project_id="proj",
            signing_key=KEY,
        )


class _FakeRenderResult:
    """A minimal stand-in used only to drive the vault's failure branches."""

    def __init__(self, root: Path) -> None:
        from nexus_ai_agent.creative.studio_cas_vault_bridge import CasStoryboardRenderResult

        out = root / "render"
        out.mkdir(parents=True, exist_ok=True)
        mp4 = out / "render.mp4"
        mp4.write_bytes(b"\x00" * 32)
        manifest = out / "job.manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        passport = out / "render.passport.json"
        passport.write_text("{}", encoding="utf-8")
        manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
        profile = mz.RenderProfile.from_mapping(PROFILE)
        bundle = mz.MaterializedBundle(
            out_dir=str(out),
            profile=profile,
            manifest_path=str(manifest),
            manifest_sha256=manifest_sha,
            manifest_size=2,
            manifest_inode=1,
            assets=(),
            shot_durations_s=(1.0,),
            asset_merkle_root=mz.EMPTY_MERKLE_ROOT,
            total_asset_leaves=0,
            shot_hashes=(),
            shot_merkle_root=mz.EMPTY_MERKLE_ROOT,
            shot_inclusion_proofs=(),
        )
        self._result = CasStoryboardRenderResult(
            artifact=bundle,
            verification=av.Mp4Verdict(status=av.PASS),
            passport={
                "job_id": "job",
                "output_sha256": hashlib.sha256(b"\x00" * 32).hexdigest(),
                "manifest_sha256": manifest_sha,
            },
            passport_path=passport,
            manifest_path=manifest,
            cas_hits=0,
            cas_misses=1,
            shot_segment_hashes=(),
            peak_memory_bytes=0,
            output_mp4_path=mp4,
            receipt_path=out / "job.receipt.json",
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._result, name)


def test_vault_sync_refuses_an_incomplete_restore(tmp_path: Path, monkeypatch: Any) -> None:
    from types import SimpleNamespace

    class _DroppingManager:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def push_project_snapshot(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                snapshot_id="snap-x", uploaded_chunks=1, deduplicated_chunks=0, total_chunks=1
            )

        def pull_project_snapshot(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(files={})

    monkeypatch.setattr(bridge, "PersonalCloudVaultManager", _DroppingManager)
    with pytest.raises(bridge.StudioBridgeError, match="vault_restore_incomplete"):
        bridge.sync_and_verify_studio_bundle_in_vault(
            render_result=_FakeRenderResult(tmp_path),
            vault_storage_dir=tmp_path / "vault",
            vault_journal_db_path=tmp_path / "journal.jsonl",
            restore_dir=tmp_path / "restore",
            master_secret=b"secret",
            project_id="proj",
            signing_key=KEY,
        )


_TEST_KEY_MATERIAL = _derive_vault_keys(b"secret", b"\x01" * 16, iterations=100_000)


def test_vault_sync_refuses_restored_bytes_that_differ(tmp_path: Path, monkeypatch: Any) -> None:
    from types import SimpleNamespace

    class _LyingManager:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def push_project_snapshot(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                snapshot_id="snap-x", uploaded_chunks=1, deduplicated_chunks=0, total_chunks=1
            )

        def pull_project_snapshot(self, *, target_dir: Path, **_kwargs: Any) -> Any:
            target = Path(target_dir)
            (target / "render.mp4").write_bytes(b"different bytes")
            (target / "job.manifest.json").write_text("{}", encoding="utf-8")
            (target / "render.passport.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(files={"render.mp4": b"x"})

    monkeypatch.setattr(bridge, "PersonalCloudVaultManager", _LyingManager)
    with pytest.raises(bridge.StudioBridgeError, match="vault_restored_bytes_differ"):
        bridge.sync_and_verify_studio_bundle_in_vault(
            render_result=_FakeRenderResult(tmp_path),
            vault_storage_dir=tmp_path / "vault",
            vault_journal_db_path=tmp_path / "journal.jsonl",
            restore_dir=tmp_path / "restore",
            master_secret=b"secret",
            project_id="proj",
            signing_key=KEY,
        )


def test_vault_sync_fails_closed_when_the_restored_passport_is_invalid(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from types import SimpleNamespace

    class _Manager:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def push_project_snapshot(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                snapshot_id="snap-x", uploaded_chunks=1, deduplicated_chunks=0, total_chunks=1
            )

        def pull_project_snapshot(self, *, target_dir: Path, **_kwargs: Any) -> Any:
            result = self.result
            target = Path(target_dir)
            (target / "render.mp4").write_bytes(result.output_mp4_path.read_bytes())
            (target / "job.manifest.json").write_text("{}", encoding="utf-8")
            (target / "render.passport.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(files={"render.mp4": b"x"})

    manager = _Manager()
    fake = _FakeRenderResult(tmp_path)
    manager.result = fake
    monkeypatch.setattr(bridge, "PersonalCloudVaultManager", lambda *_a, **_k: manager)
    monkeypatch.setattr(
        bridge,
        "verify_artifact_passport",
        lambda *_a, **_k: (_ for _ in ()).throw(ap.PassportError("seal_mismatch", "x")),
    )
    with pytest.raises(bridge.StudioBridgeError, match="vault_passport_verification_failed"):
        bridge.sync_and_verify_studio_bundle_in_vault(
            render_result=fake,
            vault_storage_dir=tmp_path / "vault",
            vault_journal_db_path=tmp_path / "journal.jsonl",
            restore_dir=tmp_path / "restore",
            master_secret=b"secret",
            project_id="proj",
            signing_key=KEY,
        )


def test_vault_sync_raises_when_the_restored_artifact_is_not_pass(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from types import SimpleNamespace

    fake = _FakeRenderResult(tmp_path)

    class _Manager:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def push_project_snapshot(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                snapshot_id="snap-x", uploaded_chunks=1, deduplicated_chunks=0, total_chunks=1
            )

        def pull_project_snapshot(self, *, target_dir: Path, **_kwargs: Any) -> Any:
            target = Path(target_dir)
            (target / "render.mp4").write_bytes(fake.output_mp4_path.read_bytes())
            (target / "job.manifest.json").write_text("{}", encoding="utf-8")
            (target / "render.passport.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(files={"render.mp4": b"x"})

    monkeypatch.setattr(bridge, "PersonalCloudVaultManager", _Manager)
    monkeypatch.setattr(
        bridge,
        "verify_artifact_passport",
        lambda *_a, **_k: SimpleNamespace(
            passport={"manifest_sha256": hashlib.sha256(b"{}").hexdigest()}
        ),
    )
    monkeypatch.setattr(
        bridge,
        "verify_mp4",
        lambda *_a, **_k: av.Mp4Verdict(status=av.VIOLATED, reasons=("forged",)),
    )
    with pytest.raises(RuntimeError, match="failed verification"):
        bridge.sync_and_verify_studio_bundle_in_vault(
            render_result=fake,
            vault_storage_dir=tmp_path / "vault",
            vault_journal_db_path=tmp_path / "journal.jsonl",
            restore_dir=tmp_path / "restore",
            master_secret=b"secret",
            project_id="proj",
            signing_key=KEY,
        )


# --------------------------------------------------------------------------- #
# AST mutation gate
# --------------------------------------------------------------------------- #

MUTATION_TARGETS = {
    "artifact_passport": "src/nexus_ai_agent/creative/artifact_passport.py",
    "artifact_verifier": "src/nexus_ai_agent/creative/artifact_verifier.py",
    "materializer": "src/nexus_ai_agent/creative/materializer.py",
    "studio_cas_vault_bridge": "src/nexus_ai_agent/creative/studio_cas_vault_bridge.py",
    "artifact_download": "src/nexus_ai_agent/creative/artifact_download.py",
}


def _load_mutant(relative_path: str, mutate: Any, name: str) -> ModuleType:
    """Parse ``relative_path``, apply an AST mutation, exec it as its own module.

    ``sys.modules`` is never cleared: the mutant is *added* under a private name
    while every C-extension module (``numpy``, ``PIL``, ``sqlalchemy``, …) keeps
    its original instance. Only the module under test is replaced by the mutant.
    """
    source_path = REPO_ROOT / relative_path
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    mutate(tree)
    ast.fix_missing_locations(tree)
    code = compile(tree, f"<mutant:{name}>", "exec")
    module = ModuleType(f"nexus_mutant_{name}")
    module.__file__ = str(source_path)
    module.__dict__["__builtins__"] = __builtins__
    sys.modules[module.__name__] = module
    exec(code, module.__dict__)  # noqa: S102 - executing our own parsed AST
    return module


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _refuses(action: Any, exc_type: type[BaseException], *, contains: str = "") -> None:
    """Assert that ``action`` is refused with ``exc_type`` (mutation-check primitive).

    Deliberately not :func:`pytest.raises`: a mutation check must fail with a
    plain ``AssertionError`` so :func:`_kill` can classify it, and pytest's own
    ``Failed`` outcome would escape that classification.
    """
    try:
        action()
    except exc_type as exc:
        if contains:
            assert contains in str(exc), f"{contains!r} missing from {exc!r}"
        return
    raise AssertionError(f"{exc_type.__name__} was not raised: the refusal disappeared")


def _kill(mutant_behaviour: Any) -> None:
    """A mutant is KILLED when the assertion that guards the invariant now fails."""
    try:
        mutant_behaviour()
    except AssertionError:
        return  # the guarded invariant failed: the mutant is killed
    raise AssertionError("mutant survived: the invariant was not enforced")


def test_mutant_passport_ignoring_inclusion_proof_replay_is_killed() -> None:
    """Mutant 1: drop ``verify_shot_inclusion_proofs`` from the passport verifier."""

    def _strip(tree: ast.Module) -> None:
        func = _function(tree, "passport_shot_proof_violations")
        targets = [
            node
            for node in ast.walk(func)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", "") == "verify_shot_inclusion_proofs"
        ]
        assert len(targets) == 1, "the proof replay must be a single return statement"
        targets[0].value = ast.Tuple(elts=[], ctx=ast.Load())

    def _behaviour(violations_fn: Any) -> None:
        hashes = list(_shot_hashes(3))
        payloads = mz.shot_leaf_payloads(hashes)
        root = mz.merkle_root_sha256(payloads)
        proofs = [mz.merkle_inclusion_proof_record(payloads, index) for index in range(3)]
        tampered = [dict(proof) for proof in proofs]
        tampered[1]["sibling_hashes"] = ["0" * 64 for _ in tampered[1]["sibling_hashes"]]
        passport = {
            "shot_hashes": hashes,
            "shot_merkle_root": root,
            "shot_inclusion_proofs": tampered,
        }
        assert violations_fn(passport) == ("shot_inclusion_proof_invalid",), (
            "a flipped sibling must be a violation"
        )

    _behaviour(ap.passport_shot_proof_violations)  # positive control
    mutant = _load_mutant(MUTATION_TARGETS["artifact_passport"], _strip, "passport_no_proof_replay")
    _kill(lambda: _behaviour(mutant.passport_shot_proof_violations))


def test_mutant_materializer_without_rfc6962_domain_separation_is_killed() -> None:
    """Mutant 2: drop the ``0x00``/``0x01`` RFC 6962 domain-separation prefixes."""
    expected_root = mz.merkle_root_sha256([b"a", b"b", b"c"])

    def _strip(tree: ast.Module) -> None:
        for func_name in ("_rfc6962_leaf", "_rfc6962_node"):
            func = _function(tree, func_name)
            for node in ast.walk(func):
                if isinstance(node, ast.Constant) and node.value in (b"\x00", b"\x01"):
                    node.value = b""

    def _behaviour(root_fn: Any) -> None:
        # The pinned RFC 6962 vector is the test; without the prefixes it changes.
        assert root_fn([b"a", b"b", b"c"]) == expected_root

    _behaviour(mz.merkle_root_sha256)  # positive control
    mutant = _load_mutant(MUTATION_TARGETS["materializer"], _strip, "materializer_no_domain_sep")
    _kill(lambda: _behaviour(mutant.merkle_root_sha256))
    assert mutant.merkle_root_sha256([b"a", b"b", b"c"]) != expected_root


def test_mutant_bridge_ignoring_restored_pass_status_is_killed() -> None:
    """Mutant 3: ignore the ``PASS`` requirement after a vault restore."""

    def _neuter(tree: ast.Module) -> None:
        func = _function(tree, "sync_and_verify_studio_bundle_in_vault")
        found = False
        for node in ast.walk(func):
            if isinstance(node, ast.If) and "artifact_verdict" in ast.unparse(node.test):
                node.test = ast.Constant(value=False)
                found = True
        assert found, "the artifact-verdict guard must exist"

    def _behaviour(sync_fn: Any, tmp_root: Path) -> None:
        from types import SimpleNamespace

        fake = _FakeRenderResult(tmp_root)

        class _Manager:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def push_project_snapshot(self, **_kwargs: Any) -> Any:
                return SimpleNamespace(
                    snapshot_id="snap-x", uploaded_chunks=1, deduplicated_chunks=0, total_chunks=1
                )

            def pull_project_snapshot(self, *, target_dir: Path, **_kwargs: Any) -> Any:
                target = Path(target_dir)
                (target / "render.mp4").write_bytes(fake.output_mp4_path.read_bytes())
                (target / "job.manifest.json").write_text("{}", encoding="utf-8")
                (target / "render.passport.json").write_text("{}", encoding="utf-8")
                return SimpleNamespace(files={"render.mp4": b"x"})

        original_manager = sync_fn.__globals__["PersonalCloudVaultManager"]
        original_verify_passport = sync_fn.__globals__["verify_artifact_passport"]
        original_verify_mp4 = sync_fn.__globals__["verify_mp4"]
        try:
            sync_fn.__globals__["PersonalCloudVaultManager"] = _Manager
            sync_fn.__globals__["verify_artifact_passport"] = lambda *_a, **_k: SimpleNamespace(
                passport={"manifest_sha256": hashlib.sha256(b"{}").hexdigest()}
            )
            sync_fn.__globals__["verify_mp4"] = lambda *_a, **_k: av.Mp4Verdict(
                status=av.VIOLATED, reasons=("forged",)
            )
            _refuses(
                lambda: sync_fn(
                    render_result=fake,
                    vault_storage_dir=tmp_root / "vault",
                    vault_journal_db_path=tmp_root / "journal.jsonl",
                    restore_dir=tmp_root / "restore",
                    master_secret=b"secret",
                    project_id="proj",
                    signing_key=KEY,
                ),
                RuntimeError,
                contains="failed verification",
            )
        finally:
            sync_fn.__globals__["PersonalCloudVaultManager"] = original_manager
            sync_fn.__globals__["verify_artifact_passport"] = original_verify_passport
            sync_fn.__globals__["verify_mp4"] = original_verify_mp4

    _behaviour(bridge.sync_and_verify_studio_bundle_in_vault, tmp_path_factory_dir())
    mutant = _load_mutant(
        MUTATION_TARGETS["studio_cas_vault_bridge"], _neuter, "bridge_no_restore_gate"
    )
    _kill(lambda: _behaviour(mutant.sync_and_verify_studio_bundle_in_vault, tmp_path_factory_dir()))


def tmp_path_factory_dir() -> Path:
    """A fresh scratch directory for checks that must not share state."""
    import tempfile

    return Path(tempfile.mkdtemp(prefix="mutant-check-"))


def test_mutant_download_ignoring_the_passport_is_killed(tmp_path: Path, real_mp4: Path) -> None:
    """Mutant 4: ignore ``verify_artifact_passport`` when authorizing a download."""

    def _bypass(tree: ast.Module) -> None:
        func = _function(tree, "authorize_artifact_download")
        replaced = False
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", "") == "verify_artifact_passport"
            ):
                node.value = ast.parse("_permissive_check(resolved, root)").body[0].value
                replaced = True
        assert replaced, "the passport verification call must exist"

    def _behaviour(authorize: Any, root: Path) -> None:
        mp4 = root / "storyboard.mp4"
        mp4.write_bytes(real_mp4.read_bytes())
        ap.write_passport(
            root / f"storyboard{ap.PASSPORT_SUFFIX}",
            _passport(mp4, output_rel_path="storyboard.mp4"),
        )
        raw = bytearray(mp4.read_bytes())
        raw[-16] ^= 0xFF
        mp4.write_bytes(bytes(raw))

        def _attempt() -> None:
            authorize(root, "storyboard.mp4", KEY)

        try:
            _attempt()
        except ad.DownloadRefused as refused:
            assert refused.status_code == 409, "a tampered artifact must never be served"
            return
        raise AssertionError("a tampered artifact was served: the passport gate is gone")

    root = tmp_path / "pristine"
    root.mkdir()
    _behaviour(ad.authorize_artifact_download, root)  # positive control

    mutant = _load_mutant(MUTATION_TARGETS["artifact_download"], _bypass, "download_no_passport")

    class _Permissive:
        def __init__(self, resolved: Path, root: Path) -> None:
            self.passport = {
                "output_rel_path": str(resolved.relative_to(root)),
                "output_size_bytes": resolved.stat().st_size,
                "output_sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
            }

    mutant.__dict__["_permissive_check"] = _Permissive
    mutant_root = tmp_path / "mutant"
    mutant_root.mkdir()
    _kill(lambda: _behaviour(mutant.authorize_artifact_download, mutant_root))


def test_mutant_modules_are_isolated_from_production_modules() -> None:
    """Loading (and running) mutants must never rebind a production module name."""

    def _noop(_tree: ast.Module) -> None:
        return

    production = {
        "nexus_ai_agent.creative.materializer": mz,
        "nexus_ai_agent.creative.artifact_passport": ap,
        "nexus_ai_agent.creative.artifact_download": ad,
        "nexus_ai_agent.creative.studio_cas_vault_bridge": bridge,
    }
    before = {name: id(module) for name, module in sys.modules.items() if name in production}
    numpy_before = id(sys.modules["numpy"]) if "numpy" in sys.modules else None

    mutant = _load_mutant(MUTATION_TARGETS["materializer"], _noop, "isolation_probe")
    assert mutant is not mz
    assert mutant.__name__ == "nexus_mutant_isolation_probe"
    assert sys.modules[mutant.__name__] is mutant
    assert mutant.merkle_root_sha256([b"a", b"b", b"c"]) == mz.merkle_root_sha256(
        [b"a", b"b", b"c"]
    )

    after = {name: id(module) for name, module in sys.modules.items() if name in production}
    assert after == before
    assert all(sys.modules[name] is module for name, module in production.items())
    if numpy_before is not None:
        assert id(sys.modules["numpy"]) == numpy_before, "C-extension modules must stay bound"
    assert mz.merkle_root_sha256([b"a", b"b", b"c"]) != hashlib.sha256(b"abc").hexdigest()
    assert stat.S_ISREG(os.stat(REPO_ROOT / MUTATION_TARGETS["materializer"]).st_mode)


# --------------------------------------------------------------------------- #
# Fault injection at the narrowest seam: TOCTOU guards and dead-code control
# --------------------------------------------------------------------------- #


def test_resolve_within_refuses_a_path_that_resolves_outside(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Defence in depth: even if the symlink walk is bypassed, ``resolve`` is checked."""
    root = tmp_path / "ws"
    (root / "inner").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "inner" / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(mz, "refuse_symlink_chain", lambda *_args, **_kwargs: None)
    with pytest.raises(mz.MaterializationError, match="path_outside_workspace"):
        mz.resolve_within(root, "inner/escape/deep.bin")


def test_download_gate_refuses_a_path_that_resolves_outside(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    real_resolve = Path.resolve

    def _resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        if self.name == "storyboard.mp4":
            return tmp_path / "elsewhere.mp4"
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(ad.Path, "resolve", _resolve)
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(root, "storyboard.mp4", KEY)
    assert (info.value.status_code, info.value.reason) == (403, "path_outside_workspace")


def test_verify_passport_detects_a_passport_swapped_for_a_non_regular_file(
    tmp_path: Path, copied_mp4: Path, monkeypatch: Any
) -> None:
    """TOCTOU: the descriptor check must refuse when the file changed after lstat."""
    passport_path = _write(tmp_path / "storyboard.passport.json", _passport(copied_mp4))
    real_fstat = os.fstat

    def _fstat(fd: int) -> Any:
        fake = real_fstat(fd)
        return os.stat_result((stat.S_IFIFO | 0o600, fake.st_ino, fake.st_dev, 1, 0, 0, 0, 0, 0, 0))

    monkeypatch.setattr(ap.os, "fstat", _fstat)
    with pytest.raises(ap.PassportError, match="passport_not_regular"):
        ap.verify_artifact_passport(passport_path, copied_mp4, KEY)


def test_verify_passport_detects_a_passport_that_shrank_after_lstat(
    tmp_path: Path, copied_mp4: Path, monkeypatch: Any
) -> None:
    passport_path = _write(tmp_path / "storyboard.passport.json", _passport(copied_mp4))
    real_fstat = os.fstat

    def _fstat(fd: int) -> Any:
        fake = real_fstat(fd)
        return os.stat_result(
            (fake.st_mode, fake.st_ino, fake.st_dev, 0, fake.st_uid, fake.st_gid, 0, 0, 0, 0)
        )

    monkeypatch.setattr(ap.os, "fstat", _fstat)
    with pytest.raises(ap.PassportError, match="passport_size"):
        ap.verify_artifact_passport(passport_path, copied_mp4, KEY)


def test_full_96_shot_storyboard_passport_seals_and_verifies_every_leaf(
    real_mp4: Path, tmp_path: Path
) -> None:
    """A 96-shot v2 passport survives the disk round-trip whole.

    Every one of the 96 shots seals its own RFC 6962 leaf and audit path, so
    the document is far bigger than the old 64 KiB ceiling (~87 KiB measured):
    the raised ``MAX_PASSPORT_BYTES`` must admit it, all 96 inclusion proofs
    must replay against the sealed root, and the HMAC (plus the artifact's own
    bytes) must verify after the passport is re-read from disk.
    """
    shot_hashes = [hashlib.sha256(f"shot-{index}".encode()).hexdigest() for index in range(96)]
    leaves = mz.shot_leaf_payloads(shot_hashes)
    passport = ap.build_passport(
        job_id="job-96",
        project_id="proj-96",
        source_sha256=hashlib.sha256(b"source").hexdigest(),
        manifest_sha256=hashlib.sha256(b"manifest").hexdigest(),
        render_profile_hash=hashlib.sha256(b"profile").hexdigest(),
        asset_merkle_root=hashlib.sha256(b"assets").hexdigest(),
        total_asset_leaves=4,
        output_rel_path="projects/proj-96/renders/job-96/storyboard.mp4",
        output_size_bytes=real_mp4.stat().st_size,
        output_sha256=hashlib.sha256(real_mp4.read_bytes()).hexdigest(),
        probed_duration_s=1.0,
        verifier_status="PASS",
        created_at_unix_s=1_000,
        shot_hashes=shot_hashes,
        signing_key=KEY,
    )
    assert len(passport["shot_hashes"]) == 96
    assert len(passport["shot_inclusion_proofs"]) == 96

    # Disk round-trip: the sealed document is stored and re-read whole.
    passport_path = tmp_path / "storyboard.passport.json"
    ap.write_passport(passport_path, passport)
    on_disk = passport_path.stat().st_size
    assert 65_536 < on_disk <= ap.MAX_PASSPORT_BYTES, (
        "a full 96-shot board with every audit path must exceed the old 64 KiB "
        "ceiling yet fit the raised one"
    )
    artifact = tmp_path / "storyboard.mp4"
    artifact.write_bytes(real_mp4.read_bytes())
    reread = json.loads(passport_path.read_text(encoding="utf-8"))

    # N passport leaves == 96, and every leaf's audit path replays to the root.
    assert len(reread["shot_hashes"]) == 96
    root = reread["shot_merkle_root"]
    for index, proof in enumerate(reread["shot_inclusion_proofs"]):
        assert proof["leaf_index"] == index
        assert proof["leaf_hash"] == shot_hashes[index]
        assert proof["total_leaves"] == 96
        assert proof["root_hash"] == root
        assert mz.verify_merkle_inclusion_proof(
            leaves[index],
            index=index,
            total_leaves=96,
            proof=proof["sibling_hashes"],
            expected_root=root,
        )
    # The whole passport (HMAC included) verifies against the artifact bytes.
    check = ap.verify_artifact_passport(passport_path, artifact, KEY, min_version=2)
    assert check.passport["shot_merkle_root"] == root
    assert ap.passport_shot_proof_violations(check.passport) == ()


def test_verify_passport_refuses_a_read_that_exceeds_the_cap(
    tmp_path: Path, copied_mp4: Path, monkeypatch: Any
) -> None:
    """TOCTOU: the file grew between the stat and the read, so the read is capped."""
    passport_path = tmp_path / "storyboard.passport.json"
    passport_path.write_bytes(json.dumps(_passport(copied_mp4)).encode() + b" " * 263_000)
    small = os.stat_result((stat.S_IFREG | 0o600, 1, 1, 1, 0, 0, 10, 0, 0, 0))
    monkeypatch.setattr(ap.os, "lstat", lambda _path: small)
    monkeypatch.setattr(ap.os, "fstat", lambda _fd: small)
    with pytest.raises(ap.PassportError, match="passport_size"):
        ap.verify_artifact_passport(passport_path, copied_mp4, KEY)


def test_verify_passport_refuses_an_artifact_whose_hash_read_fails(
    tmp_path: Path, copied_mp4: Path, monkeypatch: Any
) -> None:
    """The digest read runs on the single open descriptor (``os.pread``); a
    read that fails there is a typed refusal - never a verified passport."""
    passport_path = _write(tmp_path / "storyboard.passport.json", _passport(copied_mp4))

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("gone")

    monkeypatch.setattr(ap.os, "pread", _raise)
    with pytest.raises(ap.PassportError, match="artifact_mismatch"):
        ap.verify_artifact_passport(passport_path, copied_mp4, KEY)


def test_verify_passport_refuses_an_artifact_that_changed_during_hashing(
    tmp_path: Path, copied_mp4: Path, monkeypatch: Any
) -> None:
    """A file whose hashed length drifts from its ``fstat`` size changed while
    it was being hashed through the descriptor - a typed refusal."""
    passport_path = _write(tmp_path / "storyboard.passport.json", _passport(copied_mp4))
    real_pread = ap.os.pread
    truncated_at = copied_mp4.stat().st_size - 1

    def _shrinking_pread(fd: int, count: int, offset: int) -> bytes:
        data = real_pread(fd, count, offset)
        return data[: max(0, truncated_at - offset)]  # "shrank" mid-hash

    monkeypatch.setattr(ap.os, "pread", _shrinking_pread)
    with pytest.raises(ap.PassportError, match="artifact changed during verification"):
        ap.verify_artifact_passport(passport_path, copied_mp4, KEY)


def test_atomic_roundtrip_probe_reports_oserror(tmp_path: Path, monkeypatch: Any) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("tempdir refused")

    monkeypatch.setattr(studio_cli.tempfile, "mkstemp", _boom)
    assert studio_cli._atomic_roundtrip(tmp_path) == (False, "OSError")


def test_compiler_probe_reports_a_compiler_refusal(monkeypatch: Any) -> None:
    from nexus_ai_agent.creative import storyboard_compiler

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("parser exploded")

    monkeypatch.setattr(storyboard_compiler, "compile_storyboard", _boom)
    assert studio_cli._compiler_ok() == (False, "ValueError")


def test_cli_requires_a_principal_when_an_approval_is_named(tmp_path: Path) -> None:
    from nexus_ai_agent.nagar.ibpc.approval import ApprovalLedger

    ledger = ApprovalLedger(now=lambda: "2026-11-18T00:00:00Z")
    buffer = io.StringIO()
    code = studio_cli.run_studio_cli(
        [
            "render",
            "--script",
            "INT. ROOM - DAY\nA: Hi.\n",
            "--output-dir",
            str(tmp_path / "out"),
            "--cas-dir",
            str(tmp_path / "cas"),
            "--signing-key",
            KEY,
            "--approval-id",
            "approval-1",
        ],
        ledger=ledger,
        out=buffer,
    )
    payload = json.loads(buffer.getvalue())
    assert (code, payload["status"]) == (2, "refused")
    assert "requires --principal-id" in payload["reason"]


def test_restore_from_cas_refuses_staged_bytes_that_do_not_match(tmp_path: Path) -> None:
    """A CAS body that does not hash to the indexed content is refused, not used."""
    target = tmp_path / "staged.mp4"

    class _LyingCAS:
        def verify_segment_artifact(self, *_args: Any, **_kwargs: Any) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(content_sha="a" * 64, body_size=4, mode="ffmpeg_subprocess_pipe")

        def stream_segment_body(self, _key: str, sink: Any, **_kwargs: Any) -> Any:
            sink.write(b"body")
            return None

    with pytest.raises(bridge.StudioBridgeError, match="cas_staged_mismatch"):
        bridge._restore_segment_from_cas(
            cas=_LyingCAS(),  # type: ignore[arg-type]
            cas_key="c" * 64,
            expected_content_sha="a" * 64,
            target=target,
            pipeline_digest="d" * 64,
            meter=bridge._MemoryMeter(1024 * 1024),
        )
    assert not target.exists()


def test_restore_from_cas_refuses_an_index_that_disagrees_with_the_cas(tmp_path: Path) -> None:
    from types import SimpleNamespace

    class _DisagreeingCAS:
        def verify_segment_artifact(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(content_sha="b" * 64, body_size=4, mode="ffmpeg_subprocess_pipe")

    with pytest.raises(bridge.StudioBridgeError, match="cas_content_mismatch"):
        bridge._restore_segment_from_cas(
            cas=_DisagreeingCAS(),  # type: ignore[arg-type]
            cas_key="c" * 64,
            expected_content_sha="a" * 64,
            target=tmp_path / "staged.mp4",
            pipeline_digest="d" * 64,
            meter=bridge._MemoryMeter(1024 * 1024),
        )


@needs_media
def test_render_accepts_a_precompiled_manifest(tmp_path: Path) -> None:
    """A caller that already compiled (and approved) must not pay for a second compile."""
    compiled = compile_storyboard(SCRIPT)
    result = bridge.render_storyboard_with_segment_cas(
        SCRIPT,
        "precompiled",
        tmp_path / "out",
        tmp_path / "cas",
        "job",
        signing_key=KEY,
        manifest=compiled,
        **FAST,
    )
    assert json.loads(result.manifest_path.read_text())["manifest_sha256"] == (
        compiled.manifest_sha256
    )
    assert result.verification.status == av.PASS


@needs_media
def test_render_refuses_a_cached_segment_that_no_longer_verifies(
    tmp_path: Path, monkeypatch: Any
) -> None:
    bridge.render_storyboard_with_segment_cas(
        SCRIPT, "t", tmp_path / "out", tmp_path / "cas", "job", signing_key=KEY, **FAST
    )
    monkeypatch.setattr(
        bridge,
        "verify_mp4",
        lambda *_a, **_k: av.Mp4Verdict(status=av.VIOLATED, reasons=("drift",)),
    )
    with pytest.raises(bridge.StudioBridgeError, match="cached_segment_invalid"):
        bridge.render_storyboard_with_segment_cas(
            SCRIPT, "t", tmp_path / "out2", tmp_path / "cas", "job", signing_key=KEY, **FAST
        )


@needs_media
def test_render_refuses_a_final_film_that_fails_verification(
    tmp_path: Path, monkeypatch: Any
) -> None:
    real_verify = bridge.verify_mp4

    def _verify(path: Any, **kwargs: Any) -> av.Mp4Verdict:
        if ".render.tmp" in str(path):
            return av.Mp4Verdict(status=av.VIOLATED, reasons=("concat_drift",))
        return real_verify(path, **kwargs)

    monkeypatch.setattr(bridge, "verify_mp4", _verify)
    with pytest.raises(bridge.StudioBridgeError, match="verifier_failed"):
        bridge.render_storyboard_with_segment_cas(
            SCRIPT, "t", tmp_path / "out", tmp_path / "cas", "job", signing_key=KEY, **FAST
        )


@needs_media
def test_render_refuses_a_film_that_changed_after_verification(
    tmp_path: Path, monkeypatch: Any
) -> None:
    real_stream = bridge.stream_sha256

    def _stream(path: Any) -> Any:
        digest, size, inode = real_stream(path)
        if Path(path).name == "render.mp4":
            return ("f" * 64, size, inode)
        return digest, size, inode

    monkeypatch.setattr(bridge, "stream_sha256", _stream)
    with pytest.raises(bridge.StudioBridgeError, match="artifact_changed_after_verify"):
        bridge.render_storyboard_with_segment_cas(
            SCRIPT, "t", tmp_path / "out", tmp_path / "cas", "job", signing_key=KEY, **FAST
        )


def test_vault_head_resolution_refuses_an_ambiguous_head(monkeypatch: Any) -> None:
    class _Driver:
        def list_manifests(self) -> list[str]:
            return ["snap-a", "snap-b", "snap-other"]

        def get_manifest(self, snapshot_id: str) -> bytes:
            project = "other-project" if snapshot_id == "snap-other" else "proj"
            return json.dumps({"project_id": project, "parent_snapshot_id": None}).encode()

    class _Manifest:
        def __init__(self, project_id: str, parent_snapshot_id: str | None) -> None:
            self.project_id = project_id
            self.parent_snapshot_id = parent_snapshot_id

        @staticmethod
        def from_json_bytes(raw: bytes) -> Any:
            payload = json.loads(raw)
            return _Manifest(payload["project_id"], payload["parent_snapshot_id"])

    monkeypatch.setattr(bridge, "VaultSnapshotManifest", _Manifest)
    with pytest.raises(bridge.StudioBridgeError, match="vault_ambiguous_head"):
        bridge._current_vault_head(_Driver(), "proj")
    assert bridge._current_vault_head(_Driver(), "other-project") == "snap-other"


# --------------------------------------------------------------------------- #
# Audit findings A-01 / A-04 / A-05 / A-08
# --------------------------------------------------------------------------- #


def _mp4_box(kind: str, payload: bytes, *, largesize: bool = False, size: int = 0) -> bytes:
    """Build one ISO-BMFF box; ``size`` forces a declared size (0 means "to EOF")."""
    header = b"\x00\x00\x00\x01" + kind.encode("latin-1") if largesize else b""
    if largesize:
        return header + (16 + len(payload)).to_bytes(8, "big") + payload
    declared = 8 + len(payload) if size == 0 else size
    return declared.to_bytes(4, "big") + kind.encode("latin-1") + payload


def _mdhd_payload(timescale: int, *, mdhd_version: int = 0) -> bytes:
    """Build a standards-conformant ``mdhd`` payload (20 bytes for v0, 32 for v1)."""
    if mdhd_version == 1:
        return (
            b"\x01\x00\x00\x00"
            + b"\x00" * 8
            + b"\x00" * 8
            + timescale.to_bytes(4, "big")
            + b"\x00" * 8
        )
    return b"\x00\x00\x00\x00" + b"\x00" * 8 + timescale.to_bytes(4, "big") + b"\x00" * 4


def _sample_entry(codec: str, *, body: bytes | None = None) -> bytes:
    """Build one ``stsd`` entry: size, format, then the 28-byte AudioSampleEntry body."""
    if body is None:
        # 6 reserved + data_reference_index(2) + 8 reserved + channelcount(2)
        # + samplesize(2) + pre_defined/reserved(4) + samplerate(16.16 fixed).
        body = (
            b"\x00" * 6
            + (1).to_bytes(2, "big")
            + b"\x00" * 8
            + (2).to_bytes(2, "big")
            + (16).to_bytes(2, "big")
            + b"\x00" * 4
            + (16_000 << 16).to_bytes(4, "big")
        )
    return (8 + len(body)).to_bytes(4, "big") + codec.encode("latin-1") + body


def _trak(
    handler: str,
    codec: str,
    timescale: int,
    *,
    mdhd_version: int = 0,
    body: bytes | None = None,
) -> bytes:
    """Build a ``trak`` whose ``mdia`` declares one handler, one codec, one timescale."""
    mdhd_payload = _mdhd_payload(timescale, mdhd_version=mdhd_version)
    stsd_entry = _sample_entry(codec, body=body)
    stsd_payload = b"\x00\x00\x00\x00" + (1).to_bytes(4, "big") + stsd_entry
    hdlr_payload = b"\x00\x00\x00\x00" + b"\x00" * 4 + handler.encode("latin-1") + b"\x00" * 8
    mdia = (
        _mp4_box("hdlr", hdlr_payload)
        + _mp4_box("mdhd", mdhd_payload)
        + _mp4_box("minf", _mp4_box("stbl", _mp4_box("stsd", stsd_payload)))
    )
    return _mp4_box("trak", _mp4_box("mdia", mdia))


def test_moov_stream_table_reads_every_track_shape(tmp_path: Path) -> None:
    """A-08: the container's own stream table is parsed, never assumed."""
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(
        _mp4_box("ftyp", b"isom")
        + _mp4_box(
            "moov",
            _mp4_box("free", b"")
            + _trak("vide", "avc1", 10_240)
            + _trak("soun", "mp4a", 16_000, mdhd_version=1)
            + _trak("soun", "mp4a", 0),
        )
        + _mp4_box("mdat", b"\x00" * 8)
    )
    tracks, problems = av.read_moov_tracks(path)
    assert problems == []
    assert [(t.handler, t.codec, t.timescale) for t in tracks] == [
        ("vide", "avc1", 10_240),
        ("soun", "mp4a", 16_000),
        ("soun", "mp4a", 0),
    ]
    assert tracks[0].is_video is True
    assert tracks[1].is_aac_audio is True
    # A zero timescale is not a playable audio track, so it never satisfies A-08.
    assert tracks[2].is_aac_audio is False


def test_moov_stream_table_reports_layout_gaps(tmp_path: Path) -> None:
    """Unparsable metadata is a typed refusal, never a silent pass."""
    no_moov = tmp_path / "no-moov.mp4"
    no_moov.write_bytes(_mp4_box("ftyp", b"isom") + _mp4_box("mdat", b"\x00" * 8))
    assert av.read_moov_tracks(no_moov) == ((), [])

    empty_moov = tmp_path / "empty-moov.mp4"
    empty_moov.write_bytes(
        _mp4_box("ftyp", b"isom") + _mp4_box("moov", b"") + _mp4_box("mdat", b"")
    )
    assert av.read_moov_tracks(empty_moov) == ((), ["moov payload size 0 out of range"])

    trackless = tmp_path / "trackless-moov.mp4"
    trackless.write_bytes(
        _mp4_box("ftyp", b"isom") + _mp4_box("moov", _mp4_box("free", b"")) + _mp4_box("mdat", b"")
    )
    assert av.read_moov_tracks(trackless) == ((), ["moov declares no trak box"])

    oversized = tmp_path / "oversized-moov.mp4"
    oversized.write_bytes(
        _mp4_box("ftyp", b"isom") + _mp4_box("moov", b"\x00" * 64) + _mp4_box("mdat", b"")
    )
    monkeypatched = av._MAX_MOOV_BYTES
    try:
        av._MAX_MOOV_BYTES = 8
        assert av.read_moov_tracks(oversized)[1] == ["moov payload size 64 out of range"]
    finally:
        av._MAX_MOOV_BYTES = monkeypatched


def test_moov_stream_table_survives_largesize_and_open_ended_boxes(tmp_path: Path) -> None:
    """A ``largesize`` trak and an inner box that does not fit are handled, not trusted."""
    path = tmp_path / "largesize.mp4"
    bad_inner = b"\x00\x00\x00\x02free"  # declares 2 bytes: smaller than its header
    path.write_bytes(
        _mp4_box("ftyp", b"isom")
        + _mp4_box(
            "moov",
            _trak("vide", "avc1", 10_240, mdhd_version=1),
            largesize=True,
        )
        + _mp4_box("mdat", bad_inner, size=0)
    )
    tracks, problems = av.read_moov_tracks(path)
    assert problems == []
    assert [(t.handler, t.codec) for t in tracks] == [("vide", "avc1")]
    # The malformed inner box simply cannot declare a stream: no audio, no crash.
    verdict = av.verify_mp4(path, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status != av.PASS
    assert "missing_audio_stream" in verdict.reasons


def test_moov_stream_table_refuses_a_malformed_stsd(tmp_path: Path) -> None:
    """A ``stsd`` too short to hold a sample entry yields no codec, never a guess."""
    short_stsd = _mp4_box("stsd", b"\x00\x00\x00\x00\x00\x00\x00\x01")
    mdia = _mp4_box("hdlr", b"\x00" * 8 + b"soun" + b"\x00" * 8) + _mp4_box(
        "minf", _mp4_box("stbl", short_stsd)
    )
    path = tmp_path / "short-stsd.mp4"
    path.write_bytes(
        _mp4_box("ftyp", b"isom")
        + _mp4_box("moov", _mp4_box("trak", _mp4_box("mdia", mdia)))
        + _mp4_box("mdat", b"")
    )
    tracks, problems = av.read_moov_tracks(path)
    assert problems == ["malformed_moov_structure: stsd declares 1 entries but holds none"]
    assert [(t.handler, t.codec, t.timescale) for t in tracks] == [("soun", None, 0)]
    assert tracks[0].is_aac_audio is False


@pytest.fixture(scope="module")
def silent_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A genuine H.264 MP4 with no audio track at all (the A-08 failure mode)."""
    out = tmp_path_factory.mktemp("silent") / "silent.mp4"
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x90:rate=10:duration=1",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(out),
            ],
            check=True,
            timeout=120,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment gate
        pytest.skip("real ffmpeg is required for the audio-contract tests")
    return out


def test_verify_mp4_refuses_a_missing_audio_stream(silent_mp4: Path, real_mp4: Path) -> None:
    """A-08: a silent render is VIOLATED unless the caller explicitly allows silence."""
    verdict = av.verify_mp4(silent_mp4, expected_duration_s=1.0, tolerance_s=0.3)
    assert verdict.status == av.VIOLATED
    assert verdict.reasons == ("missing_audio_stream",)
    assert verdict.has_audio_stream is False
    assert verdict.audio_codec is None
    assert verdict.video_codec == "avc1"

    relaxed = av.verify_mp4(
        silent_mp4, expected_duration_s=1.0, tolerance_s=0.3, require_audio=False
    )
    assert relaxed.status == av.PASS
    assert relaxed.has_audio_stream is False
    assert relaxed.reasons == ()

    with_audio = av.verify_mp4(real_mp4, expected_duration_s=1.0, tolerance_s=0.3)
    assert with_audio.status == av.PASS
    assert with_audio.has_audio_stream is True
    assert with_audio.audio_codec == "mp4a"


def test_verify_mp4_reports_moov_layout_problems(tmp_path: Path) -> None:
    """A ``moov`` without a single usable ``trak`` is a violation, not an observation."""
    trackless = tmp_path / "no-trak.mp4"
    trackless.write_bytes(
        _mp4_box("ftyp", b"isom")
        + _mp4_box("moov", _mp4_box("free", b""))
        + _mp4_box("mdat", b"\x00")
    )
    verdict = av.verify_mp4(trackless, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED
    assert verdict.reasons == ("moov_layout: moov declares no trak box",)

    empty_moov = tmp_path / "empty-moov.mp4"
    empty_moov.write_bytes(
        _mp4_box("ftyp", b"isom") + _mp4_box("moov", b"") + _mp4_box("mdat", b"\x00")
    )
    verdict = av.verify_mp4(empty_moov, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED
    assert verdict.reasons == ("moov_layout: moov payload size 0 out of range",)


def test_verify_mp4_refuses_a_video_free_container(tmp_path: Path) -> None:
    """A container with only an audio track is violated before ffprobe is asked."""
    path = tmp_path / "audio-only.mp4"
    path.write_bytes(
        _mp4_box("ftyp", b"isom")
        + _mp4_box("moov", _trak("soun", "mp4a", 16_000))
        + _mp4_box("mdat", b"\x00")
    )
    verdict = av.verify_mp4(path, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED
    assert verdict.reasons == ("no_video_stream",)
    assert verdict.has_video is False
    assert verdict.has_audio_stream is True


def test_empty_shot_tree_refuses_a_stray_inclusion_proof() -> None:
    """A-04: zero leaves means zero proofs - a proof for the empty tree is refused."""
    empty_root = mz.EMPTY_MERKLE_ROOT
    assert mz.verify_shot_inclusion_proofs(shot_hashes=(), merkle_root=empty_root, proofs=()) == ()
    assert mz.verify_shot_inclusion_proofs(
        shot_hashes=(), merkle_root=empty_root, proofs=[{"unexpected": "proof"}]
    ) == ("shot_inclusion_proof_count_mismatch",)
    assert mz.verify_shot_inclusion_proofs(
        shot_hashes=(), merkle_root=empty_root, proofs=[{"schema": mz.MERKLE_PROOF_SCHEMA}]
    ) == ("shot_inclusion_proof_count_mismatch",)


def test_proof_schema_tag_is_mandatory() -> None:
    """A-05: a proof must declare ``MERKLE_PROOF_SCHEMA`` or it is not a proof."""
    hashes = _shot_hashes(3)
    payloads = mz.shot_leaf_payloads(hashes)
    root = mz.merkle_root_sha256(payloads)
    proof = mz.merkle_inclusion_proof_record(payloads, 1)
    assert proof["schema"] == mz.MERKLE_PROOF_SCHEMA
    assert (
        mz.verify_shot_inclusion_proofs(
            shot_hashes=hashes,
            merkle_root=root,
            proofs=[mz.merkle_inclusion_proof_record(payloads, i) for i in range(3)],
        )
        == ()
    )

    for poisoned in (
        {k: v for k, v in proof.items() if k != "schema"},
        dict(proof, schema="some-other-protocol-v1"),
        dict(proof, schema=None),
    ):
        proofs = [
            mz.merkle_inclusion_proof_record(payloads, index) if index != 1 else poisoned
            for index in range(3)
        ]
        assert mz.verify_shot_inclusion_proofs(
            shot_hashes=hashes, merkle_root=root, proofs=proofs
        ) == ("shot_inclusion_proof_invalid",)


def test_build_passport_emits_version_two(copied_mp4: Path) -> None:
    """A-01: new passports are version 2 and always carry the sealed shot tree."""
    passport = _passport(copied_mp4)
    assert passport["passport_version"] == 2
    assert ap.PASSPORT_VERSION == 2
    assert ap.SUPPORTED_PASSPORT_VERSIONS == (1, 2)
    assert set(ap.REQUIRED_FIELDS) - set(ap.REQUIRED_V1_FIELDS) == set(ap.SHOT_MERKLE_FIELDS)
    assert ap.REQUIRED_FIELDS == ap.REQUIRED_V1_FIELDS | frozenset(ap.SHOT_MERKLE_FIELDS)


def _legacy_v1_passport(mp4: Path, **overrides: Any) -> dict[str, Any]:
    """A passport exactly as version 1 wrote it: no Merkle field at all."""
    base = _passport(mp4)
    legacy = {name: base[name] for name in ap.PASSPORT_V1_FIELDS if name in base}
    legacy["passport_version"] = 1
    legacy.update(overrides)
    return _sealed(legacy)


def test_version_one_passports_still_verify(copied_mp4: Path, tmp_path: Path) -> None:
    """A-01: legacy passports on disk keep verifying - no forced migration."""
    path = _write(tmp_path / "storyboard.passport.json", _legacy_v1_passport(copied_mp4))
    check = ap.verify_artifact_passport(path, copied_mp4, KEY)
    assert check.passport["passport_version"] == 1
    assert "shot_inclusion_proofs" not in check.passport

    with pytest.raises(ap.PassportError, match="passport_version_unsupported"):
        ap.verify_artifact_passport(path, copied_mp4, KEY, min_version=2)
    assert (
        ap.verify_artifact_passport(path, copied_mp4, KEY, min_version=1).passport[
            "passport_version"
        ]
        == 1
    )


def test_version_two_passports_satisfy_a_min_version_of_two(
    copied_mp4: Path, tmp_path: Path
) -> None:
    path = _write(
        tmp_path / "storyboard.passport.json",
        _passport(copied_mp4, shot_hashes=list(_shot_hashes(2))),
    )
    check = ap.verify_artifact_passport(path, copied_mp4, KEY, min_version=2)
    assert check.passport["passport_version"] == 2
    assert len(check.passport["shot_inclusion_proofs"]) == 2


@pytest.mark.parametrize("bad", [0, -1, True, "1", 1.0])
def test_verify_passport_refuses_an_invalid_min_version(copied_mp4: Path, bad: object) -> None:
    path = _write(Path(str(copied_mp4) + ".passport.json"), _legacy_v1_passport(copied_mp4))
    with pytest.raises(ap.PassportError, match="invalid_min_version"):
        ap.verify_artifact_passport(path, copied_mp4, KEY, min_version=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("version", [0, 3, "2", None, 2.0, True])
def test_verify_passport_refuses_an_unsupported_version(
    copied_mp4: Path, tmp_path: Path, version: object
) -> None:
    data = _passport(copied_mp4)
    data["passport_version"] = version
    path = _write(tmp_path / "storyboard.passport.json", _sealed(data))
    with pytest.raises(ap.PassportError, match="passport_version_unsupported"):
        ap.verify_artifact_passport(path, copied_mp4, KEY)


def test_version_one_passport_with_partial_merkle_material_is_still_strict(
    copied_mp4: Path, tmp_path: Path
) -> None:
    """A-01: a version 1 passport that *does* carry Merkle fields is fully validated."""
    hashes = list(_shot_hashes(2))
    data = _passport(copied_mp4, shot_hashes=hashes)

    good = _sealed(dict(data, passport_version=1))
    path = _write(tmp_path / "good.passport.json", good)
    assert ap.verify_artifact_passport(path, copied_mp4, KEY).passport["passport_version"] == 1

    broken_root = _sealed(dict(data, passport_version=1, shot_merkle_root="f" * 64))
    with pytest.raises(ap.PassportError, match="shot_merkle_root_mismatch"):
        ap.verify_artifact_passport(
            _write(tmp_path / "broken.passport.json", broken_root), copied_mp4, KEY
        )

    short = _sealed(
        dict(data, passport_version=1, shot_inclusion_proofs=data["shot_inclusion_proofs"][:1])
    )
    with pytest.raises(ap.PassportError, match="shot_inclusion_proof_count_mismatch"):
        ap.verify_artifact_passport(
            _write(tmp_path / "short.passport.json", short), copied_mp4, KEY
        )

    # A version 1 passport that carries only *one* of the three fields is refused:
    # the tree is either present in full or absent entirely.
    lone_root = {name: data[name] for name in ap.PASSPORT_V1_FIELDS if name in data}
    lone_root["passport_version"] = 1
    lone_root["shot_merkle_root"] = data["shot_merkle_root"]
    with pytest.raises(ap.PassportError, match="shot_inclusion_proof_invalid"):
        ap.verify_artifact_passport(
            _write(tmp_path / "lone.passport.json", _sealed(lone_root)), copied_mp4, KEY
        )


def test_version_two_passport_missing_a_merkle_field_is_malformed(
    copied_mp4: Path, tmp_path: Path
) -> None:
    data = _passport(copied_mp4, shot_hashes=list(_shot_hashes(2)))
    for field in ap.SHOT_MERKLE_FIELDS:
        partial = {k: v for k, v in data.items() if k != field}
        partial["hmac_sha256"] = ap._seal(partial, KEY.encode())
        with pytest.raises(ap.PassportError, match="passport_malformed"):
            ap.verify_artifact_passport(
                _write(tmp_path / f"missing-{field}.json", partial), copied_mp4, KEY
            )


def test_moov_parser_handles_every_nested_box_irregularity(tmp_path: Path) -> None:
    """Nested ``largesize``/open-ended/overrunning boxes are parsed or ignored, never trusted."""
    largesize_free = b"\x00\x00\x00\x01" + b"free" + (24).to_bytes(8, "big") + b"\x00" * 8
    open_ended_free = b"\x00\x00\x00\x00" + b"free" + b"\x00" * 4
    overrun_free = b"\x00\x00\x00\x04" + b"free"
    short_mdhd = _mp4_box("mdhd", b"\x00\x00\x00\x00" + b"\x00" * 8)
    stbl_without_stsd = _mp4_box("minf", _mp4_box("stbl", _mp4_box("free", b"")))
    hdlr = _mp4_box("hdlr", b"\x00" * 8 + b"soun" + b"\x00" * 8)

    # trak A: a too-short mdhd, a minf without stsd, then a large and an open-ended box.
    trak_a = _mp4_box(
        "trak",
        _mp4_box("mdia", hdlr + short_mdhd + stbl_without_stsd + largesize_free + open_ended_free),
    )
    # trak B: an inner box that claims to be smaller than its own header ends the walk.
    trak_b = _mp4_box("trak", _mp4_box("mdia", hdlr + overrun_free))
    # trak C: a 64-bit size whose ``largesize`` field does not fit in the buffer.
    trak_c = _mp4_box("trak", _mp4_box("mdia", hdlr + b"\x00\x00\x00\x01free"))

    path = tmp_path / "irregular.mp4"
    path.write_bytes(
        _mp4_box("ftyp", b"isom")
        + _mp4_box("moov", trak_a + trak_b + trak_c)
        + _mp4_box("mdat", b"")
    )
    tracks, problems = av.read_moov_tracks(path)
    # A-08: every one of the three defects is reported, nothing is silently skipped.
    assert [problem.split(":", 1)[0] for problem in problems] == [av.MALFORMED_MOOV] * 3
    assert "mdhd v0 is 12 bytes, needs 20" in problems[0]
    assert "declares 4 bytes" in problems[1]
    assert "largesize at" in problems[2] and "truncated" in problems[2]
    assert [(t.handler, t.codec, t.timescale) for t in tracks] == [
        ("soun", None, 0),
        ("soun", None, 0),
        ("soun", None, 0),
    ]
    assert all(track.is_aac_audio is False for track in tracks)


# --------------------------------------------------------------------------- #
# Audit findings A-02 / A-03 / A-06 / A-07 / A-09 / A-10 / A-11
# --------------------------------------------------------------------------- #


def test_prepare_directory_creates_a_missing_chain_without_following_links(
    tmp_path: Path,
) -> None:
    """A-02: each component is created individually, then proven by descriptor."""
    deep = tmp_path / "a" / "b" / "c"
    assert bridge._prepare_directory(deep, label="out") == deep.resolve()
    assert deep.is_dir()

    # An existing symlink anywhere on the chain is refused before anything is made.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(bridge.StudioBridgeError, match="symlink_refused"):
        bridge._prepare_directory(link / "inner", label="out")
    assert not (elsewhere / "inner").exists()

    # A component that is a regular file cannot be a parent.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(bridge.StudioBridgeError, match="not_a_directory"):
        bridge._prepare_directory(blocker / "inner", label="out")
    with pytest.raises(bridge.StudioBridgeError, match="not_a_directory"):
        bridge._prepare_directory(blocker, label="out")


def test_prepare_directory_detects_a_directory_swapped_after_creation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A-02: the descriptor and ``lstat`` must agree on dev/inode, or nothing is returned."""
    target = tmp_path / "raced"
    real_fstat = os.fstat

    def _fstat(fd: int) -> Any:
        fake = real_fstat(fd)
        return os.stat_result(
            (
                fake.st_mode,
                fake.st_ino + 1,  # a different inode than the path reports
                fake.st_dev,
                fake.st_nlink,
                fake.st_uid,
                fake.st_gid,
                fake.st_size,
                fake.st_atime,
                fake.st_mtime,
                fake.st_ctime,
            )
        )

    monkeypatch.setattr(bridge.os, "fstat", _fstat)
    with pytest.raises(bridge.StudioBridgeError, match="directory_identity_mismatch"):
        bridge._prepare_directory(target, label="out")


def test_prepare_directory_never_writes_outside_a_swapped_parent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A-02: the parent is renamed and replaced by a symlink *between* components.

    The create of ``b`` must land inside the descriptor of the real ``a`` inode,
    never inside the symlink target: this is the exact attack the multi-component
    ``os.mkdir("/workspace/a/b")`` was vulnerable to.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    target = root / "a" / "b"
    real_descend = bridge._descend_or_create
    swapped = {"done": False}

    def _racing_descend(parent_fd: int, name: str, *, label: str) -> int:
        child_fd = real_descend(parent_fd, name, label=label)
        if name == "a" and not swapped["done"]:
            swapped["done"] = True
            (root / "a").rename(root / "a-real")
            (root / "a").symlink_to(outside, target_is_directory=True)
        return child_fd

    monkeypatch.setattr(bridge, "_descend_or_create", _racing_descend)
    with pytest.raises(bridge.StudioBridgeError):
        bridge._prepare_directory(target, label="out")

    # 1) nothing at all was created in the attacker's directory
    assert list(outside.iterdir()) == []
    # 2) the child was created inside the genuine inode we held open
    assert (root / "a-real" / "b").is_dir()
    assert swapped["done"] is True


def test_descend_or_create_accepts_an_existing_directory(tmp_path: Path) -> None:
    """A-02: an existing component is opened and proven, not re-created."""
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "child").mkdir()
    parent_fd = os.open(parent, bridge._DIR_OPEN_FLAGS)
    try:
        child_fd = bridge._descend_or_create(parent_fd, "child", label="child")
        try:
            assert stat.S_ISDIR(os.fstat(child_fd).st_mode)
        finally:
            os.close(child_fd)
    finally:
        os.close(parent_fd)


@pytest.mark.parametrize(
    "prepare,reason",
    [
        (lambda parent: (parent / "child").write_text("x", encoding="utf-8"), "not_a_directory"),
        (
            lambda parent: (parent / "child").symlink_to(parent, target_is_directory=True),
            "symlink_refused",
        ),
    ],
)
def test_descend_or_create_refuses_a_non_directory_component(
    tmp_path: Path, prepare: Any, reason: str
) -> None:
    """A-02: a file or a link already sitting on the component is refused."""
    parent = tmp_path / "parent"
    parent.mkdir()
    prepare(parent)
    parent_fd = os.open(parent, bridge._DIR_OPEN_FLAGS)
    try:
        with pytest.raises(bridge.StudioBridgeError, match=reason):
            bridge._descend_or_create(parent_fd, "child", label="child")
    finally:
        os.close(parent_fd)


def test_descend_or_create_reports_a_failing_mkdir_and_open(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A-02: a refused ``mkdir`` and an unopenable component are typed refusals."""
    parent = tmp_path / "parent"
    parent.mkdir()
    parent_fd = os.open(parent, bridge._DIR_OPEN_FLAGS)

    def _mkdir_refused(_name: str, _mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        raise PermissionError("mkdir refused")

    try:
        with monkeypatch.context() as patched:
            patched.setattr(bridge.os, "mkdir", _mkdir_refused)
            with pytest.raises(bridge.StudioBridgeError, match="not_a_directory"):
                bridge._descend_or_create(parent_fd, "child", label="child")

        # The component vanished between the create attempt and the open.
        def _mkdir_noop(_name: str, _mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            return None

        with monkeypatch.context() as patched:
            patched.setattr(bridge.os, "mkdir", _mkdir_noop)
            with pytest.raises(bridge.StudioBridgeError, match="not_a_directory"):
                bridge._descend_or_create(parent_fd, "gone", label="child")

        # ``lstat`` cannot see the component that ``mkdir`` reported as existing.
        def _mkdir_exists(_name: str, _mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            raise FileExistsError(_name)

        with monkeypatch.context() as patched:
            patched.setattr(bridge.os, "mkdir", _mkdir_exists)
            with pytest.raises(bridge.StudioBridgeError, match="not_a_directory"):
                bridge._descend_or_create(parent_fd, "ghost", label="child")
    finally:
        os.close(parent_fd)


def test_descend_or_create_refuses_a_descriptor_that_is_not_a_directory(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A-02: even a descriptor that opened must be a directory, and is closed on refusal."""
    parent = tmp_path / "parent"
    parent.mkdir()
    decoy = parent / "decoy"
    decoy.write_text("x", encoding="utf-8")
    parent_fd = os.open(parent, bridge._DIR_OPEN_FLAGS)
    captured: list[int] = []

    def _mkdir_noop(_name: str, _mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        return None

    def _open_decoy(_name: str, *, parent_fd: int | None = None, label: str = "") -> int:
        fd = os.open(decoy, os.O_RDONLY)
        captured.append(fd)
        return fd

    try:
        with monkeypatch.context() as patched:
            patched.setattr(bridge.os, "mkdir", _mkdir_noop)
            patched.setattr(bridge, "_open_directory", _open_decoy)
            with pytest.raises(bridge.StudioBridgeError, match="not_a_directory"):
                bridge._descend_or_create(parent_fd, "child", label="child")
    finally:
        os.close(parent_fd)
    with pytest.raises(OSError):
        os.fstat(captured[0])  # the refused descriptor was closed, not leaked


def test_prepare_directory_refuses_a_descriptor_that_is_not_a_directory(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A-02: the opened descriptor itself must report a directory."""
    real_fstat = os.fstat

    def _fstat(fd: int) -> Any:
        fake = real_fstat(fd)
        return os.stat_result(
            (
                stat.S_IFREG | 0o600,
                fake.st_ino,
                fake.st_dev,
                fake.st_nlink,
                fake.st_uid,
                fake.st_gid,
                fake.st_size,
                fake.st_atime,
                fake.st_mtime,
                fake.st_ctime,
            )
        )

    monkeypatch.setattr(bridge.os, "fstat", _fstat)
    with pytest.raises(bridge.StudioBridgeError, match="not_a_directory"):
        bridge._prepare_directory(tmp_path / "reg", label="out")


def test_prepare_directory_detects_a_final_identity_mismatch(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A-02: the path must still name the inode we created once the walk is done."""
    target = tmp_path / "created" / "deep"
    bridge._prepare_directory(tmp_path / "created", label="out")
    real_lstat = os.lstat

    def _lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
        fake = real_lstat(path, *args, **kwargs)
        if Path(path) == target:  # only the *final* proof sees a different inode
            return os.stat_result(
                (
                    fake.st_mode,
                    fake.st_ino + 1,
                    fake.st_dev,
                    fake.st_nlink,
                    fake.st_uid,
                    fake.st_gid,
                    fake.st_size,
                    fake.st_atime,
                    fake.st_mtime,
                    fake.st_ctime,
                )
            )
        return fake

    monkeypatch.setattr(bridge.os, "lstat", _lstat)
    with pytest.raises(bridge.StudioBridgeError, match="directory_identity_mismatch"):
        bridge._prepare_directory(target, label="out")


def test_read_small_json_is_fd_bound_and_fail_closed(tmp_path: Path, monkeypatch: Any) -> None:
    """A-03: one descriptor, ``fstat`` on it, a hard cap and a metered buffer."""
    meter = bridge._MemoryMeter(1024 * 1024)
    target = tmp_path / "target.json"
    target.write_text('{"a": 1}', encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(bridge.StudioBridgeError, match="not_regular_file"):
        bridge._read_small_json(link, limit=1024, meter=meter)

    missing = tmp_path / "absent.json"
    with pytest.raises(bridge.StudioBridgeError, match="not_regular_file"):
        bridge._read_small_json(missing, limit=1024, meter=meter)

    # A file that is large before the cap is checked is refused by fstat.
    big = tmp_path / "big.json"
    big.write_bytes(b"{}" + b" " * 4096)
    with pytest.raises(bridge.StudioBridgeError, match="invalid_json_size"):
        bridge._read_small_json(big, limit=1024, meter=meter)

    # A file that grows after the fstat is refused by the running total.
    small = tmp_path / "grow.json"
    payload = b'{"a": ' + b"1" * 200 + b"}"
    small.write_bytes(payload)
    real_fstat = os.fstat

    def _fstat(fd: int) -> Any:
        fake = real_fstat(fd)
        return os.stat_result(
            (
                fake.st_mode,
                fake.st_ino,
                fake.st_dev,
                fake.st_nlink,
                fake.st_uid,
                fake.st_gid,
                8,  # claims to be tiny
                fake.st_atime,
                fake.st_mtime,
                fake.st_ctime,
            )
        )

    with monkeypatch.context() as patched:
        patched.setattr(bridge.os, "fstat", _fstat)
        with pytest.raises(bridge.StudioBridgeError, match="grew past"):
            bridge._read_small_json(small, limit=64, meter=meter)
    # The meter released everything it charged, even on the refusal path.
    assert meter.live_bytes == 0


def test_shot_index_reader_ignores_undecodable_bytes(tmp_path: Path) -> None:
    """A-03: a corrupt index entry is a cache miss, never an exception."""
    meter = bridge._MemoryMeter(1024 * 1024)
    key = "a" * 64
    path = tmp_path / f"{key}.json"
    path.write_bytes(b"\xff\xfe\x00")
    assert bridge._read_shot_index(path, segment_key_hash=key, meter=meter) is None


def test_vault_sync_refuses_a_manifest_that_disagrees_with_the_passport(tmp_path: Path) -> None:
    """A-06: the manifest digest is re-derived before anything is encrypted."""
    fake = _FakeRenderResult(tmp_path)
    stored = dict(fake.passport)
    object.__setattr__(fake._result, "passport", dict(stored, manifest_sha256="b" * 64))
    with pytest.raises(bridge.StudioBridgeError, match="manifest_sha256_mismatch"):
        bridge.sync_and_verify_studio_bundle_in_vault(
            render_result=fake,
            vault_storage_dir=tmp_path / "vault",
            vault_journal_db_path=tmp_path / "journal.jsonl",
            restore_dir=tmp_path / "restore",
            master_secret=b"secret",
            project_id="proj",
            signing_key=KEY,
        )


def test_vault_sync_refuses_a_tampered_restored_manifest(tmp_path: Path, monkeypatch: Any) -> None:
    """A-06: a vault that reconstitutes a different manifest must not pass as a backup."""
    from types import SimpleNamespace

    fake = _FakeRenderResult(tmp_path)
    sealed_manifest_sha = hashlib.sha256(fake.manifest_path.read_bytes()).hexdigest()

    class _TamperingManager:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def push_project_snapshot(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                snapshot_id="snap-x", uploaded_chunks=1, deduplicated_chunks=0, total_chunks=1
            )

        def pull_project_snapshot(self, *, target_dir: Path, **_kwargs: Any) -> Any:
            target = Path(target_dir)
            (target / "render.mp4").write_bytes(fake.output_mp4_path.read_bytes())
            (target / "job.manifest.json").write_text('{"tampered": true}', encoding="utf-8")
            (target / "render.passport.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(files={"render.mp4": b"x"})

    monkeypatch.setattr(bridge, "PersonalCloudVaultManager", _TamperingManager)
    monkeypatch.setattr(
        bridge,
        "verify_artifact_passport",
        lambda *_a, **_k: SimpleNamespace(passport={"manifest_sha256": sealed_manifest_sha}),
    )
    monkeypatch.setattr(bridge, "verify_mp4", lambda *_a, **_k: av.Mp4Verdict(status=av.PASS))
    with pytest.raises(bridge.StudioBridgeError, match="vault_restored_manifest_mismatch"):
        bridge.sync_and_verify_studio_bundle_in_vault(
            render_result=fake,
            vault_storage_dir=tmp_path / "vault",
            vault_journal_db_path=tmp_path / "journal.jsonl",
            restore_dir=tmp_path / "restore",
            master_secret=b"secret",
            project_id="proj",
            signing_key=KEY,
        )


def _stub_binary(path: Path, *, version: str, extra: str = "") -> Path:
    """Write an executable stub FFmpeg whose ``-version`` prints ``version``."""
    body = "#!/bin/sh\n" + (f"{extra}\n" if extra else "") + f"echo '{version}'\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_rss_telemetry_reports_process_and_children(monkeypatch: Any) -> None:
    """A-07: the three reported values are honest about what each one measures."""
    from types import SimpleNamespace

    def _rusage(who: int) -> Any:
        return SimpleNamespace(ru_maxrss=1000 if who == bridge.resource.RUSAGE_SELF else 500)

    monkeypatch.setattr(bridge.resource, "getrusage", _rusage)
    assert bridge._process_tree_rss_hwm() == (1000 * 1024, 500 * 1024, 1500 * 1024)
    # The compatibility accessor is the same combined estimate, not a new number.
    assert bridge._current_process_tree_peak_rss_bytes() == 1500 * 1024

    monkeypatch.setattr(bridge.sys, "platform", "darwin")
    assert bridge._process_tree_rss_hwm() == (1000, 500, 1500)
    assert bridge._current_process_tree_peak_rss_bytes() == 1500


def test_rss_telemetry_observes_a_real_child_process() -> None:
    """A-07: both high-water marks are real - neither is a constant or a guess."""
    before_self, before_children, _ = bridge._process_tree_rss_hwm()
    assert before_self > 0
    subprocess.run(
        [sys.executable, "-c", "x = bytearray(2 * 1024 * 1024)"],
        check=True,
        timeout=120,
        capture_output=True,
    )
    own, children, estimate = bridge._process_tree_rss_hwm()
    assert own > 0
    assert children > 0
    assert children >= before_children
    assert estimate == own + children


def test_rss_hwm_docstrings_say_it_is_an_estimate_not_simultaneous_rss() -> None:
    """A-07: the documentation may not present a sum of two peaks as one measurement."""
    for function in (
        bridge._process_tree_rss_hwm,
        bridge._current_process_tree_peak_rss_bytes,
    ):
        doc = (function.__doc__ or "").lower()
        assert "estimate" in doc
        assert "simultaneous" in doc


def test_ffmpeg_fingerprint_binds_version_and_binary(tmp_path: Path) -> None:
    """A-09: the toolchain identity is cached, stable and binds the binary's bytes."""
    bridge._cached_toolchain_fingerprint.cache_clear()
    real = bridge._ffmpeg_toolchain_fingerprint(_ffmpeg_bin())
    assert real == bridge._ffmpeg_toolchain_fingerprint(_ffmpeg_bin())
    assert len(real) == 64
    # The second call was served from the cache, keyed by the binary's stamp.
    assert bridge._cached_toolchain_fingerprint.cache_info().hits >= 1

    # A different binary path is a different fingerprint.
    twin = tmp_path / "ffmpeg-twin"
    twin.write_bytes(Path(_ffmpeg_bin()).read_bytes()[:4096])
    twin.chmod(0o755)
    assert bridge._ffmpeg_toolchain_fingerprint(str(twin)) != real


def test_binary_identity_records_the_real_file(tmp_path: Path) -> None:
    """A-09: the identity is the canonical path, a real digest and a positive size."""
    binary = _stub_binary(tmp_path / "ffmpeg", version="ffmpeg version 9.0.2")
    identity = bridge._binary_identity(str(binary))
    assert identity["resolved_path"] == str(binary.resolve(strict=True))
    assert len(identity["sha256"]) == 64
    int(identity["sha256"], 16)  # a real hex digest, never a placeholder word
    assert identity["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert identity["size_bytes"] == binary.stat().st_size > 0


def test_binary_identity_resolves_a_bare_command_name(tmp_path: Path) -> None:
    """A-09: a bare ``ffmpeg`` is found on ``PATH`` exactly as the pipeline would find it."""
    binary = _stub_binary(tmp_path / "ffmpeg", version="ffmpeg version 9.0.2")
    monkeypatch_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{tmp_path}{os.pathsep}{monkeypatch_path}"
    try:
        assert bridge._resolve_binary("ffmpeg") == binary.resolve(strict=True)
        named = bridge._ffmpeg_toolchain_fingerprint("ffmpeg")
        explicit = bridge._ffmpeg_toolchain_fingerprint(str(binary))
    finally:
        os.environ["PATH"] = monkeypatch_path
    # The same file reached two ways is one identity: the fingerprint is path-canonical.
    assert named == explicit


def test_binary_identity_follows_a_symlinked_binary(tmp_path: Path) -> None:
    """A-09: a symlink to the binary resolves to the same file, and so to one identity."""
    binary = _stub_binary(tmp_path / "ffmpeg-real", version="ffmpeg version 9.0.2")
    link = tmp_path / "ffmpeg-link"
    link.symlink_to(binary)
    assert bridge._resolve_binary(str(link)) == binary.resolve(strict=True)
    assert bridge._binary_identity(str(link)) == bridge._binary_identity(str(binary))
    assert bridge._ffmpeg_toolchain_fingerprint(str(link)) == bridge._ffmpeg_toolchain_fingerprint(
        str(binary)
    )


def test_binary_replaced_in_place_busts_the_fingerprint_cache(tmp_path: Path) -> None:
    """A-09: new bytes at the same path are a new fingerprint, even with an identical version."""
    binary = _stub_binary(tmp_path / "ffmpeg", version="ffmpeg version 9.0.2")
    bridge._cached_toolchain_fingerprint.cache_clear()
    before = bridge._ffmpeg_toolchain_fingerprint(str(binary))
    stamp_before = bridge._binary_stat_key(str(binary))

    # Same path, same ``-version`` output, different bytes: a rebuilt binary.
    _stub_binary(binary, version="ffmpeg version 9.0.2", extra="# rebuilt")
    stamp_after = bridge._binary_stat_key(str(binary))
    assert stamp_after != stamp_before
    after = bridge._ffmpeg_toolchain_fingerprint(str(binary))
    assert after != before
    # The cache key includes the bytes' stamp, so nothing stale can be served.
    assert (str(binary.resolve()),) == (stamp_after[0],)
    assert len({stamp_before, stamp_after}) == 2


@pytest.mark.parametrize("target", ["missing", "directory", "empty", "dangling"])
def test_binary_identity_refuses_an_unusable_binary(tmp_path: Path, target: str) -> None:
    """A-09: an identity that cannot be vouched for raises, never ``"unavailable"``."""
    path = tmp_path / "ffmpeg"
    if target == "directory":
        path.mkdir()
    elif target == "empty":
        path.write_bytes(b"")
    elif target == "dangling":
        path.symlink_to(tmp_path / "nowhere")
    with pytest.raises(bridge.StudioBridgeError, match="ffmpeg_unavailable"):
        bridge._binary_identity(str(path))
    with pytest.raises(bridge.StudioBridgeError, match="ffmpeg_unavailable"):
        bridge._binary_stat_key(str(path))
    with pytest.raises(bridge.StudioBridgeError, match="ffmpeg_unavailable"):
        bridge._ffmpeg_toolchain_fingerprint(str(path))


def test_binary_identity_refuses_a_command_name_that_is_not_on_path() -> None:
    """A-09: an unresolvable bare name is ``ffmpeg_unavailable``, not a silent fallback."""
    with pytest.raises(bridge.StudioBridgeError, match="ffmpeg_unavailable"):
        bridge._binary_identity("ffmpeg-that-does-not-exist-9f2c")


def test_binary_identity_reports_an_unreadable_binary(monkeypatch: Any, tmp_path: Path) -> None:
    """A-09: a refusal at the descriptor is reported as an unreadable binary."""
    binary = _stub_binary(tmp_path / "ffmpeg", version="ffmpeg version 9.0.2")

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("binary vanished")

    monkeypatch.setattr(bridge.os, "open", _boom)
    with pytest.raises(bridge.StudioBridgeError, match="ffmpeg_unavailable"):
        bridge._binary_identity(str(binary))


def test_binary_identity_reports_a_failing_descriptor_stat(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A-09: a descriptor that cannot be stat'ed is an unusable binary, not a guess."""
    binary = _stub_binary(tmp_path / "ffmpeg", version="ffmpeg version 9.0.2")

    def _boom(_fd: int) -> Any:
        raise OSError("descriptor vanished")

    # ``os.fstat`` is shared with the test runner, so it is restored in a
    # ``finally`` and the refusal is checked without pytest's own machinery.
    real_fstat = bridge.os.fstat
    bridge.os.fstat = _boom  # type: ignore[assignment]
    try:
        _refuses(
            lambda: bridge._binary_identity(str(binary)),
            bridge.StudioBridgeError,
            contains="ffmpeg_unavailable",
        )
    finally:
        bridge.os.fstat = real_fstat


def test_binary_stat_key_reports_a_failing_stat(monkeypatch: Any, tmp_path: Path) -> None:
    """A-09: a binary that cannot be stamped cannot key the fingerprint cache."""
    binary = _stub_binary(tmp_path / "ffmpeg", version="ffmpeg version 9.0.2")

    target = str(binary.resolve(strict=True))
    state = {"armed": False}
    real_lstat = bridge.os.lstat
    real_resolve = bridge._resolve_binary

    def _resolve_then_arm(name: str) -> Path:
        resolved = real_resolve(name)
        state["armed"] = True  # every lstat from here on is the one under test
        return resolved

    def _flakey_lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if state["armed"] and os.fspath(path) == target:
            raise OSError("path vanished")
        return real_lstat(path, *args, **kwargs)

    bridge._resolve_binary = _resolve_then_arm  # type: ignore[assignment]
    bridge.os.lstat = _flakey_lstat
    try:
        _refuses(
            lambda: bridge._binary_stat_key(str(binary)),
            bridge.StudioBridgeError,
            contains="ffmpeg_unavailable",
        )
    finally:
        bridge._resolve_binary = real_resolve
        bridge.os.lstat = real_lstat


def test_toolchain_fingerprint_binds_a_binary_that_cannot_run(tmp_path: Path) -> None:
    """A-09: an executable that will not run still has an identity, without a version line."""
    unusable = tmp_path / "ffmpeg-garbage"
    unusable.write_bytes(b"\x00\x01\x02\x03" * 8)
    unusable.chmod(0o755)
    bridge._cached_toolchain_fingerprint.cache_clear()
    fingerprint = bridge._ffmpeg_toolchain_fingerprint(str(unusable))
    assert len(fingerprint) == 64
    int(fingerprint, 16)
    # Its identity is the bytes and the (empty) version line - not the stub's.
    stub = _stub_binary(tmp_path / "ffmpeg-stub", version="ffmpeg version 9.0.2")
    assert fingerprint != bridge._ffmpeg_toolchain_fingerprint(str(stub))


def test_binary_identity_refuses_a_binary_that_shrank_while_hashed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A-09: bytes that do not match the descriptor's size are refused, not hashed blindly."""
    binary = _stub_binary(tmp_path / "ffmpeg", version="ffmpeg version 9.0.2")
    calls = {"n": 0}

    def _short_read(_fd: int, _size: int) -> bytes:
        calls["n"] += 1
        return b"#" if calls["n"] == 1 else b""

    monkeypatch.setattr(bridge.os, "read", _short_read)
    with pytest.raises(bridge.StudioBridgeError, match="changed while hashed"):
        bridge._binary_identity(str(binary))


def test_shot_cache_key_binds_the_toolchain() -> None:
    """A-09: the fingerprint reaches both the payload and the CAS pipeline digest."""
    keyed = {
        "shot_id": "s000-sh000",
        "shot_content_hash": "a" * 64,
        "duration_ms": 1000,
        "fps": 12,
        "width": 320,
        "height": 180,
        "image_sha256": "b" * 64,
    }
    one = bridge.shot_cache_key_hash(**keyed, toolchain_fingerprint="1" * 64)
    two = bridge.shot_cache_key_hash(**keyed, toolchain_fingerprint="2" * 64)
    assert one != two
    assert b"toolchain_fingerprint" in bridge.shot_cache_key_payload(
        **keyed, toolchain_fingerprint="1" * 64
    )
    digest_one = bridge._segment_pipeline_digest(
        fps=12, width=320, height=180, sample_rate=16_000, toolchain_fingerprint="1" * 64
    )
    digest_two = bridge._segment_pipeline_digest(
        fps=12, width=320, height=180, sample_rate=16_000, toolchain_fingerprint="2" * 64
    )
    assert digest_one != digest_two


def test_render_reports_bridge_and_process_memory_in_the_receipt(tmp_path: Path) -> None:
    """A-07: both numbers are reported, and the receipt records them."""
    if not _ffmpeg_available():  # pragma: no cover - environment gate
        pytest.skip("real ffmpeg is required for the ink-and-memory test")
    result = bridge.render_storyboard_with_segment_cas(
        "INT. ROOM - DAY\nA: Hello there.\n",
        "memory",
        tmp_path / "out",
        tmp_path / "cas",
        "job",
        signing_key=KEY,
        fps=12,
        width=320,
        height=180,
        ffmpeg=_ffmpeg_bin(),
        ffprobe=_ffprobe_bin(),
    )
    assert result.bridge_buffer_peak_bytes == result.peak_memory_bytes > 0
    assert result.process_peak_rss_bytes > 0
    # A-07: the three honest values are recorded, and the compatibility alias
    # carries the combined estimate - never one opaque number pretending to be RSS.
    assert result.self_peak_rss_bytes > 0
    assert result.children_max_rss_bytes > 0  # FFmpeg ran and was reaped
    assert result.process_hwm_estimate_bytes == (
        result.self_peak_rss_bytes + result.children_max_rss_bytes
    )
    assert result.process_peak_rss_bytes == result.process_hwm_estimate_bytes
    receipt = json.loads(result.receipt_path.read_text())
    assert receipt["bridge_buffer_peak_bytes"] == result.bridge_buffer_peak_bytes
    assert receipt["process_peak_rss_bytes"] == result.process_peak_rss_bytes
    assert receipt["self_peak_rss_bytes"] == result.self_peak_rss_bytes
    assert receipt["children_max_rss_bytes"] == result.children_max_rss_bytes
    assert receipt["process_hwm_estimate_bytes"] == result.process_hwm_estimate_bytes
    assert len(receipt["ffmpeg_toolchain_fingerprint"]) == 64


def test_cli_reads_secret_and_script_through_one_descriptor(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A-11: oversized, empty, symlinked and non-UTF-8 files are all refused."""
    oversized = tmp_path / "huge.txt"
    oversized.write_bytes(b"x" * (studio_cli.MAX_SECRET_BYTES + 1))
    with pytest.raises(studio_cli.StudioCliError, match="size out of range"):
        studio_cli._read_bounded_text(
            oversized, limit=studio_cli.MAX_SECRET_BYTES, label="secret file"
        )

    blank = tmp_path / "blank.txt"
    blank.write_bytes(b"")
    with pytest.raises(studio_cli.StudioCliError, match="size out of range"):
        studio_cli._read_bounded_text(blank, limit=1024, label="secret file")

    binary = tmp_path / "binary.txt"
    binary.write_bytes(b"\xff\xfe")
    with pytest.raises(studio_cli.StudioCliError, match="not valid UTF-8"):
        studio_cli._read_bounded_text(binary, limit=1024, label="script file")

    target = tmp_path / "target.txt"
    target.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(studio_cli.StudioCliError, match="non-symlink"):
        studio_cli._read_bounded_text(link, limit=1024, label="secret file")

    with pytest.raises(studio_cli.StudioCliError, match="must be a regular, non-symlink file"):
        studio_cli._read_bounded_text(tmp_path, limit=1024, label="secret file")

    # A text file that grows after the fstat is refused by the running total.
    growing = tmp_path / "growing.txt"
    growing.write_bytes(b"y" * 64)
    real_fstat = os.fstat

    def _fstat(fd: int) -> Any:
        fake = real_fstat(fd)
        return os.stat_result(
            (
                fake.st_mode,
                fake.st_ino,
                fake.st_dev,
                fake.st_nlink,
                fake.st_uid,
                fake.st_gid,
                4,
                fake.st_atime,
                fake.st_mtime,
                fake.st_ctime,
            )
        )

    with monkeypatch.context() as patched:
        patched.setattr(studio_cli.os, "fstat", _fstat)
        with pytest.raises(studio_cli.StudioCliError, match="grew past"):
            studio_cli._read_bounded_text(growing, limit=32, label="script file")


def test_cli_secret_file_environment_fallbacks(tmp_path: Path, monkeypatch: Any) -> None:
    """A-10: the file *paths* may come from the environment, never the secrets."""
    key_file = tmp_path / "signing.key"
    key_file.write_text("  " + KEY + "  \n", encoding="utf-8")
    vault_file = tmp_path / "vault.secret"
    vault_file.write_text("vault-secret\n", encoding="utf-8")

    monkeypatch.delenv(studio_cli.SIGNING_KEY_ENV, raising=False)
    monkeypatch.setenv(studio_cli.SIGNING_KEY_FILE_ENV, str(key_file))
    monkeypatch.setenv(studio_cli.VAULT_SECRET_FILE_ENV, str(vault_file))
    assert (
        studio_cli._read_secret(
            inline=None,
            path=None,
            env_name=studio_cli.SIGNING_KEY_ENV,
            file_env_name=studio_cli.SIGNING_KEY_FILE_ENV,
        )
        == KEY
    )
    assert (
        studio_cli._read_secret(
            inline=None,
            path=None,
            env_name=studio_cli.VAULT_SECRET_ENV,
            file_env_name=studio_cli.VAULT_SECRET_FILE_ENV,
        )
        == "vault-secret"
    )

    # An empty value in the environment falls through to the inline/env sources.
    monkeypatch.setenv(studio_cli.SIGNING_KEY_FILE_ENV, "   ")
    monkeypatch.setenv(studio_cli.SIGNING_KEY_ENV, "env-key")
    assert (
        studio_cli._read_secret(
            inline=None,
            path=None,
            env_name=studio_cli.SIGNING_KEY_ENV,
            file_env_name=studio_cli.SIGNING_KEY_FILE_ENV,
        )
        == "env-key"
    )

    # An empty secret file is refused instead of silently yielding "".
    blank = tmp_path / "blank.key"
    blank.write_text("\n", encoding="utf-8")
    with pytest.raises(studio_cli.StudioCliError, match="empty or oversized"):
        studio_cli._read_secret(
            inline=None,
            path=blank,
            env_name=studio_cli.SIGNING_KEY_ENV,
            file_env_name=studio_cli.SIGNING_KEY_FILE_ENV,
        )


def test_cli_utc_timestamp_is_a_sortable_zulu_string() -> None:
    """A-10: the durable ledger gets a UTC, second-resolution timestamp."""
    stamp = studio_cli._utc_now_iso()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp)


def test_cli_resolves_the_durable_approval_database(tmp_path: Path, monkeypatch: Any) -> None:
    """A-10: an explicit ``--approval-db`` wins, else the conventional name is found."""
    parser = studio_cli.build_parser()
    explicit = parser.parse_args(
        [
            "render",
            "--script",
            "INT. ROOM - DAY\nA: Hi.\n",
            "--output-dir",
            str(tmp_path / "out"),
            "--approval-db",
            str(tmp_path / "custom.sqlite3"),
        ]
    )
    assert studio_cli._resolve_approval_db(explicit) == tmp_path / "custom.sqlite3"

    plain = parser.parse_args(
        ["render", "--script", "INT. ROOM - DAY\nA: Hi.\n", "--output-dir", str(tmp_path / "out")]
    )
    monkeypatch.chdir(tmp_path)
    assert studio_cli._resolve_approval_db(plain) is None

    beside_output = tmp_path / "out" / studio_cli.DEFAULT_APPROVAL_DB
    beside_output.parent.mkdir(parents=True, exist_ok=True)
    beside_output.write_bytes(b"")
    assert studio_cli._resolve_approval_db(plain) == beside_output

    beside_output.unlink()
    in_cwd = tmp_path / studio_cli.DEFAULT_APPROVAL_DB
    in_cwd.write_bytes(b"")
    assert studio_cli._resolve_approval_db(plain) == in_cwd

    with pytest.raises(studio_cli.StudioCliError, match="approval database unusable"):
        # A parent that is a regular file can never hold the SQLite database:
        # the ledger refuses to open it (a merely missing parent directory is
        # created, but this path is unusable no matter what).
        blocked = tmp_path / "not-a-dir"
        blocked.write_text("i am a file", encoding="utf-8")
        studio_cli._open_durable_ledger(blocked / "x.sqlite3")


def test_cli_renders_with_a_real_durable_approval_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A-10: ``main()`` consumes ``--approval-id`` from a real SQLite ledger."""
    from nexus_ai_agent.creative.storyboard_render import issue_render_approval
    from nexus_ai_agent.nagar.ibpc.approval import DurableApprovalLedger

    if not _ffmpeg_available():  # pragma: no cover - environment gate
        pytest.skip("real ffmpeg is required for the durable-ledger render")

    script = "INT. ROOM - DAY\nA: Hello there, friend.\n"
    compiled = compile_storyboard(script)
    profile_hash = mz.RenderProfile.from_mapping(PROFILE).profile_hash()
    db_path = tmp_path / "approvals.sqlite3"
    ledger = DurableApprovalLedger(db_path, now=lambda: "2026-11-18T00:00:00Z")
    try:
        approval_id = issue_render_approval(
            ledger,
            project_id="proj-cli",
            job_id="cli-job",
            principal_id="owner-1",
            source_sha256=hashlib.sha256(script.encode("utf-8")).hexdigest(),
            script_sha256=compiled.script_sha256,
            manifest_sha256=compiled.manifest_sha256,
            render_profile_hash=profile_hash,
        )
    finally:
        ledger.close()

    argv = [
        "render",
        "--script",
        script,
        "--job-id",
        "cli-job",
        "--project-id",
        "proj-cli",
        "--output-dir",
        str(tmp_path / "out"),
        "--cas-dir",
        str(tmp_path / "cas"),
        "--fps",
        "12",
        "--width",
        "320",
        "--height",
        "180",
        "--signing-key",
        KEY,
        "--approval-id",
        approval_id,
        "--principal-id",
        "owner-1",
        "--approval-db",
        str(db_path),
        "--ffmpeg",
        _ffmpeg_bin(),
        "--ffprobe",
        _ffprobe_bin(),
    ]
    assert studio_cli.main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "rendered"
    assert payload["verifier_status"] == av.PASS

    # The approval really was consumed in the database, so a replay is refused.
    replay = io.StringIO()
    assert studio_cli.run_studio_cli(argv, out=replay) == 2
    assert json.loads(replay.getvalue())["status"] == "refused"

    # Without --approval-db the conventional database beside the output is found.
    beside = tmp_path / "out" / studio_cli.DEFAULT_APPROVAL_DB
    beside.parent.mkdir(parents=True, exist_ok=True)
    beside.write_bytes(db_path.read_bytes())
    assert (
        studio_cli._resolve_approval_db(
            studio_cli.build_parser().parse_args(
                [
                    "render",
                    "--script",
                    script,
                    "--output-dir",
                    str(tmp_path / "out"),
                ]
            )
        )
        == beside
    )


def test_mutant_bridge_ignoring_the_restored_manifest_hash_is_killed() -> None:
    """A-06 mutant: dropping the restored-manifest digest check must be caught.

    The check has no other observer - a vault that reconstitutes a different
    manifest would otherwise pass as a verified backup - so this mutant is the
    proof that the new gate is load-bearing rather than decorative.
    """

    def _neuter(tree: ast.Module) -> None:
        func = _function(tree, "sync_and_verify_studio_bundle_in_vault")
        found = False
        for node in ast.walk(func):
            if isinstance(node, ast.If) and "vault_restored_manifest_mismatch" in ast.unparse(node):
                node.test = ast.Constant(value=False)
                found = True
        assert found, "the restored-manifest gate must exist"

    def _behaviour(sync_fn: Any, tmp_root: Path) -> None:
        from types import SimpleNamespace

        fake = _FakeRenderResult(tmp_root)
        sealed_manifest_sha = hashlib.sha256(fake.manifest_path.read_bytes()).hexdigest()

        class _TamperingManager:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def push_project_snapshot(self, **_kwargs: Any) -> Any:
                return SimpleNamespace(
                    snapshot_id="snap-x", uploaded_chunks=1, deduplicated_chunks=0, total_chunks=1
                )

            def pull_project_snapshot(self, *, target_dir: Path, **_kwargs: Any) -> Any:
                target = Path(target_dir)
                (target / "render.mp4").write_bytes(fake.output_mp4_path.read_bytes())
                (target / "job.manifest.json").write_text('{"tampered": true}', encoding="utf-8")
                (target / "render.passport.json").write_text("{}", encoding="utf-8")
                return SimpleNamespace(files={"render.mp4": b"x"})

        original = (
            sync_fn.__globals__["PersonalCloudVaultManager"],
            sync_fn.__globals__["verify_artifact_passport"],
            sync_fn.__globals__["verify_mp4"],
        )
        try:
            sync_fn.__globals__["PersonalCloudVaultManager"] = _TamperingManager
            sync_fn.__globals__["verify_artifact_passport"] = lambda *_a, **_k: SimpleNamespace(
                passport={"manifest_sha256": sealed_manifest_sha}
            )
            sync_fn.__globals__["verify_mp4"] = lambda *_a, **_k: av.Mp4Verdict(status=av.PASS)
            _refuses(
                lambda: sync_fn(
                    render_result=fake,
                    vault_storage_dir=tmp_root / "vault",
                    vault_journal_db_path=tmp_root / "journal.jsonl",
                    restore_dir=tmp_root / "restore",
                    master_secret=b"secret",
                    project_id="proj",
                    signing_key=KEY,
                ),
                bridge.StudioBridgeError,
                contains="vault_restored_manifest_mismatch",
            )
        finally:
            (
                sync_fn.__globals__["PersonalCloudVaultManager"],
                sync_fn.__globals__["verify_artifact_passport"],
                sync_fn.__globals__["verify_mp4"],
            ) = original

    _behaviour(bridge.sync_and_verify_studio_bundle_in_vault, tmp_path_factory_dir())
    mutant = _load_mutant(
        MUTATION_TARGETS["studio_cas_vault_bridge"],
        _neuter,
        "bridge_no_manifest_gate",
    )
    _kill(lambda: _behaviour(mutant.sync_and_verify_studio_bundle_in_vault, tmp_path_factory_dir()))


def test_mutant_verifier_without_the_audio_gate_is_killed(real_mp4: Path, monkeypatch: Any) -> None:
    """A-08 mutant: drop the demuxer's audio-stream gate and a forged track passes.

    The container test cannot see this defect - ``real_mp4`` genuinely declares an
    ``mp4a`` track with a sane AudioSampleEntry - so the only observer of a
    container whose audio the demuxer cannot find is the gate itself. This mutant
    is the proof that the gate is load-bearing.
    """

    def _neuter(tree: ast.Module) -> None:
        func = _function(tree, "verify_mp4")
        found = False
        for node in ast.walk(func):
            if isinstance(node, ast.If) and "missing_audio_stream" in ast.unparse(node):
                node.test = ast.Constant(value=False)
                found = True
        assert found, "the audio-stream gate must exist"

    def _behaviour(module: Any) -> None:
        with monkeypatch.context() as patch:
            patch.setattr(
                module,
                "_probe",
                lambda *a, **k: {
                    "streams": [{"codec_type": "video", "codec_name": "h264"}],
                    "format": {"duration": "1.0"},
                },
            )
            verdict = module.verify_mp4(real_mp4, expected_duration_s=1.0, tolerance_s=0.3)
        assert verdict.status == module.VIOLATED, "a forged audio track passed verification"
        assert verdict.reasons == ("missing_audio_stream",)

    _behaviour(av)  # positive control: the real gate refuses it
    mutant = _load_mutant(MUTATION_TARGETS["artifact_verifier"], _neuter, "verifier_no_audio_gate")
    _kill(lambda: _behaviour(mutant))


# --------------------------------------------------------------------------- #
# Audit finding A-08 - strict ISO-BMFF structure and ffprobe audio agreement
# --------------------------------------------------------------------------- #


def _moov_file(path: Path, moov_payload: bytes) -> Path:
    """Write a minimal MP4 whose ``moov`` holds exactly ``moov_payload``."""
    path.write_bytes(
        _mp4_box("ftyp", b"isom") + _mp4_box("moov", moov_payload) + _mp4_box("mdat", b"\x00" * 8)
    )
    return path


def test_moov_parser_reports_trailing_bytes_instead_of_ignoring_them(tmp_path: Path) -> None:
    """A-08: stray bytes after the last child box are a typed structural defect."""
    stray = _moov_file(tmp_path / "stray.mp4", _trak("soun", "mp4a", 16_000) + b"\x00\x00\x00")
    tracks, problems = av.read_moov_tracks(stray)
    assert [track.handler for track in tracks] == ["soun"]
    assert len(problems) == 1
    assert problems[0].startswith(f"{av.MALFORMED_MOOV}: moov has 3 trailing bytes at ")
    verdict = av.verify_mp4(stray, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED
    assert verdict.reasons[0].startswith("moov_layout: malformed_moov_structure:")


@pytest.mark.parametrize(
    "payload,expected",
    [
        (b"\x00", "mdhd is 1 bytes, shorter than its version field"),
        (b"\x02\x00\x00\x00" + b"\x00" * 20, "mdhd version 2 is not 0 or 1"),
        (b"\x01\x00\x00\x00" + b"\x00" * 20, "mdhd v1 is 24 bytes, needs 32"),
    ],
)
def test_mdhd_rejects_an_unknown_version_or_a_short_payload(
    tmp_path: Path, payload: bytes, expected: str
) -> None:
    """A-08: a ``mdhd`` whose version/length is not understood yields no timescale."""
    mdia = (
        _mp4_box("hdlr", b"\x00" * 8 + b"soun" + b"\x00" * 8)
        + _mp4_box("mdhd", payload)
        + _mp4_box("minf", _mp4_box("stbl", _mp4_box("stsd", b"\x00" * 8)))
    )
    path = _moov_file(tmp_path / "bad-mdhd.mp4", _mp4_box("trak", _mp4_box("mdia", mdia)))
    tracks, problems = av.read_moov_tracks(path)
    assert tracks[0].timescale == 0
    assert any(problem.endswith(expected) for problem in problems)
    assert av.verify_mp4(path, expected_duration_s=1.0, tolerance_s=0.2).status == av.VIOLATED


@pytest.mark.parametrize(
    "payload,expected",
    [
        (b"\x00\x00\x00\x00", "stsd is 4 bytes, shorter than its entry table"),
        (
            b"\x00\x00\x00\x00" + (0).to_bytes(4, "big"),
            "stsd declares 0 sample entries",
        ),
        (
            b"\x00\x00\x00\x00" + (1).to_bytes(4, "big") + b"\x00\x00\x00\x05",
            "stsd declares 1 entries but holds none",
        ),
        (
            b"\x00\x00\x00\x00" + (1).to_bytes(4, "big") + (4).to_bytes(4, "big") + b"mp4a",
            "stsd entry 'mp4a' declares 4 bytes, only 8 remain",
        ),
        (
            b"\x00\x00\x00\x00" + (1).to_bytes(4, "big") + (200).to_bytes(4, "big") + b"mp4a",
            "stsd entry 'mp4a' declares 200 bytes, only 8 remain",
        ),
    ],
)
def test_stsd_rejects_a_broken_entry_table(tmp_path: Path, payload: bytes, expected: str) -> None:
    """A-08: an ``stsd`` that cannot name one whole entry yields no codec at all."""
    mdia = (
        _mp4_box("hdlr", b"\x00" * 8 + b"soun" + b"\x00" * 8)
        + _mp4_box("mdhd", _mdhd_payload(16_000))
        + _mp4_box("minf", _mp4_box("stbl", _mp4_box("stsd", payload)))
    )
    path = _moov_file(tmp_path / "bad-stsd.mp4", _mp4_box("trak", _mp4_box("mdia", mdia)))
    tracks, problems = av.read_moov_tracks(path)
    assert tracks[0].codec is None
    assert [problem.split(": ", 1)[1] for problem in problems] == [expected]
    assert av.verify_mp4(path, expected_duration_s=1.0, tolerance_s=0.2).status == av.VIOLATED


@pytest.mark.parametrize(
    "body,expected",
    [
        (b"\x00" * 20, "mp4a body is 20 bytes, needs 28"),
        (
            b"\x00" * 6
            + (1).to_bytes(2, "big")
            + b"\x00" * 8
            + (0).to_bytes(2, "big")
            + (16).to_bytes(2, "big")
            + b"\x00" * 4
            + (16_000 << 16).to_bytes(4, "big"),
            "mp4a declares 0 channels",
        ),
        (
            b"\x00" * 6
            + (1).to_bytes(2, "big")
            + b"\x00" * 8
            + (2).to_bytes(2, "big")
            + (16).to_bytes(2, "big")
            + b"\x00" * 4
            + b"\x00" * 4,
            "mp4a declares sample rate 0",
        ),
    ],
)
def test_audio_sample_entry_rejects_an_unusable_header(
    tmp_path: Path, body: bytes, expected: str
) -> None:
    """A-08: naming ``mp4a`` is not enough - the AudioSampleEntry must be usable."""
    path = _moov_file(tmp_path / "bad-entry.mp4", _trak("soun", "mp4a", 16_000, body=body))
    tracks, problems = av.read_moov_tracks(path)
    assert tracks[0].codec is None
    assert [problem.split(": ", 1)[1] for problem in problems] == [expected]
    verdict = av.verify_mp4(path, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED
    assert verdict.has_audio_stream is False


def _stsd_payload(*entries: bytes) -> bytes:
    """Build an ``stsd`` payload: version/flags, entry count, then every entry."""
    return b"\x00\x00\x00\x00" + len(entries).to_bytes(4, "big") + b"".join(entries)


def test_audio_sample_entry_accepts_the_standard_28_byte_header(tmp_path: Path) -> None:
    """A-08: a standards-conformant ``mp4a`` entry is believed, channels and rate intact."""
    path = _moov_file(tmp_path / "good-entry.mp4", _trak("soun", "mp4a", 16_000))
    tracks, problems = av.read_moov_tracks(path)
    assert problems == []
    assert [(track.codec, track.timescale) for track in tracks] == [("mp4a", 16_000)]
    assert tracks[0].is_aac_audio is True


def test_audio_sample_entry_is_skipped_for_non_audio_codecs(tmp_path: Path) -> None:
    """A-08: the 28-byte AudioSampleEntry body is only required for ``mp4a``."""
    path = _moov_file(tmp_path / "video-entry.mp4", _trak("vide", "avc1", 10_240))
    tracks, problems = av.read_moov_tracks(path)
    assert problems == []
    assert [(track.handler, track.codec) for track in tracks] == [("vide", "avc1")]


def test_verifier_reports_a_short_hdlr_as_a_structural_defect(tmp_path: Path) -> None:
    """A-08: an ``hdlr`` too short to name its handler is reported, not guessed."""
    mdia = (
        _mp4_box("hdlr", b"\x00" * 4)
        + _mp4_box("mdhd", _mdhd_payload(16_000))
        + _mp4_box("minf", _mp4_box("stbl", _mp4_box("stsd", _stsd_payload(_sample_entry("mp4a")))))
    )
    path = _moov_file(tmp_path / "short-hdlr.mp4", _mp4_box("trak", _mp4_box("mdia", mdia)))
    tracks, problems = av.read_moov_tracks(path)
    assert [(track.handler, track.codec) for track in tracks] == [("", "mp4a")]
    assert problems == ["malformed_moov_structure: hdlr is too short to declare a handler"]


def test_verifier_reports_moov_layout_problems_before_audio_is_judged(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A-08: a container defect decides the verdict before ffprobe is ever consulted."""
    path = _moov_file(tmp_path / "no-audio-box.mp4", _trak("vide", "avc1", 10_240))
    monkeypatch.setattr(
        av,
        "_probe",
        lambda *a, **k: pytest.fail("a malformed container must not reach ffprobe"),
    )
    verdict = av.verify_mp4(path, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED
    # A video-only container is refused on the container's own word alone.
    assert verdict.reasons == ("missing_audio_stream",)
    assert verdict.has_audio_stream is False
    assert verdict.probed_duration_s == 0.0
    assert verdict.size_bytes > 0


def test_verifier_refuses_a_forged_audio_track(real_mp4: Path, monkeypatch: Any) -> None:
    """A-08: the container's AAC claim is cross-checked against the demuxer's stream list."""
    monkeypatch.setattr(
        av,
        "_probe",
        lambda *a, **k: {
            "streams": [{"codec_type": "video", "codec_name": "h264"}],
            "format": {"duration": "1.0"},
        },
    )
    verdict = av.verify_mp4(real_mp4, expected_duration_s=1.0, tolerance_s=0.3)
    assert verdict.status == av.VIOLATED
    assert verdict.reasons == ("missing_audio_stream",)
    # The container's own evidence is still reported, so the refusal is diagnosable.
    assert verdict.has_audio_stream is True
    assert verdict.audio_codec == "mp4a"

    allowed = av.verify_mp4(real_mp4, expected_duration_s=1.0, tolerance_s=0.3, require_audio=False)
    assert allowed.status == av.PASS


# --------------------------------------------------------------------------- #
# CP4 closure: descriptor lifetime, gate tampering and skip-branch coverage
# --------------------------------------------------------------------------- #


def test_cp4_download_stream_iteration_paths(copied_mp4: Path) -> None:
    """``iter_authorized_chunks`` over a bare path: full iteration to EOF,
    the iterator context manager, and StopIteration once the lease is closed."""
    stream = ad.iter_authorized_chunks(copied_mp4)
    with stream:
        assert iter(stream) is stream
        chunks: list[bytes] = []
        while True:
            try:
                chunks.append(next(stream))
            except StopIteration:
                break
    assert b"".join(chunks) == copied_mp4.read_bytes()
    with pytest.raises(StopIteration):
        next(stream)  # the lease is retired: a closed stream stays closed


def test_cp4_download_authorized_object_and_lease_lifecycle(copied_mp4: Path) -> None:
    """The authorized object carries its verified descriptor through the lease:
    ``fd`` reads through, the stream closes it, ``close`` is idempotent."""
    fd = os.open(copied_mp4, os.O_RDONLY)
    lease = ad._FdLease(fd)
    authorized = ad.AuthorizedDownload(path=copied_mp4, size_bytes=1, sha256="0" * 64, _lease=lease)
    assert authorized.fd == fd
    stream = ad.iter_authorized_chunks(authorized)
    first = next(stream)
    assert first == copied_mp4.read_bytes()[: len(first)]
    stream.close()
    assert authorized.fd is None  # the lease behind the authorization is closed
    authorized.close()  # idempotent: a second close is a no-op
    with pytest.raises(StopIteration):
        next(stream)

    # The context manager of the authorized object closes its own lease.
    fd2 = os.open(copied_mp4, os.O_RDONLY)
    with ad.AuthorizedDownload(
        path=copied_mp4, size_bytes=1, sha256="0" * 64, _lease=ad._FdLease(fd2)
    ) as held:
        assert held.fd == fd2
    with pytest.raises(OSError):
        os.fstat(fd2)
    # The finalize target tolerates an already-emptied box (double finalization).
    ad._close_fd_box([None])


def test_cp4_download_read_error_closes_descriptor(
    copied_mp4: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read error mid-stream closes the descriptor before it propagates."""
    fd = os.open(copied_mp4, os.O_RDONLY)
    stream = ad.iter_authorized_chunks(fd)
    real_read = os.read

    def _boom(read_fd: int, count: int) -> bytes:
        if read_fd == fd:
            raise OSError("injected read failure")
        return real_read(read_fd, count)

    monkeypatch.setattr(os, "read", _boom)
    with pytest.raises(OSError, match="injected read failure"):
        next(stream)
    with pytest.raises(OSError):
        os.fstat(fd)  # the error path must have closed the descriptor


def test_cp4_download_gate_open_and_identity_failures(
    tmp_path: Path, copied_mp4: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every failure between the single stat and the served descriptor: open
    errors, a non-regular descriptor, an inode swap, and a size disagreement."""
    root = tmp_path / "ws"
    render_dir = root / "projects" / "p" / "renders" / "j"
    render_dir.mkdir(parents=True)
    mp4 = render_dir / "storyboard.mp4"
    mp4.write_bytes(copied_mp4.read_bytes())
    ap.write_passport(render_dir / "storyboard.passport.json", _passport(mp4))
    rel = "projects/p/renders/j/storyboard.mp4"
    real_open = ad._open_readonly_no_follow

    def _refuse_open(path: Path) -> int:
        raise OSError("gone")

    monkeypatch.setattr(ad, "_open_readonly_no_follow", _refuse_open)
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(root, rel, KEY)
    assert (info.value.status_code, info.value.reason) == (404, "artifact_not_found")

    monkeypatch.setattr(ad, "_open_readonly_no_follow", lambda path: os.open(root, os.O_RDONLY))
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(root, rel, KEY)
    assert (info.value.status_code, info.value.reason) == (404, "artifact_not_found")

    decoy = tmp_path / "decoy.mp4"
    decoy.write_bytes(copied_mp4.read_bytes())
    monkeypatch.setattr(ad, "_open_readonly_no_follow", lambda path: real_open(decoy))
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(root, rel, KEY)
    assert (info.value.status_code, info.value.reason) == (409, "artifact_changed")

    monkeypatch.setattr(ad, "_open_readonly_no_follow", real_open)
    from types import SimpleNamespace

    fake_check = SimpleNamespace(
        passport={
            "output_rel_path": rel,
            "output_size_bytes": mp4.stat().st_size + 7,
            "output_sha256": "0" * 64,
        }
    )
    monkeypatch.setattr(ad, "verify_artifact_passport", lambda *a, **k: fake_check)
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(root, rel, KEY)
    assert (info.value.status_code, info.value.reason) == (409, "artifact_changed")


def test_cp4_render_local_images_validation_and_digest_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local-image contract: shape validation, escaping paths and
    unreadable content all refuse before a single shot renders."""
    from nexus_ai_agent.creative import storyboard_render as sr

    with pytest.raises(sr.StoryboardRenderError, match="invalid_local_images"):
        sr._extract_local_images({"local_images": []})
    with pytest.raises(sr.StoryboardRenderError, match="invalid_local_images"):
        sr._extract_local_images({"local_images": {"bad shot!": "a.png"}})
    with pytest.raises(sr.StoryboardRenderError, match="invalid_local_images"):
        sr._extract_local_images({"local_images": {"s001-sh001": "/abs.png"}})
    with pytest.raises(sr.StoryboardRenderError, match="invalid_local_images"):
        sr._extract_local_images({"local_images": {"s001-sh001": "   "}})
    assert sr._extract_local_images({}) == {}
    assert sr._extract_local_images({"local_images": None}) == {}
    assert sr._extract_local_images({"local_images": {"s001-sh001": "img.png"}}) == {
        "s001-sh001": "img.png"
    }

    with pytest.raises(sr.StoryboardRenderError):
        sr.local_image_content_digests(tmp_path, {"shot_1": "../escape.png"})

    (tmp_path / "img.png").write_bytes(b"png-bytes")

    def _unreadable(*_a: object, **_k: object) -> tuple[str, int, int]:
        raise OSError("unreadable")

    monkeypatch.setattr(sr, "stream_sha256", _unreadable)
    with pytest.raises(sr.StoryboardRenderError, match="local_image_unreadable"):
        sr.local_image_content_digests(tmp_path, {"shot_1": "img.png"})


def test_cp4_render_sync_explicit_attempt_is_quarantined(tmp_path: Path) -> None:
    """An explicit ``attempt`` takes the quarantined staging branch even
    without a contextvar claim; an invalid payload still fails closed."""
    from nexus_ai_agent.creative.storyboard_render import (
        RenderContext,
        StoryboardRenderError,
        render_storyboard_sync,
    )
    from nexus_ai_agent.nagar.ibpc.approval import ApprovalLedger

    ctx = RenderContext(
        workspace_root=tmp_path,
        approval_ledger=ApprovalLedger(now=lambda: "2026-11-19T12:00:00Z"),
        signing_key=KEY,
    )
    with pytest.raises(StoryboardRenderError):
        render_storyboard_sync({"garbage": True}, ctx, queue_job_id="job-cp4", attempt=3)


def test_cp4_storyboard_jobs_refusal_and_skip_branches(
    tmp_path: Path, copied_mp4: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skip/refusal branches of the job helpers: missing configuration,
    escaping destinations, broken passports, vanished staging and the
    finalize skip reasons."""
    from nexus_ai_agent.creative.storyboard_jobs import (
        finalize_storyboard_approval,
        storyboard_render_verifier,
        verify_storyboard_final_pair,
    )
    from nexus_ai_agent.creative.storyboard_render import (
        approval_identity_for,
        issue_render_approval,
    )
    from nexus_ai_agent.nagar.ibpc.approval import (
        ApprovalState,
        DurableApprovalLedger,
        durable_approval_ledger_path,
    )

    workspace = tmp_path / "studio"
    workspace.mkdir()
    monkeypatch.delenv("NEXUS_STUDIO_WORKSPACE", raising=False)
    # finalize without a configured studio fails closed with the typed reason
    assert "NEXUS_STUDIO_WORKSPACE" in finalize_storyboard_approval("a", job_id="j", attempt=1)
    # ...and so does the final-pair verifier
    assert "NEXUS_STUDIO_WORKSPACE" in str(
        verify_storyboard_final_pair(copied_mp4, copied_mp4.with_suffix(".passport.json"))
    )
    monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE", str(workspace))
    monkeypatch.setenv("NEXUS_STUDIO_SIGNING_KEY", KEY)

    outside = tmp_path / "outside"
    outside.mkdir()
    mp4_out = outside / "storyboard.mp4"
    mp4_out.write_bytes(copied_mp4.read_bytes())
    pp_out = outside / "storyboard.passport.json"
    ap.write_passport(pp_out, _passport(mp4_out))
    assert verify_storyboard_final_pair(mp4_out, pp_out) == "path_outside_workspace"

    inside = workspace / "projects" / "p" / "renders" / "j"
    inside.mkdir(parents=True)
    mp4_in = inside / "storyboard.mp4"
    mp4_in.write_bytes(copied_mp4.read_bytes())
    pp_in = inside / "storyboard.passport.json"
    pp_in.write_bytes(b"definitely not json")
    reason = verify_storyboard_final_pair(mp4_in, pp_in)
    assert isinstance(reason, str) and reason.startswith("passport_")

    payload = {"fps": FPS}
    base_result: dict[str, Any] = {
        "status": "rendered",
        "verifier_status": "PASS",
        "output_rel_path": "projects/p/renders/j/storyboard.mp4",
        "passport_rel_path": "projects/p/renders/j/storyboard.passport.json",
        "output_sha256": "0" * 64,
        "shot_durations_s": [0.5, 0.5],
    }
    escaping = dict(
        base_result,
        staged_output_path=str(mp4_out),
        staged_passport_path=str(pp_out),
    )
    outcome = storyboard_render_verifier(payload, escaping)
    assert outcome.ok is False and outcome.reason_code == "path_outside_workspace"

    vanished = dict(
        base_result,
        staged_output_path=str(workspace / ".staging" / "x.mp4"),
        staged_passport_path=str(workspace / ".staging" / "x.passport.json"),
    )
    outcome = storyboard_render_verifier(payload, vanished)
    assert outcome.ok is False and outcome.reason_code == "artifact_missing"

    # The finalize skips on a real durable ledger: a superseded attempt and
    # an unknown approval are typed skips, never crashes.
    script = "INT. ROOM - DAY\nA: Hello there, friend.\n"
    compiled = compile_storyboard(script)
    profile_hash = mz.RenderProfile.from_mapping(PROFILE).profile_hash()
    ident = approval_identity_for(
        project_id="p",
        job_id="j",
        principal_id="owner-1",
        source_sha256=hashlib.sha256(script.encode("utf-8")).hexdigest(),
        script_sha256=compiled.script_sha256,
        manifest_sha256=compiled.manifest_sha256,
        render_profile_hash=profile_hash,
    )
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(workspace), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        approval_id = issue_render_approval(
            ledger,
            project_id="p",
            job_id="j",
            principal_id="owner-1",
            source_sha256=hashlib.sha256(script.encode("utf-8")).hexdigest(),
            script_sha256=compiled.script_sha256,
            manifest_sha256=compiled.manifest_sha256,
            render_profile_hash=profile_hash,
        )
        assert ledger.consume(approval_id, identity=ident, job_id="j", attempt=1).state is (
            ApprovalState.CONSUMED
        )
        ledger.consume(approval_id, identity=ident, job_id="j", attempt=2)
    finally:
        ledger.close()
    assert finalize_storyboard_approval(approval_id, job_id="j", attempt=1) == ("already_consumed")
    assert finalize_storyboard_approval("missing-approval", job_id="j", attempt=1) == (
        "unknown_approval"
    )


def test_cp4_approval_consume_and_reservation_fences() -> None:
    """The reservation fence itself: invalid shapes, takeover, the active
    reservation assertions and the finalize ownership rules."""
    from nexus_ai_agent.creative.storyboard_render import (
        approval_identity_for,
        issue_render_approval,
    )
    from nexus_ai_agent.nagar.ibpc.approval import (
        ApprovalAlreadyConsumedError,
        ApprovalError,
        ApprovalLedger,
        ApprovalState,
    )

    ledger = ApprovalLedger(now=lambda: "2026-11-19T12:00:00Z")
    ident = approval_identity_for(
        project_id="p",
        job_id="j",
        principal_id="owner-1",
        source_sha256="a" * 64,
        script_sha256="b" * 64,
        manifest_sha256="c" * 64,
        render_profile_hash="d" * 64,
    )
    approval_id = issue_render_approval(
        ledger,
        project_id="p",
        job_id="j",
        principal_id="owner-1",
        source_sha256="a" * 64,
        script_sha256="b" * 64,
        manifest_sha256="c" * 64,
        render_profile_hash="d" * 64,
    )
    with pytest.raises(ApprovalError, match="invalid_job_id"):
        ledger.consume(approval_id, identity=ident, job_id="", attempt=1)
    with pytest.raises(ApprovalError, match="invalid_job_id"):
        ledger.consume(approval_id, identity=ident, job_id=123, attempt=1)
    for bad_attempt in (0, -1, True, "1", 2**63):
        with pytest.raises(ApprovalError, match="invalid_attempt"):
            ledger.consume(approval_id, identity=ident, job_id="j", attempt=bad_attempt)

    assert ledger.consume(approval_id, identity=ident, job_id="j", attempt=1).state is (
        ApprovalState.CONSUMED
    )
    # A NEWER attempt takes the reservation over (the takeover branch)...
    assert ledger.consume(approval_id, identity=ident, job_id="j", attempt=2).state is (
        ApprovalState.CONSUMED
    )
    # ...and re-reading with the SAME attempt is the idempotent path.
    assert ledger.consume(approval_id, identity=ident, job_id="j", attempt=2).state is (
        ApprovalState.CONSUMED
    )
    # The active-reservation gate accepts exactly the owning attempt.
    ledger.assert_active_reservation(approval_id, job_id="j", attempt=2, identity=ident)
    with pytest.raises(ApprovalError, match="replay"):
        ledger.assert_active_reservation(approval_id, job_id="other", attempt=2, identity=ident)
    with pytest.raises(ApprovalError, match="stale_attempt"):
        ledger.assert_active_reservation(approval_id, job_id="j", attempt=1, identity=ident)

    # A binding tampered with after consumption refuses the re-read.
    tampered = ledger._bindings[approval_id].model_copy(update={"principal_id": "intruder"})
    ledger._bindings[approval_id] = tampered
    with pytest.raises(ApprovalError, match="reserved identity changed"):
        ledger.consume(approval_id, identity=ident, job_id="j", attempt=2)
    with pytest.raises(ApprovalError, match="reserved identity changed"):
        ledger.assert_active_reservation(approval_id, job_id="j", attempt=2, identity=ident)
    restored = tampered.model_copy(update={"principal_id": "owner-1"})
    ledger._bindings[approval_id] = restored

    # Finalization ownership: a foreign job is refused, a stale attempt can
    # never close a newer attempt's reservation.
    with pytest.raises(ApprovalError, match="replay"):
        ledger.finalize_consumption(approval_id, job_id="other", attempt=2)
    with pytest.raises(ApprovalAlreadyConsumedError):
        ledger.finalize_consumption(approval_id, job_id="j", attempt=1)
    ledger.finalize_consumption(approval_id, job_id="j", attempt=2)
    ledger.finalize_consumption(approval_id, job_id="j", attempt=2)  # idempotent


def test_cp4_approval_consume_without_a_reservation_cannot_re_read() -> None:
    """A consumed approval consumed without ``job_id`` has no reservation: any
    later consume is a plain replay, and the active-reservation gate refuses."""
    from nexus_ai_agent.creative.storyboard_render import (
        approval_identity_for,
        issue_render_approval,
    )
    from nexus_ai_agent.nagar.ibpc.approval import ApprovalError, ApprovalLedger

    ledger = ApprovalLedger(now=lambda: "2026-11-19T12:00:00Z")
    ident = approval_identity_for(
        project_id="p",
        job_id="j",
        principal_id="owner-1",
        source_sha256="a" * 64,
        script_sha256="b" * 64,
        manifest_sha256="c" * 64,
        render_profile_hash="d" * 64,
    )
    approval_id = issue_render_approval(
        ledger,
        project_id="p",
        job_id="j",
        principal_id="owner-1",
        source_sha256="a" * 64,
        script_sha256="b" * 64,
        manifest_sha256="c" * 64,
        render_profile_hash="d" * 64,
    )
    ledger.consume(approval_id, identity=ident)
    with pytest.raises(ApprovalError, match="replay"):
        ledger.consume(approval_id, identity=ident)
    with pytest.raises(ApprovalError, match="replay"):
        ledger.consume(approval_id, identity=ident, job_id="j", attempt=9)
    with pytest.raises(ApprovalError, match="replay"):
        ledger.assert_active_reservation(approval_id, job_id="j", attempt=1, identity=ident)
    with pytest.raises(ApprovalError, match="replay"):
        ledger.finalize_consumption(approval_id, job_id="j", attempt=1)


def test_cp4_durable_ledger_migrates_a_legacy_reservation_table(tmp_path: Path) -> None:
    """A pre-fencing database (no ``last_attempt`` column) is migrated in place
    on open, so the reservation fence exists exactly once everywhere."""
    from nexus_ai_agent.nagar.ibpc.approval import DurableApprovalLedger

    db = tmp_path / "ledger.sqlite3"
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "CREATE TABLE approvals ("
            " approval_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,"
            " turn_id TEXT NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE approval_reservations ("
            " approval_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,"
            " fingerprint TEXT NOT NULL, finalized_at TEXT)"
        )
        connection.commit()
    finally:
        connection.close()

    ledger = DurableApprovalLedger(db, now=lambda: "2026-11-19T12:00:00Z")
    try:
        assert ledger._read_reservation("nope") is None
    finally:
        ledger.close()

    connection = sqlite3.connect(db)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(approval_reservations)")}
    finally:
        connection.close()
    assert "last_attempt" in columns


def test_cp4_passport_verification_tamper_surfaces(
    tmp_path: Path, copied_mp4: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single-open verification window: a passport swapped after its
    lstat, an artifact that vanishes, and a non-regular descriptor all refuse
    with typed reasons."""
    mp4 = tmp_path / "storyboard.mp4"
    original_bytes = copied_mp4.read_bytes()
    mp4.write_bytes(original_bytes)
    passport_path = tmp_path / "storyboard.passport.json"
    ap.write_passport(passport_path, _passport(mp4))
    real_lstat = ap._lstat_regular

    def _swapping_lstat(path: Path, label: str) -> os.stat_result:
        result = real_lstat(path, label)
        if str(path) == str(passport_path):
            # The lstat saw a DIFFERENT inode than any later open can return:
            # tamper the stat result itself (the filesystem would happily
            # recycle the freed inode number on a physical swap).
            return os.stat_result(
                (
                    result.st_mode,
                    result.st_ino + 1,
                    result.st_dev,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    result.st_size,
                    result.st_atime,
                    result.st_mtime,
                    result.st_ctime,
                )
            )
        return result

    monkeypatch.setattr(ap, "_lstat_regular", _swapping_lstat)
    with pytest.raises(ap.PassportError, match="passport_unreadable"):
        ap.verify_artifact_passport(passport_path, mp4, KEY)
    monkeypatch.setattr(ap, "_lstat_regular", real_lstat)

    def _vanishing_lstat(path: Path, label: str) -> os.stat_result:
        result = real_lstat(path, label)
        if str(path) == str(mp4):
            path.unlink()  # present at lstat, gone at open
        return result

    monkeypatch.setattr(ap, "_lstat_regular", _vanishing_lstat)
    with pytest.raises(ap.PassportError, match="artifact changed during verification"):
        ap.verify_artifact_passport(passport_path, mp4, KEY)
    monkeypatch.setattr(ap, "_lstat_regular", real_lstat)

    mp4.write_bytes(original_bytes)
    real_open = os.open

    def _directory_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if str(path) == str(mp4):
            return real_open(tmp_path, flags)  # type: ignore[call-overload]
        return real_open(path, flags, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(os, "open", _directory_open)
    with pytest.raises(ap.PassportError, match="artifact_not_regular"):
        ap.verify_artifact_passport(passport_path, mp4, KEY)
    monkeypatch.setattr(os, "open", real_open)

    # The artifact's inode differs from the one the preceding lstat saw: the
    # descriptor hashes different bytes than were measured.
    def _inodo_lstat(path: Path, label: str) -> os.stat_result:
        result = real_lstat(path, label)
        if str(path) == str(mp4):
            return os.stat_result(
                (
                    result.st_mode,
                    result.st_ino + 1,
                    result.st_dev,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    result.st_size,
                    result.st_atime,
                    result.st_mtime,
                    result.st_ctime,
                )
            )
        return result

    monkeypatch.setattr(ap, "_lstat_regular", _inodo_lstat)
    with pytest.raises(ap.PassportError, match="artifact inode changed"):
        ap.verify_artifact_passport(passport_path, mp4, KEY)
    monkeypatch.setattr(ap, "_lstat_regular", real_lstat)
