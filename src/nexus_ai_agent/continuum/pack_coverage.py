"""Deterministic, stdlib-traced coverage evidence for Nagar capability packs.

This module deliberately treats a coverage percentage as evidence, not merely a
number.  A percentage is only eligible for a green verdict when all canonical
pack targets ran successfully, the complete canonical pack surface was found,
and the traced test process was isolated from the caller.  Focused runs remain
useful diagnostics, but they are explicitly reported as incomplete and cannot
produce a green acceptance verdict.

The harness uses only :mod:`dis`, :mod:`trace`, and a fresh Python subprocess
around ``pytest``.  ``pytest`` is a test-time dependency, not a package runtime
dependency.  The subprocess is essential: repeated ``pytest.main()`` calls in
the caller process leak imported tests, plugins, caches, and other global state
into the next measurement.
"""

from __future__ import annotations

import dis
import json
import math
import os
import subprocess
import sys
import tempfile
import textwrap
import types
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Pack-focused test modules: every test that exercises a pack contract, the
#: substrate that composes them, or the gap operations added by Wave 5.
DEFAULT_TEST_TARGETS: tuple[str, ...] = (
    # pack contracts (one module per pack)
    "tests/unit/test_slideshow_pack.py",
    "tests/unit/test_caption_pack.py",
    "tests/unit/test_caption_ass.py",
    "tests/unit/test_caption_engine_adapters.py",
    "tests/unit/test_edit_pack.py",
    "tests/unit/test_motion_pack.py",
    "tests/unit/test_audio_pack.py",
    "tests/unit/test_delivery_pack.py",
    "tests/unit/test_delivery_signing.py",
    # the substrate that composes and verifies them
    "tests/unit/test_pack_manifest_verify.py",
    "tests/unit/test_pack_runtime_composition.py",
    "tests/unit/test_nagar_wave1_green_cockpit.py",
    # Wave 5 gap operations (the two op-gap waves)
    "tests/unit/test_opgap_audio_motion.py",
    "tests/unit/test_opgap_wave5.py",
    # pack-surface tests that also execute pack code
    "tests/unit/test_slideshow_30s_target.py",
    "tests/unit/test_slideshow_upscale.py",
    # architecture gates that import and exercise every pack
    "tests/architecture/test_pack_activation_completeness.py",
    "tests/architecture/test_pack_manifest_is_data_only.py",
    "tests/architecture/test_nagar_studio_isolation.py",
    "tests/architecture/test_audio_pack_boundary.py",
    "tests/architecture/test_caption_substrate_boundary.py",
    "tests/architecture/test_delivery_pack_boundary.py",
    "tests/architecture/test_edit_pack_boundary.py",
    "tests/architecture/test_motion_pack_boundary.py",
)

#: Files directly under ``creative/packs/`` (the substrate) are grouped here.
CORE_GROUP = "core"

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Where the measured packs live, relative to this module.
DEFAULT_PACK_ROOT = _REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs"

#: Default acceptance bar.  The production goal remains 95%; this is the
#: regression threshold, not a claim that the full pack surface has reached it.
DEFAULT_THRESHOLD = 85.0

_TRACE_RUNNER = textwrap.dedent(
    """
    import json
    from pathlib import Path
    import sys
    import trace

    import pytest


    output_path = Path(sys.argv[1])
    args = json.loads(sys.argv[2])
    tracer = trace.Trace(count=1, trace=0)
    pytest_exit_code = int(tracer.runfunc(pytest.main, args))
    counts = [
        [str(Path(filename).resolve()), lineno, hits]
        for (filename, lineno), hits in sorted(tracer.results().counts.items())
        if hits and lineno > 0
    ]
    output_path.write_text(
        json.dumps(
            {"pytest_exit_code": pytest_exit_code, "counts": counts},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    """
)


@dataclass(frozen=True)
class ModuleCoverage:
    """Executed vs. executable traceable lines for one module."""

    path: str
    executed: int
    executable: int
    missing: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("module coverage path must be non-empty")
        if type(self.executed) is not int or type(self.executable) is not int:
            raise TypeError("coverage counts must be integers")
        if self.executed < 0 or self.executable < 0 or self.executed > self.executable:
            raise ValueError("coverage counts must satisfy 0 <= executed <= executable")
        if any(type(line) is not int or line <= 0 for line in self.missing):
            raise ValueError("missing lines must be positive integers")
        if self.missing != tuple(sorted(set(self.missing))):
            raise ValueError("missing lines must be sorted and unique")

    @property
    def percent(self) -> float:
        if self.executable == 0:
            return 0.0
        return round(100.0 * self.executed / self.executable, 2)


@dataclass(frozen=True)
class PackCoverage:
    """One pack (or the core substrate) with its per-module detail."""

    pack: str
    modules: tuple[ModuleCoverage, ...]

    def __post_init__(self) -> None:
        if not self.pack:
            raise ValueError("pack name must be non-empty")
        paths = [module.path for module in self.modules]
        if len(paths) != len(set(paths)):
            raise ValueError(f"pack {self.pack!r} contains duplicate module paths")

    @property
    def executed(self) -> int:
        return sum(module.executed for module in self.modules)

    @property
    def executable(self) -> int:
        return sum(module.executable for module in self.modules)

    @property
    def percent(self) -> float:
        if self.executable == 0:
            return 0.0
        return round(100.0 * self.executed / self.executable, 2)

    def worst(self, limit: int = 3) -> tuple[ModuleCoverage, ...]:
        return tuple(sorted(self.modules, key=lambda module: (module.percent, module.path))[:limit])


@dataclass(frozen=True)
class CoverageReport:
    """A measurement and the evidence needed to decide whether it is credible."""

    packs: tuple[PackCoverage, ...]
    threshold: float = DEFAULT_THRESHOLD
    tests: tuple[str, ...] = ()
    pytest_exit_code: int = 0
    measurement_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.threshold, bool) or not isinstance(self.threshold, (int, float)):
            raise TypeError("coverage threshold must be a finite number")
        if not math.isfinite(float(self.threshold)) or self.threshold < 0:
            raise ValueError("coverage threshold must be finite and >= 0")
        if type(self.pytest_exit_code) is not int or self.pytest_exit_code < 0:
            raise ValueError("pytest exit code must be a non-negative integer")
        names = [pack.pack for pack in self.packs]
        if len(names) != len(set(names)):
            raise ValueError("coverage report contains duplicate pack names")
        if any(not issue for issue in self.measurement_issues):
            raise ValueError("measurement issues must be non-empty messages")

    @property
    def executed(self) -> int:
        return sum(pack.executed for pack in self.packs)

    @property
    def executable(self) -> int:
        return sum(pack.executable for pack in self.packs)

    @property
    def percent(self) -> float:
        if self.executable == 0:
            return 0.0
        return round(100.0 * self.executed / self.executable, 2)

    @property
    def verified(self) -> bool:
        """Whether the measurement had enough successful evidence for a verdict."""

        return (
            self.pytest_exit_code == 0
            and bool(self.packs)
            and self.executable > 0
            and not self.measurement_issues
        )

    @property
    def below_threshold(self) -> bool:
        return bool(coverage_failures(self))

    def as_dict(self) -> dict[str, object]:
        """Canonical, JSON-serialisable evidence suitable for an artifact."""

        packs = sorted(self.packs, key=lambda pack: pack.pack)
        return {
            "threshold": self.threshold,
            "verified": self.verified,
            "measurement_issues": list(self.measurement_issues),
            "total": {
                "executed": self.executed,
                "executable": self.executable,
                "percent": self.percent,
            },
            "pytest_exit_code": self.pytest_exit_code,
            "tests": list(self.tests),
            "packs": [
                {
                    "pack": pack.pack,
                    "executed": pack.executed,
                    "executable": pack.executable,
                    "percent": pack.percent,
                    "modules": [
                        {
                            "path": module.path,
                            "executed": module.executed,
                            "executable": module.executable,
                            "percent": module.percent,
                            "missing_lines": list(module.missing),
                        }
                        for module in sorted(pack.modules, key=lambda module: module.path)
                    ],
                }
                for pack in packs
            ],
        }


def executable_lines(path: Path) -> frozenset[int]:
    """Return compiler line starts that can also be emitted by ``trace``.

    Nested functions and comprehensions are included recursively.  CPython 3.11+
    adds a synthetic ``RESUME`` entry at line zero; a Python line tracer can never
    report that synthetic line, so retaining it would create an un-coverable
    denominator and understate real coverage.
    """

    source = path.read_text(encoding="utf-8")
    root = compile(source, str(path.resolve()), "exec")
    lines: set[int] = set()
    stack: list[types.CodeType] = [root]
    while stack:
        code = stack.pop()
        lines.update(line for _, line in dis.findlinestarts(code) if line > 0)
        stack.extend(const for const in code.co_consts if isinstance(const, types.CodeType))
    return frozenset(lines)


def _iter_pack_modules(root: Path, packs: set[str] | None) -> list[tuple[str, Path]]:
    """Return sorted ``(group, path)`` pairs for every Python module in *root*."""

    entries: list[tuple[str, Path]] = []
    for path in sorted(root.rglob("*.py"), key=lambda candidate: candidate.as_posix()):
        relative = path.relative_to(root)
        group = relative.parts[0] if len(relative.parts) > 1 else CORE_GROUP
        if packs is None or group in packs:
            entries.append((group, path))
    return entries


def _normalised_target(target: str) -> str:
    """Normalise a pytest node id without losing its ``::`` selection suffix."""

    file_name, separator, selection = target.partition("::")
    candidate = Path(file_name)
    if not candidate.is_absolute():
        candidate = _REPO_ROOT / candidate
    return str(candidate.resolve()) + (separator + selection if separator else "")


def _validate_targets(tests: Sequence[str] | None) -> tuple[str, ...]:
    if tests is None:
        targets = DEFAULT_TEST_TARGETS
    else:
        if isinstance(tests, str):
            raise TypeError("tests must be a sequence of pytest target strings, not one string")
        targets = tuple(tests)
        if not targets:
            raise ValueError(
                "at least one pytest target is required; an empty target set is not evidence"
            )

    identities: set[str] = set()
    for target in targets:
        if not isinstance(target, str) or not target:
            raise ValueError("pytest targets must be non-empty strings")
        identity = _normalised_target(target)
        if identity in identities:
            raise ValueError(f"duplicate pytest target: {target}")
        identities.add(identity)
        path_part = target.partition("::")[0]
        candidate = Path(path_part)
        if not candidate.is_absolute():
            candidate = _REPO_ROOT / candidate
        if not candidate.exists():
            raise ValueError(f"pytest target does not exist: {target}")
    return targets


def _validate_pack_selection(
    root: Path, packs: Iterable[str] | None
) -> tuple[set[str] | None, set[str]]:
    all_modules = _iter_pack_modules(root, None)
    if not all_modules:
        raise ValueError(f"pack root contains no Python modules: {root}")
    available = {group for group, _path in all_modules}
    if packs is None:
        return None, available
    if isinstance(packs, str):
        raise TypeError("packs must be an iterable of pack names, not one string")
    selected_values = tuple(packs)
    if not selected_values or any(
        not isinstance(pack, str) or not pack for pack in selected_values
    ):
        raise ValueError("at least one non-empty pack name is required")
    selected = set(selected_values)
    if len(selected) != len(selected_values):
        raise ValueError("duplicate pack selection")
    unknown = sorted(selected - available)
    if unknown:
        raise ValueError(f"unknown pack selection: {', '.join(unknown)}")
    return selected, available


def _measurement_issues(
    root: Path,
    targets: tuple[str, ...],
    selected_packs: set[str] | None,
    available_packs: set[str],
) -> tuple[str, ...]:
    """State why a valid scoped diagnostic cannot claim full-pack acceptance."""

    issues: list[str] = []
    if root != DEFAULT_PACK_ROOT.resolve():
        issues.append("non-canonical pack root: diagnostic coverage cannot certify shipped packs")
    default_targets = {_normalised_target(target) for target in DEFAULT_TEST_TARGETS}
    target_set = {_normalised_target(target) for target in targets}
    if target_set != default_targets:
        issues.append(
            "partial test target set: "
            f"ran {len(target_set)} of {len(default_targets)} canonical targets"
        )
    if selected_packs is not None and selected_packs != available_packs:
        issues.append(
            "partial pack selection: "
            f"measured {len(selected_packs)} of {len(available_packs)} pack groups"
        )
    return tuple(issues)


def _run_traced_pytest(targets: tuple[str, ...]) -> tuple[int, dict[str, set[int]]]:
    """Run pytest in a fresh interpreter and return its line-event numerator.

    Disabling plugin auto-load makes the result independent of arbitrary plugins
    installed in the parent process.  ``pytest_asyncio`` is loaded explicitly
    because this repository's own conftest uses asynchronous fixtures.
    """

    args = ["-q", "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin", *targets]
    environment = os.environ.copy()
    source = str(_REPO_ROOT / "src")
    old_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source if not old_pythonpath else source + os.pathsep + old_pythonpath
    )
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    with tempfile.TemporaryDirectory(prefix="nexus-pack-coverage-") as directory:
        output = Path(directory) / "trace.json"
        try:
            completed = subprocess.run(
                [sys.executable, "-c", _TRACE_RUNNER, str(output), json.dumps(args)],
                cwd=_REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"traced pytest runner failed to start or finish: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().replace("\n", " ")
            raise RuntimeError(
                f"traced pytest runner failed with process exit {completed.returncode}: {detail}"
            )
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(
                f"traced pytest runner produced no valid evidence artifact: {exc}"
            ) from exc

    pytest_exit_code = payload.get("pytest_exit_code")
    raw_counts = payload.get("counts")
    if type(pytest_exit_code) is not int or not isinstance(raw_counts, list):
        raise RuntimeError("traced pytest runner produced an invalid evidence artifact")

    executed: dict[str, set[int]] = {}
    for row in raw_counts:
        if (
            not isinstance(row, list)
            or len(row) != 3
            or not isinstance(row[0], str)
            or type(row[1]) is not int
            or type(row[2]) is not int
            or row[1] <= 0
            or row[2] <= 0
        ):
            raise RuntimeError("traced pytest runner produced malformed line counts")
        executed.setdefault(str(Path(row[0]).resolve()), set()).add(row[1])
    return pytest_exit_code, executed


def _display_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(_REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.relative_to(root).as_posix()


def measure(
    tests: Sequence[str] | None = None,
    *,
    pack_root: Path | None = None,
    packs: Iterable[str] | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    quiet: bool = True,
) -> CoverageReport:
    """Measure packs under an isolated tracer and return auditable evidence.

    ``quiet`` is retained for API compatibility.  Pytest output is always kept
    out of the parent process; the report itself is the deterministic output.
    Invalid roots, selections, targets, thresholds, and trace-runner failures
    raise an observable exception rather than returning a fabricated green report.
    """

    del quiet  # The subprocess never leaks pytest output into the report stream.
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise TypeError("coverage threshold must be a finite number")
    if not math.isfinite(float(threshold)) or threshold < 0:
        raise ValueError("coverage threshold must be finite and >= 0")

    root = (pack_root or DEFAULT_PACK_ROOT).resolve()
    if not root.is_dir():
        raise ValueError(f"pack root does not exist or is not a directory: {root}")
    targets = _validate_targets(tests)
    selected_packs, available_packs = _validate_pack_selection(root, packs)
    selected_modules = _iter_pack_modules(root, selected_packs)
    if not selected_modules:
        raise ValueError("selected pack surface contains no Python modules")

    executable_modules = [(group, path, executable_lines(path)) for group, path in selected_modules]
    executable_modules = [entry for entry in executable_modules if entry[2]]
    if not executable_modules:
        raise ValueError("selected pack surface has zero traceable executable lines")

    pytest_exit_code, executed = _run_traced_pytest(targets)
    per_group: dict[str, list[ModuleCoverage]] = {}
    for group, path, executable in executable_modules:
        ran = executed.get(str(path.resolve()), set())
        covered = executable & ran
        per_group.setdefault(group, []).append(
            ModuleCoverage(
                path=_display_path(path, root),
                executed=len(covered),
                executable=len(executable),
                missing=tuple(sorted(executable - ran)),
            )
        )

    pack_reports = tuple(
        PackCoverage(pack=group, modules=tuple(sorted(modules, key=lambda module: module.path)))
        for group, modules in sorted(per_group.items())
    )
    return CoverageReport(
        packs=pack_reports,
        threshold=float(threshold),
        tests=targets,
        pytest_exit_code=pytest_exit_code,
        measurement_issues=_measurement_issues(root, targets, selected_packs, available_packs),
    )


def coverage_failures(report: CoverageReport) -> tuple[str, ...]:
    """Return every reason a report is ineligible for a green verdict."""

    failures: list[str] = []
    if not report.packs:
        failures.append("measurement produced no pack reports")
    if report.executable == 0:
        failures.append("measurement produced zero executable lines")
    if report.pytest_exit_code != 0:
        failures.append(f"pytest exited with {report.pytest_exit_code}")
    failures.extend(f"measurement incomplete: {issue}" for issue in report.measurement_issues)
    for pack in sorted(report.packs, key=lambda candidate: candidate.pack):
        if pack.percent < report.threshold:
            worst = ", ".join(f"{module.path} {module.percent:.2f}%" for module in pack.worst())
            failures.append(
                f"pack {pack.pack!r}: {pack.percent:.2f}% < "
                f"{report.threshold:.2f}% (worst: {worst})"
            )
    return tuple(failures)


def format_table(report: CoverageReport) -> str:
    """Return a compact, stable table that cannot label invalid evidence as OK."""

    packs = tuple(sorted(report.packs, key=lambda pack: pack.pack))
    width = max([len(pack.pack) for pack in packs] + [len("TOTAL")]) + 2
    header = (
        f"{'pack'.ljust(width)}{'modules':>8}{'executed':>10}{'executable':>12}{'cover':>9}  status"
    )
    lines = [header, "-" * (width + 41)]
    for pack in packs:
        status = "BELOW" if pack.percent < report.threshold else "OK"
        if not report.verified and status == "OK":
            status = "UNVERIFIED"
        lines.append(
            f"{pack.pack.ljust(width)}{len(pack.modules):>8}{pack.executed:>10}"
            f"{pack.executable:>12}{pack.percent:>8.2f}%  {status}"
        )
    lines.append("-" * (width + 41))
    total_status = "UNVERIFIED"
    if report.verified and report.percent >= report.threshold:
        total_status = "OK"
    elif report.verified and report.percent < report.threshold:
        total_status = "BELOW"
    lines.append(
        f"{'TOTAL'.ljust(width)}{sum(len(pack.modules) for pack in packs):>8}"
        f"{report.executed:>10}{report.executable:>12}{report.percent:>8.2f}%  {total_status}"
    )
    lines.append(
        f"threshold: {report.threshold:.2f}% · tests: {len(report.tests)} module(s) · "
        f"pytest exit: {report.pytest_exit_code} · evidence: "
        f"{'verified' if report.verified else 'incomplete'}"
    )
    for issue in report.measurement_issues:
        lines.append(f"! measurement incomplete: {issue}")
    return "\n".join(lines)


__all__ = [
    "CORE_GROUP",
    "DEFAULT_PACK_ROOT",
    "DEFAULT_TEST_TARGETS",
    "DEFAULT_THRESHOLD",
    "CoverageReport",
    "ModuleCoverage",
    "PackCoverage",
    "coverage_failures",
    "executable_lines",
    "format_table",
    "measure",
]
