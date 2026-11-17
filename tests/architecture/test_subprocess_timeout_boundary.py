"""Every ``git`` / ``ffmpeg`` spawn in the media and evidence spines is bounded.

A hung child process is an availability defect: it holds a fenced render attempt
(``morpheus`` mux) or a Continuum evidence producer (``git``) open forever, so a
worker can never make progress and the fence can never be released.  This rail
is the static half of that contract: it re-parses the modules that spawn
subprocesses and refuses an unbounded ``subprocess.run`` call.

The runtime half lives with the modules under test
(``tests/creative/morpheus`` exercises the mux timeout and the Continuum gate
tests exercise the git bound).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]

#: Modules pinned by this rail, with the budget their spawns must respect.
BOUNDED_SPAWN_MODULES = (
    "src/nexus_ai_agent/continuum/gate.py",
    "src/nexus_ai_agent/continuum/provenance.py",
    "src/nexus_ai_agent/creative/morpheus/morpheus_real_media_spine.py",
)


def _spawn_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
            and func.attr in {"run", "Popen", "call", "check_output", "check_call"}
        ):
            calls.append(node)
    return calls


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


@pytest.mark.parametrize("relative", BOUNDED_SPAWN_MODULES)
def test_spawns_are_all_timeout_bounded(relative: str) -> None:
    path = ROOT / relative
    calls = _spawn_calls(path)
    assert calls, f"{relative}: expected at least one subprocess spawn to pin"
    for call in calls:
        timeout = _keyword(call, "timeout")
        assert timeout is not None, (
            f"{relative}:{call.lineno}: subprocess spawn without a bounded timeout"
        )
        assert not (isinstance(timeout, ast.Constant) and timeout.value is None), (
            f"{relative}:{call.lineno}: timeout=None is not a bound"
        )


@pytest.mark.parametrize("relative", BOUNDED_SPAWN_MODULES)
def test_spawns_never_use_shell_true(relative: str) -> None:
    path = ROOT / relative
    for call in _spawn_calls(path):
        shell = _keyword(call, "shell")
        assert shell is None or (isinstance(shell, ast.Constant) and shell.value is False), (
            f"{relative}:{call.lineno}: shell must be absent or False"
        )


def test_morpheus_mux_exposes_a_configurable_budget() -> None:
    """The mux budget is a parameter (default 120 s), not a magic constant."""
    from nexus_ai_agent.creative.morpheus import morpheus_real_media_spine as spine

    signature = spine.mux_mp4_ffmpeg.__code__
    assert "timeout" in signature.co_varnames, "mux_mp4_ffmpeg must accept a timeout"
    default = spine.mux_mp4_ffmpeg.__defaults__ or ()
    assert 120.0 in default, "the default mux budget must be 120 s"
