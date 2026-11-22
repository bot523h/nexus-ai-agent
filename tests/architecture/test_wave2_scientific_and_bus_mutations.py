"""Twenty isolated AST mutants. No extension module is unloaded or reimported.

Each mutant must fail the same behavioral witness that passes on live code.
Only the selected Python function/method is compiled in a copied namespace;
monkeypatch restores the method after each test.
"""

import ast
import copy
import hashlib
import inspect
import runpy
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import aces_hald_film_optic as aces
from nexus_ai_agent.creative.audio_engine import cinema_kernels as kernels
from nexus_ai_agent.creative.audio_engine import forensic_qc as qc
from nexus_ai_agent.creative.audio_engine import master_cinema_pipeline as pipeline
from nexus_ai_agent.creative.audio_engine import spine_lineage as lineage
from nexus_ai_agent.creative.audio_engine import stereo_hpss as hpss
from nexus_ai_agent.creative.photoreal_intrinsic import screened_poisson_skin_transport as poisson
from nexus_ai_agent.creative.storyboard_compiler import cinematography_solver as cinema
from nexus_ai_agent.creative.studio import bus
from nexus_ai_agent.selfhood import vitals

ROOT = Path(__file__).resolve().parents[2]


class Mutate(ast.NodeTransformer):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.changed = 0

    def visit(self, node: Any) -> Any:
        result = self.change(node)
        if result is not None:
            self.changed += 1
            return ast.copy_location(result, node)
        return super().visit(node)

    def change(self, node: Any) -> Any:
        result: Any
        k = self.kind
        text = ast.unparse(node)
        if k == "M01" and isinstance(node, ast.Call) and text == "reversed(succeeded)":
            return ast.parse("iter(succeeded)", mode="eval").body
        if k == "M02" and isinstance(node, ast.Break):
            return ast.Pass()
        if k in ("M03", "M05", "M06", "M07", "M11", "M15", "M16", "M20"):
            needles = {
                "M03": "rollback_errors",
                "M05": "bus.state_hash",
                "M06": "ortho_err > tol",
                "M07": "cos_theta < -0.99",
                "M11": "volume_m3 <= 0",
                "M15": "report.status",
                "M16": "screening_lambda < 0",
                "M20": "dt <= 0",
            }
            if isinstance(node, ast.If) and needles[k] in ast.unparse(node.test):
                result = copy.deepcopy(node)
                result.test = ast.Constant(False)
                return result
        if k == "M04" and isinstance(node, ast.Expr) and text.startswith("setattr(self._bus"):
            return ast.Pass()
        if (
            k == "M08"
            and isinstance(node, ast.Assign)
            and text.startswith("coefficient = 1.0 / theta ** 2")
        ):
            result = copy.deepcopy(node)
            result.value = ast.Constant(0.0)
            return result
        if k in ("M09", "M17") and isinstance(node, ast.FunctionDef):
            result = copy.deepcopy(node)
            result.body = (
                [
                    ast.Return(
                        ast.Call(ast.Name("float", ast.Load()), [ast.Name("value", ast.Load())], [])
                    )
                ]
                if k == "M09"
                else [ast.Pass()]
            )
            return result
        if k == "M10" and isinstance(node, ast.Constant) and node.value == 10.0:
            return ast.Constant(9.0)
        if k == "M12" and isinstance(node, ast.Assign) and text.startswith("mask_percussive ="):
            result = copy.deepcopy(node)
            result.value = ast.parse("np.zeros_like(mask_harmonic)", mode="eval").body
            return result
        if k == "M13" and isinstance(node, ast.Tuple) and text == "('audio_qc_nonfinite',)":
            return ast.Tuple([], ast.Load())
        if k == "M14" and isinstance(node, ast.Name) and node.id == "_LEAF_PREFIX":
            return ast.Constant(b"\x01")
        if k == "M18" and isinstance(node, ast.Assign) and text.startswith("integral ="):
            result = copy.deepcopy(node)
            result.value = ast.parse(
                "previous.integral + .5 * dt * (error + previous.error)", mode="eval"
            ).body
            return result
        if k == "M19" and isinstance(node, ast.BinOp) and text == "c.kp * error + c.ki * integral":
            result = copy.deepcopy(node)
            result.op = ast.Sub()
            return result
        return None


def _bus_witness(kind: str) -> Any:
    helpers = runpy.run_path(str(ROOT / "tests/unit/test_plan_transaction.py"))
    if kind == "M04":
        helpers["test_failed_plan_restores_bus_snapshot"]()
        return
    if kind == "M05":
        helpers[
            "test_rollback_committed_plan_detects_aba_revision_drift_after_intervening_do_and_undo"
        ]()
        return
    order: list[tuple[str, int]] = []
    sink = SimpleNamespace(append=order.append)

    def step(index: int, fail: bool = False, undo_fail: bool = False) -> Any:
        def execute() -> Any:
            sink.append(("execute", index))
            if fail:
                raise ValueError("forward")
            return index

        def compensate(_: Any) -> Any:
            sink.append(("undo", index))
            if undo_fail:
                raise ValueError("undo")

        return bus.PlanStep(str(index), execute=execute, compensate=compensate)

    plan = bus.PlanTransaction(
        steps=(step(0), step(1, undo_fail=kind == "M03"), step(2, fail=True), step(3))
    )
    with pytest.raises(bus.PlanTransactionError):
        plan.commit()
    assert order[:3] == [("execute", 0), ("execute", 1), ("execute", 2)]
    assert ("execute", 3) not in order
    assert order[3:] == [("undo", 1), ("undo", 0)]
    expected = (
        bus.PlanTransactionState.ROLLBACK_FAILED
        if kind == "M03"
        else bus.PlanTransactionState.ROLLED_BACK
    )
    assert plan.state is expected


def witness(kind: str) -> Any:
    if kind in ("M01", "M02", "M03", "M04", "M05"):
        return _bus_witness(kind)
    if kind == "M06":
        with pytest.raises(ValueError):
            cinema.so3_log(np.diag([2.0, 1.0, 0.5]))
    elif kind == "M07":
        r = np.diag([1.0, -1.0, -1.0])
        np.testing.assert_allclose(cinema.so3_exp(cinema.so3_log(r)), r, atol=1e-6)
    elif kind == "M08":
        t = np.eye(4)
        t[:3, :3] = np.diag([1.0, -1.0, -1.0])
        t[:3, 3] = [2.0, 3.0, 4.0]
        np.testing.assert_allclose(cinema.se3_exp(cinema.se3_log(t)), t, atol=1e-6)
    elif kind == "M09":
        with pytest.raises(TypeError):
            cinema._require_finite_scalar(True, name="test")
    elif kind == "M10":
        assert cinema.verify_minimum_jerk_calculus_invariants()["verified"]
    elif kind == "M11":
        with pytest.raises(kernels.KernelError, match="absorption"):
            kernels.eyring_t60(100.0, 100.0, 1.0)
    elif kind == "M12":
        a, b = hpss.hpss_masks(np.zeros((5, 5)))
        np.testing.assert_array_equal(a + b, np.ones((5, 5)))
    elif kind == "M13":
        assert (
            qc.analyze_forensic_audio_qc(np.array([np.nan]), sample_rate=48000).status == "VIOLATED"
        )
    elif kind == "M14":
        assert lineage.merkle_leaf_hash(b"evidence") == hashlib.sha256(b"\x00evidence").digest()
    elif kind == "M15":
        helpers = runpy.run_path(
            str(ROOT / "tests/creative/audio_engine/test_master_cinema_pipeline_and_guards.py")
        )
        with pytest.MonkeyPatch.context() as patch:
            helpers["test_pipeline_qc_fail_closed"](patch)
    elif kind == "M16":
        with pytest.raises(ValueError):
            poisson.solve_screened_poisson_neumann(np.ones((3, 3)), -0.01)
    elif kind == "M17":
        with pytest.raises(TypeError):
            aces.aces_tone_map(np.ones(3), a=True)
    elif kind in ("M18", "M19"):
        c = vitals.HomeostaticPIDController(
            vitals.HomeostaticPIDConfig(kp=1.0, ki=2.0, integral_limit=0.1, setpoint=1.0)
        )
        result = c.step(0.0, 1.0)
        assert result.state.integral == pytest.approx(0.1)
        assert result.output == pytest.approx(1.2)
    elif kind == "M20":
        with pytest.raises(ValueError):
            vitals.HomeostaticPIDController().step(1.0, -0.01)


TARGETS = [
    (bus.PlanTransaction, "_run_compensations"),
    (bus.PlanTransaction, "_commit_steps"),
    (bus.PlanTransaction, "_commit_steps"),
    (bus.PlanTransaction, "_restore_snapshot"),
    (bus.PlanTransaction, "rollback_committed_plan"),
    (cinema, "_validate_so3"),
    (cinema, "so3_log"),
    (cinema, "se3_log"),
    (cinema, "_require_finite_scalar"),
    (cinema, "minimum_jerk_scalar"),
    (kernels, "eyring_t60"),
    (hpss, "hpss_masks"),
    (qc, "analyze_forensic_audio_qc"),
    (lineage, "merkle_leaf_hash"),
    (pipeline.MasterCinemaAudioPipeline, "run"),
    (poisson, "solve_screened_poisson_neumann"),
    (aces, "_require_finite_scalar"),
    (vitals.HomeostaticPIDController, "step"),
    (vitals.HomeostaticPIDController, "step"),
    (vitals.HomeostaticPIDController, "step"),
]


@pytest.mark.parametrize("index", range(20), ids=[f"M{i:02d}_KILLED" for i in range(1, 21)])
def test_wave2_ast_mutant_is_killed(index: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # These five targets are class objects captured at collection. A previous
    # module reload/replacement can leave that class disconnected from the
    # facade classes imported by the behavioral witnesses. A fresh interpreter
    # binds the class, helpers and all direct-import consumers consistently;
    # no extension module is ever removed from sys.modules.
    if index < 5:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), str(index)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"M{index + 1:02d} KILLED" in result.stdout
        return
    _assert_mutant_killed(index, monkeypatch)


def _assert_mutant_killed(index: int, monkeypatch: pytest.MonkeyPatch) -> Any:
    kind = f"M{index + 1:02d}"
    owner, name = TARGETS[index]
    original = getattr(owner, name)
    witness(kind)
    tree = ast.parse(textwrap.dedent(inspect.getsource(original)))
    transformer = Mutate(kind)
    tree = transformer.visit(tree)
    assert transformer.changed > 0, f"{kind}: mutation did not apply"
    ast.fix_missing_locations(tree)
    namespace = dict(original.__globals__)
    exec(compile(tree, f"<wave2-{kind}>", "exec"), namespace)
    monkeypatch.setattr(owner, name, namespace[name])
    try:
        witness(kind)
    except (AssertionError, ValueError, TypeError, ZeroDivisionError, pytest.fail.Exception):
        return
    pytest.fail(f"{kind} SURVIVED")


@pytest.mark.parametrize("index", range(5), ids=[f"M{i:02d}_STALE_ALIAS" for i in range(1, 6)])
def test_bus_mutations_ignore_preexisting_cached_class_methods(
    index: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce an anchorless cached class without changing any import table.

    The old in-process harness would inspect this replacement and report
    'mutation did not apply'. The child must test disk code and its witnesses,
    not this parent's cached class or direct-import aliases.
    """
    owner, name = TARGETS[index]
    monkeypatch.setattr(owner, name, lambda *args, **kwargs: None)
    test_wave2_ast_mutant_is_killed(index, monkeypatch)


if __name__ == "__main__":
    selected = int(sys.argv[1])
    with pytest.MonkeyPatch.context() as patch:
        _assert_mutant_killed(selected, patch)
    print(f"M{selected + 1:02d} KILLED")
