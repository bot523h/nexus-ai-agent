"""Deterministic, stdlib-traced coverage evidence for Nagar capability packs.

This module deliberately treats a coverage percentage as evidence, not merely a
number.  A measurement is **accepted** only when every one of these holds:

* the complete canonical target set ran (:data:`DEFAULT_TEST_TARGETS`), in a
  fresh, hermetic interpreter, with zero failed, errored or deselected tests;
* the complete canonical pack surface was measured, every shipped pack
  (a directory with ``pack.manifest.json``) is mapped to its own canonical
  test targets in :data:`PACK_TEST_TARGETS`, and no stale mapping remains;
* the evidence is bound to a clean commit (``git status`` shows no tracked,
  untracked or ignored-importable change under the evidence source paths);
* **every pack** is at or above :data:`ACCEPTANCE_THRESHOLD` (95%), and the
  requested threshold is not below that contract.

Focused runs, custom roots and lowered thresholds remain useful diagnostics,
but they are explicitly reported as not accepted and cannot produce a green
exit.  :func:`verify_artifact` closes the consumer side: a stored JSON
artifact is trusted only if a fresh measurement on the current checkout
reproduces it byte for byte and is itself accepted.

The harness uses only :mod:`dis`, :mod:`trace`, and a fresh Python subprocess
around ``pytest``.  ``pytest`` is a test-time dependency, not a package runtime
dependency.  The subprocess is essential: repeated ``pytest.main()`` calls in
the caller process leak imported tests, plugins, caches, and other global state
into the next measurement.
"""

from __future__ import annotations

import ast
import dis
import json
import math
import re
import secrets
import subprocess
import sys
import tempfile
import textwrap
import trace
import types
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus_ai_agent.continuum import provenance

#: Canonical test targets per shipped pack directory.  Every directory under
#: ``creative/packs/`` that carries a ``pack.manifest.json`` (== every entry of
#: ``creative.packs.runtime.COMPOSITION``, pinned by the architecture gate) must
#: appear here; a shipped pack without targets, or a mapping for a pack that no
#: longer ships, makes the measurement unverified.
PACK_TEST_TARGETS: Mapping[str, tuple[str, ...]] = {
    "audio": (
        "tests/unit/test_audio_pack.py",
        "tests/architecture/test_audio_pack_boundary.py",
    ),
    "caption": (
        "tests/unit/test_caption_pack.py",
        "tests/unit/test_caption_ass.py",
        "tests/unit/test_caption_engine_adapters.py",
        "tests/architecture/test_caption_substrate_boundary.py",
    ),
    "delivery": (
        "tests/unit/test_delivery_pack.py",
        "tests/unit/test_delivery_signing.py",
        # OTIO round-trip: timeline.markers/media refs -> real document (task-110)
        "tests/unit/test_otio_interop.py",
        "tests/architecture/test_delivery_pack_boundary.py",
    ),
    "edit": (
        "tests/unit/test_edit_pack.py",
        "tests/architecture/test_edit_pack_boundary.py",
    ),
    "motion": (
        "tests/unit/test_motion_pack.py",
        "tests/architecture/test_motion_pack_boundary.py",
    ),
    "slideshow": (
        "tests/unit/test_slideshow_pack.py",
        "tests/unit/test_slideshow_invariants.py",
        "tests/unit/test_slideshow_30s_target.py",
        "tests/unit/test_slideshow_upscale.py",
        "tests/architecture/test_slideshow_adapter_boundary.py",
    ),
}

#: Test modules that import a pack package but are evidence for a *host* layer,
#: not for the pack.  Every other test module that imports a pack must be a
#: canonical target (:func:`pack_test_import_issues`), so a new pack test can
#: never be silently left out of the evidence, and no entry here may go stale.
HOST_LAYER_PACK_IMPORTERS: Mapping[str, str] = {
    "tests/unit/test_capability_lifecycle.py": "studio lifecycle gate (creative/studio)",
    "tests/unit/test_creative_render_jobs.py": "worker render adapter; encodes with FFmpeg",
    "tests/unit/test_optional_extras.py": "optional-extras install legs (CI extras matrix)",
    "tests/unit/test_cognition_bus_integration.py": (
        "cognition boundary -> CommandBus integration (nagar.cognition, host layer)"
    ),
    "tests/unit/test_cognition_model_kill.py": (
        "Model Kill Test: deterministic path survives with no model (nagar.cognition, host layer)"
    ),
}

#: The substrate (``core``) and cross-pack contracts that compose every pack.
SUBSTRATE_TEST_TARGETS: tuple[str, ...] = (
    "tests/unit/test_pack_manifest_verify.py",
    "tests/unit/test_pack_runtime_composition.py",
    "tests/unit/test_nagar_wave1_green_cockpit.py",
    # capability-pack trust root (ADR 0006): trust.py, ed25519.py, verify.py
    "tests/unit/test_pack_trust_root.py",
    # Wave 5 gap operations (the two op-gap waves)
    "tests/unit/test_opgap_audio_motion.py",
    "tests/unit/test_opgap_wave5.py",
    # architecture gates that import and exercise every pack
    "tests/architecture/test_pack_activation_completeness.py",
    "tests/architecture/test_pack_manifest_is_data_only.py",
    "tests/architecture/test_nagar_studio_isolation.py",
)

#: The canonical target set: substrate first, then each pack in name order.
DEFAULT_TEST_TARGETS: tuple[str, ...] = SUBSTRATE_TEST_TARGETS + tuple(
    target for pack in sorted(PACK_TEST_TARGETS) for target in PACK_TEST_TARGETS[pack]
)

#: Files directly under ``creative/packs/`` (the substrate) are grouped here.
CORE_GROUP = "core"

#: The file that marks a directory under the pack root as a shipped pack.
PACK_MANIFEST = "pack.manifest.json"

_REPO_ROOT = provenance.REPO_ROOT

#: Where the measured packs live, relative to this module.
DEFAULT_PACK_ROOT = _REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs"

#: The acceptance contract: every pack (and therefore the total) must reach
#: this per-pack line coverage.  Recorded as DECISION_LOG D-0023.  A caller may
#: ask for a *stricter* bar; a lower one is a diagnostic that is never accepted.
ACCEPTANCE_THRESHOLD = 95.0

#: The default bar is the acceptance contract (never a softer regression bar).
DEFAULT_THRESHOLD = ACCEPTANCE_THRESHOLD

#: Version tag of the JSON artifact produced by :meth:`CoverageReport.as_dict`.
ARTIFACT_SCHEMA = "nexus.pack-coverage/2"

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_OUTCOME_KEYS = ("collected", "deselected", "passed", "failed", "skipped", "errors", "xfailed")
_TRACE_ARTIFACT_KEYS = frozenset({"nonce", "pytest_exit_code", "counts", "outcomes"})

# Keep the parent and isolated child explicitly tied to the stdlib tracer.  The
# child must be a fresh process, but the import is a deliberate architecture
# marker as well as a standard-library availability check.
_TRACE_MODULE_NAME = trace.__name__
_TRACE_RUNNER = textwrap.dedent(
    """
    import json
    import os
    from pathlib import Path
    import sys
    import trace

    import pytest


    class Outcomes:
        def __init__(self):
            self.values = {
                "collected": None,
                "deselected": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "errors": 0,
                "xfailed": 0,
            }

        def pytest_deselected(self, items):
            self.values["deselected"] += len(items)

        def pytest_collection_finish(self, session):
            self.values["collected"] = len(session.items)

        def pytest_runtest_logreport(self, report):
            if report.when == "call":
                if hasattr(report, "wasxfail") and report.skipped:
                    self.values["xfailed"] += 1
                elif report.passed:
                    self.values["passed"] += 1
                elif report.failed:
                    self.values["failed"] += 1
                else:
                    self.values["skipped"] += 1
            elif report.failed:
                self.values["errors"] += 1
            elif report.when == "setup" and report.skipped:
                key = "xfailed" if hasattr(report, "wasxfail") else "skipped"
                self.values[key] += 1


    output_path = Path(sys.argv[1])
    nonce = sys.argv[2]
    args = json.loads(sys.argv[3])
    outcomes = Outcomes()
    tracer = trace.Trace(count=1, trace=0)
    pytest_exit_code = int(tracer.runfunc(pytest.main, args, plugins=[outcomes]))
    merged = {}
    for (filename, lineno), hits in tracer.results().counts.items():
        if hits and lineno > 0:
            key = (str(Path(filename).resolve()), lineno)
            merged[key] = merged.get(key, 0) + hits
    counts = [[filename, lineno, hits] for (filename, lineno), hits in sorted(merged.items())]
    temporary = output_path.with_suffix(".partial")
    temporary.write_text(
        json.dumps(
            {
                "nonce": nonce,
                "pytest_exit_code": pytest_exit_code,
                "counts": counts,
                "outcomes": outcomes.values,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, output_path)
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
class RunOutcomes:
    """What the traced pytest run actually did (not what it was asked to do)."""

    collected: int
    deselected: int
    passed: int
    failed: int
    skipped: int
    errors: int
    xfailed: int

    def __post_init__(self) -> None:
        for key in _OUTCOME_KEYS:
            value = getattr(self, key)
            if type(value) is not int or value < 0:
                raise ValueError(f"test outcome {key!r} must be a non-negative integer")

    def as_dict(self) -> dict[str, int]:
        return {key: getattr(self, key) for key in _OUTCOME_KEYS}


@dataclass(frozen=True)
class CoverageProvenance:
    """The exact source state a measurement is bound to."""

    git_commit: str | None
    worktree_drift: tuple[str, ...]
    source_digest: str
    interpreter: str

    def __post_init__(self) -> None:
        if self.git_commit is not None and not provenance.COMMIT_ID_PATTERN.fullmatch(
            self.git_commit
        ):
            raise ValueError("provenance commit must be a full hexadecimal commit id")
        if not _DIGEST_PATTERN.fullmatch(self.source_digest):
            raise ValueError("provenance source digest must be 'sha256:' + 64 hex digits")
        if not self.interpreter:
            raise ValueError("provenance interpreter must be non-empty")
        if any(not entry for entry in self.worktree_drift):
            raise ValueError("worktree drift entries must be non-empty")

    def as_dict(self) -> dict[str, object]:
        return {
            "git_commit": self.git_commit,
            "worktree_drift": list(self.worktree_drift),
            "source_digest": self.source_digest,
            "interpreter": self.interpreter,
        }


def _validate_threshold(threshold: object) -> float:
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise TypeError("coverage threshold must be a finite number")
    if not math.isfinite(float(threshold)) or threshold < 0:
        raise ValueError("coverage threshold must be finite and >= 0")
    return float(threshold)


@dataclass(frozen=True)
class CoverageReport:
    """A measurement and the evidence needed to decide whether it is credible."""

    packs: tuple[PackCoverage, ...]
    threshold: float = DEFAULT_THRESHOLD
    tests: tuple[str, ...] = ()
    pytest_exit_code: int = 0
    measurement_issues: tuple[str, ...] = ()
    outcomes: RunOutcomes | None = None
    provenance: CoverageProvenance | None = None

    def __post_init__(self) -> None:
        _validate_threshold(self.threshold)
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
            and self.outcomes is not None
            and self.provenance is not None
            and self.provenance.git_commit is not None
        )

    @property
    def accepted(self) -> bool:
        """The single green verdict: verified evidence that meets the contract."""

        return not coverage_failures(self)

    @property
    def below_threshold(self) -> bool:
        return bool(coverage_failures(self))

    def as_dict(self) -> dict[str, object]:
        """Canonical, JSON-serialisable evidence suitable for an artifact."""

        packs = sorted(self.packs, key=lambda pack: pack.pack)
        failures = coverage_failures(self)
        return {
            "schema": ARTIFACT_SCHEMA,
            "accepted": not failures,
            "acceptance_threshold": ACCEPTANCE_THRESHOLD,
            "failures": list(failures),
            "threshold": self.threshold,
            "verified": self.verified,
            "measurement_issues": list(self.measurement_issues),
            "total": {
                "executed": self.executed,
                "executable": self.executable,
                "percent": self.percent,
            },
            "pytest_exit_code": self.pytest_exit_code,
            "test_outcomes": None if self.outcomes is None else self.outcomes.as_dict(),
            "provenance": None if self.provenance is None else self.provenance.as_dict(),
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


def canonical_json(report: CoverageReport) -> str:
    """The artifact bytes: sorted keys, two-space indent, one trailing newline."""

    return (
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    )


def executable_lines(path: Path) -> frozenset[int]:
    """Return compiler line starts that can also be emitted by ``trace``.

    Nested functions and comprehensions are included recursively.  CPython 3.11+
    adds a synthetic ``RESUME`` entry at line zero; a Python line tracer can never
    report that synthetic line, so retaining it would create an un-coverable
    denominator and understate real coverage.  Interpreters that report an
    instruction without a line (``None``) are handled the same way.

    A module whose AST body is empty (blank or comment-only source) has no real
    executable statements on any supported interpreter.  CPython 3.11+ already
    reports its implicit ``return None`` at the synthetic line zero, but 3.10
    assigns it a positive line number pointing at non-code text.  Counting that
    synthetic line would let a comment-only file fabricate a traceable —and
    trivially 100%-coverable— surface, so the empty body is rejected explicitly
    instead of relying on version-specific line numbering.
    """

    source = path.read_text(encoding="utf-8")
    if not ast.parse(source, filename=str(path)).body:
        return frozenset()
    root = compile(source, str(path.resolve()), "exec")
    lines: set[int] = set()
    stack: list[types.CodeType] = [root]
    while stack:
        code = stack.pop()
        lines.update(line for _, line in dis.findlinestarts(code) if line is not None and line > 0)
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


def _target_path(target: str) -> Path:
    candidate = Path(target.partition("::")[0])
    if not candidate.is_absolute():
        candidate = _REPO_ROOT / candidate
    return candidate


def _normalised_target(target: str) -> str:
    """Normalise a pytest node id without losing its ``::`` selection suffix."""

    _file_name, separator, selection = target.partition("::")
    return str(_target_path(target).resolve()) + (separator + selection if separator else "")


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
        if not _target_path(target).exists():
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


def pack_mapping_issues(
    root: Path,
    groups: Iterable[str],
    targets_by_pack: Mapping[str, Sequence[str]] = PACK_TEST_TARGETS,
) -> tuple[str, ...]:
    """Why the pack surface and the canonical target mapping disagree.

    Law: what the runtime ships must be visible in the evidence surface.  A
    shipped pack (manifest on disk) or a measured pack directory without its
    own canonical targets would be measured only incidentally; a mapping for a
    pack that no longer ships is a stale target set.
    """

    shipped = {
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and (entry / PACK_MANIFEST).is_file()
    }
    measured = set(groups) - {CORE_GROUP}
    mapped = {pack for pack, targets in targets_by_pack.items() if targets}
    issues = [
        f"shipped pack {pack!r} has no canonical test target"
        for pack in sorted((shipped | measured) - mapped)
    ]
    issues.extend(
        f"stale canonical target mapping: pack {pack!r} is not shipped"
        for pack in sorted(mapped - shipped)
    )
    issues.extend(
        f"pack directory {pack!r} has Python modules but no {PACK_MANIFEST}"
        for pack in sorted(measured - shipped)
    )
    return tuple(issues)


def _imported_packs(path: Path, packs: Iterable[str]) -> set[str]:
    """Pack directories that the test module at *path* imports (AST, not text)."""

    prefix = ["nexus_ai_agent", "creative", "packs"]
    wanted = set(packs)
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        modules: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        elif isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        for module in modules:
            parts = module.split(".")
            if parts[:3] == prefix and len(parts) > 3 and parts[3] in wanted:
                found.add(parts[3])
    return found


def pack_test_import_issues(
    tests_root: Path | None = None,
    *,
    targets: Iterable[str] = DEFAULT_TEST_TARGETS,
    host_layer: Mapping[str, str] = HOST_LAYER_PACK_IMPORTERS,
    packs: Iterable[str] = tuple(PACK_TEST_TARGETS),
) -> tuple[str, ...]:
    """Why the canonical target set is stale with respect to the test tree.

    Law: a test module that imports a pack is pack evidence unless it is
    explicitly classified as host-layer evidence, and a classification must
    point at a real pack importer.
    """

    root = tests_root or _REPO_ROOT / "tests"
    pack_names = tuple(packs)
    canonical = set(targets)
    importers: set[str] = set()
    for path in sorted(root.rglob("test_*.py"), key=lambda candidate: candidate.as_posix()):
        if _imported_packs(path, pack_names):
            importers.add(_display_path(path, root.parent))
    issues = [
        f"test module {name} imports a pack but is neither a canonical target nor host-layer"
        for name in sorted(importers - canonical - set(host_layer))
    ]
    issues.extend(
        f"stale host-layer classification: {name} does not import a pack"
        for name in sorted(set(host_layer) - importers)
    )
    issues.extend(
        f"{name} is both a canonical target and host-layer evidence"
        for name in sorted(canonical & set(host_layer))
    )
    return tuple(issues)


def _measurement_issues(
    root: Path,
    targets: tuple[str, ...],
    selected_packs: set[str] | None,
    available_packs: set[str],
) -> list[str]:
    """State why a valid scoped diagnostic cannot claim full-pack acceptance."""

    issues: list[str] = []
    if root != DEFAULT_PACK_ROOT.resolve():
        issues.append("non-canonical pack root: diagnostic coverage cannot certify shipped packs")
    else:
        issues.extend(pack_mapping_issues(root, available_packs))
        issues.extend(pack_test_import_issues())
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
    return issues


def _outcome_issues(pytest_exit_code: int, outcomes: RunOutcomes) -> list[str]:
    issues: list[str] = []
    if outcomes.collected == 0:
        issues.append("pytest collected no tests")
    if outcomes.deselected:
        issues.append(
            f"pytest deselected {outcomes.deselected} test(s): the canonical target set "
            "must run in full"
        )
    if pytest_exit_code == 0 and (outcomes.failed or outcomes.errors):
        issues.append(
            f"pytest exit 0 contradicts {outcomes.failed} failed and {outcomes.errors} "
            "errored test(s)"
        )
    if outcomes.passed == 0:
        issues.append("no test passed")
    return issues


def _parse_outcomes(raw: object, pytest_exit_code: int) -> RunOutcomes:
    if not isinstance(raw, dict) or set(raw) != set(_OUTCOME_KEYS):
        raise RuntimeError("traced pytest runner produced malformed test outcomes")
    values = dict(raw)
    if values["collected"] is None and pytest_exit_code != 0:
        values["collected"] = 0  # collection itself failed; the exit code says so
    try:
        return RunOutcomes(**{key: values[key] for key in _OUTCOME_KEYS})
    except ValueError as exc:
        raise RuntimeError(f"traced pytest runner produced malformed test outcomes: {exc}") from exc


def _run_traced_pytest(
    targets: tuple[str, ...],
) -> tuple[int, dict[str, set[int]], RunOutcomes]:
    """Run pytest in a fresh interpreter and return its line-event numerator.

    The environment is hermetic (:func:`provenance.isolated_python_environment`):
    plugin auto-load is disabled, selection-steering variables such as
    ``PYTEST_ADDOPTS`` are removed and the configuration file is pinned, so the
    caller's shell cannot deselect tests.  ``pytest_asyncio`` is loaded
    explicitly because this repository's own conftest uses asynchronous
    fixtures.  A per-run nonce binds the artifact to this invocation.
    """

    args = [
        "-q",
        "-c",
        str(_REPO_ROOT / "pyproject.toml"),
        "--rootdir",
        str(_REPO_ROOT),
        "-p",
        "no:cacheprovider",
        "-p",
        "pytest_asyncio.plugin",
        *targets,
    ]
    nonce = secrets.token_hex(16)
    with tempfile.TemporaryDirectory(prefix="nexus-pack-coverage-") as directory:
        output = Path(directory) / "trace.json"
        try:
            completed = subprocess.run(
                [sys.executable, "-c", _TRACE_RUNNER, str(output), nonce, json.dumps(args)],
                cwd=_REPO_ROOT,
                env=provenance.isolated_python_environment(_REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
                timeout=900,
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

    if not isinstance(payload, dict) or set(payload) != _TRACE_ARTIFACT_KEYS:
        raise RuntimeError("traced pytest runner produced an invalid evidence artifact")
    if payload["nonce"] != nonce:
        raise RuntimeError("traced pytest runner artifact does not belong to this run (nonce)")
    pytest_exit_code = payload["pytest_exit_code"]
    raw_counts = payload["counts"]
    if type(pytest_exit_code) is not int or not isinstance(raw_counts, list):
        raise RuntimeError("traced pytest runner produced an invalid evidence artifact")
    outcomes = _parse_outcomes(payload["outcomes"], pytest_exit_code)

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
        lines = executed.setdefault(str(Path(row[0]).resolve()), set())
        if row[1] in lines:
            # trace keys counts by (file, line); a repeated row is fabricated.
            raise RuntimeError("traced pytest runner produced duplicate line counts")
        lines.add(row[1])
    return pytest_exit_code, executed, outcomes


def _display_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(_REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.relative_to(root).as_posix()


def _evidence_files(modules: Iterable[Path], targets: Iterable[str]) -> list[Path]:
    """Every file whose bytes determine the measurement (surface, tests, config)."""

    files = set(modules)
    for target in targets:
        path = _target_path(target)
        found = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        files.update(found)
        directory = path if path.is_dir() else path.parent
        for parent in (directory, *directory.parents):
            conftest = parent / "conftest.py"
            if conftest.is_file():
                files.add(conftest)
            if parent == _REPO_ROOT:
                break
    files.add(_REPO_ROOT / "pyproject.toml")
    return sorted(files)


def _provenance(
    modules: Iterable[Path], targets: tuple[str, ...]
) -> tuple[CoverageProvenance, list[str]]:
    issues: list[str] = []
    commit: str | None
    drift: tuple[str, ...] = ()
    try:
        commit = provenance.current_commit(_REPO_ROOT)
        drift = provenance.working_tree_drift(_REPO_ROOT, scope=provenance.EVIDENCE_SOURCE_PATHS)
    except RuntimeError as exc:
        commit = None
        issues.append(f"evidence is not bound to a commit: {exc}")
    if drift:
        shown = ", ".join(drift[:3]) + (", …" if len(drift) > 3 else "")
        issues.append(
            f"working tree differs from {commit} in {len(drift)} evidence path(s): {shown}"
        )
    record = CoverageProvenance(
        git_commit=commit,
        worktree_drift=drift,
        source_digest=provenance.source_digest(_REPO_ROOT, _evidence_files(modules, targets)),
        interpreter=provenance.interpreter_identity(),
    )
    return record, issues


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
    threshold = _validate_threshold(threshold)

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

    pytest_exit_code, executed, outcomes = _run_traced_pytest(targets)
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
    record, provenance_issues = _provenance([path for _g, path in selected_modules], targets)
    # Empty modules leave the surface; a pack with *no* surface must not leave the
    # verdict, or a hollowed-out pack would skip the per-pack threshold unseen.
    hollow_packs = [
        f"pack {group!r} has zero traceable executable lines"
        for group in sorted(available_packs if selected_packs is None else selected_packs)
        if group not in per_group
    ]
    issues = (
        hollow_packs
        + _measurement_issues(root, targets, selected_packs, available_packs)
        + _outcome_issues(pytest_exit_code, outcomes)
        + provenance_issues
    )
    return CoverageReport(
        packs=pack_reports,
        threshold=threshold,
        tests=targets,
        pytest_exit_code=pytest_exit_code,
        measurement_issues=tuple(issues),
        outcomes=outcomes,
        provenance=record,
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
    if report.outcomes is None:
        failures.append("measurement carries no test outcomes")
    if report.provenance is None or report.provenance.git_commit is None:
        failures.append("measurement is not bound to a commit")
    failures.extend(f"measurement incomplete: {issue}" for issue in report.measurement_issues)
    if report.threshold < ACCEPTANCE_THRESHOLD:
        failures.append(
            f"threshold {report.threshold:.2f}% is below the {ACCEPTANCE_THRESHOLD:.2f}% "
            "acceptance contract (diagnostic run only)"
        )
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
    diagnostic = report.threshold < ACCEPTANCE_THRESHOLD
    for pack in packs:
        if pack.percent < report.threshold:
            status = "BELOW"
        elif not report.verified:
            status = "UNVERIFIED"
        elif diagnostic:
            status = "DIAGNOSTIC"
        else:
            status = "OK"
        lines.append(
            f"{pack.pack.ljust(width)}{len(pack.modules):>8}{pack.executed:>10}"
            f"{pack.executable:>12}{pack.percent:>8.2f}%  {status}"
        )
    lines.append("-" * (width + 41))
    failures = coverage_failures(report)
    if not report.verified:
        total_status = "UNVERIFIED"
    elif not failures:
        total_status = "OK"
    elif any(pack.percent < report.threshold for pack in packs):
        total_status = "BELOW"
    else:
        total_status = "NOT ACCEPTED"
    lines.append(
        f"{'TOTAL'.ljust(width)}{sum(len(pack.modules) for pack in packs):>8}"
        f"{report.executed:>10}{report.executable:>12}{report.percent:>8.2f}%  {total_status}"
    )
    lines.append(
        f"threshold: {report.threshold:.2f}% (contract {ACCEPTANCE_THRESHOLD:.2f}%) · "
        f"tests: {len(report.tests)} module(s) · pytest exit: {report.pytest_exit_code} · "
        f"evidence: {'verified' if report.verified else 'incomplete'}"
    )
    if report.provenance is not None:
        lines.append(
            f"commit: {report.provenance.git_commit or 'UNBOUND'} · "
            f"source: {report.provenance.source_digest} · {report.provenance.interpreter}"
        )
    for issue in report.measurement_issues:
        lines.append(f"! measurement incomplete: {issue}")
    lines.append(
        "verdict: ACCEPTED"
        if not failures
        else f"verdict: NOT ACCEPTED ({len(failures)} reason(s))"
    )
    return "\n".join(lines)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(token: str) -> object:
    raise ValueError(f"non-finite number {token}")


def _differences(expected: object, actual: object, prefix: str = "") -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        keys = sorted(set(expected) | set(actual))
        found: list[str] = []
        for key in keys:
            found.extend(
                _differences(expected.get(key), actual.get(key), f"{prefix}{key}.")
                if key in expected and key in actual
                else [f"{prefix}{key}"]
            )
        return found
    return [] if expected == actual else [prefix.rstrip(".") or "<root>"]


def verify_artifact(raw: bytes) -> list[str]:
    """Return every reason stored coverage evidence cannot be trusted now.

    The only way a stored artifact is accepted is if a fresh canonical
    measurement on the current checkout reproduces it byte for byte *and* is
    itself accepted.  This rejects forged numbers, artifacts from another
    commit or interpreter, partial runs, lowered thresholds and hand edits.
    """

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        return [f"artifact unreadable: {exc}"]
    if not isinstance(payload, dict):
        return ["artifact unreadable: root must be a JSON object"]
    if payload.get("schema") != ARTIFACT_SCHEMA:
        return [f"artifact schema {payload.get('schema')!r} is not {ARTIFACT_SCHEMA!r}"]
    try:
        threshold = _validate_threshold(payload.get("threshold"))
    except (TypeError, ValueError) as exc:
        return [f"artifact threshold invalid: {exc}"]

    try:
        fresh = measure(threshold=threshold)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return [f"re-measurement failed: {exc}"]
    fresh_bytes = canonical_json(fresh).encode("utf-8")
    problems: list[str] = []
    if fresh_bytes != raw:
        differing = _differences(json.loads(fresh_bytes), payload)
        if differing:
            problems.append(
                "artifact does not reproduce on this checkout; differs in: "
                + ", ".join(differing[:12])
                + (", …" if len(differing) > 12 else "")
            )
        else:
            problems.append("artifact bytes are not canonical")
    problems.extend(
        f"fresh measurement not accepted: {failure}" for failure in coverage_failures(fresh)
    )
    return problems


__all__ = [
    "ACCEPTANCE_THRESHOLD",
    "ARTIFACT_SCHEMA",
    "CORE_GROUP",
    "DEFAULT_PACK_ROOT",
    "DEFAULT_TEST_TARGETS",
    "DEFAULT_THRESHOLD",
    "PACK_MANIFEST",
    "PACK_TEST_TARGETS",
    "HOST_LAYER_PACK_IMPORTERS",
    "SUBSTRATE_TEST_TARGETS",
    "CoverageProvenance",
    "CoverageReport",
    "ModuleCoverage",
    "PackCoverage",
    "RunOutcomes",
    "canonical_json",
    "coverage_failures",
    "executable_lines",
    "format_table",
    "measure",
    "pack_mapping_issues",
    "pack_test_import_issues",
    "verify_artifact",
]
