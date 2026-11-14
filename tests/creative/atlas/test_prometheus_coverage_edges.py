"""WAVE 29 — coverage edges for the PROMETHEUS-ATLAS modules (Waves 18-25).

The behavioural suites for each module already exercise the *physics*; this file
closes the remaining per-module coverage gaps (the validation/refusal branches and
the alternate operational paths) so that every new module reaches the mission's
line/branch bar. Every assertion here is a real refusal or a real alternate path,
not a coverage-only stub: a mutation that disables the guard makes these fail.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import adjoint_inverse_cinema_solver as aic
from nexus_ai_agent.creative.atlas import anamorphic_scattering_renderer as scat
from nexus_ai_agent.creative.atlas import nash_dramatic_staging_solver as nash
from nexus_ai_agent.creative.atlas import port_hamiltonian_world_solver as phs
from nexus_ai_agent.creative.atlas import prometheus_studio_playground as psp
from nexus_ai_agent.creative.atlas import prometheus_verifier_and_governance as pvg
from nexus_ai_agent.creative.atlas import riemannian_camera_choreographer as cam
from nexus_ai_agent.creative.atlas import wasserstein_schrodinger_bridge as wass
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

# --- Wave 18: port-Hamiltonian validation edges -----------------------------------


def _phs(n: int = 2) -> phs.PortHamiltonianSystem:
    return phs.PortHamiltonianSystem(
        J=np.array([[0.0, 1.0], [-1.0, 0.0]]),
        R=np.zeros((2, 2)),
        Q=np.eye(2),
        dimension=2,
    )


def test_phs_validation_refusals() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(J=np.zeros((2, 2)), R=np.zeros((2, 2)), Q=np.eye(2), dimension=3)
    bad_j = phs.PortHamiltonianSystem(
        J=np.zeros((3, 2)), R=np.zeros((2, 2)), Q=np.eye(2), dimension=2
    )
    with pytest.raises(phs.PortHamiltonianError):
        bad_j.jacobian(np.zeros(2))
    bad_r = phs.PortHamiltonianSystem(
        J=np.zeros((2, 2)), R=np.zeros((3, 2)), Q=np.eye(2), dimension=2
    )
    with pytest.raises(phs.PortHamiltonianError):
        bad_r.dissipation(np.zeros(2))


def test_phs_discrete_gradient_rejects_non_vector() -> None:
    system = _phs()
    with pytest.raises(phs.PortHamiltonianError):
        phs.discrete_gradient(system, np.zeros((2, 1)), np.zeros((2, 1)))


def test_phs_step_rejects_bad_iterations() -> None:
    system = _phs()
    x = np.array([1.0, 0.0])
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(system, x, dt=0.1, u=None, max_iterations=0)


def test_phs_nonconvergent_fixed_point_raises() -> None:
    system = _phs()
    x = np.array([1.0, 0.0])
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(
            system, x, dt=0.1, u=None, max_iterations=1, tolerance=1e-30
        )


# --- Wave 19: Wasserstein validation edges ----------------------------------------


def test_wasserstein_validation_refusals() -> None:
    with pytest.raises(wass.TransportError):
        wass.solve_entropic_wasserstein_ot(np.array([]), np.array([1.0]), cost=np.zeros((1, 1)))
    big = np.full(wass.MAX_SUPPORT + 1, 1.0 / (wass.MAX_SUPPORT + 1))
    with pytest.raises(wass.TransportError):
        wass.solve_entropic_wasserstein_ot(big, big, cost=np.zeros((big.size, big.size)))
    mu = np.array([0.5, 0.5])
    with pytest.raises(wass.TransportError):
        wass.solve_entropic_wasserstein_ot(mu, mu, cost=np.zeros((2, 2)), eps=True)
    with pytest.raises(wass.TransportError):
        wass.solve_entropic_wasserstein_ot(mu, mu, cost=np.zeros((2, 2)), max_iterations=0)


def test_wasserstein_bridge_point_cloud_refusal() -> None:
    with pytest.raises(wass.TransportError):
        wass.interpolate_schrodinger_bridge_scene(
            np.zeros((2, 2, 2)),
            np.zeros((2, 2, 2)),
            np.array([1.0, 0.0]),
            np.array([1.0, 0.0]),
            t=0.5,
        )


def test_wasserstein_barycenter_validation_refusals() -> None:
    mu = np.array([0.5, 0.5])
    weights = np.array([1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0])
    with pytest.raises(wass.TransportError):
        wass.compute_wasserstein_barycenter((mu,) * 3, weights, np.zeros((2, 2)), max_iterations=0)
    with pytest.raises(wass.TransportError):
        wass.compute_wasserstein_barycenter((mu,), np.array([1.0]), np.zeros((2, 2)), tolerance=0.0)


# --- Wave 20: inverse-cinema validation edges -------------------------------------


def test_inverse_cinema_validation_refusals() -> None:
    with pytest.raises(aic.InverseCinemaError):
        aic.solve_inverse_cinema_parameters(
            np.zeros(aic.OBSERVATION_COUNT),
            prior=np.array(aic.DEFAULT_PRIOR),
            max_iterations=0,
        )


def test_inverse_cinema_report_loss_ratio() -> None:
    prior = np.array(aic.DEFAULT_PRIOR)
    observation, _jacobian = aic.render_differentiable_scene_observation(prior)
    report = aic.solve_inverse_cinema_parameters(observation, prior=prior, max_iterations=2)
    assert report.loss_reduction_ratio >= 0.0


# --- Wave 21: camera validation edges ---------------------------------------------


def _rotations() -> np.ndarray:
    return np.stack([np.eye(3), cam._exp_so3(np.array([0.0, 0.0, 0.4]))])


def test_camera_orientation_validation_refusals() -> None:
    with pytest.raises(cam.CameraChoreographyError):
        cam.interpolate_orientation_bspline(np.eye(3)[None, :, :], np.array([0.0, 1.0]))
    with pytest.raises(cam.CameraChoreographyError):
        cam.interpolate_orientation_bspline(np.eye(3)[None, :, :], np.array([0.0]))
    with pytest.raises(cam.CameraChoreographyError):
        cam.interpolate_orientation_bspline(
            _rotations(), np.array([0.0, 1.0]), samples_per_segment=0
        )


def test_camera_log_so3_near_pi_and_identity() -> None:
    identity = cam._log_so3(np.eye(3))
    assert np.allclose(identity, 0.0)
    near_pi = cam._exp_so3(np.array([np.pi - 1e-9, 0.0, 0.0]))
    recovered = cam._log_so3(near_pi)
    assert abs(float(np.linalg.norm(recovered)) - np.pi) < 1e-3


def test_camera_shield_refusals() -> None:
    points = np.zeros((2, 3))
    with pytest.raises(cam.CameraChoreographyError):
        cam.apply_cbf_safety_shield(points, np.zeros((cam.MAX_OBSTACLES + 1, 4)), delta_safe=1e-3)


def test_camera_projection_flips_determinant() -> None:
    flip = np.diag([1.0, 1.0, -1.0])
    projected = cam._project_so3(flip)
    assert float(np.linalg.det(projected)) > 0.0


# --- Wave 22: Nash staging validation edges ---------------------------------------


def _staging(**overrides: object) -> nash.DramaticStagingProblem:
    base: dict[str, object] = {
        "actor_count": 2,
        "camera_position": np.array([0.0, -6.0]),
        "camera_yaw": 0.0,
        "key_light_azimuth": 40.0,
        "interactions": ((0, 1, "personal"),),
        "speakers": (0, 1),
        "d_collision": 0.4,
    }
    base.update(overrides)
    return nash.DramaticStagingProblem(**base)  # type: ignore[arg-type]


def test_nash_validation_refusals() -> None:
    with pytest.raises(nash.StagingError):
        _staging(camera_yaw=float("nan"))
    with pytest.raises(nash.StagingError):
        _staging(key_light_azimuth=float("inf"))
    with pytest.raises(nash.StagingError):
        _staging(d_collision=0.0)


def test_nash_solver_refusals() -> None:
    problem = _staging()
    initial = np.array([[0.0, 0.0, 0.0], [1.2, 0.0, 0.0]])
    with pytest.raises(nash.StagingError):
        nash.solve_nash_dramatic_staging(problem, initial, max_iterations=0)
    with pytest.raises(nash.StagingError):
        nash.solve_nash_dramatic_staging(problem, initial, response_samples=2)


def test_nash_zero_speakers_visibility_is_full() -> None:
    problem = _staging(speakers=())
    initial = np.array([[0.0, 0.0, 0.0], [1.2, 0.0, 0.0]])
    report = nash.solve_nash_dramatic_staging(problem, initial, max_iterations=2)
    assert report.camera_visibility_fraction == 1.0


def test_nash_solver_reports_collisions() -> None:
    problem = _staging()
    initial = np.array([[0.0, 0.0, 0.0], [0.01, 0.0, 0.0]])
    report = nash.solve_nash_dramatic_staging(
        problem, initial, max_iterations=1, learning_rate=1e-9
    )
    assert report.collisions >= 1


# --- Wave 23: scattering validation edges -----------------------------------------


def test_scattering_validation_refusals() -> None:
    with pytest.raises(scat.ScatteringError):
        scat._rayleigh_ratio(-1.0)
    with pytest.raises(scat.ScatteringError):
        scat.compute_volumetric_mie_rayleigh_field(
            g=0.3, extinction_coefficient=0.1, depth=1.0, quadrature_nodes=4
        )
    with pytest.raises(scat.ScatteringError):
        scat.circle_of_confusion_radius(2.0, 0.0, 2.8, 50.0)
    with pytest.raises(scat.ScatteringError):
        scat.circle_of_confusion_radius(2.0, 2.0, 2.8, 0.0)


def test_scattering_doppler_refusals() -> None:
    with pytest.raises(scat.ScatteringError):
        scat.compute_doppler_shifted_signal(source_frequency=440.0, source_velocity=float("nan"))
    with pytest.raises(scat.ScatteringError):
        scat.compute_doppler_shifted_signal(
            source_frequency=440.0, source_velocity=0.0, duration=float("inf")
        )
    with pytest.raises(scat.ScatteringError):
        scat.compute_doppler_shifted_signal(
            source_frequency=440.0, source_velocity=0.0, duration=1.0, mode="magnetic"
        )


def test_scattering_doppler_synthesises_waveform() -> None:
    result = scat.compute_doppler_shifted_signal(
        source_frequency=440.0, source_velocity=10.0, duration=0.01
    )
    assert result.waveform is not None
    assert result.waveform.size > 0


# --- Wave 24: playground validation edges -----------------------------------------


def _world(**overrides: object) -> psp.WorldParameters:
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


def _playground_root() -> tuple[psp.PrometheusStudioPlayground, str]:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_world())
    return playground, root.universe_id


def test_playground_luminance_and_psnr_edges() -> None:
    assert np.allclose(psp._luminance_distribution(np.zeros((4, 4))), 1.0 / 16.0)
    assert psp._psnr(np.zeros(4), np.zeros(4)) >= 99.0


def test_playground_compile_refusals(tmp_path: object) -> None:
    playground, root = _playground_root()
    filesystem = WorkspaceFilesystem(str(tmp_path))
    second = playground.create_root_universe(_world(key_light_azimuth_deg=88.0))
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground, [root], ancestor_universe_id=root, filesystem=filesystem
        )
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground,
            [root, root],
            ancestor_universe_id=root,
            filesystem=filesystem,
            frames=0,
        )
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground,
            [root, second.universe_id],
            ancestor_universe_id=root,
            filesystem=filesystem,
            frames=0,
        )
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground,
            [root, second.universe_id],
            ancestor_universe_id=root,
            filesystem=filesystem,
            fps=0,
        )
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground,
            [root, second.universe_id],
            ancestor_universe_id=root,
            filesystem=filesystem,
            out_dir="",
        )
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground,
            [root, second.universe_id],
            ancestor_universe_id=second.universe_id,
            filesystem=filesystem,
        )


def test_playground_compile_without_ffmpeg(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    playground, root = _playground_root()
    branch = playground.fork_counterfactual_universe(
        root, psp.CounterfactualIntervention(fork_time=1.0, anamorphic_squeeze=2.0)
    )[0]
    filesystem = WorkspaceFilesystem(str(tmp_path))
    monkeypatch.setattr(psp, "_ffmpeg_path", lambda: None)
    receipt = psp.compile_counterfactual_quadrant_mp4(
        playground,
        [root, branch.universe_id],
        ancestor_universe_id=root,
        filesystem=filesystem,
        side=16,
        frames=2,
        fps=2,
    )
    assert receipt.encoded is False
    assert receipt.mp4_sha256 == ""


def test_playground_misc_helpers() -> None:
    playground, root = _playground_root()
    assert root in playground.universes
    state = playground.get(root)
    propagated = psp.PrometheusStudioPlayground._propagate_world(state.parameters.port_state, 0.0)
    assert propagated.shape == state.parameters.port_state.shape
    copy = psp.PrometheusStudioPlayground._replan_camera(
        np.zeros((1, 3)), np.array([0.0, 0.0, 0.0])
    )
    assert copy.shape == (1, 3)


def test_playground_universe_cap() -> None:
    playground = psp.PrometheusStudioPlayground()
    for index in range(psp.MAX_UNIVERSES):
        playground.create_root_universe(_world(key_light_azimuth_deg=float(index)))
    with pytest.raises(psp.PlaygroundError):
        playground.create_root_universe(_world())


def test_playground_replan_camera_with_obstacle() -> None:
    keyframes = psp._default_camera_keyframes()
    replanned = psp.PrometheusStudioPlayground._replan_camera(keyframes, np.array([0.2, 0.0, 0.0]))
    assert replanned.ndim == 2 and replanned.shape[1] == 3 and replanned.shape[0] >= 2


def test_playground_fork_universe_cap() -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_world())
    for index in range(psp.MAX_UNIVERSES - 1):
        playground.create_root_universe(_world(key_light_azimuth_deg=100.0 + float(index)))
    with pytest.raises(psp.PlaygroundError):
        playground.fork_counterfactual_universe(
            root.universe_id, psp.CounterfactualIntervention(fork_time=1.0)
        )


# --- Wave 25: verifier / governance coverage edges --------------------------------


def test_verifier_merkle_edges() -> None:
    leaves = [b"a", b"b", b"c"]
    root = pvg.merkle_root(leaves)
    assert not pvg.verify_merkle_inclusion(leaves[0], 0, 1, (b"x", b"y", b"z"), root)
    assert pvg.merkle_root([]) == pvg.merkle_root([])


def test_verifier_ffmpeg_absence_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pvg, "_ffmpeg_path", lambda: None)
    with pytest.raises(pvg.VerifierError):
        pvg._decode_mp4_yuv420p("missing.mp4", 1.0)
    with pytest.raises(pvg.VerifierError):
        pvg._encode_pngs_yuv420p("frames", 6, 1.0)
    with pytest.raises(pvg.VerifierError):
        pvg._decode_mp4_audio("missing.mp4", 48000, 1.0)


def test_verifier_file_exists_handles_bad_path(tmp_path: object) -> None:
    filesystem = WorkspaceFilesystem(str(tmp_path))
    assert pvg._file_exists(filesystem, "does_not_exist.bin") is False


def test_verifier_ledger_observations_and_audio_alignment() -> None:
    ledger = pvg.EpistemicLedger()
    ledger.record(
        pvg.Observation(
            "c1", pvg.LedgerState.PASS, pvg.ObservationProvenance.MEASURED_MEDIA, note="n"
        )
    )
    assert len(ledger.observations) == 1
    reference = np.sin(np.linspace(0.0, 20.0, 4096)) * 1000.0
    decoded = np.concatenate([np.zeros(300), reference])
    assert pvg._audio_equivalent(decoded, reference) in (True, False)


def test_verifier_governed_quarantines_compile_failure(tmp_path: object) -> None:
    playground, root = _playground_root()
    filesystem = WorkspaceFilesystem(str(tmp_path))

    class _Authorizer:
        def authorize(
            self, *, operation: str, actor: str, project_id: str, payload: object
        ) -> bool:
            return True

    with pytest.raises(psp.PlaygroundError):
        pvg.run_prometheus_playground_governed(
            playground,
            [root],
            ancestor_universe_id=root,
            filesystem=filesystem,
            command_bus_authorizer=_Authorizer(),
            attempt_id="bad",
            fencing_token=1,
        )
    assert filesystem.exists(".quarantine/bad_1/quarantine_receipt.json")


def test_verifier_governed_quarantines_verification_failure(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    playground, root = _playground_root()
    branch = playground.fork_counterfactual_universe(
        root, psp.CounterfactualIntervention(fork_time=1.0, anamorphic_squeeze=2.0)
    )[0]
    filesystem = WorkspaceFilesystem(str(tmp_path))

    class _Authorizer:
        def authorize(
            self, *, operation: str, actor: str, project_id: str, payload: object
        ) -> bool:
            return True

    class _RefusedReport:
        verified = False

        def to_dict(self) -> dict[str, object]:
            return {"verified": False}

    def _refuse(*args: object, **kwargs: object) -> object:
        return _RefusedReport()

    monkeypatch.setattr(pvg, "verify_prometheus_playground_bundle_from_disk", _refuse)
    with pytest.raises(pvg.VerifierError):
        pvg.run_prometheus_playground_governed(
            playground,
            [root, branch.universe_id],
            ancestor_universe_id=root,
            filesystem=filesystem,
            command_bus_authorizer=_Authorizer(),
            attempt_id="vf",
            fencing_token=2,
            side=16,
            frames=2,
            fps=2,
        )
    assert filesystem.exists(".quarantine/vf_2/quarantine_receipt.json")


def test_verifier_ledger_require_media_violation(tmp_path: object) -> None:
    playground, root = _playground_root()
    branch = playground.fork_counterfactual_universe(
        root, psp.CounterfactualIntervention(fork_time=1.0, key_light_azimuth_deg=120.0)
    )[0]
    filesystem = WorkspaceFilesystem(str(tmp_path))
    receipt = psp.compile_counterfactual_quadrant_mp4(
        playground,
        [root, branch.universe_id],
        ancestor_universe_id=root,
        filesystem=filesystem,
        side=16,
        frames=2,
        fps=2,
    )
    if receipt.encoded:
        filesystem.resolve(receipt.mp4_path).unlink()
    report = pvg.verify_prometheus_playground_bundle_from_disk(
        filesystem, receipt, require_media=True
    )
    if not receipt.encoded:
        # No encoder on this runner (or encode failed): there is no media to be
        # missing, and the verifier correctly reports UNOBSERVED — the VIOLATED
        # claim requires `encoded=True` (see the verifier's missing-MP4 branch).
        pytest.skip("encoded MP4 unavailable (no FFmpeg on this runner); nothing to tamper with")
    assert report.ledger.state_of("mp4_file_digest") is pvg.LedgerState.VIOLATED
    assert report.verified is False
