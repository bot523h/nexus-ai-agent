"""Disk and publish falsification gates; a healed-in-memory report is insufficient."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas import veritas_spine_adapter as spine
from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import (
    _inclusion_proof,
    _leaf_digest,
    _merkle_root,
    compile_and_heal_real_media_shot,
)


def request(root: Path) -> spine.VeritasSpinePublishRequest:
    return spine.VeritasSpinePublishRequest(
        "project",
        "revision",
        "shot",
        "attempt",
        7,
        root,
        root / "published",
        input_frames_rgb=np.zeros((2, 8, 8, 3)),
        input_audio_pcm=np.zeros(100),
    )


def test_staged_publish_and_relocated_verification(tmp_path: Path) -> None:
    result = spine.VeritasStagedSpineAdapter(lambda: 7).execute(request(tmp_path))
    assert result.post_disk_verified and result.compile_result.post_disk_verified
    assert result.asset_version.fencing_token == 7
    assert Path(result.asset_version.published_wav_path).is_file()
    assert Path(result.asset_version.published_manifest_path).is_file()
    assert len(result.asset_version.asset_version_id) == 64
    assert not (tmp_path / ".staging" / "attempt_7").exists()
    assert spine.verify_compiled_veritas_bundle_on_disk(result.compile_result)


def test_rfc6962_unbalanced_tree_known_shape_and_all_proofs() -> None:
    leaves = [_leaf_digest({"i": i}) for i in range(15)]

    def reference(values: list[str]) -> str:
        if len(values) == 1:
            return values[0]
        split = 1
        while 2 * split < len(values):
            split *= 2
        return hashlib.sha256(
            b"\x01"
            + bytes.fromhex(reference(values[:split]))
            + bytes.fromhex(reference(values[split:]))
        ).hexdigest()

    assert _merkle_root([]) == hashlib.sha256(b"").hexdigest()
    assert _merkle_root(leaves) == reference(leaves)
    for i, leaf in enumerate(leaves):
        proof = _inclusion_proof(leaves, i)
        assert spine._verify_inclusion(bytes.fromhex(leaf), i, 15, proof, reference(leaves))
        assert not spine._verify_inclusion(
            bytes.fromhex(leaf), i, 15, proof[:-1], reference(leaves)
        )
        assert not spine._verify_inclusion(bytes.fromhex(leaf), i, 15, ["zz"], reference(leaves))
        assert not spine._verify_inclusion(
            bytes.fromhex(leaf), i, 15, proof + [leaf], reference(leaves)
        )


@pytest.mark.parametrize("kind", ["png", "wav", "leaf", "proof", "delete", "manifest_symlink"])
def test_tamper_refuses_without_published_bytes(tmp_path: Path, kind: str) -> None:
    def tamper(stage: Path) -> None:
        manifest_path = stage / "veritas_merkle_bundle.json"
        if kind in ("leaf", "proof"):
            data = json.loads(manifest_path.read_text())
            if kind == "leaf":
                data["leaf_payloads"][0]["output_audio_sha256"] = "0" * 64
            else:
                data["inclusion_proofs"][0][0] = "0" * 64
            manifest_path.write_text(json.dumps(data))
        elif kind == "manifest_symlink":
            other = stage / "copy.json"
            manifest_path.rename(other)
            manifest_path.symlink_to(other)
        else:
            path = stage / ("frame_0000.png" if kind != "wav" else "dialogue.wav")
            if kind == "delete":
                path.unlink()
            else:
                raw = bytearray(path.read_bytes())
                raw[-1] ^= 1
                path.write_bytes(raw)

    with pytest.raises(spine.VeritasSpineVerificationError):
        spine.VeritasStagedSpineAdapter(lambda: 7, tamper).execute(request(tmp_path))
    assert not (tmp_path / "published").exists()
    assert not (tmp_path / ".staging" / "attempt_7").exists()


@pytest.mark.parametrize("when", ["before", "during"])
def test_fence_refusal(tmp_path: Path, when: str) -> None:
    token = [6 if when == "before" else 7]

    def supersede(stage: Path) -> None:
        token[0] = 8

    with pytest.raises(spine.VeritasStaleFencingTokenError):
        spine.VeritasStagedSpineAdapter(lambda: token[0], supersede).execute(request(tmp_path))
    assert not (tmp_path / "published").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", ""),
        ("revision_id", "../x"),
        ("shot_id", "/absolute"),
        ("attempt_id", "a/b"),
        ("fencing_token", 0),
        ("fencing_token", True),
        ("fps", 30),
        ("sample_rate_hz", 16000),
    ],
)
def test_request_validation(tmp_path: Path, field: str, value: Any) -> None:
    with pytest.raises(spine.VeritasSpineVerificationError):
        spine.VeritasStagedSpineAdapter(lambda: 7).execute(
            replace(request(tmp_path), **{field: value})
        )
    assert not (tmp_path / "published").exists()


def test_workspace_boundary_and_staging_symlink(tmp_path: Path) -> None:
    adapter = spine.VeritasStagedSpineAdapter(lambda: 7)
    with pytest.raises(spine.VeritasSpineVerificationError, match="within"):
        adapter.execute(replace(request(tmp_path), published_root=tmp_path.parent / "outside"))
    with pytest.raises(spine.VeritasSpineVerificationError, match="within"):
        adapter.execute(replace(request(tmp_path), published_root=tmp_path))
    with pytest.raises(spine.VeritasSpineVerificationError, match="staging"):
        adapter.execute(replace(request(tmp_path), published_root=tmp_path / ".staging" / "pub"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / ".staging").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(spine.VeritasSpineVerificationError, match="staging"):
        adapter.execute(request(tmp_path))


def test_existing_publication_and_false_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = tmp_path / "published" / "shot"
    published.mkdir(parents=True)
    sentinel = published / "existing"
    sentinel.write_text("keep")
    with pytest.raises(spine.VeritasSpineVerificationError, match="exists"):
        spine.VeritasStagedSpineAdapter(lambda: 7).execute(request(tmp_path))
    assert sentinel.read_text() == "keep"
    monkeypatch.setattr(spine, "verify_compiled_veritas_bundle_on_disk", lambda _: False)
    with pytest.raises(spine.VeritasSpineVerificationError, match="returned false"):
        spine.VeritasStagedSpineAdapter(lambda: 7).execute(request(tmp_path))


@pytest.mark.parametrize(
    "change",
    [
        "name",
        "empty",
        "symlink",
        "duplicate",
        "no_frames",
        "rate",
        "digest",
        "tree",
        "receipt",
        "proof",
        "post",
        "mp4",
        "physical",
        "empty_audio",
    ],
)
def test_independent_verifier_failures(tmp_path: Path, change: str) -> None:
    from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import _write_wav

    bundle = compile_and_heal_real_media_shot(
        tmp_path,
        input_frames=np.zeros((2, 8, 8, 3)),
        input_audio_pcm=np.zeros(100),
        mux_mp4=False,
    )
    manifest_path = tmp_path / "veritas_merkle_bundle.json"
    manifest = json.loads(manifest_path.read_text())
    if change == "name":
        manifest["png_names"][0] = "../outside.png"
    elif change == "empty":
        Path(bundle.png_paths[0]).write_bytes(b"")
    elif change == "symlink":
        path = Path(bundle.png_paths[0])
        path.unlink()
        path.symlink_to(Path(bundle.png_paths[1]))
    elif change == "duplicate":
        manifest["png_names"] = [manifest["png_names"][0]] * 2
    elif change == "no_frames":
        manifest["png_names"] = []
    elif change == "rate":
        manifest["sample_rate_hz"] = 16000
    elif change == "digest":
        manifest["output_frames_content_sha256"] = "0" * 64
    elif change == "tree":
        manifest["inclusion_proofs"] = []
    elif change == "receipt":
        manifest["leaf_payloads"][0]["law"] = 2
    elif change == "proof":
        manifest["inclusion_proofs"][0][0] = "0" * 64
    elif change == "post":
        manifest["post_heal_violations"] = 1
    elif change == "mp4":
        manifest["mp4_name"] = "fake.mp4"
        (tmp_path / "fake.mp4").write_bytes(b"not an mp4")
    elif change == "empty_audio":
        _write_wav(Path(bundle.wav_path), np.zeros(0), 48000)
    else:
        # Fresh content-bound tree, but a raw decoded observation remains bad.
        bundle = compile_and_heal_real_media_shot(
            tmp_path,
            input_frames=np.random.default_rng(4).random((2, 8, 8, 3)),
            input_audio_pcm=np.zeros(100),
            mux_mp4=False,
        )
        manifest = json.loads(manifest_path.read_text())
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(spine.VeritasMediaVerificationError):
        spine.verify_compiled_veritas_bundle_on_disk(bundle)


def test_extra_artifact_clock_traversal_and_staging_reuse(tmp_path: Path) -> None:
    bundle = compile_and_heal_real_media_shot(
        tmp_path / "bundle",
        input_frames=np.zeros((2, 8, 8, 3)),
        input_audio_pcm=np.zeros(100),
        mux_mp4=False,
    )
    extra = Path(bundle.output_dir) / "unbound.txt"
    extra.write_text("not in evidence")
    with pytest.raises(spine.VeritasMediaVerificationError, match="unbound"):
        spine.verify_compiled_veritas_bundle_on_disk(bundle)
    extra.unlink()
    manifest_path = Path(bundle.output_dir) / "veritas_merkle_bundle.json"
    data = json.loads(manifest_path.read_text())
    data["fps"] = 30
    manifest_path.write_text(json.dumps(data))
    with pytest.raises(spine.VeritasMediaVerificationError, match="clock"):
        spine.verify_compiled_veritas_bundle_on_disk(bundle)
    adapter = spine.VeritasStagedSpineAdapter(lambda: 7)
    with pytest.raises(spine.VeritasSpineVerificationError, match="traversal"):
        adapter.execute(replace(request(tmp_path), workspace_root=tmp_path / "x" / ".."))
    stage = tmp_path / ".staging" / "attempt_7"
    stage.mkdir(parents=True)
    sentinel = stage / "existing"
    sentinel.write_text("keep")
    with pytest.raises(spine.VeritasSpineVerificationError, match="already exists"):
        adapter.execute(request(tmp_path))
    assert sentinel.read_text() == "keep"


def test_raw_physical_audit_refuses_falsely_clean_compiler_receipt(tmp_path: Path) -> None:
    # Simulate a faulty compiler claiming success: all file hashes and Merkle
    # paths are valid, but its pixels still contain an observed physical defect.
    bundle = compile_and_heal_real_media_shot(
        tmp_path,
        input_frames=np.random.default_rng(44).random((2, 8, 8, 3)),
        input_audio_pcm=np.zeros(100),
        mux_mp4=False,
    )
    path = tmp_path / "veritas_merkle_bundle.json"
    manifest = json.loads(path.read_text())
    for leaf in manifest["leaf_payloads"]:
        leaf["violations_after"] = []
    leaves = [_leaf_digest(payload) for payload in manifest["leaf_payloads"]]
    root = _merkle_root(leaves)
    manifest.update(
        merkle_root=root,
        post_heal_violations=0,
        inclusion_proofs=[_inclusion_proof(leaves, i) for i in range(15)],
    )
    path.write_text(json.dumps(manifest))
    faulty_bundle = replace(bundle, merkle_root=root, post_heal_violations=0)
    with pytest.raises(spine.VeritasMediaVerificationError, match="physical observations"):
        spine.verify_compiled_veritas_bundle_on_disk(faulty_bundle)


def test_mp4_decode_and_byte_tamper_refusal(tmp_path: Path) -> None:
    bundle = compile_and_heal_real_media_shot(
        tmp_path,
        input_frames=np.zeros((2, 8, 8, 3)),
        input_audio_pcm=np.zeros(100),
        mux_mp4=True,
    )
    if bundle.mp4_path is None:
        pytest.skip("local FFmpeg is optional")
    assert spine.verify_compiled_veritas_bundle_on_disk(bundle)
    mp4 = Path(bundle.mp4_path)
    mp4.write_bytes(b"not an mp4")
    with pytest.raises(spine.VeritasMediaVerificationError, match="digest"):
        spine.verify_compiled_veritas_bundle_on_disk(bundle)
    # Even a newly anchored hash cannot make undecodable MP4 bytes valid.
    digest = hashlib.sha256(mp4.read_bytes()).hexdigest()
    manifest_path = tmp_path / "veritas_merkle_bundle.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["output_mp4_sha256"] = digest
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(spine.VeritasMediaVerificationError, match="decode"):
        spine.verify_compiled_veritas_bundle_on_disk(replace(bundle, output_mp4_sha256=digest))
