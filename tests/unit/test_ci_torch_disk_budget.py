"""CI supply-chain budget — CPU torch only, from its own index, for every
supported Python, with real behavioral proof (P1).

``pip install -e ".[dev]"`` resolves the full runtime closure. ``sentence-transformers``
(and ``llama-cpp-python``) pull ``torch``, and the *default* PyPI ``torch`` wheel ships the
CUDA stack (~539 MB torch plus ~1.8 GB of ``nvidia-*``/``triton`` wheels). On a standard
runner that exhausts the disk mid-install and fails with ``OSError: [Errno 28]`` — observed
on the REQUIRED ``test`` job of PR #307 (green on a rerun of the same SHA).

The design is package-scoped and source-exact (``scripts/ci_install.sh``): step 1 installs
**only** the ``torch`` distribution from the PyTorch CPU index **exclusively** with
``--no-deps``; step 2 installs the editable project and every other distribution from
**PyPI only**; step 3 fails closed unless ``pip check`` is clean, no ``nvidia-*``/``triton``
package is present, and ``torch`` is still ``+cpu``.

Two classes of evidence live here, deliberately:

* **Textual invariants** (fast) pin the shape of the script and the workflows.
* **Behavioral tests** execute the real postcondition against synthetic install
  metadata, run the real installer through a ``python`` shim (argv, order, exit codes),
  and exercise pip's own ``--isolated`` environment handling. A textual assertion can be
  satisfied by a comment or by dead code, so the postcondition and the installer are
  proven to actually *fail closed* under the conditions the audit named (H1).

``torch==2.14.1+cpu`` is the pin: 2.15 dropped CPython 3.10 wheels, the repository floor
is ``>=3.10``, and CI runs a 3.10/3.11/3.12 parity matrix (see
``docs/audits/NEXUS_OMNI_CI_CD_SUPPLY_CHAIN_AUDIT_2026-11-03.md``).
"""

from __future__ import annotations

import importlib.util
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPT = REPO_ROOT / "scripts" / "ci_install.sh"
CONSTRAINTS = REPO_ROOT / "constraints" / "ci-cpu-torch.txt"
CPU_INDEX = "https://download.pytorch.org/whl/cpu"
PYPI_INDEX = "https://pypi.org/simple"

# The CPU index has a wheel for each of these interpreters for the pinned build.
# Verified 2026-11-03: torch 2.15.0+cpu publishes no cp310 wheel; 2.14.1+cpu
# publishes cp310, cp311 and cp312. This table must stay equal to BOTH CI parity
# matrices (python-parity and continuum-evidence) — a new interpreter fails the
# guard and forces the pin to be re-proven.
_CPU_WHEEL_PYTHONS = frozenset({"3.10", "3.11", "3.12"})
_EXPECTED_PIN = "torch==2.14.1+cpu"

#: Workflows that install the project but are allowed to NOT go through the shared
#: script. Every entry must carry a reason and an owning PR; the guard fails if an
#: entry is missing either, or if it names a file that no longer exists. This is the
#: *bounded* exception list — not a place to hide an un-migrated heavy install.
_INSTALL_EXCEPTIONS: dict[str, dict[str, str]] = {
    "queue-ownership-mutations.yml": {
        "reason": (
            "installs the project's own bounded dependency set; its migration to a "
            "minimal torch-free install is owned by PR #304 "
            "(task-omni-ci-disk-budget-01) and must not be duplicated here"
        ),
        "pr": "#304",
    },
}

_CPU_PIN = re.compile(r"^torch==(\d+\.\d+\.\d+)\+cpu\s*$", re.MULTILINE)
_EDITABLE_INSTALL = re.compile(r"pip\s+install\b[^\n]*?(?:-e\b|--editable\b)")
_NO_DEPS = re.compile(r"--no-deps\b")
_CI_INSTALL_REF = re.compile(r"scripts/ci_install\.sh")
_MATRIX = re.compile(r"python-version:\s*\[([^\]]*)\]")

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


# --------------------------------------------------------------------------- #
# readers
# --------------------------------------------------------------------------- #
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


def _job_block(workflow_text: str, job: str) -> str:
    """The text of one ``jobs:`` entry (2-space indented key)."""
    match = re.search(rf"^  {re.escape(job)}:\s*$", workflow_text, re.MULTILINE)
    assert match is not None, f"job {job!r} not found in the workflow"
    rest = workflow_text[match.end() :]
    nxt = re.search(r"^  [A-Za-z0-9_-]+:\s*$", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _matrix_pythons(block: str) -> set[str]:
    match = _MATRIX.search(block)
    assert match is not None, "no python-version matrix in this job"
    return {part.strip().strip("\"'") for part in match.group(1).split(",") if part.strip()}


def _postcondition_body() -> str:
    """The real Python postcondition, extracted from the ``python - <<'PY'`` block."""
    text = _script()
    marker = "python - <<'PY'\n"
    start = text.rindex(marker) + len(marker)
    end = text.index("\nPY\n", start)
    return text[start:end]


# --------------------------------------------------------------------------- #
# behavioral harness: run the real postcondition against synthetic metadata
# --------------------------------------------------------------------------- #
def _fake_site(tmp_path: Path, packages: tuple[tuple[str, str], ...]) -> Path:
    """A site directory holding ``*.dist-info`` metadata for each (name, version).

    ``importlib.metadata`` (and therefore ``pip check``) reads ``METADATA`` from
    any ``*.dist-info`` directory on ``sys.path``, so this reproduces "this
    package is installed" without touching the network or the real environment.
    """
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    for name, version in packages:
        info = site / f"{name}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
        )
    return site


def _run_postcondition(
    tmp_path: Path, packages: tuple[tuple[str, str], ...]
) -> subprocess.CompletedProcess[str]:
    site = _fake_site(tmp_path, packages)
    env = {**os.environ, "PYTHONPATH": str(site)}
    return subprocess.run(
        [sys.executable, "-c", _postcondition_body()],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------------- #
# behavioral harness: run the real installer through a python shim
# --------------------------------------------------------------------------- #
_SHIM = """\
#!/usr/bin/env bash
# records every invocation; serves the heredoc postcondition on stdin
{ printf 'CALL %s\\n' "$*"; } >> "$CALL_LOG"
if [ "${1:-}" = "-" ]; then
  cat >> "$CALL_LOG"
  exit "${SHIM_STDIN_EXIT:-0}"
fi
exit 0
"""


def _installer_calls(
    tmp_path: Path,
    args: tuple[str, ...] = (".[dev]",),
    env_extra: dict[str, str] | None = None,
    stdin_exit: int = 0,
) -> tuple[list[str], int, str, str]:
    """Run ``scripts/ci_install.sh`` with a ``python`` shim on PATH.

    Returns (recorded ``CALL`` lines, installer exit code, combined output, full
    log). The full log includes the heredoc body the shim captured from stdin.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "python"
    shim.write_text(_SHIM, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "CALL_LOG": str(log),
        "SHIM_STDIN_EXIT": str(stdin_exit),
        **(env_extra or {}),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    full = log.read_text(encoding="utf-8") if log.exists() else ""
    calls = [ln for ln in full.splitlines() if ln.startswith("CALL ")]
    return calls, proc.returncode, proc.stdout + proc.stderr, full


# --------------------------------------------------------------------------- #
# textual invariants (fast shape checks)
# --------------------------------------------------------------------------- #
def test_constraint_pins_a_cpu_torch_build() -> None:
    assert CONSTRAINTS.is_file(), "constraints/ci-cpu-torch.txt is missing"
    _constraint_pin()  # raises if the +cpu pin is missing or duplicated


def test_the_pin_covers_every_supported_python() -> None:
    matrix = set(parity_pythons(_workflow("ci.yml")))
    assert matrix == _CPU_WHEEL_PYTHONS, (
        f"the parity matrix {sorted(matrix)} is not the set {sorted(_CPU_WHEEL_PYTHONS)} "
        "this pin was verified against — re-verify the CPU wheels and update both."
    )
    assert CONSTRAINTS.read_text(encoding="utf-8").count(_EXPECTED_PIN) == 1, (
        f"the constraint must be exactly {_EXPECTED_PIN} (the newest release with cp310 "
        "wheels); a newer pin such as 2.15.0+cpu has no CPython 3.10 wheel."
    )


def test_parity_and_continuum_matrices_agree() -> None:
    """L3: both Python matrices must be the same contract, not two drifting lists."""
    text = _workflow("ci.yml")
    parity = _matrix_pythons(_job_block(text, "python-parity"))
    continuum = _matrix_pythons(_job_block(text, "continuum-evidence"))
    assert parity == continuum == _CPU_WHEEL_PYTHONS, (
        "python-parity and continuum-evidence must run exactly the interpreters the CPU "
        f"pin was proven for; got parity={sorted(parity)} continuum={sorted(continuum)} "
        f"expected={sorted(_CPU_WHEEL_PYTHONS)}"
    )


def test_no_global_extra_index_anywhere() -> None:
    for name in _discover_workflows():
        text = _workflow(name)
        assert "PIP_EXTRA_INDEX_URL" not in text, (
            f"{name} still sets PIP_EXTRA_INDEX_URL; that applies the CPU index to every "
            "dependency (dependency-confusion surface). Use scripts/ci_install.sh instead."
        )
        assert "--extra-index-url" not in text, f"{name} passes --extra-index-url to pip"


def test_script_takes_torch_from_the_cpu_index_only() -> None:
    code = _code(_script())
    assert CPU_INDEX in code, "ci_install.sh must use the PyTorch CPU index for torch"
    assert "--no-deps" in code, (
        "the CPU-index step must pass --no-deps so the CPU index is never consulted for "
        "torch's transitive requirements (source provenance is torch-only)"
    )
    assert "--extra-index-url" not in code, "ci_install.sh must never use an extra index"
    assert "--find-links" not in code, "ci_install.sh must never accept find-links"
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
    assert code.count('"${CONSTRAINTS}"') >= 2, (
        'both install steps must apply the CPU-torch constraint (-c "${CONSTRAINTS}")'
    )


def test_script_isolates_pip_from_the_environment() -> None:
    """M3: both install steps must ignore PIP_* / pip.conf via --isolated."""
    code = _code(_script())
    assert code.count("--isolated") >= 2, (
        "both pip install steps must pass --isolated so PIP_INDEX_URL / "
        "PIP_EXTRA_INDEX_URL / PIP_FIND_LINKS / pip.conf cannot inject an index"
    )


def test_script_fails_closed_on_cuda_or_non_cpu_torch() -> None:
    code = _code(_script())
    assert 'endswith("+cpu")' in code, "ci_install.sh must assert the +cpu build"
    assert "nvidia-" in code and '"triton"' in code, (
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


# --------------------------------------------------------------------------- #
# behavioral postcondition tests (H1)
# --------------------------------------------------------------------------- #
def test_postcondition_accepts_cpu_torch_only(tmp_path: Path) -> None:
    proc = _run_postcondition(tmp_path, (("torch", "2.14.1+cpu"),))
    assert proc.returncode == 0, proc.stderr
    assert "no CUDA packages" in proc.stdout


def test_postcondition_rejects_nvidia_dash_name(tmp_path: Path) -> None:
    proc = _run_postcondition(tmp_path, (("torch", "2.14.1+cpu"), ("nvidia-cublas-cu12", "12.0.0")))
    assert proc.returncode != 0, "an nvidia-* package must fail the install closed"
    assert "CUDA/NVIDIA" in proc.stderr


def test_postcondition_rejects_nvidia_underscore_name(tmp_path: Path) -> None:
    """L1: an equivalent spelling (underscores) must not slip past detection."""
    proc = _run_postcondition(tmp_path, (("torch", "2.14.1+cpu"), ("nvidia_nccl_cu12", "2.0.0")))
    assert proc.returncode != 0, "`nvidia_nccl_cu12` is packaging-equivalent to `nvidia-nccl-cu12`"
    assert "CUDA/NVIDIA" in proc.stderr


def test_postcondition_rejects_nvidia_dot_name(tmp_path: Path) -> None:
    """L1: dot-separated spelling must not slip past detection either."""
    proc = _run_postcondition(tmp_path, (("torch", "2.14.1+cpu"), ("nvidia.cublas.cu12", "12.0.0")))
    assert proc.returncode != 0, "`nvidia.cublas.cu12` normalizes to `nvidia-cublas-cu12`"
    assert "CUDA/NVIDIA" in proc.stderr


def test_postcondition_rejects_triton(tmp_path: Path) -> None:
    proc = _run_postcondition(tmp_path, (("torch", "2.14.1+cpu"), ("triton", "3.0.0")))
    assert proc.returncode != 0, "triton must fail the install closed"
    assert "CUDA/NVIDIA" in proc.stderr


def test_postcondition_rejects_non_cpu_torch(tmp_path: Path) -> None:
    proc = _run_postcondition(tmp_path, (("torch", "2.14.1"),))
    assert proc.returncode != 0, "a torch build without the +cpu suffix must fail closed"
    assert "not the CPU build" in proc.stderr


def test_postcondition_fails_closed_on_missing_torch(tmp_path: Path) -> None:
    """No torch at all (e.g. unreadable metadata) must fail closed, not pass."""
    proc = _run_postcondition(tmp_path, ())
    assert proc.returncode != 0, "an unreadable/absent torch must fail the install closed"
    assert "could not be evaluated" in proc.stderr


def test_postcondition_fails_closed_on_broken_metadata(tmp_path: Path) -> None:
    """A dist-info with no METADATA must make the postcondition fail closed."""
    site = tmp_path / "site"
    (site / "torch-2.14.1+cpu.dist-info").mkdir(parents=True)
    (site / "nvidia-broken-1.0.dist-info").mkdir(parents=True)
    env = {**os.environ, "PYTHONPATH": str(site)}
    proc = subprocess.run(
        [sys.executable, "-c", _postcondition_body()],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0, "unreadable package metadata must fail closed"


# --------------------------------------------------------------------------- #
# behavioral installer tests (H1): real argv, order, exit codes
# --------------------------------------------------------------------------- #
def test_installer_step1_is_isolated_cpu_index_torch_only(tmp_path: Path) -> None:
    calls, rc, out, _ = _installer_calls(tmp_path)
    assert rc == 0, out
    torch_step = calls[0]
    assert "--isolated" in torch_step
    assert f"--index-url {CPU_INDEX}" in torch_step
    assert "--only-binary=:all:" in torch_step
    assert "--no-deps" in torch_step
    assert "-c" in torch_step and "ci-cpu-torch.txt" in torch_step
    assert torch_step.rstrip().endswith("torch"), "step 1 must install only the torch requirement"


def test_installer_step2_is_isolated_pypi_editable(tmp_path: Path) -> None:
    calls, rc, out, _ = _installer_calls(tmp_path)
    assert rc == 0, out
    pypi_step = calls[1]
    assert "--isolated" in pypi_step
    assert f"--index-url {PYPI_INDEX}" in pypi_step
    assert "-e" in pypi_step and ".[dev]" in pypi_step
    assert "ci-cpu-torch.txt" in pypi_step, "step 2 must apply the same constraint"
    assert CPU_INDEX not in pypi_step, "the PyPI step must not leak the CPU index"


def test_installer_runs_the_real_postcondition(tmp_path: Path) -> None:
    calls, rc, out, full = _installer_calls(tmp_path)
    assert rc == 0, out
    assert any(c.startswith("CALL -") for c in calls), (
        "the installer must run the postcondition via a stdin heredoc"
    )
    assert '"pip", "check"' in full, "the postcondition body must run `pip check`"
    assert "sys.exit(1)" in full, "the postcondition body must be able to fail closed"


def test_installer_propagates_postcondition_failure(tmp_path: Path) -> None:
    """A failing postcondition must make the installer exit non-zero (fail closed)."""
    _, rc, _, _ = _installer_calls(tmp_path, stdin_exit=1)
    assert rc != 0, "the installer must propagate a non-zero postcondition exit"


def test_installer_ignores_hostile_pip_environment(tmp_path: Path) -> None:
    """M3: a poisoned PIP_* environment must not reach the recorded argv."""
    hostile = {
        "PIP_INDEX_URL": "https://example.invalid/simple",
        "PIP_EXTRA_INDEX_URL": "https://example.invalid/extra",
        "PIP_FIND_LINKS": "https://example.invalid/wheels",
    }
    calls, rc, out, _ = _installer_calls(tmp_path, env_extra=hostile)
    assert rc == 0, out
    joined = "\n".join(calls)
    assert "example.invalid" not in joined, "the hostile env must not reach any pip argv"
    assert "--extra-index-url" not in joined
    assert "--find-links" not in joined
    assert f"--index-url {CPU_INDEX}" in calls[0]
    assert f"--index-url {PYPI_INDEX}" in calls[1]


def test_pip_isolated_ignores_environment_config(tmp_path: Path) -> None:
    """Real pip: ``--isolated`` drops PIP_* configuration (the M3 mechanism)."""
    env = {
        **os.environ,
        "PIP_INDEX_URL": "https://example.invalid/simple",
        "PIP_EXTRA_INDEX_URL": "https://example.invalid/extra",
        "PIP_CONFIG_FILE": os.devnull,
    }
    exposed = subprocess.run(
        [sys.executable, "-m", "pip", "config", "list"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    isolated = subprocess.run(
        [sys.executable, "-m", "pip", "--isolated", "config", "list"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "example.invalid" in exposed.stdout, (
        "without --isolated, pip must read the environment index (documents the hazard)"
    )
    assert "example.invalid" not in isolated.stdout, (
        "--isolated must ignore PIP_INDEX_URL / PIP_EXTRA_INDEX_URL entirely"
    )


# --------------------------------------------------------------------------- #
# dynamic workflow discovery (M4)
# --------------------------------------------------------------------------- #
def _discover_workflows() -> list[str]:
    """Every workflow file, discovered — never a hardcoded list."""
    return sorted(
        p.name for p in [*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")] if p.is_file()
    )


def _classify(text: str) -> str:
    """How a workflow installs the project.

    ``shared``            — routes through scripts/ci_install.sh (the policy);
    ``bounded-editable``  — an editable install guarded by --no-deps (no transitive
                            resolution, so torch cannot be pulled);
    ``heavy-editable``    — an editable install that may resolve the full closure
                            (torch/CUDA) and is NOT the shared script → must be an
                            explicit, documented exception.
    """
    if _CI_INSTALL_REF.search(text):
        return "shared"
    editable = _EDITABLE_INSTALL.search(text)
    if editable:
        return "bounded-editable" if _NO_DEPS.search(editable.group(0)) else "heavy-editable"
    return "no-project-install"


def test_every_workflow_is_classified_and_heavy_ones_are_exceptions() -> None:
    heavy = []
    for name in _discover_workflows():
        kind = _classify(_workflow(name))
        if kind == "heavy-editable" and name not in _INSTALL_EXCEPTIONS:
            heavy.append(name)
    assert not heavy, (
        "these workflows resolve the full dependency closure (torch/CUDA) without "
        f"scripts/ci_install.sh and have no documented exception: {heavy}"
    )


def test_documented_exceptions_are_bounded_and_live() -> None:
    for name, meta in _INSTALL_EXCEPTIONS.items():
        assert (WORKFLOWS / name).is_file(), f"exception names a missing workflow: {name}"
        assert meta.get("reason", "").strip(), f"exception {name} needs a reason"
        assert re.match(r"#\d+", meta.get("pr", "")), f"exception {name} needs an owning PR"
        assert _classify(_workflow(name)) != "shared", (
            f"exception {name} now uses the shared script — drop the exception"
        )


def test_a_new_heavy_workflow_would_be_flagged() -> None:
    """A brand-new (or renamed) heavy workflow must not slip past discovery."""
    synthetic = 'jobs:\n  build:\n    steps:\n      - run: pip install -e ".[dev]"\n'
    assert _classify(synthetic) == "heavy-editable"
    assert "not-a-real-workflow.yml" not in _INSTALL_EXCEPTIONS


def test_renamed_workflow_is_still_discovered() -> None:
    """Discovery is by glob, so a rename keeps the file under the guard."""
    names = _discover_workflows()
    assert "ci.yml" in names
    assert len(names) == len(set(names))
    assert all(n.endswith((".yml", ".yaml")) for n in names)


def test_no_install_is_swallowed_by_continue_on_error_or_true() -> None:
    install_re = re.compile(r"(\bpip\s+install\b|scripts/ci_install\.sh)")
    for name in _discover_workflows():
        # Strip YAML comment lines: a comment saying "no continue-on-error" must
        # not itself trip the guard.
        code = "\n".join(
            line for line in _workflow(name).splitlines() if not line.lstrip().startswith("#")
        )
        for line in code.splitlines():
            if install_re.search(line):
                assert "|| true" not in line, (
                    f"{name}: an install must not be `|| true`-masked: {line.strip()}"
                )
        assert "continue-on-error" not in code, (
            f"{name}: continue-on-error can hide a failed install; remove it or justify it"
        )


def test_guard_is_not_satisfied_by_a_comment() -> None:
    """The comment-stripping helper must drop prose that mentions the flags."""
    sample = "# step uses --no-deps and --isolated\npip install --index-url x\n"
    code = _code(sample)
    assert "--no-deps" not in code
    assert "--isolated" not in code
