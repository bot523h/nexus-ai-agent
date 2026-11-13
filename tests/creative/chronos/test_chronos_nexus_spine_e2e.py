"""Adversarial real-disk tests for the Chronos→NEXUS staged execution spine."""

from __future__ import annotations

import hashlib
import json
import wave
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from test_multicamera_mastering import _fixture_shot

from nexus_ai_agent.creative.chronos import (
    CHRONOS_SPINE_OPERATIONS,
    CHRONOS_STAGED_SPINE_MASTER,
    REAL_MEDIA_OPERATION,
    CharacterIdentityAnchor,
    ChronosSpineAttempt,
    ChronosSpineExecutionError,
    ChronosVerificationRefusalError,
    RealMediaShot,
    compute_chronos_recipe_identity,
    dispatch_chronos_spine_operation,
    jcs_canonical_json,
    merkle_ledger,
)
from nexus_ai_agent.creative.chronos import nexus_spine_adapter as spine_adapter
from nexus_ai_agent.creative.chronos.acoustic_space_continuity import AcousticRoomSpec
from nexus_ai_agent.creative.chronos.multicamera_mastering import MultiCameraShotInput
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem


def _anchor() -> CharacterIdentityAnchor:
    return CharacterIdentityAnchor(
        "spine-character",
        np.array(
            [
                [-0.06, -0.05, 0.0],
                [0.06, -0.05, 0.0],
                [-0.05, 0.05, 0.0],
                [0.05, 0.05, 0.0],
                [-0.025, 0.0, 0.015],
                [0.025, 0.0, -0.015],
            ],
            dtype=np.float64,
        ),
    )


def _attempt(
    *,
    project_id: str = "spine-project",
    parent_revision_id: str = "sha256:parent-revision",
    attempt_id: str = "attempt-spine",
    fencing_token: int = 7,
    parameters: dict[str, object] | None = None,
    shots: tuple[MultiCameraShotInput, ...] | None = None,
) -> ChronosSpineAttempt:
    return ChronosSpineAttempt(
        project_id,
        parent_revision_id,
        attempt_id,
        fencing_token,
        "chronos.omniverse_mastering",
        parameters or {"max_delta_e00": 1.0, "render_mp4": False},
        multicamera_shots=shots or (_fixture_shot(1), _fixture_shot(2)),
        show_bible_anchor=_anchor(),
        room=AcousticRoomSpec("spine-stage", (12.0, 10.0, 4.0), (0.25,) * 6),
        speaker_position_m=np.array([3.0, 5.0, 2.0]),
    )


def _reader(expected: int) -> Any:
    return lambda project_id, attempt_id: expected


def _real_media_attempt() -> ChronosSpineAttempt:
    anchor = _anchor()
    frames = np.full((2, 8, 8, 3), (0.24, 0.34, 0.42), dtype=np.float64)
    shots = tuple(
        RealMediaShot(
            1,
            shot_index,
            frames.copy(),
            anchor.cranial_landmarks_m.copy(),
            np.zeros(4004, dtype=np.float64),
        )
        for shot_index in (1, 2)
    )
    return ChronosSpineAttempt(
        "real-media-project",
        "sha256:real-media-parent",
        "real-media-attempt",
        4,
        REAL_MEDIA_OPERATION,
        {
            "episode_count": 1,
            "shots_per_episode": 2,
            "frame_count_per_shot": 2,
            "frame_size": (8, 8),
            "max_delta_e00": 1.0,
            "render_mp4": False,
        },
        real_media_shots=shots,
        show_bible_anchor=anchor,
    )


def test_omniverse_mastering_is_disk_verified_then_atomically_published(
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    workspace = tmp_path / "execution-root"
    receipt = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        workspace,
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )

    record = receipt.asset_version
    assert CHRONOS_STAGED_SPINE_MASTER in CHRONOS_SPINE_OPERATIONS
    assert receipt.verification.verification_tier == "REAL-DISK"
    assert receipt.verification.sample_drift == 0
    assert receipt.verification.max_delta_e00 < 1.0
    assert receipt.verification.epipolar_sampson_error is not None
    assert receipt.verification.epipolar_sampson_error < 1e-10
    assert receipt.verification.rt60_error == 0.0
    assert record.project_id == attempt.project_id
    assert record.parent_revision_id == attempt.parent_revision_id
    assert record.attempt_id == attempt.attempt_id
    assert record.fencing_token == attempt.fencing_token
    assert record.produced_revision_id.startswith("rev_")
    assert record.asset_version_id.startswith("av_")
    assert record.recipe_digest == receipt.recipe_identity.recipe_digest
    assert record.merkle_root == receipt.verification.merkle_root
    assert record.verification_tier == "REAL-DISK"
    assert record.verified_at_utc.endswith("+00:00")
    assert receipt.artifact_passport.passport_digest
    assert receipt.artifact_passport.asset_version_id == record.asset_version_id
    assert receipt.artifact_passport.output_content_sha256 == record.output_content_sha256
    passport_projection = receipt.artifact_passport.to_dict()
    assert passport_projection["passport_digest"] == receipt.artifact_passport.passport_digest

    published = Path(record.published_path)
    assert published.is_dir()
    assert not (workspace / ".staging" / f"{attempt.attempt_id}_{attempt.fencing_token}").exists()
    output_fs = WorkspaceFilesystem(published)
    for relative, expected in record.published_file_digests:
        actual, size = output_fs.hash_file(relative)
        assert actual == expected
        assert size > 0
    assert {item.relative_path for item in receipt.verification.files} == {
        relative for relative, _ in record.published_file_digests
    }
    manifest = json.loads(output_fs.read_text("media/season-continuity-manifest.json"))
    assert manifest["sample_count"] == manifest["exact_sample_horizon"] == 8008
    assert manifest["merkle_root_sha256"] == record.merkle_root
    recomputed = merkle_ledger.merkle_root(manifest["receipts"]).hex()
    assert recomputed == record.merkle_root
    assert all(
        merkle_ledger.verify_rfc6962_bundle(bundle) for bundle in manifest["inclusion_bundles"]
    )
    for index in range(1, 3):
        wav_path = published / f"media/episode-0001/shot-{index:04d}/audio.wav"
        with wave.open(str(wav_path), "rb") as reader:
            assert reader.getnchannels() == 2
            assert reader.getnframes() == 4004
            assert reader.getframerate() == 48_000


def test_real_media_is_disk_verified_then_atomically_published(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "real-media-root"
    receipt = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        workspace,
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )

    assert receipt.asset_version.published_path
    assert receipt.verification.verification_tier == "REAL-DISK"
    assert receipt.verification.sample_drift == 0
    assert receipt.verification.max_delta_e00 < 1.0
    published = Path(receipt.asset_version.published_path)
    assert published.is_dir()
    assert len(receipt.verification.files) == 10
    assert not (workspace / ".staging" / "real-media-attempt_4").exists()
    for relative, expected in receipt.asset_version.published_file_digests:
        actual, size = WorkspaceFilesystem(published).hash_file(relative)
        assert actual == expected and size > 0


def test_optional_mp4_is_hashed_and_decoded_from_persisted_bytes(tmp_path: Path) -> None:
    base = _real_media_attempt()
    attempt = replace(
        base,
        attempt_id="mp4-attempt",
        parameters={**base.parameters, "render_mp4": True},
    )
    receipt = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        tmp_path / "mp4-root",
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )
    mp4_records = [
        item for item in receipt.verification.files if item.relative_path.endswith(".mp4")
    ]
    assert len(mp4_records) == 2
    for item in mp4_records:
        assert item.size_bytes > 12
        assert item.sha256 in dict(receipt.asset_version.published_file_digests).values()

    tampered_attempt = replace(attempt, attempt_id="mp4-tampered")
    tampered_workspace = tmp_path / "mp4-tamper-root"

    def tamper_mp4(stage: Path) -> None:
        mp4 = next(stage.rglob("*.mp4"))
        content = bytearray(mp4.read_bytes())
        content[-1] ^= 1
        mp4.write_bytes(content)

    with pytest.raises(ChronosVerificationRefusalError, match="differs from its receipt"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            tampered_workspace,
            tampered_attempt,
            active_fencing_token=_reader(tampered_attempt.fencing_token),
            before_verify=tamper_mp4,
        )
    assert not (tampered_workspace / "published").exists()


def test_existing_asset_version_is_never_overwritten(tmp_path: Path) -> None:
    attempt = _attempt(attempt_id="immutable-version", fencing_token=13)
    workspace = tmp_path / "immutable-root"
    first = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        workspace,
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )
    published = Path(first.asset_version.published_path)
    before = {
        relative: (published / relative).read_bytes()
        for relative, _ in first.asset_version.published_file_digests
    }

    with pytest.raises(ChronosSpineExecutionError, match="destination already exists"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
        )
    assert all((published / relative).read_bytes() == data for relative, data in before.items())
    assert not (workspace / ".staging" / f"{attempt.attempt_id}_{attempt.fencing_token}").exists()


def test_duplicate_stage_refusal_does_not_move_another_owner_stage(tmp_path: Path) -> None:
    attempt = _attempt(attempt_id="owned-stage", fencing_token=14)
    workspace = tmp_path / "stage-collision-root"
    stage = workspace / ".staging" / f"{attempt.attempt_id}_{attempt.fencing_token}"
    stage.mkdir(parents=True)
    marker = stage / "owner-marker"
    marker.write_bytes(b"active worker data")

    with pytest.raises(ChronosSpineExecutionError, match="staging path already exists"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
        )
    assert marker.read_bytes() == b"active worker data"
    assert not (workspace / ".staging" / "_quarantine").exists()


def test_tampered_private_png_is_quarantined_without_any_publish(tmp_path: Path) -> None:
    attempt = _attempt(attempt_id="tampered-attempt", fencing_token=3)
    workspace = tmp_path / "tamper-root"

    def corrupt_one_png(stage: Path) -> None:
        png = next((stage / "media").rglob("frame-00000.png"))
        content = bytearray(png.read_bytes())
        content[len(content) // 2] ^= 1
        png.write_bytes(content)

    with pytest.raises(ChronosVerificationRefusalError, match="differs from its receipt"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=corrupt_one_png,
        )
    assert not (workspace / "published").exists()
    quarantined = list(
        (workspace / ".staging" / "_quarantine").glob("tampered-attempt_3.*.refused")
    )
    assert len(quarantined) == 1 and quarantined[0].is_dir()
    assert (
        not list((workspace / "published").rglob("*"))
        if (workspace / "published").exists()
        else True
    )


def test_fencing_token_advanced_during_verification_refuses_before_publish(
    tmp_path: Path,
) -> None:
    attempt = _attempt(attempt_id="zombie-worker", fencing_token=11)
    workspace = tmp_path / "fence-root"
    observed = iter((11, 11, 12))

    with pytest.raises(ChronosVerificationRefusalError, match="stale"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=lambda project_id, attempt_id: next(observed),
        )
    assert not (workspace / "published").exists()
    quarantined = list((workspace / ".staging" / "_quarantine").glob("zombie-worker_11.*.refused"))
    assert len(quarantined) == 1 and quarantined[0].is_dir()


def test_recipe_identity_binds_parameters_lineage_and_media_bytes() -> None:
    base = _attempt()
    first = compute_chronos_recipe_identity(base)
    assert compute_chronos_recipe_identity(base).recipe_digest == first.recipe_digest
    changed_parameter = replace(base, parameters={"max_delta_e00": 0.3, "render_mp4": False})
    changed_parent = replace(base, parent_revision_id="sha256:different-parent")
    changed_anchor = replace(
        base,
        show_bible_anchor=replace(_anchor(), reference_illuminant_kelvin=6200.0),
    )
    first_shot = base.multicamera_shots[0]
    changed_media_shot = replace(
        first_shot,
        camera_a_frames_linear_rgb=first_shot.camera_a_frames_linear_rgb + 0.001,
    )
    changed_media = replace(
        base, multicamera_shots=(changed_media_shot, *base.multicamera_shots[1:])
    )
    digests = {
        compute_chronos_recipe_identity(changed).recipe_digest
        for changed in (changed_parameter, changed_parent, changed_anchor, changed_media)
    }
    assert len(digests) == 4
    assert first.recipe_digest not in digests
    assert first.engine_version == "chronos-omniverse-1.0"
    assert len(first.input_media_digests) >= 8
    assert first.parameters["max_delta_e00"] == 1.0


def test_jcs_canonicalization_uses_rfc8785_number_and_utf16_key_rules() -> None:
    assert jcs_canonical_json({"z": 1, "a": 2}) == '{"a":2,"z":1}'
    assert jcs_canonical_json(333333333.33333329) == "333333333.3333333"
    assert jcs_canonical_json(1e-7) == "1e-7"
    assert jcs_canonical_json(1e21) == "1e+21"
    assert jcs_canonical_json({"\U00010000": 1, "\ue000": 2}) == '{"𐀀":1,"\ue000":2}'
    assert hashlib.sha256(jcs_canonical_json({"x": 1}).encode()).hexdigest()


@pytest.mark.parametrize(
    "invalid_value",
    [float("nan"), float("inf"), 1 << 53, chr(0xD800), b"not-json", {1: "not-a-string-key"}],
)
def test_jcs_rejects_non_interoperable_values(invalid_value: object) -> None:
    with pytest.raises(ChronosSpineExecutionError):
        jcs_canonical_json(invalid_value)


def test_recipe_identity_rejects_invalid_lineage_and_input_buffers() -> None:
    base = _attempt()
    first = compute_chronos_recipe_identity(base)
    assert first.parameters["max_delta_e00"] == 1.0
    first.parameters["max_delta_e00"] = 0.1
    assert compute_chronos_recipe_identity(base).parameters["max_delta_e00"] == 1.0
    invalid_attempts = (
        replace(base, project_id="../escape"),
        replace(base, parent_revision_id="../escape"),
        replace(base, parent_revision_id=".."),
        replace(base, attempt_id="../escape"),
        replace(base, fencing_token=True),
        replace(base, operation_name="unknown.operation"),
        replace(base, parameters=None),
        replace(base, show_bible_anchor=None),
        replace(base, multicamera_shots=base.multicamera_shots[:1]),
        replace(
            base,
            multicamera_shots=(
                replace(
                    base.multicamera_shots[0],
                    camera_a_frames_linear_rgb=np.full((2, 8, 8, 3), np.nan),
                ),
                base.multicamera_shots[1],
            ),
        ),
    )
    for attempt in invalid_attempts:
        with pytest.raises(ChronosSpineExecutionError):
            compute_chronos_recipe_identity(attempt)

    malformed_identity = replace(first, normalized_parameters_json="[]")
    with pytest.raises(ChronosSpineExecutionError, match="not an object"):
        _ = malformed_identity.parameters


def test_nexus_execution_identity_contract_failure_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def reject_identity(**_values: object) -> None:
        raise ValueError("identity contract rejected attempt")

    monkeypatch.setattr(spine_adapter, "ExecutionIdentity", reject_identity)
    attempt = _attempt()
    with pytest.raises(ChronosSpineExecutionError, match="execution identity rejected"):
        spine_adapter.execute_chronos_staged_attempt(
            tmp_path / "execution-identity-root",
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
        )


def test_fence_authority_failure_refuses_before_creating_stage(tmp_path: Path) -> None:
    attempt = _attempt(attempt_id="no-authority", fencing_token=17)
    workspace = tmp_path / "no-authority-root"

    def unavailable(_project: str, _attempt: str) -> int:
        raise OSError("authority unavailable")

    with pytest.raises(ChronosVerificationRefusalError, match="authority was unavailable"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=unavailable,
        )
    assert not (workspace / ".staging").exists()


def test_engine_parameter_failures_are_quarantined(tmp_path: Path) -> None:
    base = _attempt()
    invalid_attempts = (
        replace(base, parameters={**base.parameters, "unknown_control": 1}),
        replace(base, parameters={**base.parameters, "render_mp4": "false"}),
        replace(base, room=None),
    )
    for index, attempt in enumerate(invalid_attempts):
        workspace = tmp_path / f"invalid-engine-{index}"
        with pytest.raises(ChronosSpineExecutionError):
            dispatch_chronos_spine_operation(
                CHRONOS_STAGED_SPINE_MASTER,
                workspace,
                attempt,
                active_fencing_token=_reader(attempt.fencing_token),
            )
        assert not (workspace / "published").exists()
        quarantined = list((workspace / ".staging" / "_quarantine").glob("*.refused"))
        assert len(quarantined) == 1


def test_invalid_frame_geometry_and_unknown_bridge_operation_refuse(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    invalid_size = replace(attempt, parameters={**attempt.parameters, "frame_size": (4, 4)})
    with pytest.raises(ChronosSpineExecutionError, match="frame_size"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            tmp_path / "invalid-size",
            invalid_size,
            active_fencing_token=_reader(invalid_size.fencing_token),
        )
    with pytest.raises(ValueError, match="unsupported Chronos spine operation"):
        dispatch_chronos_spine_operation(
            "unregistered.operation",
            tmp_path / "unknown-operation",
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
        )


def test_spine_receipt_recipe_mismatch_refuses_before_publish(tmp_path: Path) -> None:
    attempt = _attempt(attempt_id="receipt-tamper", fencing_token=18)
    workspace = tmp_path / "receipt-tamper-root"

    def tamper_receipt(stage: Path) -> None:
        receipt_path = stage / "chronos-spine-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["recipe_digest"] = "0" * 64
        receipt_path.write_text(jcs_canonical_json(receipt), encoding="utf-8")

    with pytest.raises(ChronosVerificationRefusalError, match="staged spine receipt"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=tamper_receipt,
        )
    assert not (workspace / "published").exists()
    assert len(list((workspace / ".staging" / "_quarantine").glob("*.refused"))) == 1


def test_mutated_inclusion_bundle_refuses_before_publish(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "proof-tamper-root"

    def tamper_proof(stage: Path) -> None:
        manifest_path = stage / "media" / "season-continuity-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["inclusion_bundles"][0]["root_sha256"] = "0" * 64
        manifest_path.write_text(jcs_canonical_json(manifest), encoding="utf-8")

    with pytest.raises(ChronosVerificationRefusalError, match="inclusion proof"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=tamper_proof,
        )
    assert not (workspace / "published").exists()


def test_unexpected_private_file_is_refused(tmp_path: Path) -> None:
    attempt = _attempt(attempt_id="extra-file", fencing_token=19)
    workspace = tmp_path / "extra-file-root"

    def add_untracked_file(stage: Path) -> None:
        (stage / "untracked.bin").write_bytes(b"not in manifest")

    with pytest.raises(ChronosVerificationRefusalError, match="unexpected staged file"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=add_untracked_file,
        )
    assert not (workspace / "published").exists()


def test_real_media_preflight_rejects_missing_and_mixed_shot_sets() -> None:
    base = _real_media_attempt()
    invalid_attempts = (
        replace(base, real_media_shots=()),
        replace(base, multicamera_shots=(_fixture_shot(1), _fixture_shot(2))),
        replace(base, show_bible_anchor=None),
    )
    for attempt in invalid_attempts:
        with pytest.raises(ChronosSpineExecutionError):
            compute_chronos_recipe_identity(attempt)


def test_real_media_engine_rejects_unknown_and_invalid_controls(tmp_path: Path) -> None:
    base = _real_media_attempt()
    invalid_attempts = (
        replace(base, parameters={**base.parameters, "unsupported": True}),
        replace(base, parameters={**base.parameters, "render_mp4": 1}),
        replace(base, parameters={**base.parameters, "frame_size": (4, 8)}),
    )
    for attempt in invalid_attempts:
        with pytest.raises(ChronosSpineExecutionError):
            spine_adapter._engine_run(attempt, tmp_path / attempt.attempt_id)


def test_mp4_mux_rejects_unreadable_and_malformed_manifests(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing-manifest"
    missing_root.mkdir()
    with pytest.raises(ChronosSpineExecutionError, match="cannot load"):
        spine_adapter._render_optional_mp4(missing_root, {})

    fs = WorkspaceFilesystem(missing_root)
    fs.ensure_directory("media")
    manifest_path = missing_root / "media" / "season-continuity-manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ChronosSpineExecutionError, match="rational frame rate"):
        spine_adapter._render_optional_mp4(missing_root, {"ffmpeg_path": "/unused/ffmpeg"})

    manifest = {"frame_rate": "24/1", "receipts": [False]}
    fs.write_text("media/season-continuity-manifest.json", jcs_canonical_json(manifest))
    with pytest.raises(ChronosSpineExecutionError, match="receipt is malformed"):
        spine_adapter._render_optional_mp4(missing_root, {"ffmpeg_path": "/unused/ffmpeg"})


def test_manifest_parser_refuses_malformed_and_unrecognized_receipts(tmp_path: Path) -> None:
    root = tmp_path / "manifest-parser"
    root.mkdir()
    fs = WorkspaceFilesystem(root)
    fs.ensure_directory("media")
    fs.write_text("media/season-continuity-manifest.json", "{")
    with pytest.raises(ChronosVerificationRefusalError, match="unreadable"):
        spine_adapter._read_manifest(root)
    fs.write_text("media/season-continuity-manifest.json", jcs_canonical_json({"schema": "wrong"}))
    with pytest.raises(ChronosVerificationRefusalError, match="unsupported schema"):
        spine_adapter._read_manifest(root)


def test_noncanonical_manifest_and_modified_proof_nodes_refuse(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "noncanonical-manifest"

    def add_whitespace(stage: Path) -> None:
        manifest_path = stage / "media" / "season-continuity-manifest.json"
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")

    with pytest.raises(ChronosVerificationRefusalError, match="not canonical JSON"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=add_whitespace,
        )

    def remove_proof_nodes(stage: Path) -> None:
        manifest_path = stage / "media" / "season-continuity-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["inclusion_bundles"][0].pop("proof", None)
        manifest_path.write_text(jcs_canonical_json(manifest), encoding="utf-8")

    proof_attempt = replace(attempt, attempt_id="missing-proof-nodes")
    with pytest.raises(ChronosVerificationRefusalError, match="inclusion proof"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            tmp_path / "proof-nodes-root",
            proof_attempt,
            active_fencing_token=_reader(proof_attempt.fencing_token),
            before_verify=remove_proof_nodes,
        )


def test_exact_sample_rate_and_rational_fields_are_enforced(tmp_path: Path) -> None:
    base = _real_media_attempt()
    changes = (
        ("wrong-sample-rate", lambda manifest: manifest.update(sample_rate_hz=44_100)),
        ("bad-frame-rate", lambda manifest: manifest.update(frame_rate="24fps")),
        ("zero-frame-rate-denominator", lambda manifest: manifest.update(frame_rate="24/0")),
    )
    for attempt_id, update in changes:
        attempt = replace(base, attempt_id=attempt_id)
        workspace = tmp_path / attempt_id

        def tamper_clock(stage: Path, mutate: Any = update) -> None:
            manifest_path = stage / "media" / "season-continuity-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            mutate(manifest)
            manifest_path.write_text(jcs_canonical_json(manifest), encoding="utf-8")

        with pytest.raises(ChronosVerificationRefusalError):
            dispatch_chronos_spine_operation(
                CHRONOS_STAGED_SPINE_MASTER,
                workspace,
                attempt,
                active_fencing_token=_reader(attempt.fencing_token),
                before_verify=tamper_clock,
            )
        assert not (workspace / "published").exists()


def test_missing_and_bad_digest_file_refuse_closed(tmp_path: Path) -> None:
    root = tmp_path / "verified-file-root"
    root.mkdir()
    fs = WorkspaceFilesystem(root)
    fs.write_bytes("file.bin", b"valid content")
    valid_digest = hashlib.sha256(b"valid content").hexdigest()
    assert spine_adapter._verify_staged_file(fs, "file.bin", valid_digest)[1] > 0
    with pytest.raises(ChronosVerificationRefusalError, match="invalid SHA-256"):
        spine_adapter._verify_staged_file(fs, "file.bin", "not-a-digest")
    with pytest.raises(ChronosVerificationRefusalError, match="differs from its receipt"):
        spine_adapter._verify_staged_file(fs, "file.bin", hashlib.sha256(b"other").hexdigest())
    with pytest.raises(ChronosVerificationRefusalError, match="missing or unsafe"):
        spine_adapter._verify_staged_file(fs, "missing.bin", valid_digest)
    with pytest.raises(ChronosVerificationRefusalError, match="decoder could not run"):
        spine_adapter._decode_mp4_bytes(b"invalid", {"ffmpeg_path": "/missing/ffmpeg"})
