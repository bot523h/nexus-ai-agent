"""Precise numerical killer assertions for the ATLAS/TITAN/OLYMPUS mutation campaign.

Each test here pins an exact value or boundary behaviour of a Wave 13-30 operator
so that a specific injected source defect (see
``test_titan_olympus_mutations.py``) is *killed*: the targeted assertion must fail
when the operator is mutated. These are real-computation oracles (REAL COMPUTE),
not negative-input refusal tests.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import diegetic_modal_foley as foley
from nexus_ai_agent.creative.atlas import non_euclidean_portal_metrics as ne
from nexus_ai_agent.creative.atlas import rigid_contact_pgs_lcp as rigid
from nexus_ai_agent.creative.atlas import titan_staged_execution_spine as spine
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem


def _offset_square(z: float) -> ne.PortalAperture:
    verts = np.array([[0, 0, z], [1, 0, z], [1, 1, z], [0, 1, z]], float)
    return ne.PortalAperture(vertices=verts, normal=np.array([0.0, 0.0, 1.0]))


def test_portal_signed_distance_uses_centroid_offset() -> None:
    # A square lifted to z=3: a point at z=0 is 3 units behind the plane, so the
    # signed distance is exactly -3 (a sign flip in the centroid term gives +3).
    ap = _offset_square(3.0)
    assert ap.signed_distance([0.5, 0.5, 0.0]) == pytest.approx(-3.0)
    assert ap.signed_distance([0.5, 0.5, 3.0]) == pytest.approx(0.0)


def test_portal_proper_rotation_det_tolerance_is_tight() -> None:
    # A reflection (det = -1) must be refused: |det - 1| = 2 far exceeds 1e-9.
    reflection = np.diag([1.0, 1.0, -1.0])
    with pytest.raises(ne.PortalGeometryError):
        ne.PortalTransitionManifold().glue("A", "B", reflection, np.zeros(3))


def test_rigid_inverse_mass_is_reciprocal_of_mass() -> None:
    heavy = rigid.RigidBody(mass=2.0, inertia=4.0, position=np.array([0.0, 0.0]))
    assert heavy.inv_mass == pytest.approx(0.5)
    assert heavy.inv_inertia == pytest.approx(0.25)
    static = rigid.RigidBody(mass=2.0, inertia=4.0, position=np.array([0.0, 0.0]), static=True)
    assert static.inv_mass == 0.0


def test_foley_event_heard_time_uses_distance_over_speed() -> None:
    # 343 m at 343 m/s -> exactly 1.0 s of travel delay (multiplying instead of
    # dividing gives ~117 000 s, far outside the 2 s timeline).
    ev = foley.DiegeticEvent("thunder", 0.0, 343.0, "stone")
    rep = foley.place_foley_events([ev], duration=2.0)
    assert rep.delays[0] == pytest.approx(1.0)
    assert rep.heard_times[0] == pytest.approx(1.0)
    assert rep.placed_labels == ("thunder",)


def test_foley_event_starting_exactly_at_end_is_skipped() -> None:
    # screen_time == duration puts start == n exactly: an out-of-timeline event
    # that must be skipped, not placed.
    ev = foley.DiegeticEvent("edge", 1.0, 0.0, "wood")
    rep = foley.place_foley_events([ev], duration=1.0)
    assert rep.placed_labels == ()
    assert rep.peak == 0.0


def test_foley_placement_adds_the_impact_samples() -> None:
    # A single zero-distance, zero-delay event must mix in the impact samples
    # *added* (not subtracted): the mix prefix equals the synthesised impact.
    ev = foley.DiegeticEvent("hit", 0.0, 0.0, "wood")
    rep = foley.place_foley_events([ev], duration=1.0)
    impact = foley.synthesise_impact(
        foley.MATERIALS["wood"], sample_rate=48000, duration=0.8, amplitude=1.0, seed=0
    )
    np.testing.assert_allclose(rep.mix[: impact.samples.size], impact.samples, atol=1e-12)


def _tiny_film() -> object:
    from nexus_ai_agent.creative.atlas import titan_feature_film_compiler as tf
    from nexus_ai_agent.creative.episode_causality_lab import (
        SCHEMA_VERSION,
        Character,
        Episode,
        PlanRequest,
        Scene,
        ShotCandidate,
        SourceRef,
        StoryBeat,
        StoryDocument,
        TimeOfDay,
        TriValue,
    )

    src = SourceRef(path="ep1/sc1", line=1)
    doc = StoryDocument(
        schema_version=SCHEMA_VERSION,
        characters=(Character(id="alice"),),
        episodes=(Episode(id="ep1", order_index=0),),
        scenes=(
            Scene(
                id="sc1",
                episode_id="ep1",
                location_id="loc1",
                order_index=0,
                time_of_day=TimeOfDay.DAY,
            ),
        ),
        beats=(StoryBeat(id="b1", episode_id="ep1", order_index=0),),
    )
    shot = ShotCandidate(
        id="s1",
        asset_ref="asset://s1",
        covers_beats=("b1",),
        frames=24,
        evidence_status=TriValue.TRUE,
        evidence_ref="evidence://s1",
        source=src,
    )
    req = PlanRequest(beats=("b1",), candidates=(shot,), frame_budget=100)
    return tf.compile_feature_film(doc, plan_request=req)


def test_spine_rejects_parent_traversal_out_dir(tmp_path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(spine.SpineError):
        spine.run_staged_spine(_tiny_film(), fs, out_dir="a/../b")


def test_spine_rejects_side_just_over_the_limit(tmp_path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(spine.SpineError):
        spine.run_staged_spine(_tiny_film(), fs, side=spine.MAX_FIXTURE_SIDE + 1)


def test_eikonal_heading_is_a_unit_vector() -> None:
    # Every non-goal heading is the normalised descent direction: ||v|| == 1.
    from nexus_ai_agent.creative.atlas import eikonal_crowd_social_force as ec

    cost = np.ones((33, 33))
    goal = np.zeros((33, 33), bool)
    goal[16, 16] = True
    ef = ec.solve_eikonal_navigation_potential(cost, goal)
    norms = np.linalg.norm(ef.heading[~goal], axis=-1)
    np.testing.assert_allclose(norms, 1.0, atol=1e-12)


def test_anamorphic_psf_centre_value_is_exact() -> None:
    # Green channel centre of the PSF for the default lens/defocus is a fixed
    # real number (a width change moves it).
    from nexus_ai_agent.creative.atlas import anamorphic_seidel_optics as ao

    psf = ao.anamorphic_psf(ao.AnamorphicLens(), size=33, defocus_mm=0.1, pixel_pitch_mm=0.01)
    assert float(psf[16, 16, 1]) == pytest.approx(0.06654798687149109, rel=1e-12)


def test_compiler_reopen_digest_reflects_persisted_bytes(tmp_path) -> None:
    # The reopened digest must be of the bytes read back from disk.
    import hashlib

    from nexus_ai_agent.creative.atlas import titan_feature_film_compiler as tf

    film = _tiny_film()
    fs = WorkspaceFilesystem(tmp_path)
    art = tf.export_compiled_film(film, fs, "out/film.json")
    on_disk = (tmp_path / art.relative_path).read_bytes()
    assert art.reopened_sha256 == hashlib.sha256(on_disk).hexdigest()
    assert art.reopened_sha256 == art.sha256


def test_ocean_spreading_is_cosine_squared() -> None:
    # Directional spreading is cos^2, so an aligned wave (cos=1) has exactly
    # twice the amplitude of a 60-degree wave (cos=0.5) at the same |k|.
    from nexus_ai_agent.creative.atlas import spectral_ocean_swe as o

    k = 0.1
    kx = np.array([[k, k * np.cos(np.pi / 3.0)]])
    ky = np.array([[0.0, k * np.sin(np.pi / 3.0)]])
    spec = o.jonswap_spectrum(kx, ky, wind_speed=10.0, wind_dir=(1.0, 0.0))
    ratio = float(spec.amplitudes[0, 0] / spec.amplitudes[0, 1])
    assert ratio == pytest.approx(2.0, rel=1e-12)


def test_blackbody_rgb_is_gamma_encoded() -> None:
    # The blue channel of a 3000 K blackbody is the linear value ** (1/2.2).
    from nexus_ai_agent.creative.atlas import buoyant_pyro_blackbody as p

    assert float(p.blackbody_rgb(3000.0)[2]) == pytest.approx(0.5726894892040658, rel=1e-12)


def test_botanical_mode_shape_midpoint_is_exact() -> None:
    # The unit-normalised first mode shape at xi=0.5 is a fixed real number.
    from nexus_ai_agent.creative.atlas import botanical_modal_wind as b

    stem = b.ModalStem(
        length=2.0, youngs_modulus=1e9, area_moment=1e-8, density=700.0, cross_area=1e-4
    )
    assert float(stem.mode_shape(1, np.array([0.5]))[0]) == pytest.approx(
        0.33952311286532394, rel=1e-12
    )


def test_foley_impact_decays_exponentially_in_time() -> None:
    # sample[t] = level * exp(-t / tau) * sin(...): dividing the exponent by tau
    # (rather than multiplying) changes this exact value.
    mat = foley.MATERIALS["wood"]
    impact = foley.synthesise_impact(mat, sample_rate=48000, duration=0.8, amplitude=1.0, seed=0)
    assert float(impact.samples[100]) == pytest.approx(0.6285374778771277, rel=1e-12)


def test_olympus_master_rejects_parent_traversal_out_dir(tmp_path) -> None:
    from nexus_ai_agent.creative.atlas import olympus_master as om
    from nexus_ai_agent.creative.atlas.showrunner_bible import (
        Character,
        Episode,
        ShowrunnerBible,
    )

    bible = ShowrunnerBible()
    bible.add_character(Character("a", "Alice"))
    bible.add_episode(Episode(1, "Pilot"))
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(om.MasterError):
        om.render_master(bible, fs, out_dir="a/../b", side=16, frames=1, fps=1)
