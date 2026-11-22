"""PDE conservation, colorimetry and cybernetic control witnesses."""

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas.aces_hald_film_optic import (
    aces_tone_map,
    verify_aces_colorimetric_invariants,
)
from nexus_ai_agent.creative.photoreal_intrinsic.screened_poisson_skin_transport import (
    solve_screened_poisson_neumann,
    verify_screened_poisson_conservation,
)
from nexus_ai_agent.selfhood.vitals import HomeostaticPIDConfig, HomeostaticPIDController


@pytest.mark.parametrize("coefficient", [0.0, 0.01, 1.0, 100.0])
def test_poisson_dc_and_dirichlet(coefficient):
    field = np.random.default_rng(3).normal(size=(13, 17)) + 5
    result = verify_screened_poisson_conservation(field, coefficient)
    assert result["verified"]
    assert result["dc_error"] < 1e-10
    if coefficient:
        assert result["solution_energy"] < result["source_energy"]
    else:
        np.testing.assert_allclose(solve_screened_poisson_neumann(field, 0.0), field, atol=1e-12)


@pytest.mark.parametrize("bad", [-1.0, True, np.nan, np.inf, -np.inf])
def test_poisson_rejects_helmholtz_and_nonfinite(bad):
    with pytest.raises((ValueError, TypeError)):
        solve_screened_poisson_neumann(np.ones((4, 4)), bad)


def test_aces_colorimetric_invariants():
    assert verify_aces_colorimetric_invariants()["verified"]


@pytest.mark.parametrize("bad", [True, np.nan, np.inf])
def test_aces_scalar_and_array_guards(bad):
    with pytest.raises((ValueError, TypeError)):
        aces_tone_map(np.ones(3), a=bad)
    with pytest.raises((ValueError, TypeError)):
        aces_tone_map(np.array([bad]))


def test_pid_formula_and_antiwindup():
    c = HomeostaticPIDConfig(
        kp=2.0,
        ki=3.0,
        kd=0.4,
        setpoint=1.0,
        integral_limit=0.2,
        tau_d=0.3,
        output_min=-100.0,
        output_max=100.0,
    )
    controller = HomeostaticPIDController(c)
    first = controller.step(0.0, 0.1)
    assert first.state.integral == pytest.approx(0.05)
    assert first.state.derivative == pytest.approx(2.5)
    assert first.output == pytest.approx(3.15)
    for _ in range(100):
        result = controller.step(-100.0, 0.1)
    assert result.state.integral == 0.2
    assert result.output == 100.0


def test_pid_closed_loop_energy_decreases_to_zero():
    controller = HomeostaticPIDController(HomeostaticPIDConfig(kp=2.0))
    position = 1.0
    energy = []
    for _ in range(300):
        result = controller.step(position, 0.02)
        energy.append(result.state.lyapunov)
        position += 0.02 * (-position + result.output)
    assert np.all(np.diff(energy) < 0)
    assert energy[-1] < 1e-12
    assert result.is_lyapunov_stable


@pytest.mark.parametrize("bad", [0.0, -1.0, True, np.nan, np.inf])
def test_pid_dt_guards(bad):
    controller = HomeostaticPIDController()
    before = controller.state
    with pytest.raises((ValueError, TypeError)):
        controller.step(1.0, bad)
    assert controller.state == before


@pytest.mark.parametrize("field", ["kp", "ki", "kd", "integral_limit"])
def test_pid_config_guards(field):
    with pytest.raises(ValueError):
        HomeostaticPIDConfig(**{field: -1.0})


def test_pid_negative_energy_and_poisson_bad_shape():
    from nexus_ai_agent.selfhood.vitals import HomeostaticPIDState

    with pytest.raises(ValueError, match="nonnegative"):
        HomeostaticPIDState(lyapunov=-1.0)
    with pytest.raises(ValueError, match="2-D"):
        solve_screened_poisson_neumann(np.ones(3), 1.0)
