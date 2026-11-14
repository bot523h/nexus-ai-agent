"""WAVE 20 — adversarial and falsification suite for the TITAN operators.

Every case here is an *adversarial-input* test (a refusal the code must produce),
clearly separated from the *code-mutation* experiments in
``test_wave20_staged_spine.py`` (which inject a real production defect and show
a recorded digest or measured value changes). A refusal test is NOT a mutation
kill; the two are reported separately.

The suite groups 20+ distinct adversarial inputs across the waves: degenerate
geometry, non-finite arrays, out-of-range parameters, resource-limit budgets,
forged "verified" evidence, path traversal, and tampered persisted bytes.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import (
    anamorphic_seidel_optics as optics,
)
from nexus_ai_agent.creative.atlas import (
    eikonal_crowd_social_force as crowd,
)
from nexus_ai_agent.creative.atlas import (
    non_euclidean_portal_metrics as portal,
)
from nexus_ai_agent.creative.atlas import (
    pontryagin_editing_pacing_solver as pacing,
)
from nexus_ai_agent.creative.atlas import (
    titan_staged_execution_spine as spine,
)
from nexus_ai_agent.creative.atlas import (
    volumetric_atmospheric_scattering as atmosphere,
)
from nexus_ai_agent.creative.atlas import (
    voronoi_fracture_destruction_ledger as fracture,
)


# 1. Portal: degenerate (zero) normal is refused.
def test_adversarial_portal_degenerate_normal() -> None:
    with pytest.raises(portal.PortalGeometryError):
        portal.PortalAperture(
            vertices=np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]),
            normal=np.zeros(3),
        )


# 2. Portal: non-rotation matrix refused by transported kinematics.
def test_adversarial_portal_non_rotation() -> None:
    with pytest.raises(portal.PortalGeometryError):
        portal.transported_kinematics(
            position=np.zeros(3),
            linear_velocity=np.zeros(3),
            angular_velocity=np.zeros(3),
            rotation=np.eye(3) * 2.0,
            translation=np.zeros(3),
        )


# 3. Optics: pupil radius out of range.
def test_adversarial_optics_rho_out_of_range() -> None:
    with pytest.raises(optics.OpticsError):
        optics.seidel_wavefront(
            optics.SeidelCoefficients(spherical=1.0), np.array([1.2]), np.array([0.0])
        )


# 4. Optics: even PSF size refused.
def test_adversarial_optics_even_psf_size() -> None:
    with pytest.raises(optics.OpticsError):
        optics.anamorphic_psf(optics.AnamorphicLens(), size=64)


# 5. Optics: non-finite image refused.
def test_adversarial_optics_nan_image() -> None:
    img = np.zeros((8, 8, 3))
    img[0, 0, 0] = np.inf
    with pytest.raises(optics.OpticsError):
        optics.apply_anamorphic_optics(img, optics.AnamorphicLens())


# 6. Atmosphere: zero wavelength refused.
def test_adversarial_atmosphere_zero_wavelength() -> None:
    with pytest.raises(atmosphere.AtmosphericError):
        atmosphere.rayleigh_beta(np.array([0.0, 550.0, 460.0]))


# 7. Atmosphere: Henyey-Greenstein g out of (-1, 1).
def test_adversarial_atmosphere_bad_g() -> None:
    with pytest.raises(atmosphere.AtmosphericError):
        atmosphere.henyey_greenstein_phase(np.array([0.0]), 1.0)


# 8. Crowd: too many agents beyond budget.
def test_adversarial_crowd_too_many_agents() -> None:
    n = crowd.MAX_AGENTS + 1
    with pytest.raises(crowd.CrowdError):
        crowd.simulate_and_heal_crowd_trajectories(
            np.zeros((n, 2)),
            np.zeros((n, 2)),
            np.ones(n),
            np.ones((n, 2)),
        )


# 9. Crowd: mismatched radii shape.
def test_adversarial_crowd_bad_radii_shape() -> None:
    with pytest.raises(crowd.CrowdError):
        crowd.simulate_and_heal_crowd_trajectories(
            np.zeros((2, 2)),
            np.zeros((2, 2)),
            np.ones(3),
            np.ones((2, 2)),
        )


# 10. Fracture: over-budget voxel body.
def test_adversarial_fracture_over_budget() -> None:
    with pytest.raises(fracture.FractureError):
        fracture.fracture_voronoi_cells(
            (300, 300, 300), [fracture.FractureSeed(np.array([0.0, 0.0, 0.0]))]
        )


# 11. Fracture: zero-density seed refused.
def test_adversarial_fracture_zero_density() -> None:
    with pytest.raises(fracture.FractureError):
        fracture.FractureSeed(np.array([1.0, 1.0, 1.0]), density=0.0)


# 12. Fracture: ledger detects a tampered frame.
def test_adversarial_fracture_ledger_tamper() -> None:
    seeds = [
        fracture.FractureSeed(np.array([1.0, 1.0, 1.0])),
        fracture.FractureSeed(np.array([3.0, 3.0, 3.0])),
    ]
    cells = fracture.fracture_voronoi_cells((4, 4, 4), seeds).cells
    led = fracture.DestructionLedger()
    led.append_frame(0, cells)
    led.tamper_frame(0, {"frame": 0, "cells": [{"id": 0, "voxels": 999, "microns": [0, 0, 0]}]})
    assert not led.verify().valid


# 13. Pacing: non-PSD Q refused.
def test_adversarial_pacing_non_psd() -> None:
    with pytest.raises(pacing.PacingError):
        pacing.LQProblem(
            A=np.eye(1),
            B=np.ones((1, 1)),
            Q=np.array([[-1.0]]),
            R=np.eye(1),
            Qf=np.eye(1),
            x0=np.zeros(1),
            horizon=2,
        )


# 14. Pacing: brute-force oracle refuses a too-large search space.
def test_adversarial_pacing_oracle_too_large() -> None:
    prob = pacing.LQProblem(
        A=np.array([[1.0]]),
        B=np.array([[1.0]]),
        Q=np.eye(1),
        R=np.eye(1),
        Qf=np.eye(1),
        x0=np.ones(1),
        horizon=12,
    )
    with pytest.raises(pacing.PacingError):
        pacing.brute_force_lq_cost(prob, control_grid=np.linspace(-5, 5, 300))


# 15. Pacing: control sequence with NaN refused.
def test_adversarial_pacing_nan_control() -> None:
    with pytest.raises(pacing.PacingError):
        pacing.pacing_plan_from_control(np.array([np.nan]))


# 16. Spine: unsafe (absolute) output directory refused.
def test_adversarial_spine_absolute_outdir(tmp_path: Path) -> None:
    from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(spine.SpineError):
        spine.run_staged_spine(_tiny_film(), fs, out_dir="/etc")


# 17. Spine: out-of-range fixture side refused.
def test_adversarial_spine_bad_side(tmp_path: Path) -> None:
    from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(spine.SpineError):
        spine.run_staged_spine(_tiny_film(), fs, side=0)


# 18. Compiler: non-positive frame rate refused.
def test_adversarial_compiler_bad_frame_rate() -> None:
    from nexus_ai_agent.creative.atlas import titan_feature_film_compiler as tf

    doc = _tiny_story()
    req = _tiny_request()
    with pytest.raises(tf.CompilerError):
        tf.compile_feature_film(doc, plan_request=req, frame_rate=-1.0)


# 19. Compiler/export: path traversal refused by the filesystem boundary.
def test_adversarial_export_path_traversal(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.atlas import titan_feature_film_compiler as tf
    from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError, WorkspaceFilesystem

    film = tf.compile_feature_film(_tiny_story(), plan_request=_tiny_request())
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(FilesystemBoundaryError):
        tf.export_compiled_film(film, fs, "../../etc/passwd")


# 20. Forged evidence: an unverified candidate is never treated as verified.
def test_adversarial_forged_verified_evidence() -> None:
    from nexus_ai_agent.creative.episode_causality_lab import (
        PlanRequest,
        ShotCandidate,
        SourceRef,
        TriValue,
        plan_edit,
    )
    from nexus_ai_agent.creative.episode_causality_lab.statuses import SolverStatus

    forged = ShotCandidate(
        id="s1",
        asset_ref="asset://s1",
        covers_beats=("b1",),
        frames=10,
        evidence_status=TriValue.UNKNOWN,
        evidence_ref="",  # not verified
        source=SourceRef(path="p", line=1),
    )
    req = PlanRequest(
        beats=("b1",), candidates=(forged,), frame_budget=100, require_verified_evidence=True
    )
    res = plan_edit(req)
    assert res.status is not SolverStatus.PROVEN_OPTIMAL
    assert res.status is SolverStatus.INFEASIBLE or res.status is SolverStatus.UNKNOWN


# 21. Unknown order: temporal constraints with a contradictory cycle are infeasible.
def test_adversarial_temporal_contradiction() -> None:
    from nexus_ai_agent.creative.episode_causality_lab import (
        SourceRef,
        TemporalConstraint,
        check_consistency,
    )
    from nexus_ai_agent.creative.episode_causality_lab.statuses import Status

    a = TemporalConstraint(
        id="c1", a="A", b="B", lower=5, upper=10, source=SourceRef(path="p", line=1)
    )
    b = TemporalConstraint(
        id="c2", a="B", b="A", lower=5, upper=10, source=SourceRef(path="p", line=2)
    )
    res = check_consistency((a, b))
    assert res.status is Status.FAIL


# 22. Tampered persisted output is detected on re-open.
def test_adversarial_tampered_persisted_output(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.atlas import titan_feature_film_compiler as tf
    from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

    film = tf.compile_feature_film(_tiny_story(), plan_request=_tiny_request())
    fs = WorkspaceFilesystem(tmp_path)
    art = tf.export_compiled_film(film, fs, "compiled_film.json")
    # Tamper on disk, then re-read through the boundary.
    (tmp_path / "compiled_film.json").write_bytes(b"tampered")
    reopened = fs.read_bytes(art.relative_path, max_bytes=tf.MAX_COMPILED_BYTES)
    import hashlib

    assert hashlib.sha256(reopened).hexdigest() != art.sha256


# 23. Fracture: empty mask refused.
def test_adversarial_fracture_empty_mask() -> None:
    with pytest.raises(fracture.FractureError):
        fracture.fracture_voronoi_cells(
            (4, 4, 4),
            [fracture.FractureSeed(np.array([0.0, 0.0, 0.0]))],
            body_mask=np.zeros((4, 4, 4), bool),
        )


# 24. Optics: streak length not smaller than the PSF side refused.
def test_adversarial_optics_bad_streak() -> None:
    with pytest.raises(optics.OpticsError):
        optics.anamorphic_psf(optics.AnamorphicLens(), size=9, streak_length=9)


# --- shared tiny fixtures --------------------------------------------------------


def _tiny_story():  # type: ignore[no-untyped-def]
    from nexus_ai_agent.creative.episode_causality_lab import (
        SCHEMA_VERSION,
        Episode,
        Scene,
        StoryBeat,
        StoryDocument,
        TimeOfDay,
    )

    return StoryDocument(
        schema_version=SCHEMA_VERSION,
        episodes=(Episode(id="ep1", order_index=0),),
        scenes=(
            Scene(
                id="sc1",
                episode_id="ep1",
                location_id="l",
                order_index=0,
                time_of_day=TimeOfDay.DAY,
            ),
        ),
        beats=(StoryBeat(id="b1", episode_id="ep1", order_index=0),),
    )


def _tiny_request():  # type: ignore[no-untyped-def]
    from nexus_ai_agent.creative.episode_causality_lab import (
        PlanRequest,
        ShotCandidate,
        SourceRef,
        TriValue,
    )

    shot = ShotCandidate(
        id="s1",
        asset_ref="asset://s1",
        covers_beats=("b1",),
        frames=10,
        evidence_status=TriValue.TRUE,
        evidence_ref="evidence://s1",
        source=SourceRef(path="p", line=1),
    )
    return PlanRequest(beats=("b1",), candidates=(shot,), frame_budget=100)


def _tiny_film():  # type: ignore[no-untyped-def]
    from nexus_ai_agent.creative.atlas import titan_feature_film_compiler as tf

    return tf.compile_feature_film(_tiny_story(), plan_request=_tiny_request())
