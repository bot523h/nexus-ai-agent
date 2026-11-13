"""WAVE 24 — tests for the projected Gauss-Seidel LCP contact solver."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import rigid_contact_pgs_lcp as r

_DOWN = np.array([0.0, -1.0])  # floor -> ball (screen +y is down)


def _floor() -> r.RigidBody:
    return r.RigidBody(mass=1.0, inertia=1.0, position=np.array([0.0, 1.0]), static=True)


def _ball(vx: float = 0.0, vy: float = 5.0) -> r.RigidBody:
    return r.RigidBody(
        mass=1.0, inertia=0.1, position=np.array([0.0, 0.0]), velocity=np.array([vx, vy])
    )


def _contact(*, mu: float = 0.5, restitution: float = 0.0, penetration: float = 0.01) -> r.Contact:
    return r.Contact(
        a=0,
        b=1,
        point=np.array([0.0, 0.0]),
        normal=_DOWN,
        r_a=np.zeros(2),
        r_b=np.zeros(2),
        penetration=penetration,
        mu=mu,
        restitution=restitution,
    )


def test_normal_impulse_stops_approach() -> None:
    floor, ball = _floor(), _ball()
    sol = r.solve_contacts_pgs([floor, ball], [_contact()], sweeps=50, baumgarte=0.0)
    # The normal impulse removes the closing velocity exactly (momentum = m*v).
    assert sol.normal_impulses[0] == pytest.approx(5.0, abs=1e-6)
    assert ball.velocity[1] == pytest.approx(0.0, abs=1e-6)
    assert sol.converged


def test_normal_impulse_is_nonnegative() -> None:
    floor, ball = _floor(), _ball()
    sol = r.solve_contacts_pgs([floor, ball], [_contact()], sweeps=50)
    assert np.all(sol.normal_impulses >= 0.0)


def test_restitution_bounce_exact() -> None:
    floor, ball = _floor(), _ball(vy=5.0)
    c = _contact(restitution=0.8, penetration=0.0, mu=0.0)
    r.solve_contacts_pgs([floor, ball], [c], sweeps=50, baumgarte=0.0)
    assert ball.velocity[1] == pytest.approx(-4.0, abs=1e-6)


def test_friction_reduces_tangential_speed() -> None:
    floor, ball = _floor(), _ball(vx=3.0, vy=5.0)
    sol = r.solve_contacts_pgs([floor, ball], [_contact(mu=0.5)], sweeps=100)
    assert abs(ball.velocity[0]) < 3.0  # friction slows the slide
    # Coulomb limit: |lambda_t| <= mu * lambda_n.
    assert abs(sol.tangent_impulses[0]) <= 0.5 * sol.normal_impulses[0] + 1e-6


def test_friction_cone_limit_exact() -> None:
    # Large tangential speed saturates friction at the cone boundary.
    floor, ball = _floor(), _ball(vx=100.0, vy=5.0)
    sol = r.solve_contacts_pgs([floor, ball], [_contact(mu=0.4)], sweeps=200)
    assert sol.tangent_impulses[0] == pytest.approx(-0.4 * sol.normal_impulses[0], rel=1e-3)


def test_complementarity_residual_is_zero_at_solution() -> None:
    floor, ball = _floor(), _ball()
    sol = r.solve_contacts_pgs([floor, ball], [_contact()], sweeps=50)
    assert sol.max_complementarity == pytest.approx(0.0, abs=1e-9)
    assert r.complementarity_residual([floor, ball], [_contact()], sol) == pytest.approx(
        0.0, abs=1e-9
    )


def test_baumgarte_pushes_apart() -> None:
    floor, ball = _floor(), _ball(vy=0.0)
    sol = r.solve_contacts_pgs(
        [floor, ball], [_contact(penetration=0.01)], sweeps=50, baumgarte=0.2, slop=0.001
    )
    # bias = 0.2*(0.01-0.001) = 0.0018; normal is (0,-1) so vn=+bias -> vy<0.
    assert ball.velocity[1] == pytest.approx(-0.0018, abs=1e-6)
    assert sol.residual < 1e-6


def test_static_body_is_immovable() -> None:
    floor, ball = _floor(), _ball()
    r.solve_contacts_pgs([floor, ball], [_contact()], sweeps=50)
    assert np.all(floor.velocity == 0.0)
    assert floor.angular_velocity == 0.0


def test_two_contacts_sharing_a_body() -> None:
    # A ball wedged between two static walls; both impulses must be >= 0.
    wall_l = r.RigidBody(mass=1.0, inertia=1.0, position=np.array([-1.0, 0.0]), static=True)
    wall_r = r.RigidBody(mass=1.0, inertia=1.0, position=np.array([1.0, 0.0]), static=True)
    ball = r.RigidBody(
        mass=1.0, inertia=0.1, position=np.array([0.0, 0.0]), velocity=np.array([0.0, 0.0])
    )
    c_l = r.Contact(
        a=0,
        b=2,
        point=np.array([-0.5, 0.0]),
        normal=np.array([1.0, 0.0]),
        r_a=np.zeros(2),
        r_b=np.zeros(2),
        penetration=0.0,
        mu=0.0,
    )
    c_r = r.Contact(
        a=1,
        b=2,
        point=np.array([0.5, 0.0]),
        normal=np.array([-1.0, 0.0]),
        r_a=np.zeros(2),
        r_b=np.zeros(2),
        penetration=0.0,
        mu=0.0,
    )
    sol = r.solve_contacts_pgs([wall_l, wall_r, ball], [c_l, c_r], sweeps=50)
    assert np.all(sol.normal_impulses >= 0.0)
    assert sol.converged


def test_solver_is_deterministic() -> None:
    floor, ball_a = _floor(), _ball(vx=1.0, vy=5.0)
    sol_a = r.solve_contacts_pgs([floor, ball_a], [_contact()], sweeps=100)
    floor2, ball_b = _floor(), _ball(vx=1.0, vy=5.0)
    sol_b = r.solve_contacts_pgs([floor2, ball_b], [_contact()], sweeps=100)
    np.testing.assert_array_equal(sol_a.normal_impulses, sol_b.normal_impulses)
    np.testing.assert_array_equal(ball_a.velocity, ball_b.velocity)
    assert sol_a.residual == sol_b.residual


def test_no_contacts_is_trivial() -> None:
    floor, ball = _floor(), _ball()
    sol = r.solve_contacts_pgs([floor, ball], [], sweeps=10)
    assert sol.sweeps >= 1
    assert sol.normal_impulses.shape == (0,)
    assert sol.converged


def test_angular_response_from_offset_contact() -> None:
    # A contact offset from the centre induces angular velocity.
    floor = _floor()
    box = r.RigidBody(
        mass=1.0, inertia=0.5, position=np.array([0.0, 0.0]), velocity=np.array([0.0, 5.0])
    )
    c = r.Contact(
        a=0,
        b=1,
        point=np.array([0.5, 0.0]),
        normal=_DOWN,
        r_a=np.zeros(2),
        r_b=np.array([0.5, 0.0]),
        penetration=0.0,
        mu=0.0,
    )
    r.solve_contacts_pgs([floor, box], [c], sweeps=50, baumgarte=0.0)
    assert abs(box.angular_velocity) > 0.0


def test_tangent_is_orthogonal() -> None:
    c = _contact()
    assert np.dot(c.tangent(), c.normal) == pytest.approx(0.0)
    assert np.linalg.norm(c.tangent()) == pytest.approx(1.0)


# --- adversarial / refusal --------------------------------------------------------


def test_refuse_no_bodies() -> None:
    with pytest.raises(r.ContactError):
        r.solve_contacts_pgs([], [])


def test_refuse_negative_mass() -> None:
    with pytest.raises(r.ContactError):
        r.RigidBody(mass=-1.0, inertia=1.0, position=np.zeros(2))


def test_refuse_zero_inertia_dynamic() -> None:
    with pytest.raises(r.ContactError):
        r.RigidBody(mass=1.0, inertia=0.0, position=np.zeros(2))


def test_refuse_bad_position_shape() -> None:
    with pytest.raises(r.ContactError):
        r.RigidBody(mass=1.0, inertia=1.0, position=np.zeros(3))


def test_refuse_nan_velocity() -> None:
    with pytest.raises(r.ContactError):
        r.RigidBody(mass=1.0, inertia=1.0, position=np.zeros(2), velocity=np.array([np.nan, 0.0]))


def test_refuse_zero_normal() -> None:
    with pytest.raises(r.ContactError):
        r.Contact(a=0, b=1, point=np.zeros(2), normal=np.zeros(2), r_a=np.zeros(2), r_b=np.zeros(2))


def test_refuse_negative_penetration() -> None:
    with pytest.raises(r.ContactError):
        r.Contact(
            a=0,
            b=1,
            point=np.zeros(2),
            normal=_DOWN,
            r_a=np.zeros(2),
            r_b=np.zeros(2),
            penetration=-1.0,
        )


def test_refuse_negative_mu() -> None:
    with pytest.raises(r.ContactError):
        r.Contact(
            a=0, b=1, point=np.zeros(2), normal=_DOWN, r_a=np.zeros(2), r_b=np.zeros(2), mu=-1.0
        )


def test_refuse_bad_restitution() -> None:
    with pytest.raises(r.ContactError):
        r.Contact(
            a=0,
            b=1,
            point=np.zeros(2),
            normal=_DOWN,
            r_a=np.zeros(2),
            r_b=np.zeros(2),
            restitution=1.5,
        )


def test_refuse_contact_bad_offset_shape() -> None:
    with pytest.raises(r.ContactError):
        r.Contact(a=0, b=1, point=np.zeros(2), normal=_DOWN, r_a=np.zeros(3), r_b=np.zeros(2))


def test_refuse_out_of_range_body_index() -> None:
    floor, ball = _floor(), _ball()
    bad = r.Contact(a=0, b=5, point=np.zeros(2), normal=_DOWN, r_a=np.zeros(2), r_b=np.zeros(2))
    with pytest.raises(r.ContactError):
        r.solve_contacts_pgs([floor, ball], [bad])


def test_refuse_self_contact() -> None:
    floor, ball = _floor(), _ball()
    bad = r.Contact(a=1, b=1, point=np.zeros(2), normal=_DOWN, r_a=np.zeros(2), r_b=np.zeros(2))
    with pytest.raises(r.ContactError):
        r.solve_contacts_pgs([floor, ball], [bad])


def test_refuse_bad_sweeps() -> None:
    floor, ball = _floor(), _ball()
    with pytest.raises(r.ContactError):
        r.solve_contacts_pgs([floor, ball], [_contact()], sweeps=0)
    with pytest.raises(r.ContactError):
        r.solve_contacts_pgs([floor, ball], [_contact()], sweeps=r.MAX_SWEEPS + 1)


def test_refuse_bad_tolerance() -> None:
    floor, ball = _floor(), _ball()
    with pytest.raises(r.ContactError):
        r.solve_contacts_pgs([floor, ball], [_contact()], tolerance=0.0)


def test_refuse_bad_baumgarte() -> None:
    floor, ball = _floor(), _ball()
    with pytest.raises(r.ContactError):
        r.solve_contacts_pgs([floor, ball], [_contact()], baumgarte=1.5)


def test_refuse_negative_slop() -> None:
    floor, ball = _floor(), _ball()
    with pytest.raises(r.ContactError):
        r.solve_contacts_pgs([floor, ball], [_contact()], slop=-1.0)


def test_refuse_too_many_bodies() -> None:
    bodies = [r.RigidBody(mass=1.0, inertia=1.0, position=np.zeros(2)) for _ in range(3)]
    with pytest.raises(r.ContactError):
        r.solve_contacts_pgs(bodies * (r.MAX_BODIES // 3 + 2), [])


def test_refuse_too_many_contacts() -> None:
    floor, ball = _floor(), _ball()
    c = _contact()
    with pytest.raises(r.ContactError):
        r.solve_contacts_pgs([floor, ball], [c] * (r.MAX_CONTACTS + 1))


def test_refuse_nonfinite_angle() -> None:
    with pytest.raises(r.ContactError):
        r.RigidBody(mass=1.0, inertia=1.0, position=np.zeros(2), angle=float("inf"))


def test_refuse_nonfinite_contact_point() -> None:
    with pytest.raises(r.ContactError):
        r.Contact(
            a=0, b=1, point=np.array([np.nan, 0.0]), normal=_DOWN, r_a=np.zeros(2), r_b=np.zeros(2)
        )


def test_zero_effective_mass_row_is_skipped() -> None:
    # Two static bodies -> zero effective mass; the row must be skipped safely.
    s1 = r.RigidBody(mass=1.0, inertia=1.0, position=np.zeros(2), static=True)
    s2 = r.RigidBody(mass=1.0, inertia=1.0, position=np.array([1.0, 0.0]), static=True)
    c = r.Contact(a=0, b=1, point=np.zeros(2), normal=_DOWN, r_a=np.zeros(2), r_b=np.zeros(2))
    sol = r.solve_contacts_pgs([s1, s2], [c], sweeps=5)
    assert sol.normal_impulses[0] == 0.0


def test_restitution_target_zero_when_separating() -> None:
    # A ball already moving away: no restitution target, no impulse.
    floor, ball = _floor(), _ball(vy=-2.0)
    c = _contact(restitution=0.5, penetration=0.0)
    sol = r.solve_contacts_pgs([floor, ball], [c], sweeps=10, baumgarte=0.0)
    assert sol.normal_impulses[0] == pytest.approx(0.0)
    assert ball.velocity[1] == pytest.approx(-2.0)


def test_zero_friction_cone_skips_tangent_row() -> None:
    floor, ball = _floor(), _ball(vx=3.0, vy=5.0)
    sol = r.solve_contacts_pgs([floor, ball], [_contact(mu=0.0)], sweeps=20)
    assert sol.tangent_impulses[0] == pytest.approx(0.0)
    assert ball.velocity[0] == pytest.approx(3.0)  # no friction -> unchanged
