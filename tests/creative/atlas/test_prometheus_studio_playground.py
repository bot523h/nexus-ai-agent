"""WAVE 24 — tests for the Prometheus Studio Playground.

Checks the content-addressed universe DAG, ``do(...)`` counterfactual forking,
the causal-delta receipt, and the deterministic 2x2 quadrant compilation (with a
real FFmpeg encode when it is on ``PATH``).
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import prometheus_studio_playground as psp
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

HAS_FFMPEG = psp._ffmpeg_path() is not None
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not on PATH")


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


def _intervention(**overrides: object) -> psp.CounterfactualIntervention:
    base: dict[str, object] = {"fork_time": 1.0}
    base.update(overrides)
    return psp.CounterfactualIntervention(**base)  # type: ignore[arg-type]


# --- intervention validation ------------------------------------------------------


def test_intervention_accepts_valid() -> None:
    _intervention(
        key_light_azimuth_deg=70.0,
        anamorphic_squeeze=2.0,
        fog_anisotropy_g=0.5,
        target_staging_proxemics_m=1.0,
        camera_obstacle_pos=(1.0, 0.2, 0.3),
        inverse_target_t60_sec=0.8,
    ).validate()


def test_intervention_rejects_negative_fork_time() -> None:
    with pytest.raises(psp.PlaygroundError):
        _intervention(fork_time=-1.0).validate()


def test_intervention_rejects_bad_squeeze() -> None:
    with pytest.raises(psp.PlaygroundError):
        _intervention(anamorphic_squeeze=1.5).validate()


def test_intervention_rejects_bad_fog_g() -> None:
    with pytest.raises(psp.PlaygroundError):
        _intervention(fog_anisotropy_g=1.0).validate()


def test_intervention_rejects_bad_proxemics() -> None:
    with pytest.raises(psp.PlaygroundError):
        _intervention(target_staging_proxemics_m=0.0).validate()


def test_intervention_rejects_bad_t60() -> None:
    with pytest.raises(psp.PlaygroundError):
        _intervention(inverse_target_t60_sec=-0.5).validate()


def test_intervention_rejects_bad_obstacle() -> None:
    with pytest.raises(psp.PlaygroundError):
        _intervention(camera_obstacle_pos=(1.0, 2.0)).validate()


def test_intervention_serialises() -> None:
    payload = _intervention(key_light_azimuth_deg=70.0).to_dict()
    assert payload["key_light_azimuth_deg"] == 70.0
    assert payload["camera_obstacle_pos"] is None


# --- universe DAG -----------------------------------------------------------------


def test_root_universe_is_content_addressed() -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_parameters())
    assert root.parent_universe_id is None
    assert root.universe_id == "root-" + root.world_sha256[:24]
    assert len(root.world_sha256) == 64


def test_identical_parameters_yield_identical_world_digest() -> None:
    playground = psp.PrometheusStudioPlayground()
    first = playground.create_root_universe(_parameters())
    second = playground.create_root_universe(_parameters())
    assert first.world_sha256 == second.world_sha256


def test_get_unknown_universe_raises() -> None:
    playground = psp.PrometheusStudioPlayground()
    with pytest.raises(psp.PlaygroundError):
        playground.get("nope")


def test_universe_state_serialises() -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_parameters())
    payload = root.to_dict()
    assert payload["kind"] == "atlas.prometheus_universe_state"
    assert payload["schema"] == psp.PLAYGROUND_SCHEMA
    assert payload["parent_universe_id"] is None


# --- counterfactual forking -------------------------------------------------------


def test_fork_records_parent_lineage() -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_parameters())
    branch, _receipt = playground.fork_counterfactual_universe(
        root.universe_id, _intervention(key_light_azimuth_deg=70.0)
    )
    assert branch.parent_universe_id == root.universe_id
    assert branch.universe_id.startswith("cf-")


def test_fork_changes_the_world_digest() -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_parameters())
    branch, _receipt = playground.fork_counterfactual_universe(
        root.universe_id, _intervention(key_light_azimuth_deg=70.0)
    )
    assert branch.world_sha256 != root.world_sha256


def test_fork_produces_visual_divergence() -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_parameters())
    _branch, receipt = playground.fork_counterfactual_universe(
        root.universe_id, _intervention(key_light_azimuth_deg=80.0, anamorphic_squeeze=2.0)
    )
    assert receipt.visual_divergence_nonzero is True
    assert receipt.wasserstein_divergence > 0.0


def test_fork_injects_hamiltonian_energy() -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_parameters())
    _branch, receipt = playground.fork_counterfactual_universe(
        root.universe_id, _intervention(fog_anisotropy_g=0.8, key_light_azimuth_deg=70.0)
    )
    assert receipt.energy_delta_joules > 0.0
    assert receipt.both_passive is True


def test_fork_inverse_t60_is_applied() -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_parameters())
    branch, receipt = playground.fork_counterfactual_universe(
        root.universe_id, _intervention(inverse_target_t60_sec=0.6)
    )
    assert receipt.t60_delta_sec != 0.0
    assert branch.parameters.room_absorption > root.parameters.room_absorption


def test_fork_obstacle_replans_camera() -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_parameters())
    branch, _receipt = playground.fork_counterfactual_universe(
        root.universe_id, _intervention(camera_obstacle_pos=(1.0, 0.2, 0.3))
    )
    assert branch.parameters.obstacle is not None
    assert branch.parameters.camera_keyframes.shape[0] == 12


def test_fork_rejects_unknown_base() -> None:
    playground = psp.PrometheusStudioPlayground()
    with pytest.raises(psp.PlaygroundError):
        playground.fork_counterfactual_universe("nope", _intervention())


def test_fork_rejects_invalid_intervention() -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_parameters())
    with pytest.raises(psp.PlaygroundError):
        playground.fork_counterfactual_universe(
            root.universe_id, _intervention(anamorphic_squeeze=3.0)
        )


def test_delta_receipt_serialises() -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_parameters())
    _branch, receipt = playground.fork_counterfactual_universe(
        root.universe_id, _intervention(key_light_azimuth_deg=70.0)
    )
    payload = receipt.to_dict()
    assert payload["kind"] == "atlas.counterfactual_causal_delta"
    assert payload["base_universe_id"] == root.universe_id


# --- quadrant compilation ---------------------------------------------------------


def _four_universes(playground: psp.PrometheusStudioPlayground) -> tuple[str, list[str]]:
    root = playground.create_root_universe(_parameters())
    branches = [
        _intervention(fork_time=1.0, key_light_azimuth_deg=75.0, anamorphic_squeeze=2.0),
        _intervention(fork_time=1.5, fog_anisotropy_g=0.8, camera_obstacle_pos=(1.4, 0.3, 0.2)),
        _intervention(fork_time=2.0, inverse_target_t60_sec=0.6, key_light_azimuth_deg=10.0),
        _intervention(fork_time=0.5, fog_anisotropy_g=-0.5, anamorphic_squeeze=1.8),
    ]
    ids: list[str] = []
    for intervention in branches:
        branch, _receipt = playground.fork_counterfactual_universe(root.universe_id, intervention)
        ids.append(branch.universe_id)
    return root.universe_id, ids


def test_compile_rejects_wrong_universe_count(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root, ids = _four_universes(playground)
    filesystem = WorkspaceFilesystem(str(tmp_path))
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground, ids[:3], ancestor_universe_id=root, filesystem=filesystem
        )


def test_compile_rejects_duplicate_universes(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root, ids = _four_universes(playground)
    filesystem = WorkspaceFilesystem(str(tmp_path))
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground, [ids[0], ids[0]], ancestor_universe_id=root, filesystem=filesystem
        )


def test_compile_rejects_foreign_ancestor(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    _root, ids = _four_universes(playground)
    other_root = playground.create_root_universe(_parameters(key_light_azimuth_deg=88.0))
    filesystem = WorkspaceFilesystem(str(tmp_path))
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground, ids[:2], ancestor_universe_id=other_root.universe_id, filesystem=filesystem
        )


def test_compile_rejects_bad_side(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root, ids = _four_universes(playground)
    filesystem = WorkspaceFilesystem(str(tmp_path))
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground, ids, ancestor_universe_id=root, filesystem=filesystem, side=4
        )


def test_compile_writes_frames_and_wav(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root, ids = _four_universes(playground)
    filesystem = WorkspaceFilesystem(str(tmp_path))
    receipt = psp.compile_counterfactual_quadrant_mp4(
        playground, ids, ancestor_universe_id=root, filesystem=filesystem, side=32, frames=3, fps=3
    )
    assert receipt.wav_sha256 != ""
    root_path = os.path.join(str(tmp_path), "prometheus_playground")
    assert os.path.isdir(os.path.join(root_path, "frames"))
    assert len([f for f in os.listdir(os.path.join(root_path, "frames"))]) == 3
    assert os.path.isfile(os.path.join(root_path, "quadrant_audio.wav"))


@requires_ffmpeg
def test_compile_encodes_real_mp4(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root, ids = _four_universes(playground)
    filesystem = WorkspaceFilesystem(str(tmp_path))
    receipt = psp.compile_counterfactual_quadrant_mp4(
        playground, ids, ancestor_universe_id=root, filesystem=filesystem, side=32, frames=4, fps=4
    )
    assert receipt.encoded is True
    assert receipt.mp4_sha256 != ""
    mp4 = os.path.join(str(tmp_path), receipt.mp4_path)
    assert os.path.isfile(mp4)
    assert os.path.getsize(mp4) > 0


def test_compile_receipt_serialises(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root, ids = _four_universes(playground)
    filesystem = WorkspaceFilesystem(str(tmp_path))
    receipt = psp.compile_counterfactual_quadrant_mp4(
        playground, ids, ancestor_universe_id=root, filesystem=filesystem, side=24, frames=2, fps=2
    )
    payload = receipt.to_dict()
    assert payload["kind"] == "atlas.counterfactual_quadrant_compilation"
    assert payload["universe_ids"] == ids


def test_staging_report_is_measured(tmp_path: object) -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_parameters())
    report = playground.staging_report(root.universe_id)
    assert 0.0 <= report.camera_visibility_fraction <= 1.0
    assert report.min_pairwise_distance > 0.0


def test_room_t60_matches_sabine() -> None:
    assert psp._room_t60(0.4) == pytest.approx(0.161 * 100.0 / (60.0 * 0.4))


def test_luminance_distribution_is_normalised() -> None:
    field = psp._render_luminance(_parameters(), side=16)
    distribution = psp._luminance_distribution(field)
    assert distribution.sum() == pytest.approx(1.0)
    assert np.all(distribution >= 0.0)


def test_psnr_is_high_for_identical_fields() -> None:
    field = psp._render_luminance(_parameters(), side=16)
    assert psp._psnr(field, field) >= 99.0
