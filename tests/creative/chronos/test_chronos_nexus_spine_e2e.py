"""Adversarial real-disk tests for the Chronos→NEXUS staged execution spine."""

from __future__ import annotations

import hashlib
import json
import subprocess
import wave
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from test_multicamera_mastering import _fixture_shot

import nexus_ai_agent.creative.chronos.acoustic_space_continuity as acoustic_space_continuity
import nexus_ai_agent.creative.chronos.multicam_epipolar_matching as multicam_epipolar_matching
from nexus_ai_agent.creative.chronos import (
    CHRONOS_SPINE_OPERATIONS,
    CHRONOS_STAGED_SPINE_MASTER,
    REAL_MEDIA_OPERATION,
    CharacterIdentityAnchor,
    ChronosCommand,
    ChronosCommandAuthorizationError,
    ChronosDurableLineageStore,
    ChronosLedgerConflictError,
    ChronosLedgerError,
    ChronosLineageRepositoryProtocol,
    ChronosPort,
    ChronosSpineAttempt,
    ChronosSpineExecutionError,
    ChronosVerificationRefusalError,
    FileChronosLedger,
    InMemoryChronosLedger,
    RealMediaShot,
    build_chronos_staged_spine_handler,
    compute_chronos_recipe_identity,
    dispatch_chronos_spine_operation,
    jcs_canonical_json,
    merkle_ledger,
    register_chronos_staged_spine_operation,
    serialize_chronos_execution_error,
    staged_spine_command,
)
from nexus_ai_agent.creative.chronos import bridge as chronos_bridge
from nexus_ai_agent.creative.chronos import nexus_spine_adapter as spine_adapter
from nexus_ai_agent.creative.chronos.acoustic_space_continuity import AcousticRoomSpec
from nexus_ai_agent.creative.chronos.multicamera_mastering import MultiCameraShotInput
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    IdempotencyConflictError,
    Timeline,
    new_project,
)
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


def _assert_quarantine_receipt(
    workspace: Path, attempt: ChronosSpineAttempt, refusal_code: str
) -> Path:
    quarantined = workspace / ".quarantine" / f"{attempt.attempt_id}_{attempt.fencing_token}"
    receipt_path = quarantined / "quarantine_receipt.json"
    assert quarantined.is_dir()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["attempt_id"] == attempt.attempt_id
    assert receipt["fencing_token"] == attempt.fencing_token
    assert receipt["refusal_code"] == refusal_code
    assert isinstance(receipt["stage_digests"], dict)
    for relative, evidence in receipt["stage_digests"].items():
        data = (quarantined / relative).read_bytes()
        assert evidence["sha256"] == hashlib.sha256(data).hexdigest()
        assert evidence["size_bytes"] == len(data)
    return quarantined


def _resign_manifest_after_test_mutation(stage: Path, manifest: dict[str, Any]) -> None:
    root = merkle_ledger.merkle_root(manifest["receipts"])
    manifest["merkle_root_sha256"] = root.hex()
    manifest["inclusion_bundles"] = [
        merkle_ledger.to_rfc6962_bundle(
            item,
            merkle_ledger.build_inclusion_proof(manifest["receipts"], index),
            root,
        )
        for index, item in enumerate(manifest["receipts"])
    ]
    (stage / "media" / "season-continuity-manifest.json").write_text(
        jcs_canonical_json(manifest), encoding="utf-8"
    )


def _verify_with_test_mutations(
    stage: Path,
    attempt: ChronosSpineAttempt,
    expected_message: str | None,
    *,
    verification_attempt: ChronosSpineAttempt | None = None,
    mutate_summary: Any = None,
    mutate_manifest: Any = None,
) -> None:
    bound_attempt = attempt if verification_attempt is None else verification_attempt
    receipt_path = stage / "chronos-spine-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    summary = receipt["engine_summary"]
    if mutate_manifest is not None:
        manifest_path = stage / "media" / "season-continuity-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        mutate_manifest(manifest)
        _resign_manifest_after_test_mutation(stage, manifest)
        summary["merkle_root_sha256"] = manifest["merkle_root_sha256"]
    if mutate_summary is not None:
        mutate_summary(summary)
    receipt["engine_summary"] = summary
    try:
        receipt["recipe_digest"] = compute_chronos_recipe_identity(bound_attempt).recipe_digest
    except ChronosSpineExecutionError:
        if bound_attempt.show_bible_anchor is not None:
            raise
    receipt_path.write_text(jcs_canonical_json(receipt), encoding="utf-8")
    try:
        spine_adapter.verify_staged_chronos_artifacts(
            stage, bound_attempt, expected_summary=summary
        )
    except ChronosVerificationRefusalError as exc:
        if expected_message is None:
            raise
        assert expected_message in str(exc)
        raise
    if expected_message is not None:
        raise AssertionError(f"verification unexpectedly accepted {expected_message}")


def _authorized_command_bus(project_id: str) -> tuple[CommandBus, ActorIdentity]:
    actor = ActorIdentity(kind="service", actor_id="chronos-e2e-service")
    registry = build_wave1_registry()
    register_chronos_staged_spine_operation(registry)
    access = ProjectAccess(
        actor,
        project_id,
        frozenset({"project:write", "chronos:render"}),
    )
    project = new_project(project_id, "Chronos E2E", Timeline(timeline_id="main", duration_us=0))
    return CommandBus(project, registry=registry, authorizer=access), actor


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


def test_disk_verifier_covers_refusal_edges_for_bound_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = _attempt(attempt_id="branch-disk-verifier")

    no_anchor = replace(attempt, show_bible_anchor=None)
    original_compute = spine_adapter.compute_chronos_recipe_identity
    monkeypatch.setattr(
        spine_adapter,
        "compute_chronos_recipe_identity",
        lambda _attempt: original_compute(attempt),
    )
    with pytest.raises(ChronosVerificationRefusalError, match="no Show-Bible anchor"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            tmp_path / "branch-anchor",
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=lambda stage: _verify_with_test_mutations(
                stage,
                attempt,
                "no Show-Bible anchor",
                verification_attempt=no_anchor,
            ),
        )
    monkeypatch.setattr(spine_adapter, "compute_chronos_recipe_identity", original_compute)

    ordered_shots = (
        replace(attempt.multicamera_shots[0], shot_index=9),
        attempt.multicamera_shots[1],
    )
    changed_order = replace(attempt, multicamera_shots=ordered_shots)
    with pytest.raises(ChronosVerificationRefusalError, match="ordered exact input horizons"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            tmp_path / "branch-shot-order",
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=lambda stage: _verify_with_test_mutations(
                stage,
                attempt,
                "ordered exact input horizons",
                verification_attempt=changed_order,
            ),
        )

    missing_room = replace(attempt, room=None, speaker_position_m=None)
    with pytest.raises(ChronosVerificationRefusalError, match="room-verification inputs"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            tmp_path / "branch-room-inputs",
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=lambda stage: _verify_with_test_mutations(
                stage,
                attempt,
                "room-verification inputs",
                verification_attempt=missing_room,
            ),
        )

    bad_order = replace(attempt, parameters={**attempt.parameters, "max_reflection_order": -1})
    with pytest.raises(ChronosVerificationRefusalError, match="reflection order is invalid"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            tmp_path / "branch-reflection-order",
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=lambda stage: _verify_with_test_mutations(
                stage,
                attempt,
                "reflection order is invalid",
                verification_attempt=bad_order,
            ),
        )

    monkeypatch.setattr(
        spine_adapter,
        "measure_patch_delta_e00_by_frame",
        lambda *_args, **_kwargs: [2.0],
    )
    with pytest.raises(ChronosVerificationRefusalError, match="exceeds the configured threshold"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            tmp_path / "branch-color-threshold",
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=lambda stage: _verify_with_test_mutations(
                stage, attempt, "exceeds the configured threshold"
            ),
        )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("no-shots", "omits camera audit measurements"),
        ("bad-shot", "camera audit entry is malformed"),
        ("bad-epipolar", "camera audit metric is invalid"),
        ("epipolar-mismatch", "camera metrics differ"),
        ("bad-rt60", "RT60 metric differs"),
    ],
)
def test_disk_camera_summary_refusal_edges(case: str, message: str, tmp_path: Path) -> None:
    attempt = _attempt(attempt_id=f"branch-summary-{case}")

    def mutate(summary: dict[str, Any]) -> None:
        if case == "no-shots":
            summary["shots"] = []
        elif case == "bad-shot":
            summary["shots"][0] = None
        elif case == "bad-epipolar":
            summary["shots"][0]["max_sampson_distance_squared_after"] = "bad"
        elif case == "epipolar-mismatch":
            summary["shots"][0]["max_sampson_distance_squared_after"] = 1.0
            summary["shots"][1]["max_sampson_distance_squared_after"] = 1.0
        else:
            summary["rt60_max_spread_seconds"] = 1.0

    with pytest.raises(ChronosVerificationRefusalError, match=message):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            tmp_path / f"branch-summary-{case}",
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=lambda stage: _verify_with_test_mutations(
                stage,
                attempt,
                message,
                mutate_summary=mutate,
            ),
        )


def test_disk_verifier_rejects_non_object_receipt_after_merkle_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = _attempt(attempt_id="branch-non-object-receipt")

    def leaf_hash_json_scalars(receipt: Any) -> bytes:
        if isinstance(receipt, dict):
            return original_leaf_hash(receipt)
        return hashlib.sha256(b"\x00" + jcs_canonical_json(receipt).encode("utf-8")).digest()

    original_leaf_hash = merkle_ledger.leaf_hash
    monkeypatch.setattr(merkle_ledger, "leaf_hash", leaf_hash_json_scalars)
    monkeypatch.setattr(
        merkle_ledger,
        "to_rfc6962_bundle",
        lambda receipt, _proof, root: {"receipt": receipt, "root_sha256": root.hex()},
    )
    monkeypatch.setattr(merkle_ledger, "verify_rfc6962_bundle", lambda _bundle: True)

    def mutate(manifest: dict[str, Any]) -> None:
        manifest["receipts"][0] = None

    with pytest.raises(
        ChronosVerificationRefusalError, match="per-shot media receipt is not an object"
    ):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            tmp_path / "branch-non-object-receipt",
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=lambda stage: _verify_with_test_mutations(
                stage,
                attempt,
                "per-shot media receipt is not an object",
                mutate_manifest=mutate,
            ),
        )


def test_optional_mux_receipt_branch_is_verified_when_absent(tmp_path: Path) -> None:
    attempt = _attempt(attempt_id="branch-no-mux-receipt")

    def verify_without_mux(stage: Path) -> None:
        def remove_mux(manifest: dict[str, Any]) -> None:
            for item in manifest["receipts"]:
                item.pop("mux_sha256", None)

        for mux_path in stage.rglob("mux.nut"):
            mux_path.unlink()
        _verify_with_test_mutations(
            stage,
            attempt,
            None,
            mutate_manifest=remove_mux,
        )

    with pytest.raises(ChronosVerificationRefusalError, match="does not match the engine result"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            tmp_path / "branch-no-mux-receipt",
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=verify_without_mux,
        )


def test_replaying_missing_published_version_refuses_closed(tmp_path: Path) -> None:
    attempt = _attempt(attempt_id="missing-published-on-retry")
    workspace = tmp_path / "missing-published-retry"
    receipt = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        workspace,
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )
    published = Path(receipt.asset_version.published_path)
    import shutil

    shutil.rmtree(published)
    with pytest.raises(ChronosLedgerError, match="missing published asset version"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
        )


def test_readback_chronos_execution_error_is_preserved(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "execution-error-readback"

    class ExecutionErrorOnReadback(InMemoryChronosLedger):
        def get_pair(self, asset_version_id: str, /) -> Any:
            if self._records:
                raise ChronosSpineExecutionError("repository read-back execution failure")
            return super().get_pair(asset_version_id)

    with pytest.raises(ChronosSpineExecutionError, match="repository read-back execution failure"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            lineage_repository=ExecutionErrorOnReadback(),
        )
    assert not list((workspace / "published").rglob("av_*"))


def test_real_media_adapter_accepts_defaulted_frame_size(tmp_path: Path) -> None:
    base = _real_media_attempt()
    parameters = dict(base.parameters)
    parameters.pop("frame_size")
    shots = tuple(
        replace(
            shot,
            frames_linear_rgb=np.full((2, 16, 16, 3), (0.24, 0.34, 0.42), dtype=np.float64),
        )
        for shot in base.real_media_shots
    )
    attempt = replace(
        base, attempt_id="default-frame-size", parameters=parameters, real_media_shots=shots
    )
    receipt = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        tmp_path / "default-frame-size-root",
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )
    assert receipt.verification.verification_tier == "REAL-DISK"


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
    durable_store = FileChronosLedger(workspace)
    assert isinstance(durable_store, ChronosDurableLineageStore)
    stored_pair = durable_store.get_pair(record.asset_version_id)
    assert stored_pair == (record, receipt.artifact_passport)
    assert (workspace / ".chronos-ledger" / "asset-passport-ledger-v1.json").is_file()
    assert (workspace / ".chronos-ledger" / ".writer.lock").is_file()
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


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("empty-epipolar", "camera correspondence audit is incomplete"),
        ("bad-epipolar", "Sampson residual exceeds tolerance"),
        ("room-throws", "room-continuity remeasurement failed"),
        ("room-drift", "room continuity audit failed"),
    ],
)
def test_omniverse_independent_epipolar_and_room_refusals(
    fault: str, message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    attempt = replace(_attempt(), attempt_id=f"omniverse-{fault}")
    workspace = tmp_path / fault
    if fault in {"empty-epipolar", "bad-epipolar"}:
        original_audit = multicam_epipolar_matching.audit_and_project_epipolar_correspondences
        calls = 0

        def audit(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls <= len(attempt.multicamera_shots):
                return original_audit(*args, **kwargs)
            distances = [] if fault == "empty-epipolar" else [1e-4]
            return SimpleNamespace(sampson_distance_squared_after=np.asarray(distances))

        monkeypatch.setattr(
            multicam_epipolar_matching,
            "audit_and_project_epipolar_correspondences",
            audit,
        )
    else:
        original_room_audit = acoustic_space_continuity.audit_cross_shot_acoustic_continuity
        calls = 0

        def room_audit(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                return original_room_audit(*args, **kwargs)
            if fault == "room-throws":
                raise ValueError("room audit unavailable")
            return SimpleNamespace(rt60_max_spread_seconds=0.1, drr_monotonic_with_proximity=False)

        monkeypatch.setattr(
            acoustic_space_continuity,
            "audit_cross_shot_acoustic_continuity",
            room_audit,
        )

    with pytest.raises(ChronosVerificationRefusalError, match=message):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
        )
    _assert_quarantine_receipt(workspace, attempt, "CHRONOS_VERIFICATION_REFUSED")


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


def test_optional_mp4_mux_refusals_and_quarantine_preserves_reserved_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    root = tmp_path / "mux-refusal-root"
    root.mkdir()
    fs = WorkspaceFilesystem(root)
    fs.ensure_directory("media")
    fs.write_text(
        "media/season-continuity-manifest.json",
        jcs_canonical_json(
            {
                "frame_rate": "24/1",
                "receipts": [{"episode_index": 1, "shot_index": 1, "frame_count": 1}],
            }
        ),
    )
    with monkeypatch.context() as patch:
        from nexus_ai_agent.creative.chronos import real_media_continuity

        patch.setattr(real_media_continuity, "discover_ffmpeg_executable", lambda: None)
        with pytest.raises(ChronosSpineExecutionError, match="FFmpeg is unavailable"):
            spine_adapter._render_optional_mp4(root, {})
    with monkeypatch.context() as patch:
        patch.setattr(
            subprocess,
            "run",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing binary")),
        )
        with pytest.raises(ChronosSpineExecutionError, match="mux could not run"):
            spine_adapter._render_optional_mp4(root, {"ffmpeg_path": "/fake/ffmpeg"})
    with monkeypatch.context() as patch:
        patch.setattr(
            subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
        )
        with pytest.raises(ChronosSpineExecutionError, match="mux failed"):
            spine_adapter._render_optional_mp4(root, {"ffmpeg_path": "/fake/ffmpeg"})

    attempt = replace(_real_media_attempt(), attempt_id="preexisting-receipt")
    workspace = tmp_path / "reserved-receipt-root"

    def add_reserved_receipt(stage: Path) -> None:
        (stage / "quarantine_receipt.json").write_text("user evidence", encoding="utf-8")
        raise RuntimeError("force quarantine")

    with pytest.raises(ChronosSpineExecutionError, match="failed closed"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=add_reserved_receipt,
        )
    quarantine = workspace / ".quarantine" / f"{attempt.attempt_id}_{attempt.fencing_token}"
    assert (
        list(quarantine.glob("quarantine_receipt.preexisting.*.json"))[0].read_text(
            encoding="utf-8"
        )
        == "user evidence"
    )
    assert (quarantine / "quarantine_receipt.json").is_file()


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
    assert not list((tampered_workspace / "published").rglob("av_*"))
    quarantined = _assert_quarantine_receipt(
        tampered_workspace, tampered_attempt, "CHRONOS_VERIFICATION_REFUSED"
    )
    assert len(list(quarantined.rglob("*.mp4"))) == 2


def test_tampered_private_wav_is_quarantined_with_digest_evidence(tmp_path: Path) -> None:
    attempt = replace(_real_media_attempt(), attempt_id="wav-tampered")
    workspace = tmp_path / "wav-tamper-root"

    def tamper_wav(stage: Path) -> None:
        audio = next(stage.rglob("audio.wav"))
        content = bytearray(audio.read_bytes())
        content[-1] ^= 1
        audio.write_bytes(content)

    with pytest.raises(ChronosVerificationRefusalError, match="differs from its receipt"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=tamper_wav,
        )
    quarantined = _assert_quarantine_receipt(workspace, attempt, "CHRONOS_VERIFICATION_REFUSED")
    assert len(list(quarantined.rglob("audio.wav"))) == 2
    assert not list((workspace / "published").rglob("av_*"))


def test_asset_version_retry_is_idempotent_and_never_overwrites_bytes(tmp_path: Path) -> None:
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

    retry = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        workspace,
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )
    assert retry.asset_version == first.asset_version
    assert retry.artifact_passport == first.artifact_passport
    assert all((published / relative).read_bytes() == data for relative, data in before.items())
    assert not (workspace / ".staging" / f"{attempt.attempt_id}_{attempt.fencing_token}").exists()


def test_chronos_port_and_canonical_command_bus_execute_and_persist_pair(
    tmp_path: Path,
) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "port-command-bus-root"
    command_bus, actor = _authorized_command_bus(attempt.project_id)
    ledger = InMemoryChronosLedger()
    handler = build_chronos_staged_spine_handler(
        workspace,
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
        command_bus=command_bus,
        command_actor=actor,
        asset_version_store=ledger,
        passport_sink=ledger,
    )
    port = ChronosPort({CHRONOS_STAGED_SPINE_MASTER: handler})
    port_command = staged_spine_command(attempt)
    port_receipt = port.execute(port_command)

    stored = ledger.get_pair(port_receipt.metadata["asset_version_id"])
    assert stored is not None
    assert stored[0].asset_version_id == port_receipt.metadata["asset_version_id"]
    assert stored[1].passport_digest == port_receipt.metadata["passport_digest"]
    assert port_receipt.quality_claim == "REAL-DISK_CHRONOS_SPINE_VERIFIED"
    assert command_bus.state_revision == 1
    assert len(command_bus.history) == 1
    assert CHRONOS_STAGED_SPINE_MASTER in ChronosPort.OPERATIONS
    assert CHRONOS_STAGED_SPINE_MASTER in CHRONOS_SPINE_OPERATIONS


def test_chronos_port_refuses_execution_if_bound_recipe_identity_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    attempt = _attempt()
    handler = build_chronos_staged_spine_handler(
        tmp_path / "port-recipe-drift",
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )
    monkeypatch.setattr(
        chronos_bridge,
        "dispatch_chronos_spine_operation",
        lambda *_args, **_kwargs: SimpleNamespace(
            recipe_identity=SimpleNamespace(recipe_digest="different-digest")
        ),
    )
    with pytest.raises(ValueError, match="differs from the port's bound identity"):
        handler(staged_spine_command(attempt))


def test_chronos_port_rejects_unbound_or_tampered_recipe_intent(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "port-recipe-mismatch"
    handler = build_chronos_staged_spine_handler(
        workspace,
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )
    port = ChronosPort({CHRONOS_STAGED_SPINE_MASTER: handler})
    forged = ChronosCommand(
        operation=CHRONOS_STAGED_SPINE_MASTER,
        inputs=(attempt.project_id, attempt.parent_revision_id, attempt.attempt_id),
        parameters={
            "fencing_token": str(attempt.fencing_token),
            "operation_name": attempt.operation_name,
            "recipe_digest": "0" * 64,
        },
    )
    with pytest.raises(ValueError, match="does not match its bound attempt"):
        port.execute(forged)
    assert not (workspace / ".staging").exists()


def test_command_bus_idempotency_replays_and_rejects_same_key_payload_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _real_media_attempt()
    command_bus, actor = _authorized_command_bus(attempt.project_id)
    dispatched: list[Any] = []
    outcomes: list[Any] = []
    original_dispatch = command_bus.dispatch

    def capture(command: Any) -> Any:
        dispatched.append(command)
        result = original_dispatch(command)
        outcomes.append(result)
        return result

    monkeypatch.setattr(command_bus, "dispatch", capture)
    recipe = compute_chronos_recipe_identity(attempt)
    spine_adapter._authorize_through_command_bus(command_bus, attempt, recipe, actor)
    assert len(outcomes) == 1
    replay = original_dispatch(dispatched[0])
    assert outcomes[0] == replay
    assert replay.status == "applied"
    assert command_bus.state_revision == 1
    changed = dispatched[0].model_copy(
        update={"input": {**dispatched[0].input, "parent_revision_id": "sha256:drift"}}
    )
    with pytest.raises(IdempotencyConflictError):
        original_dispatch(changed)


def test_command_bus_denial_fails_before_any_staging_or_ledger_write(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "denied-command-bus-root"
    actor = ActorIdentity(kind="service", actor_id="unprivileged-service")
    registry = build_wave1_registry()
    register_chronos_staged_spine_operation(registry)
    access = ProjectAccess(actor, attempt.project_id, frozenset({"project:write"}))
    project = new_project(
        attempt.project_id, "Denied Chronos", Timeline(timeline_id="main", duration_us=0)
    )
    command_bus = CommandBus(project, registry=registry, authorizer=access)
    handler = build_chronos_staged_spine_handler(
        workspace,
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
        command_bus=command_bus,
        command_actor=actor,
    )
    port = ChronosPort({CHRONOS_STAGED_SPINE_MASTER: handler})

    with pytest.raises(ChronosCommandAuthorizationError, match="denied"):
        port.execute(staged_spine_command(attempt))
    assert not (workspace / ".staging").exists()
    assert not (workspace / "published").exists()
    assert not (workspace / ".chronos-ledger").exists()
    assert command_bus.state_revision == 0


def test_fence_preemption_immediately_before_promotion_keeps_bytes_private(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "pre-promotion-preemption-root"
    observed = iter((attempt.fencing_token, attempt.fencing_token, attempt.fencing_token + 1))

    with pytest.raises(ChronosVerificationRefusalError, match="stale or malformed"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=lambda _project, _attempt: next(observed),
        )
    assert not list((workspace / "published").rglob("av_*"))
    quarantine = _assert_quarantine_receipt(workspace, attempt, "CHRONOS_VERIFICATION_REFUSED")
    assert list(quarantine.rglob("frame-*.png"))
    assert list(quarantine.rglob("audio.wav"))


def test_stale_fence_at_persistent_commit_rolls_back_promoted_bytes(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "stale-ledger-fence-root"
    ledger = InMemoryChronosLedger()
    observed = iter((attempt.fencing_token,) * 3 + (attempt.fencing_token + 1,))

    with pytest.raises(ChronosVerificationRefusalError, match="stale fence"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=lambda _project, _attempt: next(observed),
            asset_version_store=ledger,
            passport_sink=ledger,
        )
    assert not list((workspace / "published").rglob("av_*"))
    assert not ledger._records


def test_existing_immutable_asset_destination_is_not_overwritten(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    first_root = tmp_path / "destination-identity-source"
    receipt = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        first_root,
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )
    relative = Path(receipt.asset_version.published_path).relative_to(first_root)
    workspace = tmp_path / "destination-collision-root"
    destination = workspace / relative
    destination.mkdir(parents=True)
    marker = destination / "prior-bytes.bin"
    marker.write_bytes(b"immutable prior publication")

    with pytest.raises(ChronosSpineExecutionError, match="destination already exists"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
        )
    assert marker.read_bytes() == b"immutable prior publication"
    _assert_quarantine_receipt(workspace, attempt, "CHRONOS_EXECUTION_REFUSED")


def test_persistence_rejection_rolls_back_newly_promoted_asset(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "rejecting-ledger-root"

    class RejectingLedger(InMemoryChronosLedger):
        def commit_pair(
            self, asset_version: Any, passport: Any, *, active_fencing_token: Any
        ) -> None:
            raise ChronosLedgerError("repository rejected fenced asset/passport commit")

    ledger = RejectingLedger()
    with pytest.raises(ChronosLedgerError, match="canonical asset/passport commit failed"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            asset_version_store=ledger,
            passport_sink=ledger,
        )
    assert not list((workspace / "published").rglob("av_*"))
    assert not ledger._records


def test_separate_nontransactional_sinks_refuse_before_staging(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "split-sinks-root"
    with pytest.raises(ChronosLedgerError, match="atomic transaction coordinator"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            asset_version_store=InMemoryChronosLedger(),
            passport_sink=InMemoryChronosLedger(),
        )
    assert not (workspace / ".staging").exists()


def test_injected_lineage_repository_commits_and_reads_back_atomically(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "injected-lineage-root"
    repository = InMemoryChronosLedger()
    assert isinstance(repository, ChronosLineageRepositoryProtocol)

    receipt = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        workspace,
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
        lineage_repository=repository,
    )

    assert repository.get_pair(receipt.asset_version.asset_version_id) == (
        receipt.asset_version,
        receipt.artifact_passport,
    )
    assert Path(receipt.asset_version.published_path).is_dir()


def test_injected_lineage_repository_rollback_failure_still_removes_publish(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "rollback-failure-root"

    class UnrecoverableReadback(InMemoryChronosLedger):
        def get_pair(self, asset_version_id: str, /) -> Any:
            pair = super().get_pair(asset_version_id)
            if pair is None:
                return None
            return pair[0], replace(pair[1], passport_digest="f" * 64)

        def discard_pair(self, asset_version_id: str, *, fencing_token: int) -> None:
            raise OSError("injected rollback failure")

    with pytest.raises(ChronosLedgerError, match="rollback failed"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            lineage_repository=UnrecoverableReadback(),
        )
    assert not list((workspace / "published").rglob("av_*"))


def test_injected_lineage_repository_readback_mismatch_rolls_back(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "mismatched-readback-root"

    class MismatchedReadback(InMemoryChronosLedger):
        def get_pair(self, asset_version_id: str, /) -> Any:
            pair = super().get_pair(asset_version_id)
            if pair is None:
                return None
            return pair[0], replace(pair[1], passport_digest="f" * 64)

    repository = MismatchedReadback()
    with pytest.raises(ChronosLedgerError, match="read-back"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            lineage_repository=repository,
        )
    assert not repository._records
    assert not list((workspace / "published").rglob("av_*"))


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
    assert not list((workspace / "published").rglob("av_*"))
    quarantined = _assert_quarantine_receipt(workspace, attempt, "CHRONOS_VERIFICATION_REFUSED")
    assert any(path.name == "frame-00000.png" for path in quarantined.rglob("*.png"))


def test_fencing_token_advanced_during_verification_refuses_before_publish(
    tmp_path: Path,
) -> None:
    attempt = _attempt(attempt_id="zombie-worker", fencing_token=11)
    workspace = tmp_path / "fence-root"
    observed = iter((11, 12))

    with pytest.raises(ChronosVerificationRefusalError, match="stale"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=lambda project_id, attempt_id: next(observed),
        )
    assert not list((workspace / "published").rglob("av_*"))
    quarantined = _assert_quarantine_receipt(workspace, attempt, "CHRONOS_VERIFICATION_REFUSED")
    assert quarantined.exists()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ChronosVerificationRefusalError("bad bytes"), "CHRONOS_VERIFICATION_REFUSED"),
        (ChronosCommandAuthorizationError("denied"), "CHRONOS_AUTHORIZATION_REFUSED"),
        (ChronosLedgerError("store failed"), "CHRONOS_LINEAGE_PERSISTENCE_REFUSED"),
        (OSError("disk failed"), "CHRONOS_FILESYSTEM_REFUSED"),
        (RuntimeError("unexpected"), "CHRONOS_EXECUTION_REFUSED"),
    ],
)
def test_execution_errors_serialize_to_stable_bounded_payloads(error: Exception, code: str) -> None:
    payload = serialize_chronos_execution_error(error)
    assert payload["code"] == code
    assert payload["message"] == str(error)
    assert len(serialize_chronos_execution_error(RuntimeError("x" * 600))["message"]) == 512


def test_lineage_deserializers_reject_malformed_persisted_evidence(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    receipt = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        tmp_path / "deserialize-source",
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )
    record = receipt.asset_version.to_dict()
    passport = receipt.artifact_passport.to_dict()

    assert spine_adapter._record_from_dict(record) == receipt.asset_version
    assert spine_adapter._passport_from_dict(passport) == receipt.artifact_passport
    with pytest.raises(ChronosLedgerError, match="not an object"):
        spine_adapter._record_from_dict(None)
    with pytest.raises(ChronosLedgerError, match="not an object"):
        spine_adapter._passport_from_dict(None)
    assert spine_adapter._read_digest_map({"asset.bin": "a" * 64}) == (("asset.bin", "a" * 64),)
    for malformed in (None, {}, {"": "a" * 64}, {"asset.bin": "bad"}):
        with pytest.raises(ChronosLedgerError):
            spine_adapter._read_digest_map(malformed)

    record_mutations = (
        {"fencing_token": True},
        {"sample_drift": 1.5},
        {"verification_tier": "SYNTHETIC"},
        {"project_id": None},
        {"max_delta_e00": True},
        {"epipolar_sampson_error": False},
        {"rt60_error": "0"},
    )
    for mutation in record_mutations:
        with pytest.raises(ChronosLedgerError, match="persisted asset-version record is malformed"):
            spine_adapter._record_from_dict({**record, **mutation})

    passport_mutations = (
        {"fencing_token": False},
        {"sample_drift": 2.0},
        {"verification_tier": "SYNTHETIC"},
        {"passport_digest": None},
        {"max_delta_e00": True},
        {"epipolar_sampson_error": "bad"},
        {"rt60_error": False},
    )
    for mutation in passport_mutations:
        with pytest.raises(ChronosLedgerError, match="persisted artifact passport is malformed"):
            spine_adapter._passport_from_dict({**passport, **mutation})
    assert not spine_adapter._same_retry_evidence(
        receipt.asset_version,
        replace(receipt.artifact_passport, passport_digest="0" * 64),
        receipt.asset_version,
    )


def test_lineage_store_refuses_bad_canonical_files_and_pair_conflicts(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    source = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        tmp_path / "lineage-source",
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )
    record, passport = source.asset_version, source.artifact_passport
    root = tmp_path / "corrupt-lineage-root"
    store = ChronosDurableLineageStore(root)
    assert store.get_pair(record.asset_version_id) is None
    fs = WorkspaceFilesystem(root)
    ledger_path = ".chronos-ledger/asset-passport-ledger-v1.json"

    for raw in (b"{", b"{} "):
        fs.write_bytes(ledger_path, raw)
        with pytest.raises(ChronosLedgerError):
            store.get_pair(record.asset_version_id)

    for payload in (
        {"schema": "wrong", "asset_versions": {}, "artifact_passports": {}},
        {
            "schema": "chronos-asset-passport-ledger-v1",
            "asset_versions": {record.asset_version_id: record.to_dict()},
            "artifact_passports": {},
        },
        {
            "schema": "chronos-asset-passport-ledger-v1",
            "asset_versions": {
                record.asset_version_id: {**record.to_dict(), "asset_version_id": "other"}
            },
            "artifact_passports": {record.asset_version_id: passport.to_dict()},
        },
    ):
        fs.write_text(ledger_path, jcs_canonical_json(payload))
        with pytest.raises(ChronosLedgerError):
            store.get_pair(record.asset_version_id)

    fs.unlink(ledger_path)
    with pytest.raises(ChronosLedgerError, match="pair does not reconcile"):
        store.commit_pair(
            record,
            replace(passport, passport_digest="0" * 64),
            active_fencing_token=_reader(attempt.fencing_token),
        )
    with pytest.raises(ChronosLedgerError, match="atomic replace failed"):
        store._atomic_replace(".chronos-ledger/missing.tmp", ledger_path)
    store.commit_pair(record, passport, active_fencing_token=_reader(attempt.fencing_token))
    store.commit_pair(record, passport, active_fencing_token=_reader(attempt.fencing_token))
    with pytest.raises(ChronosLedgerConflictError, match="already registered"):
        changed_record = replace(record, output_content_sha256="0" * 64)
        store.commit_pair(
            changed_record,
            spine_adapter._passport_for_record(changed_record),
            active_fencing_token=_reader(attempt.fencing_token),
        )
    with pytest.raises(ChronosVerificationRefusalError, match="stale fence"):
        store.commit_pair(record, passport, active_fencing_token=_reader(attempt.fencing_token + 1))
    with pytest.raises(ChronosLedgerConflictError, match="another fence"):
        store.discard_pair(record.asset_version_id, fencing_token=attempt.fencing_token + 1)
    store.discard_pair(record.asset_version_id, fencing_token=attempt.fencing_token)
    store.discard_pair(record.asset_version_id, fencing_token=attempt.fencing_token)


def test_in_memory_lineage_conflicts_and_rollback_fencing(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    receipt = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        tmp_path / "in-memory-source",
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )
    record, passport = receipt.asset_version, receipt.artifact_passport
    ledger = InMemoryChronosLedger()
    ledger.discard_pair("missing", fencing_token=attempt.fencing_token)
    invalid_passport = replace(passport, passport_digest="0" * 64)
    with pytest.raises(ChronosLedgerError, match="does not reconcile"):
        ledger.commit_pair(
            record,
            invalid_passport,
            active_fencing_token=_reader(attempt.fencing_token),
        )
    ledger.commit_pair(record, passport, active_fencing_token=_reader(attempt.fencing_token))
    ledger.commit_pair(record, passport, active_fencing_token=_reader(attempt.fencing_token))
    changed = replace(record, output_content_sha256="0" * 64)
    with pytest.raises(ChronosLedgerConflictError, match="already registered"):
        ledger.commit_pair(
            changed,
            spine_adapter._passport_for_record(changed),
            active_fencing_token=_reader(attempt.fencing_token),
        )
    with pytest.raises(ChronosLedgerConflictError, match="another fence"):
        ledger.discard_pair(record.asset_version_id, fencing_token=attempt.fencing_token + 1)
    ledger.discard_pair(record.asset_version_id, fencing_token=attempt.fencing_token)


def test_staged_adapter_helpers_refuse_bad_authority_and_preserve_forensics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    attempt = _attempt()
    recipe = compute_chronos_recipe_identity(attempt)
    with pytest.raises(ChronosVerificationRefusalError, match="authority was unavailable"):
        spine_adapter._require_current_fence(
            attempt, lambda *_args: (_ for _ in ()).throw(OSError("authority down"))
        )
    with pytest.raises(ChronosCommandAuthorizationError, match="trusted actor"):
        spine_adapter._authorize_through_command_bus(
            cast(Any, object()),
            attempt,
            recipe,
            None,
        )
    registry = build_wave1_registry()
    register_chronos_staged_spine_operation(registry)
    actor = ActorIdentity(kind="service", actor_id="chronos-test")
    bad_preconditions = SimpleNamespace(
        registry=registry, state_revision=True, state_hash="valid-hash"
    )
    with pytest.raises(ChronosCommandAuthorizationError, match="invalid precondition state"):
        spine_adapter._authorize_through_command_bus(bad_preconditions, attempt, recipe, actor)
    bad_receipt = SimpleNamespace(
        registry=registry,
        state_revision=0,
        state_hash="valid-hash",
        dispatch=lambda _command: SimpleNamespace(output={}),
    )
    with pytest.raises(ChronosCommandAuthorizationError, match="invalid staged-spine"):
        spine_adapter._authorize_through_command_bus(bad_receipt, attempt, recipe, actor)
    with pytest.raises(ChronosSpineExecutionError, match="finite numeric buffer"):
        spine_adapter._array_bytes(np.array(["not numeric"]), label="bad")
    with pytest.raises(ChronosSpineExecutionError, match="safe, non-empty"):
        spine_adapter._component("..", "component")
    assert spine_adapter._copied_array(None, label="absent") is None

    source_receipt = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        tmp_path / "record-fence-source",
        _real_media_attempt(),
        active_fencing_token=_reader(4),
    )
    with pytest.raises(ChronosVerificationRefusalError, match="authority was unavailable"):
        spine_adapter._require_record_fence(
            source_receipt.asset_version,
            lambda *_args: (_ for _ in ()).throw(OSError("sink authority down")),
        )

    root = tmp_path / "helper-root"
    root.mkdir()
    fs = WorkspaceFilesystem(root)
    assert not spine_adapter._directory_exists(fs, "not-created")
    outside = tmp_path / "outside-root"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ChronosSpineExecutionError, match="unsafe path"):
        spine_adapter._directory_exists(fs, "escape")
    spine_adapter._quarantine_stage(
        fs, ".staging/absent", ".quarantine/absent", attempt, RuntimeError("nothing staged")
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            WorkspaceFilesystem,
            "iter_files",
            lambda _self: (_ for _ in ()).throw(OSError("walk denied")),
        )
        assert spine_adapter._stage_digest_evidence(root) == ({}, "OSError")
    stage = root / ".staging" / "owned"
    stage.mkdir(parents=True)
    (stage / "evidence.bin").write_bytes(b"evidence")
    assert spine_adapter._stage_digest_evidence(stage)[0]["evidence.bin"]["size_bytes"] == 8

    @contextmanager
    def denied_lock(*args: Any, **_kwargs: Any) -> Any:
        if not args:
            yield
        else:
            raise OSError("lock unavailable")

    store = ChronosDurableLineageStore(root)
    monkeypatch.setattr(store._fs, "_parent_fd", denied_lock)
    with pytest.raises(ChronosLedgerError, match="repository lock failed"):
        store.get_pair("none")

    blocked_stage = root / ".staging" / "conflict"
    blocked_stage.mkdir()
    (blocked_stage / "evidence.bin").write_bytes(b"do not overwrite")
    quarantine = root / ".quarantine" / f"{attempt.attempt_id}_{attempt.fencing_token}"
    quarantine.mkdir(parents=True)
    with pytest.raises(ChronosLedgerConflictError, match="destination already exists"):
        spine_adapter._quarantine_stage(
            fs,
            ".staging/conflict",
            f".quarantine/{attempt.attempt_id}_{attempt.fencing_token}",
            attempt,
            RuntimeError("rejected"),
        )
    assert (blocked_stage / "quarantine_receipt.json").is_file()
    assert (blocked_stage / "evidence.bin").read_bytes() == b"do not overwrite"


def test_durable_ledger_atomic_write_failure_cleans_temporary_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attempt = _real_media_attempt()
    receipt = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        tmp_path / "atomic-write-source",
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )
    root = tmp_path / "atomic-write-failure"
    store = ChronosDurableLineageStore(root)
    monkeypatch.setattr(
        store,
        "_atomic_replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace denied")),
    )
    monkeypatch.setattr(
        store._fs,
        "unlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cleanup denied")),
    )
    with pytest.raises(ChronosLedgerError, match="ledger commit failed"):
        store.commit_pair(
            receipt.asset_version,
            receipt.artifact_passport,
            active_fencing_token=_reader(attempt.fencing_token),
        )


def test_repository_injection_rejects_missing_coordinator_and_unpaired_sinks(
    tmp_path: Path,
) -> None:
    attempt = _real_media_attempt()
    with pytest.raises(ChronosSpineExecutionError, match="authoritative active fencing"):
        spine_adapter.execute_chronos_staged_attempt(
            tmp_path / "missing-fence-reader",
            attempt,
            active_fencing_token=None,  # type: ignore[arg-type]
        )
    with pytest.raises(ChronosLedgerError, match="share one atomic transaction"):
        spine_adapter.execute_chronos_staged_attempt(
            tmp_path / "unpaired-sinks",
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            asset_version_store=InMemoryChronosLedger(),
            passport_sink=InMemoryChronosLedger(),
        )
    with pytest.raises(ChronosLedgerError, match="violates the paired transaction protocol"):
        spine_adapter.execute_chronos_staged_attempt(
            tmp_path / "invalid-protocol",
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            lineage_repository=object(),  # type: ignore[arg-type]
        )


def test_command_registration_and_handler_refuse_contract_drift() -> None:
    from types import SimpleNamespace

    class BrokenRegistry:
        def __contains__(self, _operation: str) -> bool:
            return True

        def get_spec(self, _operation: str) -> Any:
            return SimpleNamespace(
                input_model=object,
                permission_level="none",
                required_permissions=(),
                deterministic=False,
                execution_modes=("remote",),
            )

    with pytest.raises(ChronosSpineExecutionError, match="authorization contract"):
        register_chronos_staged_spine_operation(BrokenRegistry())  # type: ignore[arg-type]

    payload = {
        "project_id": "bound-project",
        "parent_revision_id": "parent",
        "attempt_id": "attempt",
        "fencing_token": 1,
        "operation_name": REAL_MEDIA_OPERATION,
        "recipe_digest": "a" * 64,
    }
    context = SimpleNamespace(
        input_data=payload,
        command=SimpleNamespace(target=SimpleNamespace(project_id="bound-project")),
        history=(),
    )
    with pytest.raises(ChronosSpineExecutionError, match="project binding"):
        spine_adapter._chronos_spine_command_handler(
            cast(Any, SimpleNamespace(project_id="different-project")), cast(Any, context)
        )


def test_retry_evidence_conflict_and_published_directory_reverification(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "verified-retry-source"
    first = dispatch_chronos_spine_operation(
        CHRONOS_STAGED_SPINE_MASTER,
        workspace,
        attempt,
        active_fencing_token=_reader(attempt.fencing_token),
    )
    record = first.asset_version
    relative = str(Path(record.published_path).relative_to(workspace))
    fs = WorkspaceFilesystem(workspace)
    spine_adapter._verify_published_directory(fs, relative, record)
    with pytest.raises(ChronosLedgerError, match="published path does not match"):
        spine_adapter._verify_published_directory(
            fs, relative, replace(record, published_path=str(tmp_path / "wrong-path"))
        )
    with pytest.raises(ChronosVerificationRefusalError, match="file set differs"):
        spine_adapter._verify_published_directory(
            fs,
            relative,
            replace(
                record,
                published_file_digests=(*record.published_file_digests, ("missing.bin", "0" * 64)),
            ),
        )
    published_root = Path(record.published_path)
    relative_file, _digest = record.published_file_digests[0]
    (published_root / relative_file).write_bytes(b"tampered published bytes")
    with pytest.raises(ChronosVerificationRefusalError, match="bytes differ"):
        spine_adapter._verify_published_directory(fs, relative, record)

    conflicting = InMemoryChronosLedger()
    changed = replace(record, recipe_digest="f" * 64)
    conflicting.commit_pair(
        changed,
        spine_adapter._passport_for_record(changed),
        active_fencing_token=_reader(attempt.fencing_token),
    )
    with pytest.raises(ChronosLedgerConflictError, match="different recipe or media evidence"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            tmp_path / "retry-conflict-root",
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            lineage_repository=conflicting,
        )


def test_identity_and_jcs_refusals_cover_unsafe_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for component in (None, "", "../escape", "safe/name"):
        with pytest.raises(ChronosSpineExecutionError):
            spine_adapter._component(component, "component")
    for token in (None, "", "  ", "a/b", "a\\b", "a\x00b", ".."):
        with pytest.raises(ChronosSpineExecutionError):
            spine_adapter._lineage_token(token, "identity")
    assert spine_adapter._component("safe-1", "component") == "safe-1"
    assert spine_adapter._lineage_token("revision:1", "identity") == "revision:1"
    for invalid in (1 << 54, float("nan"), float("inf"), "\ud800", {1: "bad"}, object()):
        with pytest.raises(ChronosSpineExecutionError):
            jcs_canonical_json(invalid)
    absent_anchor = replace(_attempt(), show_bible_anchor=None, room=None, speaker_position_m=None)
    identity = spine_adapter._identity_parameters(absent_anchor)
    assert "show_bible_anchor" not in identity and "room" not in identity
    with pytest.raises(ChronosSpineExecutionError, match="cannot be normalized"):
        with monkeypatch.context() as patch:
            patch.setattr(
                np,
                "ascontiguousarray",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("cannot cast")),
            )
            spine_adapter._array_bytes(np.ones(1), label="forced-normalization-failure")
    with pytest.raises(
        ChronosSpineExecutionError, match="render parameters are not canonical JSON"
    ):
        with monkeypatch.context() as patch:
            patch.setattr(
                json,
                "loads",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad JSON")),
            )
            spine_adapter._normalized_parameters({})
    with pytest.raises(ChronosSpineExecutionError, match="must be a JSON object"):
        spine_adapter._normalized_parameters([1, 2])  # type: ignore[arg-type]
    with pytest.raises(ChronosSpineExecutionError, match="forbids NaN"):
        spine_adapter._normalized_parameters({"not-finite": float("nan")})


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
    assert jcs_canonical_json(np.float64(0.64574489882531)) == "0.64574489882531"
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
        replace(base, operation_name=cast(Any, "unknown.operation")),
        replace(base, parameters=cast(Any, None)),
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


def test_fencing_token_regression_refuses_before_staging(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "fence-regression-root"
    with pytest.raises(ChronosVerificationRefusalError, match="stale or malformed"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token - 1),
        )
    assert not (workspace / ".staging").exists()
    assert not (workspace / ".quarantine").exists()


def test_existing_forensic_quarantine_is_never_overwritten(tmp_path: Path) -> None:
    attempt = _real_media_attempt()
    workspace = tmp_path / "existing-quarantine-root"
    quarantine = workspace / ".quarantine" / f"{attempt.attempt_id}_{attempt.fencing_token}"
    quarantine.mkdir(parents=True)
    marker = quarantine / "prior-evidence.bin"
    marker.write_bytes(b"preserve the prior forensic evidence")

    with pytest.raises(ChronosSpineExecutionError, match="quarantine already exists"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
        )
    assert marker.read_bytes() == b"preserve the prior forensic evidence"
    assert not (workspace / ".staging").exists()


def test_engine_run_rejects_invalid_dimensions_missing_room_and_renderer_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_attempt = _real_media_attempt()
    bad_frame_size = replace(
        real_attempt,
        parameters={**real_attempt.parameters, "frame_size": (4, 8)},
    )
    with pytest.raises(ChronosSpineExecutionError, match="frame_size"):
        spine_adapter._engine_run(bad_frame_size, tmp_path / "bad-dimensions")

    no_room = replace(_attempt(), room=None)
    with pytest.raises(ChronosSpineExecutionError, match="room and speaker"):
        spine_adapter._engine_run(no_room, tmp_path / "no-room")

    monkeypatch.setattr(
        spine_adapter,
        "execute_real_media_season_continuity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("renderer failed")),
    )
    with pytest.raises(ChronosSpineExecutionError, match="render failed closed"):
        spine_adapter._engine_run(real_attempt, tmp_path / "renderer-error")


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
        assert not list((workspace / "published").rglob("av_*"))
        quarantined = _assert_quarantine_receipt(workspace, attempt, "CHRONOS_EXECUTION_REFUSED")
        assert quarantined.is_dir()


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
    assert not list((workspace / "published").rglob("av_*"))
    _assert_quarantine_receipt(workspace, attempt, "CHRONOS_VERIFICATION_REFUSED")


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
    assert not list((workspace / "published").rglob("av_*"))
    _assert_quarantine_receipt(workspace, attempt, "CHRONOS_VERIFICATION_REFUSED")


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
    assert not list((workspace / "published").rglob("av_*"))
    _assert_quarantine_receipt(workspace, attempt, "CHRONOS_VERIFICATION_REFUSED")


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


@pytest.mark.parametrize(
    ("attempt_id", "mutation", "message"),
    [
        (
            "empty-manifest-receipts",
            lambda manifest: manifest.update(receipts=[]),
            "no per-shot artifact entries",
        ),
        (
            "wrong-manifest-root",
            lambda manifest: manifest.update(merkle_root_sha256="0" * 64),
            "root differs from receipts",
        ),
        (
            "missing-manifest-bundles",
            lambda manifest: manifest.update(inclusion_bundles=[]),
            "bundle count is incomplete",
        ),
    ],
)
def test_manifest_receipt_merkle_structure_refusals(
    attempt_id: str,
    mutation: Any,
    message: str,
    tmp_path: Path,
) -> None:
    attempt = replace(_real_media_attempt(), attempt_id=attempt_id)

    def tamper(stage: Path) -> None:
        manifest_path = stage / "media" / "season-continuity-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        mutation(manifest)
        manifest_path.write_text(jcs_canonical_json(manifest), encoding="utf-8")

    workspace = tmp_path / attempt_id
    with pytest.raises(ChronosVerificationRefusalError, match=message):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=tamper,
        )
    _assert_quarantine_receipt(workspace, attempt, "CHRONOS_VERIFICATION_REFUSED")


def test_spine_receipt_json_decode_refuses_and_quarantines(tmp_path: Path) -> None:
    attempt = replace(_real_media_attempt(), attempt_id="bad-spine-receipt-json")
    workspace = tmp_path / "bad-spine-receipt-json"

    def corrupt(stage: Path) -> None:
        (stage / "chronos-spine-receipt.json").write_bytes(b"{")

    with pytest.raises(ChronosVerificationRefusalError, match="spine receipt is unreadable"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=corrupt,
        )
    _assert_quarantine_receipt(workspace, attempt, "CHRONOS_VERIFICATION_REFUSED")


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("timing", "per-shot timing"),
        ("png-decode", "PNG could not be decoded"),
        ("wav-decode", "WAV could not be decoded"),
        ("wav-metadata", "WAV channel/clock metadata"),
        ("mp4-path", "MP4 path is not shot-contained"),
        ("mp4-framing", "MP4 framing is invalid"),
    ],
)
def test_manifest_bound_png_wav_and_mp4_refusals_are_quarantined(
    case: str, message: str, tmp_path: Path
) -> None:
    attempt = replace(_real_media_attempt(), attempt_id=f"manifest-{case}")
    workspace = tmp_path / f"workspace-{case}"

    def tamper(stage: Path) -> None:
        manifest_path = stage / "media" / "season-continuity-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        receipt = manifest["receipts"][0]
        shot = stage / "media" / "episode-0001" / "shot-0001"
        if case == "timing":
            receipt["frame_count"] = 0
        elif case == "png-decode":
            png = shot / "frame-00000.png"
            png.write_bytes(b"not a png image")
            receipt["frame_sha256s"][0] = hashlib.sha256(png.read_bytes()).hexdigest()
        elif case == "wav-decode":
            wav = shot / "audio.wav"
            wav.write_bytes(b"not a wave file")
            receipt["audio_sha256"] = hashlib.sha256(wav.read_bytes()).hexdigest()
        elif case == "wav-metadata":
            receipt["audio_channel_count"] = 3
        elif case == "mp4-path":
            receipt["mp4_path"] = "media/episode-0001/shot-0002/mux.mp4"
            receipt["mp4_sha256"] = "0" * 64
        elif case == "mp4-framing":
            mp4 = shot / "mux.mp4"
            mp4.write_bytes(b"not a valid mp4 container")
            receipt["mp4_path"] = "media/episode-0001/shot-0001/mux.mp4"
            receipt["mp4_sha256"] = hashlib.sha256(mp4.read_bytes()).hexdigest()
        _resign_manifest_after_test_mutation(stage, manifest)

    with pytest.raises(ChronosVerificationRefusalError, match=message):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
            before_verify=tamper,
        )
    _assert_quarantine_receipt(workspace, attempt, "CHRONOS_VERIFICATION_REFUSED")


def test_invalid_disk_color_threshold_refuses_and_quarantines(tmp_path: Path) -> None:
    base = _real_media_attempt()
    attempt = replace(
        base,
        attempt_id="invalid-disk-color-threshold",
        parameters={**base.parameters, "max_delta_e00": False},
    )
    workspace = tmp_path / "invalid-disk-color-threshold"
    with pytest.raises(ChronosVerificationRefusalError, match="threshold is invalid"):
        dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace,
            attempt,
            active_fencing_token=_reader(attempt.fencing_token),
        )
    _assert_quarantine_receipt(workspace, attempt, "CHRONOS_VERIFICATION_REFUSED")


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
        assert not list((workspace / "published").rglob("av_*"))
        _assert_quarantine_receipt(workspace, attempt, "CHRONOS_VERIFICATION_REFUSED")


def test_mp4_decode_refuses_without_decoder_and_on_failed_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from nexus_ai_agent.creative.chronos import real_media_continuity

    with monkeypatch.context() as patch:
        patch.setattr(real_media_continuity, "discover_ffmpeg_executable", lambda: None)
        with pytest.raises(ChronosVerificationRefusalError, match="without FFmpeg"):
            spine_adapter._decode_mp4_bytes(b"valid-ish mp4 bytes", {})
    with monkeypatch.context() as patch:
        patch.setattr(
            subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
        )
        with pytest.raises(ChronosVerificationRefusalError, match="failed independent stream"):
            spine_adapter._decode_mp4_bytes(b"valid-ish mp4 bytes", {"ffmpeg_path": "/fake"})


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

    (root / ".chronos-ledger").mkdir()
    fs.write_bytes(".chronos-ledger/asset-passport-ledger-v1.json", b"{")
    with pytest.raises(ChronosLedgerError, match="ledger is unreadable"):
        FileChronosLedger(root).get_pair("missing-version")
