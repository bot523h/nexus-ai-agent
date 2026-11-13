"""WAVE 13 — tests for Riemannian portal metrics, SE(3) transport and geodesics.

Real computation only (NumPy); every assertion checks actual values, witnesses
or refusal behaviour.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import non_euclidean_portal_metrics as ne


def _square(
    z: float = 0.0, offset: np.ndarray | None = None, size: float = 1.0
) -> ne.PortalAperture:
    verts = np.array([[0, 0, 0], [size, 0, 0], [size, size, 0], [0, size, 0]], float)
    verts[:, 2] = z
    if offset is not None:
        verts = verts + offset
    return ne.PortalAperture(vertices=verts, normal=np.array([0.0, 0.0, 1.0]))


def _rot_z(theta: float) -> np.ndarray:
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


# --- aperture validation ---------------------------------------------------------


def test_aperture_normal_is_normalised() -> None:
    ap = ne.PortalAperture(vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], normal=[0, 0, 5.0])
    assert float(np.linalg.norm(ap.normal)) == pytest.approx(1.0)


def test_aperture_centroid() -> None:
    ap = _square()
    assert np.allclose(ap.centroid, [0.5, 0.5, 0.0])


def test_aperture_rejects_too_few_vertices() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.PortalAperture(vertices=[[0, 0, 0], [1, 0, 0]], normal=[0, 0, 1])


def test_aperture_rejects_bad_shape() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.PortalAperture(vertices=np.zeros((3, 2)), normal=[0, 0, 1])


def test_aperture_rejects_degenerate_normal() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.PortalAperture(vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], normal=[0, 0, 0])


def test_aperture_rejects_nan() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.PortalAperture(vertices=[[0, 0, 0], [1, 0, 0], [np.nan, 1, 0]], normal=[0, 0, 1])


def test_aperture_rejects_too_many_vertices() -> None:
    verts = np.zeros((ne.MAX_PORTAL_VERTICES + 1, 3))
    with pytest.raises(ne.PortalGeometryError):
        ne.PortalAperture(vertices=verts, normal=[0, 0, 1])


# --- signed distance and containment ---------------------------------------------


def test_signed_distance_signs() -> None:
    ap = _square()
    assert ap.signed_distance([0.5, 0.5, 2.0]) > 0
    assert ap.signed_distance([0.5, 0.5, -2.0]) < 0


def test_signed_distance_rejects_bad_point() -> None:
    with pytest.raises(ne.PortalGeometryError):
        _square().signed_distance([1.0, 2.0])


def test_contains_point_inside_and_outside() -> None:
    ap = _square()
    assert ap.contains_point([0.5, 0.5, 0.0])
    assert not ap.contains_point([1.5, 0.5, 0.0])
    assert not ap.contains_point([-0.5, 0.5, 0.0])


def test_contains_point_triangle() -> None:
    ap = ne.PortalAperture(vertices=[[0, 0, 0], [2, 0, 0], [0, 2, 0]], normal=[0, 0, 1])
    assert ap.contains_point([0.5, 0.5, 0.0])
    assert not ap.contains_point([1.5, 1.5, 0.0])


# --- manifold glue and transform -------------------------------------------------


def test_glue_and_forward_transform() -> None:
    m = ne.PortalTransitionManifold()
    m.add_portal("A", _square())
    m.add_portal("B", _square(offset=np.array([5.0, 0.0, 0.0])))
    m.glue("A", "B", np.eye(3), np.array([5.0, 0.0, 0.0]))
    R, t = m.transform("A", "B")
    assert np.allclose(R @ np.array([0.5, 0.5, 0.0]) + t, [5.5, 0.5, 0.0])


def test_inverse_transform_roundtrips() -> None:
    m = ne.PortalTransitionManifold()
    m.add_portal("A", _square())
    m.add_portal("B", _square())
    R = _rot_z(0.7)
    t = np.array([1.0, -2.0, 3.0])
    m.glue("A", "B", R, t)
    Rf, tf = m.transform("A", "B")
    Rb, tb = m.transform("B", "A")
    x = np.array([0.3, 0.4, 0.5])
    assert np.allclose(Rb @ (Rf @ x + tf) + tb, x, atol=1e-12)


def test_glue_rejects_improper_rotation() -> None:
    m = ne.PortalTransitionManifold()
    m.add_portal("A", _square())
    m.add_portal("B", _square())
    bad = np.diag([1.0, 1.0, -1.0])  # det = -1
    with pytest.raises(ne.PortalGeometryError):
        m.glue("A", "B", bad, np.zeros(3))


def test_glue_rejects_unknown_portal() -> None:
    m = ne.PortalTransitionManifold()
    m.add_portal("A", _square())
    with pytest.raises(ne.PortalGeometryError):
        m.glue("A", "B", np.eye(3), np.zeros(3))


def test_transform_rejects_missing_identification() -> None:
    m = ne.PortalTransitionManifold()
    m.add_portal("A", _square())
    m.add_portal("B", _square())
    with pytest.raises(ne.PortalGeometryError):
        m.transform("A", "B")


def test_add_portal_rejects_empty_name() -> None:
    m = ne.PortalTransitionManifold()
    with pytest.raises(ne.PortalGeometryError):
        m.add_portal("", _square())


# --- crossing detection ----------------------------------------------------------


def test_crossing_detects_subframe_intersection() -> None:
    m = ne.PortalTransitionManifold()
    m.add_portal("A", _square())
    cr = m.crossing(np.array([0.5, 0.5, -1.0]), np.array([0.0, 0.0, 2.0]), "A")
    assert cr.t_star == pytest.approx(0.5)
    assert cr.inside
    assert np.allclose(cr.point, [0.5, 0.5, 0.0])


def test_crossing_outside_polygon_is_not_inside() -> None:
    m = ne.PortalTransitionManifold()
    m.add_portal("A", _square())
    cr = m.crossing(np.array([3.0, 3.0, -1.0]), np.array([0.0, 0.0, 2.0]), "A")
    assert not cr.inside


def test_crossing_requires_sign_change() -> None:
    m = ne.PortalTransitionManifold()
    m.add_portal("A", _square())
    with pytest.raises(ne.PortalGeometryError):
        m.crossing(np.array([0.5, 0.5, 1.0]), np.array([0.0, 0.0, 1.0]), "A")


def test_crossing_rejects_bad_interval() -> None:
    m = ne.PortalTransitionManifold()
    m.add_portal("A", _square())
    with pytest.raises(ne.PortalGeometryError):
        m.crossing(np.array([0.5, 0.5, -1.0]), np.array([0, 0, 2.0]), "A", t0=1.0, t1=1.0)


def test_crossing_rejects_unknown_portal() -> None:
    m = ne.PortalTransitionManifold()
    with pytest.raises(ne.PortalGeometryError):
        m.crossing(np.array([0.0, 0.0, -1.0]), np.array([0, 0, 1.0]), "Z")


# --- kinematics transport --------------------------------------------------------


def test_transport_velocity_is_rotated() -> None:
    m = ne.PortalTransitionManifold()
    m.add_portal("A", _square())
    m.add_portal("B", _square())
    R = _rot_z(np.pi / 2.0)
    m.glue("A", "B", R, np.zeros(3))
    tk = ne.transport_kinematics_across_portal(
        m, "A", "B", np.array([1.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]), np.zeros(3)
    )
    assert np.allclose(tk.linear_velocity, [0.0, 1.0, 0.0], atol=1e-12)


def test_transport_continuity_gap_is_zero() -> None:
    m = ne.PortalTransitionManifold()
    m.add_portal("A", _square())
    m.add_portal("B", _square())
    m.glue("A", "B", _rot_z(0.3), np.array([2.0, 1.0, 0.0]))
    tk = ne.transport_kinematics_across_portal(
        m,
        "A",
        "B",
        np.array([0.1, 0.2, 0.3]),
        np.array([5.0, -3.0, 2.0]),
        np.array([0.5, 0.5, 0.5]),
    )
    assert tk.continuity_gap < 1e-12


def test_transport_projection_shape() -> None:
    tk = ne.transported_kinematics(
        np.zeros(3),
        np.zeros(3),
        np.zeros(3),
        np.eye(3),
        np.zeros(3),
        intrinsic=np.array([[100.0, 0, 50.0], [0, 100.0, 50.0], [0, 0, 1.0]]),
    )
    assert tk.projection.shape == (3, 4)


def test_transport_rejects_bad_intrinsic() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.transported_kinematics(
            np.zeros(3),
            np.zeros(3),
            np.zeros(3),
            np.eye(3),
            np.zeros(3),
            intrinsic=np.eye(2),
        )


def test_transport_rejects_bad_position_shape() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.transported_kinematics(np.zeros(2), np.zeros(3), np.zeros(3), np.eye(3), np.zeros(3))


def test_transport_rejects_bad_cam_translation() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.transported_kinematics(
            np.zeros(3),
            np.zeros(3),
            np.zeros(3),
            np.eye(3),
            np.zeros(3),
            cam_translation=np.zeros(2),
        )


# --- geodesics -------------------------------------------------------------------


def _n_const(_x: np.ndarray) -> float:
    return 1.5


def _n_grad_y(x: np.ndarray) -> float:
    return 1.0 + 0.1 * float(x[1])


def test_geodesic_constant_index_is_straight() -> None:
    res = ne.trace_riemannian_geodesic_rays(
        np.array([0.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]), _n_const, step=0.01, steps=100
    )
    assert res.arc_length == pytest.approx(1.0, abs=1e-9)
    assert res.norm_drift == pytest.approx(0.0, abs=1e-12)
    assert np.allclose(res.path[-1], [1.0, 0.0, 0.0], atol=1e-9)


def test_geodesic_gradient_index_conserves_norm() -> None:
    res = ne.trace_riemannian_geodesic_rays(
        np.array([0.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]), _n_grad_y, step=0.005, steps=400
    )
    assert res.norm_drift < 1e-9
    # The ray bends toward the higher index (positive y).
    assert res.path[-1][1] > 0.0


def test_geodesic_tangent_is_unit_metric_norm() -> None:
    res = ne.trace_riemannian_geodesic_rays(
        np.array([0.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]), _n_const, step=0.02, steps=50
    )
    assert np.allclose(res.metric_norm, res.metric_norm[0], atol=1e-9)


def test_geodesic_rejects_bad_start_shape() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.trace_riemannian_geodesic_rays(np.zeros(2), np.array([1.0, 0, 0]), _n_const)


def test_geodesic_rejects_zero_tangent() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.trace_riemannian_geodesic_rays(np.zeros(3), np.zeros(3), _n_const)


def test_geodesic_rejects_nonpositive_step() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.trace_riemannian_geodesic_rays(np.zeros(3), np.array([1.0, 0, 0]), _n_const, step=0.0)


def test_geodesic_rejects_bad_steps() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.trace_riemannian_geodesic_rays(np.zeros(3), np.array([1.0, 0, 0]), _n_const, steps=0)
    with pytest.raises(ne.PortalGeometryError):
        ne.trace_riemannian_geodesic_rays(
            np.zeros(3), np.array([1.0, 0, 0]), _n_const, steps=ne.MAX_GEODESIC_STEPS + 1
        )


def test_geodesic_rejects_nonpositive_index() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.trace_riemannian_geodesic_rays(
            np.zeros(3), np.array([1.0, 0, 0]), lambda _x: 0.0, steps=10
        )


def test_validate_rotation_rejects_bad_shape() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.transported_kinematics(np.zeros(3), np.zeros(3), np.zeros(3), np.eye(2), np.zeros(3))


def test_validate_rotation_rejects_non_orthonormal() -> None:
    bad = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 2.0]])
    with pytest.raises(ne.PortalGeometryError):
        ne.transported_kinematics(np.zeros(3), np.zeros(3), np.zeros(3), bad, np.zeros(3))


def test_glue_rejects_bad_translation_shape() -> None:
    m = ne.PortalTransitionManifold()
    m.add_portal("A", _square())
    m.add_portal("B", _square())
    with pytest.raises(ne.PortalGeometryError):
        m.glue("A", "B", np.eye(3), np.zeros(2))


def test_crossing_rejects_bad_origin_shape() -> None:
    m = ne.PortalTransitionManifold()
    m.add_portal("A", _square())
    with pytest.raises(ne.PortalGeometryError):
        m.crossing(np.zeros(2), np.array([0.0, 0, 1.0]), "A")


def test_metric_rejects_nonpositive_index_at_center() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.trace_riemannian_geodesic_rays(
            np.zeros(3), np.array([1.0, 0, 0]), lambda _x: -1.0, steps=5
        )


def test_metric_rejects_index_that_drops_negative_along_path() -> None:
    # Positive at the start, but the ray walks into n <= 0 -> the metric guard fires.
    def n_fn(x: np.ndarray) -> float:
        return float(1.0 - x[0])

    with pytest.raises(ne.PortalGeometryError):
        ne.trace_riemannian_geodesic_rays(
            np.zeros(3), np.array([1.0, 0, 0]), n_fn, step=0.3, steps=6
        )


def test_metric_rejects_nonpositive_index_at_probe() -> None:
    # Positive at the sample point but the +h probe crosses to <= 0.
    def n_fn(x: np.ndarray) -> float:
        return float(1e-4 - 1e8 * (x[0] - 0.5) ** 2)

    with pytest.raises(ne.PortalGeometryError):
        ne.trace_riemannian_geodesic_rays(
            np.array([0.5, 0.0, 0.0]), np.array([1.0, 0, 0]), n_fn, step=0.1, steps=1
        )


def test_aperture_rejects_bad_normal_shape() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.PortalAperture(vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], normal=[0, 1])


def test_transport_rejects_bad_translation_shape() -> None:
    with pytest.raises(ne.PortalGeometryError):
        ne.transported_kinematics(np.zeros(3), np.zeros(3), np.zeros(3), np.eye(3), np.zeros(2))
