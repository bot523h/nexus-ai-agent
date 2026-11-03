"""CI supply-chain budget — CPU torch only, and only from its own index (P1).

``pip install -e ".[dev]"`` resolves the full runtime closure. ``sentence-transformers``
(and ``llama-cpp-python``) pull ``torch``, and the *default* PyPI ``torch`` wheel ships the
CUDA stack (~539 MB torch plus ~1.8 GB of ``nvidia-*``/``triton`` wheels). On a standard
runner that exhausts the disk mid-install and fails with ``OSError: [Errno 28]`` — observed
on the REQUIRED ``test`` job of PR #307 (green on a rerun of the same SHA).

The first fix set ``PIP_EXTRA_INDEX_URL`` at workflow scope. That is only *smaller*, not
*safe*: pip consults an extra index for **every** distribution, so any package present on
both PyPI and the extra index can be served from the extra one (a dependency-confusion
surface). The corrected design is package-scoped:

* ``scripts/ci_install.sh`` installs ``torch`` from the PyTorch CPU index **exclusively**,
  then installs the editable project from **PyPI only** under
  ``constraints/ci-cpu-torch.txt`` (``torch==<x.y.z>+cpu``), and finally fails closed if
  any ``nvidia-*``/``triton`` package is present or ``torch`` is not the ``+cpu`` build.

These tests scan the workflows as text (no YAML dependency, matching
``test_ci_lint_parity.py``) and pin those invariants so the budget cannot silently regress.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPT = REPO_ROOT / "scripts" / "ci_install.sh"
CONSTRAINTS = REPO_ROOT / "constraints" / "ci-cpu-torch.txt"
CPU_INDEX = "https://download.pytorch.org/whl/cpu"
PYPI_INDEX = "https://pypi.org/simple"

# Every workflow that installs the project (bare ``pip install -e``) pulls torch.
HEAVY_INSTALL_WORKFLOWS = ("ci.yml", "maintenance.yml", "langsmith-smoke.yml")


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_constraint_pins_a_cpu_torch_build() -> None:
    assert CONSTRAINTS.is_file(), "constraints/ci-cpu-torch.txt is missing"
    text = CONSTRAINTS.read_text(encoding="utf-8")
    pins = re.findall(r"^torch==\d+\.\d+\.\d+\+cpu\s*$", text, re.MULTILINE)
    assert pins, "the constraint must pin an exact CPU build: torch==<x.y.z>+cpu"


def test_no_global_extra_index_anywhere() -> None:
    """An extra index is consulted for every package — reject it outright."""
    for name in HEAVY_INSTALL_WORKFLOWS:
        text = _workflow(name)
        assert "PIP_EXTRA_INDEX_URL" not in text, (
            f"{name} still sets PIP_EXTRA_INDEX_URL; that applies the CPU index to every "
            "dependency (dependency-confusion surface). Use scripts/ci_install.sh instead."
        )
        assert "extra-index-url" not in text, f"{name} passes --extra-index-url to pip"


def test_every_heavy_workflow_installs_via_the_shared_script() -> None:
    for name in HEAVY_INSTALL_WORKFLOWS:
        text = _workflow(name)
        assert "scripts/ci_install.sh" in text, f"{name} does not use scripts/ci_install.sh"
        # No bare editable installs left behind.
        assert not re.search(r"^\s*(?:-\s*run:\s*)?pip install -e\b", text, re.MULTILINE), (
            f"{name} still runs a bare `pip install -e`; route it through ci_install.sh"
        )


def test_script_isolates_torch_to_the_cpu_index() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert CPU_INDEX in script, "ci_install.sh must use the PyTorch CPU index for torch"
    assert PYPI_INDEX in script, "ci_install.sh must resolve the remainder from PyPI only"
    # The editable install must be PyPI-only (its own --index-url), not the CPU index.
    pypi_block = script[script.index(PYPI_INDEX) :]
    assert "-e " in pypi_block or "-e\t" in pypi_block, (
        "the PyPI-only step must perform the editable install"
    )
    # The constraint must be applied in both install steps.
    assert script.count("-c ") >= 2, "both install steps must apply the CPU-torch constraint"
    # Fail-closed: the script must verify the CPU build survived and no CUDA stack exists.
    assert "+cpu" in script, "ci_install.sh must assert the +cpu build"
    assert "nvidia-" in script and "triton" in script, (
        "ci_install.sh must reject any nvidia-*/triton package"
    )


def test_pyproject_still_pulls_torch() -> None:
    """Guard the guard: the budget only matters while torch is in the closure."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "sentence-transformers" in pyproject or "torch" in pyproject, (
        "torch no longer resolves from the dependency closure — this guard can be retired"
    )
