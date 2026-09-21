"""Wave 5 — dependency-free coverage measurement for the Nagar capability packs.

This is repository-truth tooling, which is why it lives next to
:mod:`nexus_ai_agent.continuum.snapshot` rather than inside the pack substrate:
the substrate is deliberately data-only and its import allowlist
(``tests/architecture/test_pack_manifest_is_data_only.py``) has no room for
``trace``/``dis``/``types``.  Keeping the harness here means **no architecture
gate had to be relaxed** to measure coverage, and the substrate stayed frozen.

The repository has no coverage tooling: ``pytest-cov``/``coverage.py`` are not
declared, and adding a dependency to run a number nobody can reproduce without
it contradicts the project's "core stays small" rule.  This module measures the
same property with the **standard library only**:

* :func:`executable_lines` walks a module's code objects with
  :func:`dis.findlinestarts` — the compiler's own view of "a line that can run",
  which is exactly the denominator a coverage tool uses;
* :func:`measure` runs a deterministic set of test modules under
  :class:`trace.Trace` and collects the numerator (lines that actually ran);
* :class:`PackCoverage` / :class:`CoverageReport` aggregate per pack and overall,
  and :func:`coverage_failures` turns the threshold into CI-style findings.

Why per *pack* and not per file: a capability pack is the unit that ships, is
manifested and is activated.  ``nexus packs list`` answers "what can run?"; this
harness answers "what has actually been exercised?", and both are properties of
the same six directories.

Cost: tracing is roughly 3–5× slower than an untraced run, which is why the
default target list is the pack-focused subset rather than the whole suite
(``--tests`` overrides it).  The script ``scripts/pack_coverage.py`` is the thin
CLI over this module — logic lives here because ``scripts/`` is not an installed
package (the PR#40 lesson, enforced by
``tests/architecture/test_scripts_import_boundary.py``).

**The bar.**  ``DEFAULT_THRESHOLD`` is 85%: a real bar with margin under the
weakest pack measured on Wave-5 (`nexus.color.delivery` 87.19%), so a genuine
regression turns the tool red while the repository it ships with is green.  The
measured baseline for the default 24-module test set is 94.89% overall
(audio 96.09 · caption 96.66 · core 96.26 · delivery 87.19 · edit 95.89 ·
motion 98.12 · slideshow 93.03).  The project's stated goal remains 95% per pack
(wave4-step7); ``--threshold 95`` shows exactly which packs have not reached it
yet, and the per-module ``missing_lines`` in the JSON report says where to look.
"""

from __future__ import annotations

import contextlib
import dis
import io
import trace
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

#: Where the measured packs live, relative to this module.
DEFAULT_PACK_ROOT = (
    Path(__file__).resolve().parents[3] / "src" / "nexus_ai_agent" / "creative" / "packs"
)

#: Default acceptance bar (margin below the weakest measured pack; goal is 95%).
DEFAULT_THRESHOLD = 85.0


@dataclass(frozen=True)
class ModuleCoverage:
    """Executed vs. executable lines for one module."""

    path: str
    executed: int
    executable: int
    missing: tuple[int, ...] = ()

    @property
    def percent(self) -> float:
        if self.executable == 0:  # pragma: no cover - filtered before construction
            return 100.0
        return round(100.0 * self.executed / self.executable, 2)


@dataclass(frozen=True)
class PackCoverage:
    """One pack (or the core substrate) with its per-module detail."""

    pack: str
    modules: tuple[ModuleCoverage, ...]

    @property
    def executed(self) -> int:
        return sum(module.executed for module in self.modules)

    @property
    def executable(self) -> int:
        return sum(module.executable for module in self.modules)

    @property
    def percent(self) -> float:
        if self.executable == 0:  # pragma: no cover - filtered before construction
            return 100.0
        return round(100.0 * self.executed / self.executable, 2)

    def worst(self, limit: int = 3) -> tuple[ModuleCoverage, ...]:
        return tuple(sorted(self.modules, key=lambda module: module.percent)[:limit])


@dataclass(frozen=True)
class CoverageReport:
    """A full measurement: the packs, the threshold and the pytest exit code."""

    packs: tuple[PackCoverage, ...]
    threshold: float = DEFAULT_THRESHOLD
    tests: tuple[str, ...] = ()
    pytest_exit_code: int = 0

    @property
    def executed(self) -> int:
        return sum(pack.executed for pack in self.packs)

    @property
    def executable(self) -> int:
        return sum(pack.executable for pack in self.packs)

    @property
    def percent(self) -> float:
        if self.executable == 0:  # pragma: no cover - requires an empty measurement
            return 100.0
        return round(100.0 * self.executed / self.executable, 2)

    @property
    def below_threshold(self) -> bool:
        return bool(coverage_failures(self))

    def as_dict(self) -> dict[str, object]:
        return {
            "threshold": self.threshold,
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
                        for module in pack.modules
                    ],
                }
                for pack in self.packs
            ],
        }


def executable_lines(path: Path) -> frozenset[int]:
    """Line numbers that can execute, as the compiler sees them.

    ``dis.findlinestarts`` is used instead of a private ``trace`` helper: it is
    the documented, stable view of the line table, and it recurses into nested
    code objects (functions, comprehensions) here so a module's helper bodies
    count towards the denominator exactly like they do in any coverage tool.
    """
    source = path.read_text(encoding="utf-8")
    root = compile(source, str(path), "exec")
    lines: set[int] = set()
    stack: list[types.CodeType] = [root]
    while stack:
        code = stack.pop()
        lines.update(line for _, line in dis.findlinestarts(code))
        stack.extend(const for const in code.co_consts if isinstance(const, types.CodeType))
    return frozenset(lines)


def _iter_pack_modules(root: Path, packs: Iterable[str] | None) -> list[tuple[str, Path]]:
    """``(group, path)`` for every module of the pack substrate, sorted."""
    selected = set(packs) if packs is not None else None
    entries: list[tuple[str, Path]] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        group = relative.parts[0] if len(relative.parts) > 1 else CORE_GROUP
        if selected is not None and group not in selected:
            continue
        entries.append((group, path))
    return entries


def measure(
    tests: Sequence[str] | None = None,
    *,
    pack_root: Path | None = None,
    packs: Iterable[str] | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    quiet: bool = True,
) -> CoverageReport:
    """Run ``tests`` under :class:`trace.Trace` and report pack coverage.

    ``pytest`` is imported lazily so this module stays importable (and testable)
    without the test runner installed — the same reason the CLI is separate.
    """
    import pytest  # local import: the harness is optional at runtime

    root = pack_root or DEFAULT_PACK_ROOT
    repo_root = root.parents[3]
    targets = tuple(tests) if tests else DEFAULT_TEST_TARGETS
    args = ["-q", "-p", "no:cacheprovider", *targets]
    if quiet:
        args.insert(0, "--no-header")

    tracer = trace.Trace(count=1, trace=0)
    if quiet:
        # The reporter is the table/JSON, not pytest's progress dots: a CI log
        # that interleaves both is unreadable, so the runner is silenced here.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            exit_code = int(tracer.runfunc(pytest.main, args))
    else:
        exit_code = int(tracer.runfunc(pytest.main, args))
    counts = tracer.results().counts

    executed: dict[str, set[int]] = {}
    for (filename, lineno), hits in counts.items():
        if hits:
            executed.setdefault(str(Path(filename).resolve()), set()).add(lineno)

    per_group: dict[str, list[ModuleCoverage]] = {}
    for group, path in _iter_pack_modules(root, packs):
        executable = executable_lines(path)
        if not executable:
            continue
        ran = executed.get(str(path.resolve()), set())
        hits = sorted(executable & ran)
        per_group.setdefault(group, []).append(
            ModuleCoverage(
                path=str(path.relative_to(repo_root)),
                executed=len(hits),
                executable=len(executable),
                missing=tuple(sorted(executable - ran)),
            )
        )

    pack_reports = tuple(
        PackCoverage(pack=group, modules=tuple(modules))
        for group, modules in sorted(per_group.items())
    )
    return CoverageReport(
        packs=pack_reports,
        threshold=threshold,
        tests=targets,
        pytest_exit_code=exit_code,
    )


def coverage_failures(report: CoverageReport) -> tuple[str, ...]:
    """Findings for any unit below the threshold (empty tuple = accept)."""
    failures: list[str] = []
    for pack in report.packs:
        if pack.percent < report.threshold:
            worst = ", ".join(f"{module.path} {module.percent:.2f}%" for module in pack.worst())
            failures.append(
                f"pack {pack.pack!r}: {pack.percent:.2f}% < "
                f"{report.threshold:.2f}% (worst: {worst})"
            )
    return tuple(failures)


def format_table(report: CoverageReport) -> str:
    """A compact, stable table: one row per pack plus the total."""
    width = max([len(pack.pack) for pack in report.packs] + [len("TOTAL")]) + 2
    header = (
        f"{'pack'.ljust(width)}{'modules':>8}{'executed':>10}{'executable':>12}{'cover':>9}  status"
    )
    lines = [header, "-" * (width + 41)]
    for pack in report.packs:
        status = "OK" if pack.percent >= report.threshold else "BELOW"
        lines.append(
            f"{pack.pack.ljust(width)}{len(pack.modules):>8}{pack.executed:>10}"
            f"{pack.executable:>12}{pack.percent:>8.2f}%  {status}"
        )
    lines.append("-" * (width + 41))
    total_status = "OK" if report.percent >= report.threshold else "BELOW"
    lines.append(
        f"{'TOTAL'.ljust(width)}{sum(len(pack.modules) for pack in report.packs):>8}"
        f"{report.executed:>10}{report.executable:>12}{report.percent:>8.2f}%  {total_status}"
    )
    lines.append(
        f"threshold: {report.threshold:.2f}% · tests: {len(report.tests)} module(s) · "
        f"pytest exit: {report.pytest_exit_code}"
    )
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
