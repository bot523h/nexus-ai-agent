"""WAVE 25 — tests for the post-disk verifier and managed studio governance.

Covers the RFC 6962 Merkle tree (odd/even leaf counts, inclusion proofs and
tamper rejection), the three-state epistemic ledger with its anti-erasure guard,
observation provenance, the post-disk semantic verifier, and the fail-closed
governed entrypoint with quarantine and lineage commit.
"""

from __future__ import annotations

import hashlib

import pytest

from nexus_ai_agent.creative.atlas import prometheus_studio_playground as psp
from nexus_ai_agent.creative.atlas import prometheus_verifier_and_governance as pvg
from nexus_ai_agent.creative.atlas.atlas_spine_governance import (
    AtlasGovernanceRefusalError,
    AtlasLineageStore,
)
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

HAS_FFMPEG = pvg._ffmpeg_path() is not None
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not on PATH")


class _AllowAuthorizer:
    def authorize(self, *, operation: str, actor: str, project_id: str, payload: object) -> bool:
        return True


def _parameters(**overrides: object) -> psp.WorldParameters:
    base: dict[str, object] = {
        "key_light_azimuth_deg": 40.0,
        "key_light_intensity": 1.2,
        "fog_anisotropy_g": 0.3,
        "extinction_coefficient": 0.2,
        "anamorphic_squeeze": 1.33,
        "staging_proxemics_m": 1.2,
        "room_absorption": 0.4,
        "camera_keyframes": psp._default_camera_keyframes(),
        "port_state": psp._default_port_state(),
        "obstacle": None,
    }
    base.update(overrides)
    return psp.WorldParameters(**base)  # type: ignore[arg-type]


def _four_universes(playground: psp.PrometheusStudioPlayground) -> tuple[str, list[str]]:
    root = playground.create_root_universe(_parameters())
    branches = [
        psp.CounterfactualIntervention(
            fork_time=1.0, key_light_azimuth_deg=75.0, anamorphic_squeeze=2.0
        ),
        psp.CounterfactualIntervention(
            fork_time=1.5, fog_anisotropy_g=0.8, camera_obstacle_pos=(1.4, 0.3, 0.2)
        ),
        psp.CounterfactualIntervention(
            fork_time=2.0, inverse_target_t60_sec=0.6, key_light_azimuth_deg=10.0
        ),
        psp.CounterfactualIntervention(
            fork_time=0.5, fog_anisotropy_g=-0.5, anamorphic_squeeze=1.8
        ),
    ]
    ids: list[str] = []
    for intervention in branches:
        branch, _receipt = playground.fork_counterfactual_universe(root.universe_id, intervention)
        ids.append(branch.universe_id)
    return root.universe_id, ids


# --- RFC 6962 Merkle tree ---------------------------------------------------------


def test_merkle_root_of_empty_tree() -> None:
    assert pvg.merkle_root([]) == hashlib.sha256(b"").digest()


def test_merkle_single_leaf_root() -> None:
    leaf = b"only"
    assert pvg.merkle_root([leaf]) == pvg.merkle_leaf_hash(leaf)


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 7, 11, 13])
def test_merkle_inclusion_holds_for_all_leaf_counts(count: int) -> None:
    leaves = [f"leaf-{i}".encode() for i in range(count)]
    root = pvg.merkle_root(leaves)
    for index in range(count):
        proof = pvg.merkle_inclusion_proof(leaves, index)
        assert pvg.verify_merkle_inclusion(leaves[index], index, count, proof, root)


def test_merkle_leaf_prefix_differs_from_node_prefix() -> None:
    assert pvg.merkle_leaf_hash(b"x") != pvg.merkle_node_hash(b"x", b"x")


def test_merkle_second_preimage_attack_is_rejected() -> None:
    # A leaf whose bytes equal a concatenation of two leaves must not collide.
    leaves = [b"a", b"b", b"ab"]
    root = pvg.merkle_root(leaves)
    proof = pvg.merkle_inclusion_proof(leaves, 2)
    assert pvg.verify_merkle_inclusion(b"ab", 2, 3, proof, root)
    assert not pvg.verify_merkle_inclusion(b"a", 2, 3, proof, root)


def test_merkle_tampered_leaf_is_rejected() -> None:
    leaves = [f"leaf-{i}".encode() for i in range(5)]
    root = pvg.merkle_root(leaves)
    proof = pvg.merkle_inclusion_proof(leaves, 2)
    assert not pvg.verify_merkle_inclusion(b"tampered", 2, 5, proof, root)


def test_merkle_wrong_index_is_rejected() -> None:
    leaves = [f"leaf-{i}".encode() for i in range(5)]
    root = pvg.merkle_root(leaves)
    proof = pvg.merkle_inclusion_proof(leaves, 1)
    assert not pvg.verify_merkle_inclusion(leaves[1], 3, 5, proof, root)


def test_merkle_empty_tree_has_no_proof() -> None:
    with pytest.raises(pvg.VerifierError):
        pvg.merkle_inclusion_proof([], 0)


def test_merkle_index_out_of_range() -> None:
    with pytest.raises(pvg.VerifierError):
        pvg.merkle_inclusion_proof([b"a"], 5)


def test_verify_rejects_bad_total_leaves() -> None:
    leaves = [b"a", b"b"]
    root = pvg.merkle_root(leaves)
    proof = pvg.merkle_inclusion_proof(leaves, 0)
    assert not pvg.verify_merkle_inclusion(leaves[0], 0, 0, proof, root)
    assert not pvg.verify_merkle_inclusion(leaves[0], 9, 2, proof, root)


# --- epistemic ledger -------------------------------------------------------------


def test_ledger_records_three_states() -> None:
    ledger = pvg.EpistemicLedger()
    ledger.record(
        pvg.Observation("a", pvg.LedgerState.PASS, pvg.ObservationProvenance.MEASURED_MEDIA)
    )
    ledger.record(
        pvg.Observation("b", pvg.LedgerState.VIOLATED, pvg.ObservationProvenance.MEASURED_MEDIA)
    )
    ledger.record(
        pvg.Observation(
            "c", pvg.LedgerState.UNOBSERVED, pvg.ObservationProvenance.SYNTHETIC_FIXTURE
        )
    )
    assert ledger.state_of("a") is pvg.LedgerState.PASS
    assert ledger.state_of("b") is pvg.LedgerState.VIOLATED
    assert ledger.state_of("c") is pvg.LedgerState.UNOBSERVED
    assert ledger.any_violated is True
    assert ledger.all_pass is False


def test_ledger_anti_erasure_guard_refuses_pass_to_unobserved() -> None:
    ledger = pvg.EpistemicLedger()
    ledger.record(
        pvg.Observation("x", pvg.LedgerState.PASS, pvg.ObservationProvenance.MEASURED_MEDIA)
    )
    with pytest.raises(pvg.VerifierError):
        ledger.record(
            pvg.Observation(
                "x", pvg.LedgerState.UNOBSERVED, pvg.ObservationProvenance.SYNTHETIC_FIXTURE
            )
        )


def test_ledger_anti_erasure_guard_refuses_violated_to_unobserved() -> None:
    ledger = pvg.EpistemicLedger()
    ledger.record(
        pvg.Observation("x", pvg.LedgerState.VIOLATED, pvg.ObservationProvenance.MEASURED_MEDIA)
    )
    with pytest.raises(pvg.VerifierError):
        ledger.record(
            pvg.Observation(
                "x", pvg.LedgerState.UNOBSERVED, pvg.ObservationProvenance.SYNTHETIC_FIXTURE
            )
        )


def test_ledger_allows_unobserved_to_pass() -> None:
    ledger = pvg.EpistemicLedger()
    ledger.record(
        pvg.Observation(
            "x", pvg.LedgerState.UNOBSERVED, pvg.ObservationProvenance.SYNTHETIC_FIXTURE
        )
    )
    ledger.record(
        pvg.Observation("x", pvg.LedgerState.PASS, pvg.ObservationProvenance.MEASURED_MEDIA)
    )
    assert ledger.state_of("x") is pvg.LedgerState.PASS


def test_ledger_all_pass_is_true_when_all_pass() -> None:
    ledger = pvg.EpistemicLedger()
    ledger.record(
        pvg.Observation("a", pvg.LedgerState.PASS, pvg.ObservationProvenance.MEASURED_MEDIA)
    )
    assert ledger.all_pass is True
    assert ledger.any_violated is False


def test_observation_serialises_with_provenance() -> None:
    observation = pvg.Observation(
        "claim", pvg.LedgerState.PASS, pvg.ObservationProvenance.ESTIMATED_PROXY, measured=1.0
    )
    payload = observation.to_dict()
    assert payload["provenance"] == "ESTIMATED_PROXY"
    assert payload["state"] == "PASS"


def test_ledger_canonical_bytes_round_trip() -> None:
    ledger = pvg.EpistemicLedger()
    ledger.record(
        pvg.Observation("a", pvg.LedgerState.PASS, pvg.ObservationProvenance.MEASURED_MEDIA)
    )
    assert ledger.canonical_bytes().startswith(b"{")


# --- post-disk verification -------------------------------------------------------


def _compile(
    tmp_path: object, *, side: int = 32, frames: int = 4
) -> tuple[
    psp.PrometheusStudioPlayground, WorkspaceFilesystem, psp.QuadrantCompilationReceipt, str
]:
    playground = psp.PrometheusStudioPlayground()
    root, ids = _four_universes(playground)
    filesystem = WorkspaceFilesystem(str(tmp_path))
    compilation = psp.compile_counterfactual_quadrant_mp4(
        playground,
        ids,
        ancestor_universe_id=root,
        filesystem=filesystem,
        out_dir="staging/published",
        side=side,
        frames=frames,
        fps=frames,
    )
    return playground, filesystem, compilation, root


def test_verify_reports_unobserved_without_media(tmp_path: object) -> None:
    _playground, filesystem, compilation, _root = _compile(tmp_path)
    report = pvg.verify_prometheus_playground_bundle_from_disk(
        filesystem, compilation, require_media=False
    )
    assert report.verified is True
    assert report.ledger.state_of("mp4_video_psnr") in (
        pvg.LedgerState.PASS,
        pvg.LedgerState.UNOBSERVED,
    )


def test_verify_rejects_wrong_expected_mp4_digest(tmp_path: object) -> None:
    _playground, filesystem, compilation, _root = _compile(tmp_path)
    # `_compile` only encodes the MP4 when FFmpeg is on PATH; on a runner without
    # it there is no file to hash, so the wrong-digest rejection path is never
    # exercised (the check records UNOBSERVED, not VIOLATED) and this test fails
    # for environment reasons. Pin bytes in place so the assertion holds everywhere.
    filesystem.write_bytes(compilation.mp4_path, b"not-the-real-mp4", create_parents=True)
    report = pvg.verify_prometheus_playground_bundle_from_disk(
        filesystem,
        compilation,
        expected_mp4_sha256="0" * 64,
        require_media=False,
    )
    assert report.verified is False
    assert report.ledger.state_of("mp4_file_digest") is pvg.LedgerState.VIOLATED


def test_verify_rejects_tampered_png(tmp_path: object) -> None:
    _playground, filesystem, compilation, _root = _compile(tmp_path)
    frame_rel = f"{compilation.mp4_path.rsplit('/', 1)[0]}/frames/frame_0000.png"
    filesystem.write_bytes(frame_rel, b"not-a-png", create_parents=True)
    with pytest.raises(pvg.VerifierError):
        pvg.verify_prometheus_playground_bundle_from_disk(
            filesystem, compilation, require_media=False
        )


def test_verify_records_merkle_proofs(tmp_path: object) -> None:
    _playground, filesystem, compilation, _root = _compile(tmp_path)
    report = pvg.verify_prometheus_playground_bundle_from_disk(
        filesystem, compilation, require_media=False
    )
    assert len(report.merkle_proofs) == 4 + 1
    assert all(p.verified for p in report.merkle_proofs)
    assert report.ledger.state_of("merkle_inclusion") is pvg.LedgerState.PASS


def test_verify_raises_without_reference_frames(tmp_path: object) -> None:
    _playground, filesystem, compilation, _root = _compile(tmp_path)
    import shutil

    shutil.rmtree(str(filesystem.resolve(f"{compilation.mp4_path.rsplit('/', 1)[0]}/frames")))
    with pytest.raises(pvg.VerifierError):
        pvg.verify_prometheus_playground_bundle_from_disk(filesystem, compilation)


@requires_ffmpeg
def test_verify_accepts_real_bundle(tmp_path: object) -> None:
    _playground, filesystem, compilation, _root = _compile(tmp_path)
    report = pvg.verify_prometheus_playground_bundle_from_disk(filesystem, compilation)
    assert report.verified is True
    assert report.psnr_db >= pvg.PSNR_FLOOR_DB
    assert report.audio_equivalent is True
    assert report.frame_count == 4
    assert report.ledger.all_pass is True


@requires_ffmpeg
def test_verify_rejects_truncated_mp4(tmp_path: object) -> None:
    _playground, filesystem, compilation, _root = _compile(tmp_path)
    raw = filesystem.read_bytes(compilation.mp4_path, max_bytes=1 << 27)
    filesystem.write_bytes(compilation.mp4_path, raw[: len(raw) // 3], create_parents=True)
    report = pvg.verify_prometheus_playground_bundle_from_disk(filesystem, compilation)
    assert report.verified is False


# --- governed entrypoint ----------------------------------------------------------


def test_governed_fails_closed_without_authorizer(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root, ids = _four_universes(playground)
    filesystem = WorkspaceFilesystem(str(tmp_path))
    with pytest.raises(AtlasGovernanceRefusalError) as excinfo:
        pvg.run_prometheus_playground_governed(
            playground,
            ids,
            ancestor_universe_id=root,
            filesystem=filesystem,
            side=24,
            frames=2,
            fps=2,
        )
    assert excinfo.value.code == "AUTHORIZER_REQUIRED"


def test_governed_lab_mode_allows_unauthenticated(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root, ids = _four_universes(playground)
    filesystem = WorkspaceFilesystem(str(tmp_path))
    receipt = pvg.run_prometheus_playground_governed(
        playground,
        ids,
        ancestor_universe_id=root,
        filesystem=filesystem,
        allow_unauthenticated_lab_mode=True,
        side=24,
        frames=2,
        fps=2,
    )
    assert receipt.authority_mode == "LAB_UNAUTHENTICATED"
    assert receipt.staged_dir.startswith(".staging/")


def test_governed_authorized_run_verifies_and_commits_lineage(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root, ids = _four_universes(playground)
    filesystem = WorkspaceFilesystem(str(tmp_path))
    store = AtlasLineageStore(filesystem)
    receipt = pvg.run_prometheus_playground_governed(
        playground,
        ids,
        ancestor_universe_id=root,
        filesystem=filesystem,
        command_bus_authorizer=_AllowAuthorizer(),
        side=24,
        frames=2,
        fps=2,
        lineage=store,
        attempt_id="att1",
        fencing_token=1,
    )
    assert receipt.lineage_generation == 1
    assert store.highest_token == 1
    payload = receipt.to_dict()
    assert payload["kind"] == "atlas.governed_prometheus_playground"


def test_governed_receipt_serialises(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root, ids = _four_universes(playground)
    filesystem = WorkspaceFilesystem(str(tmp_path))
    receipt = pvg.run_prometheus_playground_governed(
        playground,
        ids,
        ancestor_universe_id=root,
        filesystem=filesystem,
        allow_unauthenticated_lab_mode=True,
        side=24,
        frames=2,
        fps=2,
    )
    assert receipt.canonical_bytes().startswith(b"{")


def test_default_lineage_store_factory(tmp_path: object) -> None:
    filesystem = WorkspaceFilesystem(str(tmp_path))
    store = pvg.default_lineage_store(filesystem)
    assert isinstance(store, AtlasLineageStore)


def test_verify_audio_equivalence_helper() -> None:
    import numpy as np

    reference = (np.sin(np.linspace(0.0, 40.0, 5000)) * 10000.0).astype("<i2")
    shifted = np.concatenate([np.zeros(200, dtype="<i2"), reference])
    assert pvg._audio_equivalent(shifted, reference) is True
    assert pvg._audio_equivalent(np.zeros(0, dtype="<i2"), reference) is False


def test_verify_psnr_bytes_helper() -> None:
    assert pvg._psnr_bytes(b"", b"") == -1.0
    assert pvg._psnr_bytes(bytes([10] * 64), bytes([10] * 64)) >= 99.0
    assert pvg._psnr_bytes(bytes([10] * 64), bytes([200] * 64)) < 28.0


def test_verify_file_exists_helper(tmp_path: object) -> None:
    filesystem = WorkspaceFilesystem(str(tmp_path))
    filesystem.write_bytes("a.txt", b"x", create_parents=True)
    assert pvg._file_exists(filesystem, "a.txt") is True
    assert pvg._file_exists(filesystem, "missing.txt") is False


def test_verify_list_dir_missing_returns_empty(tmp_path: object) -> None:
    filesystem = WorkspaceFilesystem(str(tmp_path))
    assert pvg._list_dir(filesystem, "nope") == []


def test_verify_collect_returns_bundle(tmp_path: object) -> None:
    filesystem = WorkspaceFilesystem(str(tmp_path))
    filesystem.write_bytes("d/a.bin", b"aa", create_parents=True)
    filesystem.write_bytes("d/sub/b.bin", b"bb", create_parents=True)
    bundle = pvg._collect(filesystem, "d")
    assert set(bundle) == {"a.bin", "sub__b.bin"}
    assert pvg._collect(filesystem, "missing") == {}


# --------------------------------------------------------------------------- #
# Media absence is never a silent pass (Wave 3 residual fail-open closure)
# --------------------------------------------------------------------------- #


def test_missing_mp4_with_expected_digest_is_violated_without_require_media(
    tmp_path: object,
) -> None:
    """A supplied ``expected_mp4_sha256`` pins the bytes: their absence is VIOLATED.

    ``require_media=False`` relaxes the *decode* requirement only. Before this
    fix the missing-file branch returned UNOBSERVED whenever ``require_media``
    was false, so a caller that had pinned a digest could be told "verified"
    while the MP4 it pinned did not exist.
    """
    _playground, filesystem, compilation, _root = _compile(tmp_path)
    filesystem.resolve(compilation.mp4_path).unlink(missing_ok=True)
    report = pvg.verify_prometheus_playground_bundle_from_disk(
        filesystem,
        compilation,
        expected_mp4_sha256="a" * 64,
        require_media=False,
    )
    assert report.verified is False
    assert report.ledger.state_of("mp4_file_digest") is pvg.LedgerState.VIOLATED


def test_missing_mp4_with_recorded_digest_is_violated_without_require_media(
    tmp_path: object,
) -> None:
    """The receipt's own recorded digest is an expectation too."""
    _playground, filesystem, compilation, _root = _compile(tmp_path)
    if not compilation.mp4_sha256:
        pytest.skip("this runner produced no encoded MP4, so there is no recorded digest to pin")
    filesystem.resolve(compilation.mp4_path).unlink(missing_ok=True)
    report = pvg.verify_prometheus_playground_bundle_from_disk(
        filesystem, compilation, require_media=False
    )
    assert report.verified is False
    assert report.ledger.state_of("mp4_file_digest") is pvg.LedgerState.VIOLATED


def test_missing_mp4_with_no_expectation_stays_unobserved(tmp_path: object) -> None:
    """With nothing expecting the bytes, absence is honestly UNOBSERVED."""
    _playground, filesystem, compilation, _root = _compile(tmp_path)
    filesystem.resolve(compilation.mp4_path).unlink(missing_ok=True)
    bare = (
        compilation.model_copy(update={"mp4_sha256": ""})
        if hasattr(compilation, "model_copy")
        else compilation
    )
    if getattr(bare, "mp4_sha256", "") != "":
        pytest.skip("receipt cannot be re-stated without a recorded MP4 digest")
    report = pvg.verify_prometheus_playground_bundle_from_disk(
        filesystem, bare, require_media=False
    )
    assert report.ledger.state_of("mp4_file_digest") is pvg.LedgerState.UNOBSERVED


def test_missing_wav_with_expected_digest_is_violated(tmp_path: object) -> None:
    """A missing WAV is a falsified claim, not an exception and not a pass."""
    _playground, filesystem, compilation, _root = _compile(tmp_path)
    filesystem.resolve(compilation.wav_path).unlink(missing_ok=True)
    report = pvg.verify_prometheus_playground_bundle_from_disk(
        filesystem,
        compilation,
        expected_wav_sha256="b" * 64,
        require_media=False,
    )
    assert report.verified is False
    assert report.ledger.state_of("wav_file_digest") is pvg.LedgerState.VIOLATED


def test_missing_wav_records_merkle_and_audio_claims_honestly(tmp_path: object) -> None:
    """The WAV leaf leaves the tree and equivalence is never claimed."""
    _playground, filesystem, compilation, _root = _compile(tmp_path)
    filesystem.resolve(compilation.wav_path).unlink(missing_ok=True)
    report = pvg.verify_prometheus_playground_bundle_from_disk(
        filesystem,
        compilation,
        expected_wav_sha256="b" * 64,
        require_media=False,
    )
    assert compilation.wav_path not in report.file_digests
    assert report.audio_equivalent is False
    assert all(proof.verified for proof in report.merkle_proofs)
    assert report.ledger.state_of("wav_file_digest") is pvg.LedgerState.VIOLATED
