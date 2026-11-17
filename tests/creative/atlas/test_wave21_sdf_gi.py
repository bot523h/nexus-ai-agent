"""WAVE 21 — tests for analytical SDF global illumination."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import sdf_global_illumination as g


def test_sphere_exact_distance() -> None:
    s = g.Sphere(np.array([0.0, 0.0, 0.0]), 2.0)
    assert s.distance(np.array([0.0, 0.0, 0.0])) == pytest.approx(-2.0)
    assert s.distance(np.array([5.0, 0.0, 0.0])) == pytest.approx(3.0)
    assert s.distance(np.array([2.0, 0.0, 0.0])) == pytest.approx(0.0)


def test_box_exact_distance_inside_outside() -> None:
    b = g.Box(np.array([0.0, 0.0, 0.0]), np.array([1.0, 1.0, 1.0]))
    assert b.distance(np.array([0.0, 0.0, 0.0])) == pytest.approx(-1.0)
    assert b.distance(np.array([2.0, 0.0, 0.0])) == pytest.approx(1.0)
    assert b.distance(np.array([2.0, 2.0, 0.0])) == pytest.approx(np.sqrt(2.0))


def test_plane_signed_distance() -> None:
    p = g.Plane(np.array([0.0, 2.0, 0.0]), 1.0)  # unit normal (0,1,0), offset 1
    assert p.distance(np.array([0.0, 3.0, 0.0])) == pytest.approx(2.0)
    assert p.distance(np.array([0.0, 0.0, 0.0])) == pytest.approx(-1.0)


def test_torus_distance_on_ring() -> None:
    t = g.Torus(np.array([0.0, 0.0, 0.0]), major=2.0, minor=0.5)
    # On the tube surface at (major, 0, 0) + minor in x.
    assert t.distance(np.array([2.5, 0.0, 0.0])) == pytest.approx(0.0)
    assert t.distance(np.array([0.0, 0.0, 0.0])) == pytest.approx(1.5)


def test_smooth_union_blends() -> None:
    a = g.Sphere(np.array([-0.5, 0.0, 0.0]), 1.0)
    b = g.Sphere(np.array([0.5, 0.0, 0.0]), 1.0)
    su = g.SmoothUnion(a, b, k=0.5)
    # Midpoint is inside the union of both -> negative.
    assert su.distance(np.array([0.0, 0.0, 0.0])) < 0.0
    # A plain union would give the min; smooth union is <= min (blends).
    assert su.distance(np.array([0.0, 0.0, 0.0])) <= min(
        a.distance(np.array([0.0, 0.0, 0.0])), b.distance(np.array([0.0, 0.0, 0.0]))
    )


def test_intersection_and_difference() -> None:
    a = g.Box(np.array([0.0, 0.0, 0.0]), np.array([1.0, 1.0, 1.0]))
    b = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.2)
    inter = g.Intersection(a, b)
    assert inter.distance(np.array([0.0, 0.0, 0.0])) == pytest.approx(-1.0)
    diff = g.Difference(a, b)
    # Inside the removed sphere -> positive (outside the difference set).
    assert diff.distance(np.array([0.0, 0.0, 0.0])) > 0.0


def test_sphere_trace_hits_sphere() -> None:
    s = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0)
    r = g.sphere_trace(s, np.array([0.0, 0.0, 5.0]), np.array([0.0, 0.0, -1.0]))
    assert r.hit
    assert r.t == pytest.approx(4.0, abs=1e-4)
    assert np.linalg.norm(r.point - np.array([0.0, 0.0, 1.0])) < 1e-4


def test_sphere_trace_misses() -> None:
    s = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0)
    r = g.sphere_trace(s, np.array([0.0, 0.0, 5.0]), np.array([1.0, 0.0, 0.0]))
    assert not r.hit


def test_sphere_trace_cannot_tunnel() -> None:
    # Lipschitz-1 sphere tracing never steps past a surface: an exact hit is
    # found even with a huge starting distance and a grazing direction.
    s = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0)
    r = g.sphere_trace(s, np.array([0.0, 0.0, 1e3]), np.array([0.0, 1e-3, -1.0]))
    assert r.hit


def test_normal_is_unit_and_points_outward() -> None:
    s = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0)
    n = g.sdf_normal(s, np.array([1.0, 0.0, 0.0]))
    assert np.linalg.norm(n) == pytest.approx(1.0)
    assert n[0] == pytest.approx(1.0, abs=1e-3)


def test_ao_open_plane_is_one() -> None:
    plane = g.Plane(np.array([0.0, 1.0, 0.0]), 0.0)
    ao = g.sdf_ambient_occlusion(plane, np.array([0.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]))
    assert ao == pytest.approx(1.0, abs=1e-6)


def test_ao_in_concave_gap_is_less_than_one() -> None:
    # Two spheres nearly touching: the inner-facing surface is partially
    # occluded by the neighbour, so AO < 1 while an open plane gives 1.
    a = g.Sphere(np.array([-1.1, 0.0, 0.0]), 1.0)
    b = g.Sphere(np.array([1.1, 0.0, 0.0]), 1.0)

    class Union:
        def distance(self, point: np.ndarray) -> float:
            return min(a.distance(point), b.distance(point))

    u = Union()
    p = np.array([-0.1, 0.0, 0.0])
    n = g.sdf_normal(u, p)
    ao = g.sdf_ambient_occlusion(u, p, n)
    assert 0.0 < ao < 1.0


def test_soft_shadow_blocked_and_lit() -> None:
    s = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0)
    blocked = g.sdf_soft_shadow(s, np.array([2.0, 0.0, 0.0]), np.array([-1.0, 0.0, 0.0]))
    assert blocked == pytest.approx(0.0, abs=1e-6)
    lit = g.sdf_soft_shadow(s, np.array([2.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]))
    assert lit > 0.0


def test_sky_irradiance_open_vs_occluded() -> None:
    plane = g.Plane(np.array([0.0, 1.0, 0.0]), 0.0)
    open_irr, se, n = g.hemisphere_sky_irradiance(
        plane, np.array([0.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]), samples=64
    )
    assert open_irr == pytest.approx(1.0, abs=1e-6)
    assert n == 64
    assert se >= 0.0
    # A sphere directly above shades part of the hemisphere.
    scene = g.SmoothUnion(plane, g.Sphere(np.array([0.0, 1.2, 0.0]), 0.5), k=0.01)
    occ_irr, _, _ = g.hemisphere_sky_irradiance(
        scene, np.array([0.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]), samples=64
    )
    assert occ_irr < open_irr


# --- adversarial / refusal cases -------------------------------------------------


def test_refuse_zero_radius() -> None:
    with pytest.raises(g.SDFError):
        g.Sphere(np.array([0.0, 0.0, 0.0]), 0.0)


def test_refuse_nan_center() -> None:
    with pytest.raises(g.SDFError):
        g.Sphere(np.array([np.nan, 0.0, 0.0]), 1.0)


def test_refuse_bad_half_extents() -> None:
    with pytest.raises(g.SDFError):
        g.Box(np.array([0.0, 0.0, 0.0]), np.array([1.0, -1.0, 1.0]))


def test_refuse_zero_normal() -> None:
    with pytest.raises(g.SDFError):
        g.Plane(np.array([0.0, 0.0, 0.0]), 0.0)


def test_refuse_bad_torus() -> None:
    with pytest.raises(g.SDFError):
        g.Torus(np.array([0.0, 0.0, 0.0]), major=-1.0, minor=0.5)
    with pytest.raises(g.SDFError):
        g.Torus(np.array([0.0, 0.0, 0.0]), major=1.0, minor=0.0)


def test_refuse_bad_smooth_union_k() -> None:
    a = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0)
    with pytest.raises(g.SDFError):
        g.SmoothUnion(a, a, k=0.0)


def test_refuse_zero_direction() -> None:
    s = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0)
    with pytest.raises(g.SDFError):
        g.sphere_trace(s, np.array([0.0, 0.0, 5.0]), np.array([0.0, 0.0, 0.0]))


def test_refuse_bad_t_range() -> None:
    s = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0)
    with pytest.raises(g.SDFError):
        g.sphere_trace(
            s, np.array([0.0, 0.0, 5.0]), np.array([0.0, 0.0, -1.0]), t_min=2.0, t_max=1.0
        )


def test_refuse_oversized_steps() -> None:
    s = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0)
    with pytest.raises(g.SDFError):
        g.sphere_trace(s, np.array([0.0, 0.0, 5.0]), np.array([0.0, 0.0, -1.0]), max_steps=0)


def test_refuse_degenerate_normal() -> None:
    # Constant field: gradient vanishes.
    class Flat:
        def distance(self, point: np.ndarray) -> float:
            return 0.0

    with pytest.raises(g.SDFError):
        g.sdf_normal(Flat(), np.array([0.0, 0.0, 0.0]))


def test_refuse_bad_ao_params() -> None:
    plane = g.Plane(np.array([0.0, 1.0, 0.0]), 0.0)
    with pytest.raises(g.SDFError):
        g.sdf_ambient_occlusion(
            plane, np.array([0.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]), samples=0
        )
    with pytest.raises(g.SDFError):
        g.sdf_ambient_occlusion(
            plane, np.array([0.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]), falloff=0.0
        )


def test_refuse_bad_shadow_params() -> None:
    s = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0)
    with pytest.raises(g.SDFError):
        g.sdf_soft_shadow(s, np.array([2.0, 0.0, 0.0]), np.array([-1.0, 0.0, 0.0]), softness=0.0)


def test_refuse_bad_irradiance_params() -> None:
    plane = g.Plane(np.array([0.0, 1.0, 0.0]), 0.0)
    with pytest.raises(g.SDFError):
        g.hemisphere_sky_irradiance(
            plane, np.array([0.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]), samples=0
        )
    with pytest.raises(g.SDFError):
        g.hemisphere_sky_irradiance(
            plane, np.array([0.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]), sky_radiance=-1.0
        )


def test_irradiance_single_sample_has_zero_se() -> None:
    plane = g.Plane(np.array([0.0, 1.0, 0.0]), 0.0)
    irr, se, n = g.hemisphere_sky_irradiance(
        plane, np.array([0.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]), samples=1
    )
    assert se == 0.0 and n == 1


def test_refuse_nonfinite_plane_offset() -> None:
    with pytest.raises(g.SDFError):
        g.Plane(np.array([0.0, 1.0, 0.0]), float("inf"))


def test_refuse_nonpositive_eps() -> None:
    s = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0)
    with pytest.raises(g.SDFError):
        g.sphere_trace(s, np.array([0.0, 0.0, 5.0]), np.array([0.0, 0.0, -1.0]), eps=0.0)


def test_refuse_nonpositive_normal_h() -> None:
    s = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0)
    with pytest.raises(g.SDFError):
        g.sdf_normal(s, np.array([1.0, 0.0, 0.0]), h=0.0)


def test_refuse_bad_ao_step_scale() -> None:
    plane = g.Plane(np.array([0.0, 1.0, 0.0]), 0.0)
    with pytest.raises(g.SDFError):
        g.sdf_ambient_occlusion(
            plane, np.array([0.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]), step_scale=0.0
        )


def test_refuse_oversized_shadow_steps() -> None:
    s = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0)
    with pytest.raises(g.SDFError):
        g.sdf_soft_shadow(s, np.array([2.0, 0.0, 0.0]), np.array([-1.0, 0.0, 0.0]), max_steps=0)


def test_sphere_trace_exhausts_step_budget() -> None:
    # A field with a very shallow approach exhausts the step budget -> no hit,
    # and the result reports exactly max_steps (honest resource exhaustion).
    class Flat:
        def distance(self, point: np.ndarray) -> float:
            return 1e-3

    r = g.sphere_trace(Flat(), np.array([0.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]), max_steps=5)
    assert not r.hit
    assert r.steps == 5


def test_refuse_nonfinite_coordinate() -> None:
    with pytest.raises(g.SDFError):
        g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0).distance(np.array([np.inf, 0.0, 0.0]))


def test_refuse_bad_shape_vector() -> None:
    with pytest.raises(g.SDFError):
        g.Sphere(np.array([0.0, 0.0]), 1.0)


def test_soft_shadow_exhausts_step_budget() -> None:
    # Far light + small step budget: the loop exhausts its steps without ever
    # reaching t_max, returning the accumulated (lit) factor honestly.
    s = g.Sphere(np.array([0.0, 0.0, 0.0]), 1.0)
    lit = g.sdf_soft_shadow(
        s, np.array([2.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]), t_max=1e6, max_steps=4
    )
    assert 0.0 <= lit <= 1.0
