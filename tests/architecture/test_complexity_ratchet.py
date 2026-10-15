"""Law R22 — cyclomatic complexity is a ratchet (audit finding A-11).

The 2026-10-15 audit measured four execution-core functions far beyond any
maintainable bound (``InProcessJobQueue._process_job`` 63, ``_compile`` 39,
``_planned_session`` 36, ``get_job_facts`` 35 on radon's scale; 62/38/36/33
under the counter below, which reports nested scopes inside their parent).  Their
behaviour-preserving decomposition is a separate, characterisation-tested
change; what *this* guard does is make sure the debt can only shrink:

* every function in :data:`BASELINE` may not grow past the value recorded here;
* every other function in ``src/`` must stay at or below :data:`CEILING`;
* a baseline entry whose function has been simplified below the ceiling must be
  removed (a stale allowance is a silent re-opening of the debt).

Complexity is computed by :func:`cyclomatic` — McCabe as radon counts it
(decision points + 1): ``if``/``elif``/ternary, every loop, every ``except``,
every ``case``, each extra operand of a boolean expression, each ``for`` and
``if`` of a comprehension, and nested ``def``/``lambda`` bodies count towards
the enclosing function (a composition root full of nested handlers is one
unit of review).  No third-party tool is needed, so the guard runs wherever
the suite runs.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "nexus_ai_agent"

#: Nothing outside the baseline may exceed this (radon's "C" band ends at 20).
CEILING = 20

#: ``"<relative path>::<qualified name>": measured complexity`` on the audited
#: tree.  Lower a value when you simplify the function; never raise one.
BASELINE: dict[str, int] = {
    "bot/handlers.py::build_handlers": 205,
    "bot/feature_handlers.py::build_feature_command_handlers": 77,
    "adapters/in_process_job_queue.py::InProcessJobQueue._process_job": 62,
    "creative/rendering/compiler.py::_compile": 38,
    "creative/slideshow/service.py::_planned_session": 36,
    "adapters/in_process_job_queue.py::InProcessJobQueue.get_job_facts": 33,
    "bot/creative_surface.py::build_creative_handlers": 33,
    "jobs/creative_passport.py::reverify_stored_passport": 30,
    "provenance/passport.py::PassportBuilder._reconcile": 30,
    "jobs/verification.py::verify_artifact": 29,
    "creative/packs/verify.py::verify_manifest": 27,
    "creative/temporal/core.py::FrameRateResolver.resolve": 26,
    "cli.py::inspect_checkpoints": 24,
    "continuum/mutations.py::run_campaign": 23,
    "creative/studio/bus.py::CommandBus._dispatch_locked": 23,
    "adapters/whisper_local.py::turns_from_anchors": 22,
    "creative/packs/slideshow/planning.py::plan_slideshow": 22,
    "jobs/creative_passport.py::build_passport": 22,
    "creative/ffmpeg_executor.py::_execute_ffmpeg_commands_sync": 21,
    "features/calculator.py::SafeCalculator._eval": 21,
    "features/request_queue.py::GeminiRequestQueue.submit": 21,
    "nagar/cognition/router.py::DeterministicRouter.route": 21,
}


class _Counter(ast.NodeVisitor):
    def __init__(self) -> None:
        self.decisions = 0

    def visit_If(self, node: ast.If) -> None:
        self.decisions += 1
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.decisions += 1
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.decisions += 1
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.decisions += 1
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.decisions += 1
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.decisions += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.decisions += len(node.values) - 1
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.decisions += 1 + len(node.ifs)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.decisions += len(node.cases)
        self.generic_visit(node)


def cyclomatic(func: ast.AST) -> int:
    """McCabe complexity of one function body (nested scopes included)."""
    counter = _Counter()
    counter.visit(func)
    return counter.decisions + 1


def _functions(path: Path) -> list[tuple[str, int]]:
    """``(qualified name, complexity)`` for every function/method in *path*.

    Nested functions are *not* reported separately: their decisions already
    belong to the enclosing function, which is the unit a reviewer reads.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []

    def walk(nodes: list[ast.stmt], prefix: str) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((prefix + node.name, cyclomatic(node)))
            elif isinstance(node, ast.ClassDef):
                walk(node.body, f"{prefix}{node.name}.")

    walk(tree.body, "")
    return found


def measure(root: Path = SRC) -> dict[str, int]:
    out: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        for name, value in _functions(path):
            out[f"{rel}::{name}"] = value
    return out


def test_no_function_grows_past_its_baseline_or_the_ceiling() -> None:
    measured = measure()
    offenders = {
        key: value for key, value in measured.items() if value > BASELINE.get(key, CEILING)
    }
    assert offenders == {}, (
        "cyclomatic complexity grew (A-11 ratchet). Simplify the function or, "
        "for a *pre-existing* hotspot only, keep BASELINE honest — never raise it: "
        f"{offenders}"
    )


def test_the_baseline_is_not_stale() -> None:
    """Every entry must still name a real function that still needs it."""
    measured = measure()
    missing = sorted(key for key in BASELINE if key not in measured)
    assert missing == [], f"BASELINE names functions that no longer exist: {missing}"
    healed = {key: measured[key] for key in BASELINE if measured[key] <= CEILING}
    assert healed == {}, (
        "these functions are now within the ceiling — remove their BASELINE entry "
        f"so the allowance cannot be reused: {healed}"
    )
    loosened = {
        key: (measured[key], BASELINE[key]) for key in BASELINE if measured[key] < BASELINE[key]
    }
    assert loosened == {}, (
        "these functions got simpler — lower their BASELINE value to the measured "
        f"one (measured, baseline): {loosened}"
    )


def test_the_counter_matches_the_documented_rules() -> None:
    src = (
        "def f(a, b):\n"
        "    if a and b or not a:\n"  # if +1, two boolean operators +2
        "        for x in b:\n"  # +1
        "            pass\n"
        "    try:\n"
        "        pass\n"
        "    except KeyError:\n"  # +1
        "        pass\n"
        "    except ValueError:\n"  # +1
        "        pass\n"
        "    y = [v for v in a if v if v > 1]\n"  # +1 for, +2 ifs
        "    z = 1 if a else 2\n"  # +1
        "    while a:\n"  # +1
        "        break\n"
        "    def inner(q):\n"
        "        return q if q else None\n"  # nested ternary +1
        "    return y, z\n"
    )
    (func,) = ast.parse(src).body
    assert cyclomatic(func) == 1 + 1 + 2 + 1 + 1 + 1 + 3 + 1 + 1 + 1


def test_the_guard_is_not_vacuous() -> None:
    measured = measure()
    assert len(measured) > 1000, "the scan found suspiciously few functions"
    assert measured["adapters/in_process_job_queue.py::InProcessJobQueue._process_job"] > CEILING
