"""WAVE 28 — the multi-universe counterfactual quadrant E2E benchmark.

This is the end-to-end benchmark of the PROMETHEUS-ATLAS stack: it builds one
base studio universe, forks three physically distinct counterfactual worlds from
it, compiles the four-world split-screen comparison (real FFmpeg H.264 MP4 + a
48 kHz stereo WAV) through the *governed* entrypoint, and then verifies the
published bundle **from disk** — reference PNGs, WAV PCM, decoded MP4 video and
audio streams, the RFC 6962 Merkle inclusion proofs, and the atomic
``lineage_ledger.json`` commit.

The benchmark also proves the tamper-evident contract: flipping a single byte of
the published MP4 or WAV makes the verifier refuse and preserves a lawful
``.quarantine/`` receipt.

The normative equations, proofs, complexity bounds and the verification ledger
live in ``src/nexus_ai_agent/creative/atlas/PROMETHEUS_STUDIO_PLAYGROUND_DOCTRINE.md``.
"""

from __future__ import annotations

import hashlib

import pytest

from nexus_ai_agent.creative.atlas import prometheus_studio_playground as psp
from nexus_ai_agent.creative.atlas import prometheus_verifier_and_governance as pvg
from nexus_ai_agent.creative.atlas.atlas_spine_governance import AtlasLineageStore
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

HAS_FFMPEG = pvg._ffmpeg_path() is not None
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not on PATH")

SIDE = 32
FRAMES = 4


class _AllowAuthorizer:
    def authorize(self, *, operation: str, actor: str, project_id: str, payload: object) -> bool:
        return True


def _base_parameters(**overrides: object) -> psp.WorldParameters:
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


def _three_branches(
    playground: psp.PrometheusStudioPlayground, root_id: str
) -> tuple[list[str], list[psp.CounterfactualCausalDeltaReceipt]]:
    """U1 golden-hour anamorphic + close staging; U2 cold high-fog Mie + crane
    obstacle avoidance; U3 inverse-calibrated studio target with a fast Doppler
    source."""
    interventions = [
        psp.CounterfactualIntervention(
            fork_time=1.0,
            key_light_azimuth_deg=25.0,
            anamorphic_squeeze=2.0,
            target_staging_proxemics_m=1.1,
        ),
        psp.CounterfactualIntervention(
            fork_time=1.5,
            fog_anisotropy_g=0.8,
            camera_obstacle_pos=(1.4, 0.3, 0.2),
        ),
        psp.CounterfactualIntervention(
            fork_time=2.0,
            inverse_target_t60_sec=0.6,
            key_light_azimuth_deg=10.0,
            target_staging_proxemics_m=4.0,
        ),
    ]
    ids: list[str] = []
    receipts: list[psp.CounterfactualCausalDeltaReceipt] = []
    for intervention in interventions:
        branch, receipt = playground.fork_counterfactual_universe(root_id, intervention, side=SIDE)
        ids.append(branch.universe_id)
        receipts.append(receipt)
    return ids, receipts


# --- the full governed E2E benchmark ----------------------------------------------


@requires_ffmpeg
def test_prometheus_playground_e2e_governed_bundle(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_base_parameters())
    ids, _receipts = _three_branches(playground, root.universe_id)
    filesystem = WorkspaceFilesystem(str(tmp_path))
    store = AtlasLineageStore(filesystem)

    receipt = pvg.run_prometheus_playground_governed(
        playground,
        [root.universe_id, *ids],
        ancestor_universe_id=root.universe_id,
        filesystem=filesystem,
        command_bus_authorizer=_AllowAuthorizer(),
        side=SIDE,
        frames=FRAMES,
        fps=FRAMES,
        lineage=store,
        attempt_id="e2e",
        fencing_token=1,
    )

    report = receipt.verification
    assert report.verified is True
    assert report.ledger.all_pass is True
    assert report.psnr_db >= pvg.PSNR_FLOOR_DB
    assert report.frame_count == FRAMES
    assert report.audio_equivalent is True
    # One Merkle leaf per reference PNG frame plus the WAV digest.
    assert len(report.merkle_proofs) == FRAMES + 1
    assert all(proof.verified for proof in report.merkle_proofs)
    assert receipt.lineage_generation == 1
    assert store.highest_token == 1


@requires_ffmpeg
def test_prometheus_playground_e2e_artifacts_exist_on_disk(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_base_parameters())
    ids, _receipts = _three_branches(playground, root.universe_id)
    filesystem = WorkspaceFilesystem(str(tmp_path))

    receipt = pvg.run_prometheus_playground_governed(
        playground,
        [root.universe_id, *ids],
        ancestor_universe_id=root.universe_id,
        filesystem=filesystem,
        command_bus_authorizer=_AllowAuthorizer(),
        side=SIDE,
        frames=FRAMES,
        fps=FRAMES,
        lineage=AtlasLineageStore(filesystem),
        attempt_id="artifacts",
        fencing_token=1,
    )
    published = receipt.published_dir
    assert filesystem.exists(f"{published}/quadrant_comparison.mp4")
    assert filesystem.exists(f"{published}/quadrant_audio.wav")
    assert filesystem.exists(f"{published}/verification_report.json")
    frames = sorted(
        name for name in pvg._list_dir(filesystem, f"{published}/frames") if name.endswith(".png")
    )
    assert len(frames) == FRAMES
    ledger_raw = filesystem.read_bytes("_lineage/lineage_ledger.json", max_bytes=1 << 26)
    assert hashlib.sha256(ledger_raw).hexdigest() != ""


# --- tamper evidence: a single flipped byte is refused and quarantined ------------


@requires_ffmpeg
def test_prometheus_playground_e2e_mp4_tamper_is_quarantined(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_base_parameters())
    ids, _receipts = _three_branches(playground, root.universe_id)
    filesystem = WorkspaceFilesystem(str(tmp_path))

    receipt = pvg.run_prometheus_playground_governed(
        playground,
        [root.universe_id, *ids],
        ancestor_universe_id=root.universe_id,
        filesystem=filesystem,
        command_bus_authorizer=_AllowAuthorizer(),
        side=SIDE,
        frames=FRAMES,
        fps=FRAMES,
        lineage=AtlasLineageStore(filesystem),
        attempt_id="tamper_mp4",
        fencing_token=1,
    )
    mp4 = f"{receipt.published_dir}/quadrant_comparison.mp4"
    raw = filesystem.read_bytes(mp4, max_bytes=1 << 27)
    mutated = bytes([raw[0] ^ 0xFF]) + raw[1:]
    filesystem.write_bytes(mp4, mutated, create_parents=True)

    compilation = psp.QuadrantCompilationReceipt(
        ancestor_universe_id=root.universe_id,
        universe_ids=(root.universe_id, *ids),
        quadrant_side=SIDE,
        frames=FRAMES,
        fps=FRAMES,
        mp4_path=mp4,
        wav_path=f"{receipt.published_dir}/quadrant_audio.wav",
        mp4_sha256=receipt.verification.file_digests[mp4],
        wav_sha256=receipt.verification.file_digests[f"{receipt.published_dir}/quadrant_audio.wav"],
        encoded=True,
    )
    report = pvg.verify_prometheus_playground_bundle_from_disk(filesystem, compilation)
    assert report.verified is False
    assert report.ledger.state_of("mp4_file_digest") is pvg.LedgerState.VIOLATED

    quarantine = pvg.quarantine_failed_attempt(
        filesystem,
        attempt_id="tamper_mp4",
        fencing_token=1,
        failure_reason="mp4 tamper detected",
        failed_bundle={"quadrant_comparison.mp4": mutated},
        published_root=receipt.published_dir,
    )
    assert quarantine.observed_digests
    assert filesystem.exists(".quarantine/tamper_mp4_1/quadrant_comparison.mp4")
    assert filesystem.exists(".quarantine/tamper_mp4_1/quarantine_receipt.json")


@requires_ffmpeg
def test_prometheus_playground_e2e_wav_tamper_is_quarantined(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_base_parameters())
    ids, _receipts = _three_branches(playground, root.universe_id)
    filesystem = WorkspaceFilesystem(str(tmp_path))

    receipt = pvg.run_prometheus_playground_governed(
        playground,
        [root.universe_id, *ids],
        ancestor_universe_id=root.universe_id,
        filesystem=filesystem,
        command_bus_authorizer=_AllowAuthorizer(),
        side=SIDE,
        frames=FRAMES,
        fps=FRAMES,
        lineage=AtlasLineageStore(filesystem),
        attempt_id="tamper_wav",
        fencing_token=1,
    )
    wav = f"{receipt.published_dir}/quadrant_audio.wav"
    raw = filesystem.read_bytes(wav, max_bytes=1 << 26)
    mutated = raw[:44] + bytes([raw[44] ^ 0xFF]) + raw[45:]
    filesystem.write_bytes(wav, mutated, create_parents=True)

    compilation = psp.QuadrantCompilationReceipt(
        ancestor_universe_id=root.universe_id,
        universe_ids=(root.universe_id, *ids),
        quadrant_side=SIDE,
        frames=FRAMES,
        fps=FRAMES,
        mp4_path=f"{receipt.published_dir}/quadrant_comparison.mp4",
        wav_path=wav,
        mp4_sha256=receipt.verification.file_digests[
            f"{receipt.published_dir}/quadrant_comparison.mp4"
        ],
        wav_sha256=receipt.verification.file_digests[wav],
        encoded=True,
    )
    report = pvg.verify_prometheus_playground_bundle_from_disk(filesystem, compilation)
    assert report.verified is False

    pvg.quarantine_failed_attempt(
        filesystem,
        attempt_id="tamper_wav",
        fencing_token=1,
        failure_reason="wav tamper detected",
        failed_bundle={"quadrant_audio.wav": mutated},
        published_root=receipt.published_dir,
    )
    assert filesystem.exists(".quarantine/tamper_wav_1/quadrant_audio.wav")


# --- governance fail-closed and Merkle proofs (no ffmpeg required) ----------------


def test_prometheus_playground_e2e_requires_authorizer(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_base_parameters())
    ids, _receipts = _three_branches(playground, root.universe_id)
    filesystem = WorkspaceFilesystem(str(tmp_path))
    with pytest.raises(Exception) as excinfo:
        pvg.run_prometheus_playground_governed(
            playground,
            [root.universe_id, *ids],
            ancestor_universe_id=root.universe_id,
            filesystem=filesystem,
            side=24,
            frames=2,
            fps=2,
        )
    assert getattr(excinfo.value, "code", None) == "AUTHORIZER_REQUIRED"


def test_prometheus_playground_e2e_merkle_proofs_verify() -> None:
    leaves = [f"frame-{i}".encode() for i in range(5)]
    root = pvg.merkle_root(leaves)
    for index, leaf in enumerate(leaves):
        proof = pvg.merkle_inclusion_proof(leaves, index)
        assert pvg.verify_merkle_inclusion(leaf, index, len(leaves), proof, root)
    bad_proof = pvg.merkle_inclusion_proof(leaves, 2)
    assert not pvg.verify_merkle_inclusion(b"tampered", 2, 5, bad_proof, root)
