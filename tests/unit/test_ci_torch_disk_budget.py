"""CI disk budget — the heavy torch/CUDA stack must never be installed (P1).

``pip install -e ".[dev]"`` resolves the full runtime closure. ``sentence-transformers``
(and ``llama-cpp-python``) pull ``torch``, and the *default* PyPI ``torch`` wheel ships
the CUDA stack: ~539 MB for torch plus ~1.8 GB of ``nvidia-*``/``triton`` wheels. On a
standard ``ubuntu-latest`` runner that exhausts the disk mid-install and fails with
``OSError: [Errno 28] No space left on device``.

That failure was observed on the REQUIRED ``test`` job of PR #307 — a flaky merge gate
that red-ed CI intermittently while the same SHA passed on a rerun. The fix points pip
at the official CPU wheel index (``torch==...+cpu``, no CUDA dependencies) for the whole
workflow; the same torch is installed either way, so no test can regress from it.

This module is a text scan on purpose (no YAML parser), matching
``test_ci_lint_parity.py``: the core-only install ships no YAML dependency.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CPU_TORCH_INDEX = "https://download.pytorch.org/whl/cpu"

# Every workflow that installs the full package (bare ``pip install -e``) pulls torch.
HEAVY_INSTALL_WORKFLOWS = ("ci.yml", "maintenance.yml", "langsmith-smoke.yml")


def test_pyproject_still_pulls_torch() -> None:
    """Guard the guard: the disk budget only matters while torch is a dependency."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert "sentence-transformers" in pyproject or "torch" in pyproject, (
        "torch no longer resolves from the dependency closure — this guard can be retired"
    )


def test_every_heavy_workflow_pins_the_cpu_torch_index() -> None:
    for name in HEAVY_INSTALL_WORKFLOWS:
        text = (WORKFLOWS / name).read_text()
        assert "pip install -e" in text, f"{name} no longer installs the package"
        assert f"PIP_EXTRA_INDEX_URL: {CPU_TORCH_INDEX}" in text, (
            f"{name} runs a bare `pip install -e` without the CPU torch index; "
            "the CUDA wheel set (~2.3 GB) can exhaust the runner disk (Errno 28)"
        )


def test_cpu_index_is_declared_at_workflow_scope() -> None:
    """The env must be workflow-scope (before `jobs:`), not one job's local env."""
    for name in HEAVY_INSTALL_WORKFLOWS:
        text = (WORKFLOWS / name).read_text()
        idx_pos = text.index(CPU_TORCH_INDEX)
        jobs_pos = text.index("\njobs:")
        assert idx_pos < jobs_pos, (
            f"{name}: PIP_EXTRA_INDEX_URL must be workflow-scoped so every job "
            "that installs torch gets the CPU wheel"
        )
