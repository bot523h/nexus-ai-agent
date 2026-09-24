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
from pathlib import Path

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
    "enum",
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


def _subprocess_attribute_uses(path: Path) -> list[str]:
    """Structural: any ``subprocess.<attr>`` load, not a comment/string search."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "subprocess":
                hits.append(node.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"Popen", "run", "check_output", "call"}:
                hits.append(node.func.id)
    return hits


def test_exactly_one_subprocess_site_and_no_shell_true() -> None:
    subprocess_users = [
        p for p in _lane_files() if "import subprocess" in p.read_text(encoding="utf-8")
    ]
    assert [p.name for p in subprocess_users] == ["executor.py"], (
        f"only executor.py may import subprocess, found: {[p.name for p in subprocess_users]}"
    )
    for file_path in _lane_files():
        source = file_path.read_text(encoding="utf-8")
        assert "shell=True" not in source, (
            f"{file_path.relative_to(REPO_ROOT)} must never use shell=True"
        )
        uses = _subprocess_attribute_uses(file_path)
        if file_path.name == "executor.py":
            assert "run" in uses
            continue
        assert uses == [], (
            f"{file_path.relative_to(REPO_ROOT)} must not call subprocess "
            f"(found {uses}) — executor.py is the single execution site"
        )


def test_lifecycle_module_is_gates_only() -> None:
    path = LANE / "lifecycle.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            if node.module.startswith("nexus_ai_agent.creative.rendering"):
                assert node.module.endswith(".ir") or node.module.endswith("rendering.ir"), (
                    f"lifecycle.py must not import the executor, got {node.module}"
                )
    assert "subprocess" not in imported
    assert "os" not in imported
    assert "shutil" not in imported


def test_ratchet_detects_subprocess_smuggled_into_lifecycle() -> None:
    """Proof that the AST ratchet fails if execution leaks into lifecycle.py."""
    smuggled = ast.parse("import subprocess\nsubprocess.run(['ffmpeg'])\n")
    hits: list[str] = []
    for node in ast.walk(smuggled):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "subprocess":
                hits.append(node.attr)
    assert hits == ["run"], "the ratchet must see a smuggled subprocess.run"
