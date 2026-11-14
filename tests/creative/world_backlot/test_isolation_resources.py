from __future__ import annotations

import ast
import builtins
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

import pytest

from nexus_ai_agent.creative.world_backlot import RecipeQuality, resolve_director_scene_recipe

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "src/nexus_ai_agent/creative/world_backlot"
BENCHMARK = Path(__file__).with_name("benchmark_world_backlot.py")


def _environment(**overrides: str) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "OPENBLAS_NUM_THREADS": "1",
        "COVERAGE_FILE": "/tmp/.coverage_world_backlot",
        **overrides,
    }


def test_package_is_an_isolated_numerical_library_with_no_cross_package_dependencies() -> None:
    files = list(PACKAGE.glob("*.py"))
    assert len(files) >= 5
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name.split(".")[0] in sys.stdlib_module_names | {"numpy"}
                    for alias in node.names
                ), path
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    assert node.level == 1, path
                else:
                    assert node.module and node.module.split(".")[0] in sys.stdlib_module_names | {
                        "numpy"
                    }, path


def test_resolving_scenes_does_no_filesystem_or_network_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("world backlot attempted I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    recipe = resolve_director_scene_recipe(
        "midnight Iran dust wind", seed="offline", quality=RecipeQuality(32, 32, 3, 0.01)
    )
    assert recipe.verify()


def test_recipe_hashes_match_across_processes_python_hash_seeds_and_local_timezones() -> None:
    script = """
import json
from nexus_ai_agent.creative.world_backlot import RecipeQuality, resolve_director_scene_recipe
r = resolve_director_scene_recipe('midnight Iran dust wind', seed='cross-process',
                                  quality=RecipeQuality(64, 64, 3, .02))
print(json.dumps([r.sha256, r.layout.seal.sha256, r.ambient_audio.sha256,
                  r.starfield.sha256, r.particle_fields[0].sha256, r.cube.sha256]))
"""
    outputs = []
    for hash_seed, timezone in (("1", "UTC"), ("98765", "Pacific/Honolulu")):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=_environment(PYTHONHASHSEED=hash_seed, TZ=timezone),
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        outputs.append(json.loads(completed.stdout))
    assert outputs[0] == outputs[1]
    assert all(len(h) == 64 for h in outputs[0])


@pytest.mark.parametrize("maximum", [False, True])
def test_measured_latency_and_process_peak_rss_stay_within_documented_budgets(
    maximum: bool,
) -> None:
    command = [sys.executable, str(BENCHMARK), "--runs", "1" if maximum else "7"]
    if maximum:
        command.append("--maximum")
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        check=True,
        timeout=90,
    )
    result = json.loads(completed.stdout)
    # RSS includes the interpreter and NumPy, not merely tracemalloc's Python heap.
    assert result["peak_rss_mib"] < 100, result
    assert result["stars"] == (32768 if maximum else 2048)
    if not maximum:
        assert result["median_resolution_ms"] < 50, result
