"""Wave 8 architecture gates: the apply lane stays lean and single-process.

1. **zero heavy dependencies** — no ``torch``/``cv2``/``moviepy``/ML in the lane;
2. **one process site** — only ``executor.py`` may import ``subprocess``, and no
   file may pass ``shell=True`` anywhere;
3. **bounded imports** — lane files may only use stdlib + pydantic + the shared
   Wave 2c binary-resolution helpers + studio/pack contracts (never ``bot``,
   ``api``, ``features``, or other agents' zones).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
LANE = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "rendering"

FORBIDDEN_HEAVY_MODULES = {
    "cv2",
    "ffmpeg",
    "moviepy",
    "onnxruntime",
    "scipy",
    "torch",
    "torchaudio",
    "torchvision",
}

ALLOWED_TOP_LEVEL = {
    "__future__",
    "collections",
    "dataclasses",
    "hashlib",
    "json",
    "pathlib",
    "re",
    "subprocess",
    # bench.py is a pure stdlib timing harness (p50/p95 of compile_lane);
    # it never participates in the encode path.  Same carve-out pattern as
    # the delivery-pack gate for signing.py (wave-4).
    "time",
    "typing",
    "pydantic",
    "nexus_ai_agent",
}

ALLOWED_NEXUS_PREFIXES = (
    "nexus_ai_agent.creative.rendering",
    "nexus_ai_agent.creative.slideshow.ffmpeg",
    "nexus_ai_agent.creative.studio",
    "nexus_ai_agent.creative.packs",
)


def _lane_files() -> list[Path]:
    files = sorted(LANE.glob("*.py"))
    assert files, "expected rendering lane files to exist"
    return files


def _imports(path: Path) -> tuple[set[str], list[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    top: set[str] = set()
    nexus: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top.add(alias.name.split(".")[0])
                if alias.name.startswith("nexus_ai"):
                    nexus.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top.add(node.module.split(".")[0])
            if node.module.startswith("nexus_ai"):
                nexus.append(node.module)
    return top, nexus


def test_no_heavy_imports_in_rendering_lane() -> None:
    violations: list[str] = []
    for file_path in _lane_files():
        top, _ = _imports(file_path)
        hit = top & FORBIDDEN_HEAVY_MODULES
        if hit:
            violations.append(f"{file_path.relative_to(REPO_ROOT)} imports: {sorted(hit)}")
    assert not violations, "heavy imports found in the apply lane:\n" + "\n".join(violations)


def test_lane_imports_stay_on_the_allowlist() -> None:
    for file_path in _lane_files():
        top, _ = _imports(file_path)
        assert top <= ALLOWED_TOP_LEVEL, (
            f"{file_path.relative_to(REPO_ROOT)} imports outside allowlist: "
            f"{sorted(top - ALLOWED_TOP_LEVEL)}"
        )


def test_lane_does_not_cross_into_other_zones() -> None:
    for file_path in _lane_files():
        _, nexus = _imports(file_path)
        for module in nexus:
            assert module.startswith(ALLOWED_NEXUS_PREFIXES), (
                f"{file_path.relative_to(REPO_ROOT)} crosses boundary via {module!r}"
            )


def _imports_subprocess(path: Path) -> bool:
    """True when *path* imports ``subprocess`` in any form.

    A raw ``"import subprocess" in text`` scan misses ``from subprocess import run``
    (and ``import subprocess as sp``), so a rogue lane file could spawn a process
    while the boundary test stayed green.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "subprocess" or a.name.startswith("subprocess.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "subprocess" or module.startswith("subprocess."):
                return True
    return False


def _uses_shell_true(path: Path) -> bool:
    """True when *path* calls anything with a truthy ``shell=`` keyword.

    Catches ``shell=True`` regardless of whitespace (``shell = True``) and any
    ``subprocess`` alias, because it inspects call keywords rather than text.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant):
                if bool(keyword.value.value):
                    return True
    return False


def test_exactly_one_subprocess_site_and_no_shell_true() -> None:
    subprocess_users = [p.name for p in _lane_files() if _imports_subprocess(p)]
    assert subprocess_users == ["executor.py"], (
        f"only executor.py may import subprocess, found: {subprocess_users}"
    )
    shell_users = [p.relative_to(REPO_ROOT) for p in _lane_files() if _uses_shell_true(p)]
    assert not shell_users, f"lane files must never use shell=True: {shell_users}"


def test_subprocess_detector_catches_from_and_aliased_imports(tmp_path: Path) -> None:
    plain = tmp_path / "plain.py"
    plain.write_text("import subprocess\n", encoding="utf-8")
    from_import = tmp_path / "from_import.py"
    from_import.write_text("from subprocess import run\n", encoding="utf-8")
    aliased = tmp_path / "aliased.py"
    aliased.write_text("import subprocess as sp\n", encoding="utf-8")
    clean = tmp_path / "clean.py"
    clean.write_text("import json\n", encoding="utf-8")
    assert _imports_subprocess(plain)
    assert _imports_subprocess(from_import)
    assert _imports_subprocess(aliased)
    assert not _imports_subprocess(clean)


def test_shell_true_detector_is_whitespace_insensitive(tmp_path: Path) -> None:
    spaced = tmp_path / "spaced.py"
    spaced.write_text("import subprocess\nsubprocess.run(cmd, shell = True)\n", encoding="utf-8")
    compact = tmp_path / "compact.py"
    compact.write_text("import subprocess\nsubprocess.run(cmd, shell=True)\n", encoding="utf-8")
    falsey = tmp_path / "falsey.py"
    falsey.write_text("import subprocess\nsubprocess.run(cmd, shell=False)\n", encoding="utf-8")
    assert _uses_shell_true(spaced)
    assert _uses_shell_true(compact)
    assert not _uses_shell_true(falsey)


def test_rogue_lane_file_fails_the_boundary(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: a lane file that spawns a process without the literal
    ``import subprocess`` and uses ``shell = True`` must fail the boundary test."""
    (tmp_path / "executor.py").write_text("import subprocess\n", encoding="utf-8")
    (tmp_path / "rogue.py").write_text(
        "from subprocess import run\ndef go(cmd):\n    return run(cmd, shell = True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "_lane_files", lambda: sorted(tmp_path.glob("*.py")))
    with pytest.raises(AssertionError):
        test_exactly_one_subprocess_site_and_no_shell_true()
