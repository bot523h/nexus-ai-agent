"""CI supply-chain budget — CPU torch only, only from its own index, for every
supported Python (P1).

``pip install -e ".[dev]"`` resolves the full runtime closure. ``sentence-transformers``
(and ``llama-cpp-python``) pull ``torch``, and the *default* PyPI ``torch`` wheel ships the
CUDA stack (~539 MB torch plus ~1.8 GB of ``nvidia-*``/``triton`` wheels). On a standard
runner that exhausts the disk mid-install and fails with ``OSError: [Errno 28]`` — observed
on the REQUIRED ``test`` job of PR #307 (green on a rerun of the same SHA).

The first fix set ``PIP_EXTRA_INDEX_URL`` at workflow scope. That is only *smaller*, not
*safe*: pip consults an extra index for **every** distribution, so any package present on
both PyPI and the extra index can be served from the extra one (a dependency-confusion
surface). The corrected design is package-scoped and source-exact:

* ``scripts/ci_install.sh`` installs **only** the ``torch`` distribution, from the PyTorch
  CPU index **exclusively**, with ``--no-deps`` (so the CPU index is never consulted for any
  other distribution, not even torch's own transitive requirements);
* it then installs the editable project and **every** other distribution from **PyPI only**
  under ``constraints/ci-cpu-torch.txt``, so the exact ``+cpu`` build survives;
* it finally fails closed unless ``pip check`` is clean, no ``nvidia-*``/``triton`` package
  is present, and ``torch`` is still the ``+cpu`` build.

A first attempt pinned ``torch==2.15.0+cpu``. That reds the supported matrix: **PyTorch
2.15 dropped CPython 3.10 wheels**, while ``requires-python`` is ``>=3.10`` and CI runs a
3.10/3.11/3.12 parity matrix. ``2.14.1`` is the newest release whose CPU index publishes
``cp310``, ``cp311`` and ``cp312`` wheels (verified against ``download.pytorch.org/whl/cpu``
on 2026-11-03; see ``docs/audits/NEXUS_OMNI_CI_CD_SUPPLY_CHAIN_AUDIT_2026-11-03.md``).

These tests scan the workflows/script as text (no YAML dependency, matching
``test_ci_lint_parity.py``) and pin those invariants so the budget, the source-provenance
boundary, and the Python coverage cannot silently regress. The invariants are *mutated* by
``scripts/ci_torch_mutations.py``, which must show each one is load-bearing.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPT = REPO_ROOT / "scripts" / "ci_install.sh"
CONSTRAINTS = REPO_ROOT / "constraints" / "ci-cpu-torch.txt"
CPU_INDEX = "https://download.pytorch.org/whl/cpu"
PYPI_INDEX = "https://pypi.org/simple"

# Every workflow that installs the project (bare ``pip install -e``) pulls torch.
HEAVY_INSTALL_WORKFLOWS = ("ci.yml", "maintenance.yml", "langsmith-smoke.yml")

# The CPU index has a wheel for each of these interpreters for the pinned build.
# Verified 2026-11-03: torch 2.15.0+cpu publishes no cp310 wheel; 2.14.1+cpu
# publishes cp310, cp311 and cp312. This table must stay equal to the CI parity
# matrix — a new interpreter fails the guard and forces the pin to be re-proven.
_CPU_WHEEL_PYTHONS = frozenset({"3.10", "3.11", "3.12"})
_EXPECTED_PIN = "torch==2.14.1+cpu"

_CPU_PIN = re.compile(r"^torch==(\d+\.\d+\.\d+)\+cpu\s*$", re.MULTILINE)

# scripts/ is not an importable package; load the parity-matrix reader the same
# way test_ci_extras_parity.py does (importlib, no sys.path mutation).
_SPEC = importlib.util.spec_from_file_location(
    "extras_matrix_under_test", REPO_ROOT / "scripts" / "extras_matrix.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_extras_matrix = importlib.util.module_from_spec(_SPEC)
sys.modules["extras_matrix_under_test"] = _extras_matrix
_SPEC.loader.exec_module(_extras_matrix)
parity_pythons = _extras_matrix.parity_pythons


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _code(text: str) -> str:
    """The executable lines of a shell script, with comments stripped.

    Assertions must hold on the commands that actually run, not on the prose in
    a comment that mentions the same flag (a comment can say ``--no-deps`` while
    the command no longer passes it).
    """
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        lines.append(line.split(" #", 1)[0] if " #" in line else line)
    return "\n".join(lines)


def _constraint_pin() -> str:
    pins = _CPU_PIN.findall(CONSTRAINTS.read_text(encoding="utf-8"))
    assert len(pins) == 1, "the constraint must pin exactly one CPU build: torch==<x.y.z>+cpu"
    return pins[0]


def test_constraint_pins_a_cpu_torch_build() -> None:
    assert CONSTRAINTS.is_file(), "constraints/ci-cpu-torch.txt is missing"
    _constraint_pin()  # raises if the +cpu pin is missing or duplicated


def test_the_pin_covers_every_supported_python() -> None:
    """A pin with no wheel for a supported interpreter reds the parity matrix.

    PyTorch 2.15 dropped cp310; the repo floor is 3.10. The pin must be the
    release that still publishes a wheel for every interpreter the parity matrix
    runs, and the matrix itself must be the one this table was proven against.
    """
    matrix = set(parity_pythons(_workflow("ci.yml")))
    assert matrix == _CPU_WHEEL_PYTHONS, (
        f"the parity matrix {sorted(matrix)} is not the set {sorted(_CPU_WHEEL_PYTHONS)} "
        "this pin was verified against — re-verify the CPU wheels and update both."
    )
    assert CONSTRAINTS.read_text(encoding="utf-8").count(_EXPECTED_PIN) == 1, (
        f"the constraint must be exactly {_EXPECTED_PIN} (the newest release with cp310 "
        "wheels); a newer pin such as 2.15.0+cpu has no CPython 3.10 wheel."
    )


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


def test_script_takes_torch_from_the_cpu_index_only() -> None:
    code = _code(_script())
    assert CPU_INDEX in code, "ci_install.sh must use the PyTorch CPU index for torch"
    assert "--no-deps" in code, (
        "the CPU-index step must pass --no-deps so the CPU index is never consulted for "
        "torch's transitive requirements (source provenance is torch-only)"
    )
    assert "--extra-index-url" not in code, "ci_install.sh must never use an extra index"
    # The torch step must name exactly the `torch` requirement, nothing else.
    torch_step = code[: code.index(PYPI_INDEX)]
    assert re.search(r"^\s*torch\s*$", torch_step, re.MULTILINE), (
        "the CPU-index step must install the bare `torch` requirement"
    )


def test_script_resolves_everything_else_from_pypi_only() -> None:
    code = _code(_script())
    pypi_block = code[code.index(PYPI_INDEX) :]
    assert "-e " in pypi_block or "-e\t" in pypi_block, (
        "the PyPI-only step must perform the editable install"
    )
    assert "--index-url https://pypi.org/simple" in code, (
        "the remainder step must pin PyPI as its only index"
    )
    # Both steps must apply the constraint so pip cannot swap in a CUDA torch.
    assert code.count('"${CONSTRAINTS}"') >= 2, (
        'both install steps must apply the CPU-torch constraint (-c "${CONSTRAINTS}")'
    )


def test_script_fails_closed_on_cuda_or_non_cpu_torch() -> None:
    code = _code(_script())
    assert 'endswith("+cpu")' in code, "ci_install.sh must assert the +cpu build"
    assert 'startswith("nvidia-")' in code and '"triton"' in code, (
        "ci_install.sh must detect any nvidia-*/triton package"
    )
    assert "if cuda:" in code, "the detected CUDA stack must actually trigger the failure branch"
    assert '"pip", "check"' in code, (
        "ci_install.sh must run `pip check` so a broken closure fails closed"
    )
    assert "sys.exit(1)" in code, "a detected problem must exit non-zero (fail closed)"


def test_pyproject_still_pulls_torch() -> None:
    """Guard the guard: the budget only matters while torch is in the closure."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "sentence-transformers" in pyproject or "torch" in pyproject, (
        "torch no longer resolves from the dependency closure — this guard can be retired"
    )
