#!/usr/bin/env python3
"""Extras truth: ``pyproject.toml`` ↔ CI matrix ↔ per-leg smoke tests (task-132).

The invariant this script enforces
----------------------------------
Every optional dependency extra declared in ``pyproject.toml`` must be *provably
installable and smoke-tested* by a dedicated CI job — not merely "known to work"
in a developer's environment.  Concretely, three declarations must agree:

1. **pyproject truth** — ``[project.optional-dependencies]`` (read live, never
   assumed; ``dev`` is a development group, not a shipping extra, so it gets no
   matrix leg but is reported for the dev-overlap note);
2. **workflow truth** — the ``extras-matrix`` job in ``.github/workflows/ci.yml``
   must carry exactly one ``leg: <extra>`` matrix entry per shipping extra plus a
   ``core`` leg, with **no** ``continue-on-error``: a failed install is a failed
   CI run (task-132 acceptance criterion 2);
3. **smoke truth** — ``tests/unit/test_optional_extras.py`` reads its leg
   definitions from this file, so a leg that exists in CI but is unknown to the
   tests (or the reverse) is drift, not a silent skip.

The same file also guards the **Python parity matrix**: every literal
``python-version: "X.Y"`` used by any job in ``ci.yml`` must appear in the
``python-parity`` job's matrix, that matrix must include the floor declared by
``requires-python`` and the primary CI Python, and nothing below the floor.

Standard library only (Python 3.10 compatible): the script runs inside the
**core-only** CI leg where nothing beyond the core install exists, and inside
``lint-fast`` before any package is installed.

Subcommands
-----------
``check``
    Validate everything; non-zero exit on any drift.  Also verifies the
    committed ``.github/DEPENDENCY_MATRIX.md`` is current (regenerate → compare),
    so a stale generated matrix is a red build, not a lie in the repo.
``print-md``
    Print the deterministic dependency-matrix document to stdout (no volatile
    fields: no timestamps, no SHAs — provenance comes from the commit that
    carries it and from the CI artifact written by ``report``).
``report --out PATH --sha SHA [--timestamp TS]``
    Write the audit artifact: legs, versions, provenance (SHA, timestamp,
    generator).  Used by CI; every artifact is bound to its source commit.
``audit-skips --leg LEG --log PATH``
    The skip-inflation guard: parse a ``pytest -rs`` log and fail if any skip
    reason references an import module that the *active* leg just proved
    installable — i.e. a test that should have run for real got skipped.

Exit codes: ``0`` in agreement, ``1`` drift detected, ``2`` missing input.

Persian note: نام extras از pyproject زنده خوانده می‌شود؛ اگر extra جدیدی
اضافه شود و leg آن در CI تعریف نشود، این اسکریپت CI را قرمز می‌کند.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
COMMITTED_MATRIX_DOC = REPO_ROOT / ".github" / "DEPENDENCY_MATRIX.md"
LEG_TEST_FILE = "tests/unit/test_optional_extras.py"

MATRIX_JOB = "extras-matrix"
PARITY_JOB = "python-parity"
#: The Python the full ``test``/``lint`` jobs run on.  The parity matrix must
#: include it, and every literal python-version in the workflow must be either
#: this or a parity leg.
PRIMARY_PYTHON = "3.12"

#: Skip reasons that mean "a real test declined to run because of a missing
#: dependency".  ``pytest.importorskip`` reports "could not import"; the
#: intentionally-reasoned leg skips ("smoke is proven by the extras-matrix CI
#: leg … not installed in this environment") are deliberately self-describing
#: and must NOT match — they are the visible, honest kind of skip.
_MISSING_DEP_SKIP = re.compile(r"could not import|requires the optional")

_JOB_HEADER = re.compile(r"^  (?P<job>[A-Za-z0-9_-]+):[ \t]*$", re.MULTILINE)
_LEG_ENTRY = re.compile(r"^\s*-\s*leg:\s*([A-Za-z0-9_.-]+)\s*$", re.MULTILINE)
_PYTHON_MATRIX = re.compile(r"python-version:\s*\[([^\]]+)\]")
_LITERAL_PYTHON = re.compile(r'python-version:\s*"(\d+\.\d+)"')
_CONTINUE_ON_ERROR = re.compile(r"continue-on-error")
_REQUIRES_PYTHON = re.compile(r'requires-python\s*=\s*">=\s*(\d+\.\d+)"')


@dataclass(frozen=True)
class LegDefinition:
    """One shipping extra: what CI must install and smoke-import for it."""

    extra: str
    #: requirement names exactly as declared in pyproject (validated live)
    requirements: tuple[str, ...]
    #: top-level import modules the wheels of those requirements provide
    import_modules: tuple[str, ...]
    #: the focused behavioral test(s) the leg runs beyond import smoke
    focused_tests: tuple[str, ...] = field(default_factory=tuple)


#: The leg registry.  ``core`` is implicit (it is not an extra).  Keys must be
#: exactly the shipping extras of pyproject — ``check`` proves it.
LEG_DEFINITIONS: dict[str, LegDefinition] = {
    "pdf": LegDefinition(
        extra="pdf",
        requirements=("pypdf>=5.1",),
        import_modules=("pypdf",),
        focused_tests=(
            "tests/integration/test_in_process_job_queue.py::test_extract_pdf_text_reads_text_layer",
            "tests/unit/test_jobs_cli.py (pdf job CLI path)",
        ),
    ),
    "speech": LegDefinition(
        extra="speech",
        requirements=("faster-whisper>=1.0,<2",),
        import_modules=("faster_whisper",),
        focused_tests=(
            "tests/unit/test_caption_engine_adapters.py (faster-whisper adapter)",
            "tests/architecture/test_caption_substrate_boundary.py",
        ),
    ),
    "translate": LegDefinition(
        extra="translate",
        requirements=("argostranslate>=1.9,<2",),
        import_modules=("argostranslate",),
        focused_tests=("tests/unit/test_caption_engine_adapters.py (argos adapter)",),
    ),
}

CORE_LEG = "core"
DEV_EXTRA = "dev"


# --------------------------------------------------------------------------- #
# pyproject truth (tomllib when available; 3.10 has no tomllib → text fallback)
# --------------------------------------------------------------------------- #
def _optional_dependencies(pyproject_text: str) -> dict[str, list[str]]:
    """Extra name → requirement strings, read from ``[project.optional-dependencies]``."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        return _optional_dependencies_text_scan(pyproject_text)
    data = tomllib.loads(pyproject_text)
    table = data.get("project", {}).get("optional-dependencies", {})
    return {str(name): [str(req) for req in reqs] for name, reqs in table.items()}


def _optional_dependencies_text_scan(pyproject_text: str) -> dict[str, list[str]]:
    """The 3.10 fallback: scan the ``[project.optional-dependencies]`` table body.

    Entries look like ``pdf = [`` … ``]`` with quoted requirement strings; the
    scan keeps only extras whose opening bracket is found inside the table.
    """
    start = re.search(r"^\[project\.optional-dependencies\][ \t]*$", pyproject_text, re.MULTILINE)
    if start is None:
        return {}
    rest = pyproject_text[start.end() :]
    next_table = re.search(r"^\[[^\[\]]+\][ \t]*$", rest, re.MULTILINE)
    body = rest if next_table is None else rest[: next_table.start()]
    extras: dict[str, list[str]] = {}
    for match in re.finditer(r"^([A-Za-z0-9_.-]+)\s*=\s*\[", body, re.MULTILINE):
        name = match.group(1)
        tail = body[match.end() :]
        end = tail.find("]")
        chunk = tail if end < 0 else tail[:end]
        extras[name] = re.findall(r'["\']([^"\']+)["\']', chunk)
    return extras


def requires_python_floor(pyproject_text: str) -> str:
    match = _REQUIRES_PYTHON.search(pyproject_text)
    if match is None:
        raise ValueError('pyproject.toml has no requires-python = ">=X.Y" declaration')
    return match.group(1)


def _version_tuple(version: str) -> tuple[int, int]:
    major, minor = version.split(".")[:2]
    return (int(major), int(minor))


# --------------------------------------------------------------------------- #
# workflow truth (text scan on purpose — no YAML dependency in the core leg)
# --------------------------------------------------------------------------- #
def job_block(workflow: str, job: str) -> str:
    """The body of one job: from its two-space ``job:`` header to the next."""
    body: list[str] = []
    inside = False
    for line in workflow.splitlines():
        if _JOB_HEADER.match(line):
            if inside:
                break
            inside = line.strip() == f"{job}:"
            continue
        if inside:
            body.append(line)
    if not body:
        raise ValueError(f"job {job!r} not found in the workflow")
    return "\n".join(body)


def matrix_legs(workflow: str) -> list[str]:
    """The ``leg:`` entries of the extras-matrix job, in declaration order."""
    return _LEG_ENTRY.findall(job_block(workflow, MATRIX_JOB))


def parity_pythons(workflow: str) -> list[str]:
    """The literal python versions of the python-parity matrix."""
    block = job_block(workflow, PARITY_JOB)
    match = _PYTHON_MATRIX.search(block)
    if match is None:
        return []
    return [part.strip().strip("\"'") for part in match.group(1).split(",") if part.strip()]


def literal_job_pythons(workflow: str) -> list[str]:
    """Every literal ``python-version: "X.Y"`` in the workflow (matrix refs excluded)."""
    return sorted(set(_LITERAL_PYTHON.findall(workflow)))


# --------------------------------------------------------------------------- #
# the parity check
# --------------------------------------------------------------------------- #
def matrix_problems(pyproject_text: str, workflow_text: str) -> list[str]:
    """Every disagreement between pyproject, the workflow matrix and this script."""
    problems: list[str] = []
    extras = _optional_dependencies(pyproject_text)
    shipping = {name for name in extras if name != DEV_EXTRA}
    declared = set(LEG_DEFINITIONS)

    if shipping != declared:
        missing = sorted(shipping - declared)
        unknown = sorted(declared - shipping)
        if missing:
            problems.append(
                "pyproject shipping extras have no LEG_DEFINITIONS entry (add the leg "
                f"definition + CI matrix leg): {missing}"
            )
        if unknown:
            problems.append(f"LEG_DEFINITIONS declare extras pyproject does not ship: {unknown}")

    for extra, definition in sorted(LEG_DEFINITIONS.items()):
        requirements = extras.get(extra)
        if requirements is None:
            continue  # already reported above
        for requirement in definition.requirements:
            if not any(req.split(";")[0].strip() == requirement for req in requirements):
                problems.append(
                    f"leg {extra!r} expects requirement {requirement!r} but pyproject "
                    f"[{extra}] declares {requirements}"
                )

    legs = matrix_legs(workflow_text)
    expected_legs = {CORE_LEG} | shipping
    actual_legs = set(legs)
    for missing in sorted(expected_legs - actual_legs):
        problems.append(
            f"the {MATRIX_JOB} job has no `leg: {missing}` matrix entry — every shipping "
            "extra (and the core-only contract) must be a blocking CI leg"
        )
    for unknown in sorted(actual_legs - expected_legs):
        problems.append(
            f"the {MATRIX_JOB} job carries leg {unknown!r} which pyproject does not declare"
        )
    if len(legs) != len(actual_legs):
        problems.append("duplicate leg entries in the extras-matrix job")

    block = job_block(workflow_text, MATRIX_JOB)
    if _CONTINUE_ON_ERROR.search(block):
        problems.append(
            f"the {MATRIX_JOB} job must not use continue-on-error: a failed extra "
            "install is a failed CI run (task-132), not a warning"
        )
    if "fail-fast: false" not in block:
        problems.append(
            f"the {MATRIX_JOB} job must set fail-fast: false so one red leg does not "
            "cancel the others before their evidence is collected"
        )
    if LEG_TEST_FILE not in workflow_text:
        problems.append(f"the workflow never runs {LEG_TEST_FILE}")

    # ---- python parity -------------------------------------------------- #
    pythons = parity_pythons(workflow_text)
    floor = requires_python_floor(pyproject_text)
    if not pythons:
        problems.append(f"the {PARITY_JOB} job declares no python-version matrix")
    else:
        if floor not in pythons:
            problems.append(
                f"requires-python floor {floor} is missing from the {PARITY_JOB} matrix {pythons}"
            )
        if PRIMARY_PYTHON not in pythons:
            problems.append(
                f"primary CI python {PRIMARY_PYTHON} is missing from the {PARITY_JOB} matrix"
            )
        for version in pythons:
            if _version_tuple(version) < _version_tuple(floor):
                problems.append(
                    f"{PARITY_JOB} tests python {version} below the requires-python floor {floor}"
                )
        for version in literal_job_pythons(workflow_text):
            if version not in pythons:
                problems.append(
                    f"a job pins python-version {version} which the {PARITY_JOB} matrix "
                    f"({pythons}) does not parity-test"
                )
    return problems


# --------------------------------------------------------------------------- #
# the generated, committed matrix document (deterministic — no volatile fields)
# --------------------------------------------------------------------------- #
def _requirement_pins(pyproject_text: str) -> dict[str, list[str]]:
    return _optional_dependencies(pyproject_text)


def matrix_markdown(pyproject_text: str, workflow_text: str) -> str:
    """The deterministic dependency-matrix document (regenerate → compare guard)."""
    extras = _requirement_pins(pyproject_text)
    floor = requires_python_floor(pyproject_text)
    pythons = parity_pythons(workflow_text)
    legs = matrix_legs(workflow_text)
    dev_requirements = extras.get(DEV_EXTRA, [])
    lines: list[str] = []
    lines.append("# Dependency matrix — install paths CI actually proves")
    lines.append("")
    lines.append(
        "Generated by `scripts/extras_matrix.py print-md`; the `check` subcommand "
        "regenerates this document and fails when the committed copy is stale. "
        "This file is deliberately free of timestamps and SHAs: its provenance is "
        "the commit that carries it, and per-run provenance lives in the "
        "`dependency-matrix` CI artifact."
    )
    lines.append("")
    lines.append("## Environment truth")
    lines.append("")
    lines.append("| Declaration | Value | Source |")
    lines.append("|---|---|---|")
    lines.append(f"| `requires-python` floor | `{floor}` | `pyproject.toml` |")
    lines.append(f"| Primary CI Python | `{PRIMARY_PYTHON}` | `test`/`lint` jobs |")
    lines.append(
        f"| Python parity matrix | {', '.join(f'`{p}`' for p in pythons) or '**missing**'} "
        f"| `python-parity` job |"
    )
    lines.append(
        f"| Extras-matrix legs | {', '.join(f'`{leg}`' for leg in legs) or '**missing**'} "
        f"| `extras-matrix` job |"
    )
    lines.append("")
    lines.append("## Optional extras (live from `pyproject.toml`)")
    lines.append("")
    lines.append(
        "| Extra | Requirements (as declared) | Import smoke | Blocking CI leg | Focused evidence |"
    )
    lines.append("|---|---|---|---|---|")
    for extra in sorted(name for name in extras if name != DEV_EXTRA):
        definition = LEG_DEFINITIONS.get(extra)
        modules = (
            ", ".join(f"`{m}`" for m in definition.import_modules)
            if definition
            else "**UNDECLARED**"
        )
        leg = f"`{extra}`" if definition and extra in legs else "**MISSING**"
        focused = (
            "<br>".join(f"`{t}`" for t in definition.focused_tests)
            if definition
            else "add a `LegDefinition` in `scripts/extras_matrix.py`"
        )
        reqs = "<br>".join(f"`{r}`" for r in extras[extra])
        lines.append(f"| `{extra}` | {reqs} | {modules} | {leg} | {focused} |")
    lines.append("")
    lines.append("## Core-only contract (leg `core`)")
    lines.append("")
    lines.append(
        "`pip install -e .` with **no** extras, plus test-only tooling "
        "(pytest/pytest-asyncio — deliberately *not* the `dev` extra, "
        "which ships `pypdf` and `imageio-ffmpeg`). CI proves on this leg:"
    )
    lines.append("")
    lines.append(
        "- every optional module (`pypdf`, `faster_whisper`, `argostranslate`) is **absent**;"
    )
    lines.append("- `nexus --help` and `nexus run-bot --help` run (CLI/startup import tree);")
    lines.append(
        "- the whole `nexus_ai_agent` package imports with zero `ImportError` "
        "(no module-level hard dependency on an optional package);"
    )
    lines.append(
        "- the optional paths **fail closed with the typed, actionable error** "
        "(`caption_profile_unavailable` / `translate_profile_unavailable` / the "
        "`pypdf` install hint), never an unguarded `ImportError`."
    )
    lines.append("")
    lines.append("## Dev-overlap note (why the main `test` job is not enough)")
    lines.append("")
    overlapped = sorted(
        extra
        for extra, definition in LEG_DEFINITIONS.items()
        if any(
            req.split(";")[0].strip() in dev_requirements
            or any(
                dev_req.startswith(req.split(">")[0].split("<")[0].split("=")[0].strip())
                for dev_req in dev_requirements
            )
            for req in definition.requirements
        )
    )
    if overlapped:
        lines.append(
            "The `dev` extra already installs requirements overlapping "
            f"{', '.join(f'`[{e}]`' for e in overlapped)}, so the default `test` job "
            "exercises those paths *in the same process* as everything else — but it "
            "never proves the **install command** `pip install 'nexus-ai-agent[extra]'` "
            "resolves, and it cannot prove absence for the core-only contract. The "
            "extras-matrix legs close exactly that gap."
        )
    else:
        lines.append(
            "The `dev` extra installs none of the shipping extras' requirements; "
            "only the extras-matrix legs exercise them."
        )
    lines.append("")
    lines.append("## Known environment mismatches (recorded, not hidden)")
    lines.append("")
    lines.append(
        f"- `mypy` type-checks with `python_version = 3.12` while `requires-python` is "
        f"`{floor}` — the parity legs are the runtime proof for the older interpreters; "
        "the type-check floor is a separate (deliberate) decision in `pyproject.toml`."
    )
    lines.append(
        "- No lockfile is committed: CI installs from `pyproject.toml` constraints "
        "exactly like a user would. Reproducibility is proven per run by the "
        "`dependency-matrix` artifact (pinned versions of the installed environment "
        "are recorded there)."
    )
    lines.append("")
    lines.append(f"Smoke test file: `{LEG_TEST_FILE}` (leg selection via `NEXUS_EXTRA_LEG`).")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CI artifact (provenance: SHA + timestamp + generator)
# --------------------------------------------------------------------------- #
def build_report(
    pyproject_text: str,
    workflow_text: str,
    *,
    sha: str,
    timestamp: str,
    installed: list[str] | None = None,
) -> dict[str, object]:
    extras = _optional_dependencies(pyproject_text)
    return {
        "schema": 1,
        "source_commit": sha,
        "generated_at": timestamp,
        "generator": "scripts/extras_matrix.py",
        "requires_python_floor": requires_python_floor(pyproject_text),
        "primary_ci_python": PRIMARY_PYTHON,
        "parity_pythons": parity_pythons(workflow_text),
        "extras_matrix_legs": matrix_legs(workflow_text),
        "extras": {name: sorted(reqs) for name, reqs in sorted(extras.items())},
        "drift_problems": matrix_problems(pyproject_text, workflow_text),
        "installed_packages": sorted(installed) if installed is not None else None,
    }


# --------------------------------------------------------------------------- #
# skip-inflation guard
# --------------------------------------------------------------------------- #
def skip_inflation_problems(log_text: str, leg: str) -> list[str]:
    """Skip lines that decline a real test for a module the active leg provides.

    ``pytest -rs`` prints lines like ``SKIPPED [1] path: reason``.  In leg
    ``pdf``, a reason mentioning ``pypdf`` (e.g. ``could not import 'pypdf'``)
    means a test that CI promised to run for real silently did not — that is
    the "skipped test inflation" failure mode this guard kills.

    The ``core`` leg provides no optional module, so its rule is stricter and
    simpler: **no** dependency-missing skip at all.  Optional-extras tests on
    that leg skip with an explicit, self-describing reason ("smoke is proven by
    the extras-matrix CI leg"), which does not match the missing-dependency
    pattern — anything that does is a core dependency that failed to provide.
    """
    if leg == CORE_LEG:
        modules: tuple[str, ...] = ()
    elif leg in LEG_DEFINITIONS:
        modules = LEG_DEFINITIONS[leg].import_modules
    else:
        return [f"unknown leg {leg!r}: known legs are core + {sorted(LEG_DEFINITIONS)}"]
    problems: list[str] = []
    for line in log_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("SKIPPED"):
            continue
        if not _MISSING_DEP_SKIP.search(stripped):
            continue
        if leg == CORE_LEG:
            problems.append(
                f"skip inflation on leg 'core': {stripped} — the core install must "
                "provide everything a core test needs; optional-extras skips are "
                "explicitly reasoned and never match this pattern"
            )
            continue
        for module in modules:
            if module in stripped:
                problems.append(
                    f"skip inflation on leg {leg!r}: {stripped} — module {module!r} is "
                    "installed on this leg, the test must run, not skip"
                )
    return problems


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _read(path: Path) -> str:
    if not path.is_file():
        print(f"missing input: {path}", file=sys.stderr)
        raise SystemExit(2)
    return path.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="validate pyproject ↔ CI matrix ↔ committed doc")
    sub.add_parser("print-md", help="print the deterministic dependency-matrix document")

    report = sub.add_parser("report", help="write the provenance JSON artifact")
    report.add_argument("--out", required=True)
    report.add_argument("--sha", required=True)
    report.add_argument("--timestamp", default="")
    report.add_argument(
        "--installed-from",
        default=None,
        help="optional path to `pip freeze` output to embed in the artifact",
    )

    audit = sub.add_parser("audit-skips", help="fail on skips that should have run")
    audit.add_argument("--leg", required=True)
    audit.add_argument("--log", required=True)

    args = parser.parse_args(argv)
    pyproject_text = _read(PYPROJECT)
    workflow_text = _read(WORKFLOW)

    if args.command == "check":
        problems = matrix_problems(pyproject_text, workflow_text)
        if COMMITTED_MATRIX_DOC.is_file():
            if COMMITTED_MATRIX_DOC.read_text(encoding="utf-8") != matrix_markdown(
                pyproject_text, workflow_text
            ):
                problems.append(
                    f"{COMMITTED_MATRIX_DOC.name} is stale — regenerate with "
                    "`python scripts/extras_matrix.py print-md > .github/DEPENDENCY_MATRIX.md`"
                )
        else:
            problems.append(f"{COMMITTED_MATRIX_DOC} is missing (run print-md to create it)")
        if problems:
            print("extras-matrix drift detected:")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print(
            "extras matrix ok: "
            f"legs={matrix_legs(workflow_text)} parity={parity_pythons(workflow_text)} "
            f"extras={sorted(_optional_dependencies(pyproject_text))}"
        )
        return 0

    if args.command == "print-md":
        sys.stdout.write(matrix_markdown(pyproject_text, workflow_text))
        return 0

    if args.command == "report":
        installed: list[str] | None = None
        if args.installed_from:
            installed = [
                line.strip()
                for line in Path(args.installed_from).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            ]
        payload = build_report(
            pyproject_text,
            workflow_text,
            sha=args.sha,
            timestamp=args.timestamp,
            installed=installed,
        )
        Path(args.out).write_text(
            json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.out} (sha={args.sha})")
        return 0 if not payload["drift_problems"] else 1

    if args.command == "audit-skips":
        log_text = _read(Path(args.log))
        problems = skip_inflation_problems(log_text, args.leg)
        if problems:
            print("skip inflation detected:")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print(f"no skip inflation on leg {args.leg!r}")
        return 0

    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
