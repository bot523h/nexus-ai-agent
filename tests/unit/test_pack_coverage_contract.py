"""Self-defending contract tests for pack coverage evidence (DECISION_LOG D-0023).

Every test attacks one way a coverage number could lie and pins the observable
verdict — ``accepted``/``verified``, the failure text, or an exception — never
an implementation detail.  The families mirror the mutation campaign in
``nexus_ai_agent.continuum.mutations``:

* denominator — what counts as an executable line;
* numerator — forged, duplicated, negative or out-of-surface line events;
* threshold — bypass, NaN, infinity, negative, bool, lowered bar;
* target integrity — missing, duplicate, subset, unknown, alternate root, stale;
* process — failing child, missing/malformed/partial/foreign trace artifacts;
* provenance and artifact reuse — dirty tree, unbound commit, forged artifacts;
* determinism — identical inputs produce identical canonical bytes.

The trace child is replaced only where the attack *is* a hostile or broken
child; the evidence pipeline around it runs for real.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import nexus_ai_agent.continuum.pack_coverage as coverage
from nexus_ai_agent.continuum.pack_coverage import (
    ACCEPTANCE_THRESHOLD,
    DEFAULT_PACK_ROOT,
    DEFAULT_TEST_TARGETS,
    DEFAULT_THRESHOLD,
    HOST_LAYER_PACK_IMPORTERS,
    PACK_MANIFEST,
    PACK_TEST_TARGETS,
    SUBSTRATE_TEST_TARGETS,
    CoverageProvenance,
    CoverageReport,
    ModuleCoverage,
    PackCoverage,
    RunOutcomes,
    canonical_json,
    coverage_failures,
    executable_lines,
    format_table,
    measure,
    pack_mapping_issues,
    pack_test_import_issues,
    verify_artifact,
)
from nexus_ai_agent.creative.packs.runtime import COMPOSITION_BY_DIRECTORY

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "pack_coverage.py"
PROBE_SOURCE = "value = 40\ndef answer():\n    return value + 2\n"

_GOOD_OUTCOMES = {
    "collected": 1,
    "deselected": 0,
    "passed": 1,
    "failed": 0,
    "skipped": 0,
    "errors": 0,
    "xfailed": 0,
}


def _outcomes(**overrides: int) -> RunOutcomes:
    return RunOutcomes(**{**_GOOD_OUTCOMES, **overrides})


def _provenance(commit: str | None = "b" * 40) -> CoverageProvenance:
    return CoverageProvenance(
        git_commit=commit,
        worktree_drift=(),
        source_digest="sha256:" + "1" * 64,
        interpreter="cpython-3.11.2",
    )


def _report(executed: int, executable: int, *, threshold: float = DEFAULT_THRESHOLD, **kw: object):
    fields: dict[str, object] = {
        "packs": (
            PackCoverage(pack="core", modules=(ModuleCoverage("core.py", executed, executable),)),
        ),
        "threshold": threshold,
        "outcomes": _outcomes(),
        "provenance": _provenance(),
    }
    fields.update(kw)
    return CoverageReport(**fields)  # type: ignore[arg-type]


@pytest.fixture
def probe(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A one-module pack root, its module (executable lines 1-3) and a target."""

    root = tmp_path / "packs"
    root.mkdir()
    module = root / "probe.py"
    module.write_text(PROBE_SOURCE, encoding="utf-8")
    target = tmp_path / "test_probe.py"
    target.write_text("def test_probe():\n    assert True\n", encoding="utf-8")
    return root, module, target


Artifact = Callable[[str], object]


def _is_trace_child(command: object) -> bool:
    return isinstance(command, list) and command[1:3] == ["-c", coverage._TRACE_RUNNER]


def _fake_child(
    monkeypatch: pytest.MonkeyPatch,
    artifact: Artifact | None,
    *,
    returncode: int = 0,
    raw: str | None = None,
    partial: bool = False,
) -> list[list[str]]:
    """Replace the trace child with one that writes a crafted artifact.

    *artifact* receives the run nonce and returns the JSON payload; *raw*
    writes literal text instead; *partial* leaves only the unfinished file.
    """

    calls: list[list[str]] = []
    real_run = subprocess.run

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if not _is_trace_child(command):
            return real_run(command, **kwargs)  # type: ignore[call-overload,no-any-return]
        calls.append(command)
        output, nonce = Path(command[3]), command[4]
        if partial:
            output.with_suffix(".partial").write_text('{"nonce": ', encoding="utf-8")
        elif raw is not None:
            output.write_text(raw, encoding="utf-8")
        elif artifact is not None:
            output.write_text(json.dumps(artifact(nonce)), encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr="child said no")

    monkeypatch.setattr(coverage.subprocess, "run", run)
    return calls


def _artifact(module: Path, rows: list[list[object]] | None = None, **overrides: object):
    def build(nonce: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "nonce": nonce,
            "pytest_exit_code": 0,
            "counts": rows if rows is not None else [[str(module), 1, 1], [str(module), 3, 1]],
            "outcomes": dict(_GOOD_OUTCOMES),
        }
        payload.update(overrides)
        return payload

    return build


def _forbid_child(monkeypatch: pytest.MonkeyPatch) -> None:
    real_run = subprocess.run

    def run(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if _is_trace_child(command):
            raise AssertionError("invalid requests must be refused before any pytest run")
        return real_run(command, **kwargs)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(coverage.subprocess, "run", run)


# ---------------------------------------------------------------------------
# denominator
# ---------------------------------------------------------------------------


def test_denominator_is_exactly_the_traceable_statement_lines(tmp_path: Path) -> None:
    source = tmp_path / "surface.py"
    source.write_text(
        '"""doc"""\n# comment\n\nx = 1\ndef f():\n    """inner doc"""\n    return x\n',
        encoding="utf-8",
    )
    assert executable_lines(source) == frozenset({1, 4, 5, 7})


def test_probe_denominator_matches_its_statements(probe: tuple[Path, Path, Path]) -> None:
    _root, module, _target = probe
    assert executable_lines(module) == frozenset({1, 2, 3})


def test_comment_and_blank_modules_are_dropped_from_the_surface(
    monkeypatch: pytest.MonkeyPatch, probe: tuple[Path, Path, Path]
) -> None:
    root, module, target = probe
    (root / "notes.py").write_text("# nothing executable\n", encoding="utf-8")
    (root / "blank.py").write_text("", encoding="utf-8")
    _fake_child(monkeypatch, _artifact(module))
    report = measure([str(target)], pack_root=root, threshold=0)
    assert [m.path for pack in report.packs for m in pack.modules] == ["probe.py"]
    assert report.executable == 3


def test_a_pack_without_any_executable_surface_is_named_not_silently_dropped(
    monkeypatch: pytest.MonkeyPatch, probe: tuple[Path, Path, Path]
) -> None:
    """A pack whose every module is comment-only must not vanish from the verdict.

    Dropping empty *modules* is correct; dropping a whole *pack* would remove it
    from the per-pack threshold check, so a hollowed-out pack would pass unseen.
    """

    root, module, target = probe
    hollow = root / "hollow"
    hollow.mkdir()
    (hollow / "__init__.py").write_text("", encoding="utf-8")
    (hollow / "notes.py").write_text("# nothing executable\n", encoding="utf-8")
    _fake_child(monkeypatch, _artifact(module))
    report = measure([str(target)], pack_root=root, threshold=0)
    issue = "pack 'hollow' has zero traceable executable lines"
    assert issue in report.measurement_issues
    assert report.verified is False
    assert f"measurement incomplete: {issue}" in coverage_failures(report)


# ---------------------------------------------------------------------------
# numerator
# ---------------------------------------------------------------------------


def test_numerator_counts_only_lines_of_the_executable_surface(
    monkeypatch: pytest.MonkeyPatch, probe: tuple[Path, Path, Path]
) -> None:
    root, module, target = probe
    rows = [[str(module), 1, 1], [str(module), 99, 7], [str(root / "other.py"), 2, 1]]
    _fake_child(monkeypatch, _artifact(module, rows))
    report = measure([str(target)], pack_root=root, threshold=0)
    (entry,) = report.packs[0].modules
    assert (entry.executed, entry.executable, entry.missing) == (1, 3, (2, 3))
    assert report.percent == 33.33


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([["MODULE", 1, -1]], "malformed line counts"),
        ([["MODULE", 0, 1]], "malformed line counts"),
        ([["MODULE", -3, 1]], "malformed line counts"),
        ([["MODULE", True, 1]], "malformed line counts"),
        ([["MODULE", 1, 1.5]], "malformed line counts"),
        ([["MODULE", 1]], "malformed line counts"),
        ([[7, 1, 1]], "malformed line counts"),
        ([["MODULE", 1, 1], ["MODULE", 1, 4]], "duplicate line counts"),
    ],
)
def test_forged_or_duplicate_line_events_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    probe: tuple[Path, Path, Path],
    rows: list[list[object]],
    message: str,
) -> None:
    root, module, target = probe
    concrete = [[str(module) if cell == "MODULE" else cell for cell in row] for row in rows]
    _fake_child(monkeypatch, _artifact(module, concrete))
    with pytest.raises(RuntimeError, match=message):
        measure([str(target)], pack_root=root, threshold=0)


@pytest.mark.parametrize(
    ("executed", "executable"),
    [(2, 1), (-1, 1), (1, -1), (True, 1), (1, 1.0)],
)
def test_module_counts_must_be_consistent_integers(executed: object, executable: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        ModuleCoverage("m.py", executed, executable)  # type: ignore[arg-type]


def test_duplicate_modules_and_packs_are_rejected() -> None:
    module = ModuleCoverage("m.py", 1, 1)
    with pytest.raises(ValueError, match="duplicate module"):
        PackCoverage(pack="core", modules=(module, module))
    pack = PackCoverage(pack="core", modules=(module,))
    with pytest.raises(ValueError, match="duplicate pack"):
        CoverageReport(packs=(pack, pack))


# ---------------------------------------------------------------------------
# threshold
# ---------------------------------------------------------------------------


def test_the_contract_is_ninety_five_percent_per_pack() -> None:
    assert ACCEPTANCE_THRESHOLD == 95.0
    assert DEFAULT_THRESHOLD == ACCEPTANCE_THRESHOLD


def test_threshold_boundary_is_inclusive_and_exact() -> None:
    assert _report(95, 100).accepted is True
    below = _report(9499, 10000)
    assert below.accepted is False
    assert any("94.99% < 95.00%" in failure for failure in coverage_failures(below))


def test_a_lowered_threshold_is_diagnostic_and_never_accepted() -> None:
    report = _report(100, 100, threshold=85.0)
    assert report.verified is True
    assert report.accepted is False
    assert any("below the 95.00% acceptance contract" in f for f in coverage_failures(report))
    assert report.as_dict()["accepted"] is False


def test_a_stricter_threshold_is_honoured() -> None:
    assert _report(96, 100, threshold=97.0).accepted is False
    assert _report(97, 100, threshold=97.0).accepted is True


@pytest.mark.parametrize(
    "threshold", [float("nan"), float("inf"), float("-inf"), -1.0, True, False, "95", None]
)
def test_invalid_thresholds_are_refused_everywhere(
    monkeypatch: pytest.MonkeyPatch, probe: tuple[Path, Path, Path], threshold: object
) -> None:
    root, _module, target = probe
    _forbid_child(monkeypatch)
    with pytest.raises((TypeError, ValueError)):
        CoverageReport(packs=(), threshold=threshold)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        measure([str(target)], pack_root=root, threshold=threshold)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# verification gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "failure"),
    [
        ({"outcomes": None}, "measurement carries no test outcomes"),
        ({"provenance": None}, "measurement is not bound to a commit"),
        ({"provenance": _provenance(None)}, "measurement is not bound to a commit"),
        ({"pytest_exit_code": 5}, "pytest exited with 5"),
        ({"measurement_issues": ("partial",)}, "measurement incomplete: partial"),
    ],
)
def test_missing_evidence_is_unverified_even_with_perfect_numbers(
    overrides: dict[str, object], failure: str
) -> None:
    report = _report(100, 100, **overrides)
    assert report.verified is False
    assert report.accepted is False
    assert failure in coverage_failures(report)
    rows = format_table(report).splitlines()
    assert not any(row.endswith(" OK") for row in rows)
    assert rows[-1].startswith("verdict: NOT ACCEPTED")


def test_empty_surfaces_are_never_accepted() -> None:
    assert _report(0, 0).accepted is False
    assert (
        CoverageReport(packs=(), outcomes=_outcomes(), provenance=_provenance()).verified is False
    )


def test_accepted_report_renders_ok_everywhere() -> None:
    report = _report(100, 100)
    assert report.accepted is True
    rows = format_table(report).splitlines()
    assert any(row.startswith("TOTAL") and row.endswith("OK") for row in rows)
    assert rows[-1] == "verdict: ACCEPTED"


@pytest.mark.parametrize(
    ("commit", "digest"),
    [("HEAD", "sha256:" + "1" * 64), ("a" * 12, "sha256:" + "1" * 64), ("a" * 40, "md5:00")],
)
def test_provenance_requires_full_commit_ids_and_sha256_digests(commit: str, digest: str) -> None:
    with pytest.raises(ValueError):
        CoverageProvenance(
            git_commit=commit, worktree_drift=(), source_digest=digest, interpreter="cpython"
        )


# ---------------------------------------------------------------------------
# target integrity
# ---------------------------------------------------------------------------


def test_canonical_mapping_is_exactly_the_runtime_composition() -> None:
    manifests = {
        entry.name
        for entry in DEFAULT_PACK_ROOT.iterdir()
        if entry.is_dir() and (entry / PACK_MANIFEST).is_file()
    }
    assert set(PACK_TEST_TARGETS) == set(COMPOSITION_BY_DIRECTORY) == manifests
    groups = {
        path.relative_to(DEFAULT_PACK_ROOT).parts[0] for path in DEFAULT_PACK_ROOT.glob("*/*.py")
    }
    assert pack_mapping_issues(DEFAULT_PACK_ROOT, groups | {"core"}) == ()


def test_every_canonical_target_exists_is_unique_and_exercises_its_pack() -> None:
    assert len(DEFAULT_TEST_TARGETS) == len(set(DEFAULT_TEST_TARGETS))
    assert set(DEFAULT_TEST_TARGETS) == set(SUBSTRATE_TEST_TARGETS) | {
        target for targets in PACK_TEST_TARGETS.values() for target in targets
    }
    for pack, targets in PACK_TEST_TARGETS.items():
        assert targets, pack
        for target in targets:
            text = (REPO_ROOT / target).read_text(encoding="utf-8")
            assert f"creative.packs.{pack}" in text or f"packs/{pack}" in text, (pack, target)


def test_no_pack_test_module_is_left_out_of_the_evidence() -> None:
    assert pack_test_import_issues() == ()
    assert set(HOST_LAYER_PACK_IMPORTERS).isdisjoint(DEFAULT_TEST_TARGETS)


def _substrate_imports(test_file: Path, substrate: frozenset[str]) -> set[str]:
    """Substrate modules (files directly under ``creative/packs/``) *test_file* imports."""
    package = "nexus_ai_agent.creative.packs"
    found: set[str] = set()
    for node in ast.walk(ast.parse(test_file.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == package:
                found.update(alias.name for alias in node.names)
            elif node.module.startswith(package + "."):
                found.add(node.module.split(".")[3])
        elif isinstance(node, ast.Import):
            found.update(
                alias.name.split(".")[3]
                for alias in node.names
                if alias.name.startswith(package + ".")
            )
    return found & substrate


def test_every_tested_substrate_module_is_exercised_by_the_canonical_targets() -> None:
    # The core group is measured from the canonical targets only.  A substrate
    # module whose tests live outside them is measured as unexercised: PR #101
    # added creative/packs/trust.py + ed25519.py with their contract suite
    # outside SUBSTRATE_TEST_TARGETS and core fell to 79.26% after the merge.
    substrate = frozenset(
        path.stem for path in DEFAULT_PACK_ROOT.glob("*.py") if path.stem != "__init__"
    )
    tested: set[str] = set()
    for test_file in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        tested |= _substrate_imports(test_file, substrate)
    canonical: set[str] = set()
    for target in DEFAULT_TEST_TARGETS:
        canonical |= _substrate_imports(REPO_ROOT / target, substrate)
    assert tested, "the scan must see the substrate imports of the suite"
    assert sorted(tested - canonical) == []


def test_an_orphaned_pack_test_makes_the_canonical_measurement_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphan = "test module tests/unit/test_new.py imports a pack but is neither ..."
    monkeypatch.setattr(coverage, "pack_test_import_issues", lambda: (orphan,))
    _fake_child(monkeypatch, _artifact(DEFAULT_PACK_ROOT / "runtime.py"))
    report = measure(list(DEFAULT_TEST_TARGETS), threshold=0)
    assert orphan in report.measurement_issues
    assert report.verified is False


def test_import_classification_names_orphans_stale_and_conflicting_entries(
    tmp_path: Path,
) -> None:
    tests = tmp_path / "tests"
    (tests / "unit").mkdir(parents=True)
    importer = "from nexus_ai_agent.creative.packs.alpha.models import Thing\n"
    (tests / "unit" / "test_mapped.py").write_text(importer, encoding="utf-8")
    (tests / "unit" / "test_orphan.py").write_text(
        "from nexus_ai_agent.creative.packs import alpha\n", encoding="utf-8"
    )
    (tests / "unit" / "test_text_only.py").write_text(
        '"""mentions nexus_ai_agent.creative.packs.alpha in prose only"""\n', encoding="utf-8"
    )
    (tests / "unit" / "test_host.py").write_text(
        "import nexus_ai_agent.creative.packs.alpha.ops\n", encoding="utf-8"
    )
    issues = pack_test_import_issues(
        tests,
        targets=("tests/unit/test_mapped.py", "tests/unit/test_host.py"),
        host_layer={"tests/unit/test_host.py": "host", "tests/unit/test_text_only.py": "gone"},
        packs=("alpha",),
    )
    assert issues == (
        "test module tests/unit/test_orphan.py imports a pack but is neither a canonical "
        "target nor host-layer",
        "stale host-layer classification: tests/unit/test_text_only.py does not import a pack",
        "tests/unit/test_host.py is both a canonical target and host-layer evidence",
    )


def test_mapping_issues_name_unmapped_stale_and_manifestless_packs(tmp_path: Path) -> None:
    root = tmp_path / "packs"
    for name, manifest in (("alpha", True), ("beta", True), ("gamma", False)):
        (root / name).mkdir(parents=True)
        (root / name / "ops.py").write_text("x = 1\n", encoding="utf-8")
        if manifest:
            (root / name / PACK_MANIFEST).write_text("{}", encoding="utf-8")
    issues = pack_mapping_issues(
        root, {"core", "alpha", "beta", "gamma"}, {"alpha": ("t.py",), "omega": ("t.py",)}
    )
    assert "shipped pack 'beta' has no canonical test target" in issues
    assert "shipped pack 'gamma' has no canonical test target" in issues
    assert "stale canonical target mapping: pack 'omega' is not shipped" in issues
    assert f"pack directory 'gamma' has Python modules but no {PACK_MANIFEST}" in issues
    assert pack_mapping_issues(root, {"alpha", "beta"}, {"alpha": ("t",), "beta": ("t",)}) == ()


def test_missing_and_respelled_duplicate_targets_are_refused(
    monkeypatch: pytest.MonkeyPatch, probe: tuple[Path, Path, Path]
) -> None:
    root, _module, target = probe
    _forbid_child(monkeypatch)
    with pytest.raises(ValueError, match="does not exist"):
        measure([str(target.parent / "test_absent.py")], pack_root=root)
    relative = "tests/unit/test_pack_coverage_harness.py"
    with pytest.raises(ValueError, match="duplicate pytest target"):
        measure([relative, str(REPO_ROOT / relative)], pack_root=root)
    with pytest.raises(TypeError):
        measure(relative, pack_root=root)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="duplicate pack selection"):
        measure([relative], packs=["core", "core"])


def test_canonical_root_with_partial_targets_or_packs_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = DEFAULT_PACK_ROOT / "runtime.py"
    _fake_child(monkeypatch, _artifact(module))
    report = measure(list(DEFAULT_TEST_TARGETS[:2]), packs=["core"], threshold=0)
    failures = coverage_failures(report)
    assert any("partial test target set: ran 2 of" in f for f in failures)
    assert any("partial pack selection" in f for f in failures)
    assert report.accepted is False


# ---------------------------------------------------------------------------
# process and trace-artifact integrity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ({"artifact": None}, "no valid evidence artifact"),
        ({"artifact": None, "raw": "{not json"}, "no valid evidence artifact"),
        ({"artifact": None, "partial": True}, "no valid evidence artifact"),
        ({"artifact": None, "raw": "[]"}, "invalid evidence artifact"),
        ({"returncode": 3}, "process exit 3"),
    ],
)
def test_broken_trace_children_raise_instead_of_reporting(
    monkeypatch: pytest.MonkeyPatch,
    probe: tuple[Path, Path, Path],
    setup: dict[str, object],
    message: str,
) -> None:
    root, module, target = probe
    options = {"artifact": _artifact(module), **setup}
    _fake_child(monkeypatch, **options)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match=message):
        measure([str(target)], pack_root=root, threshold=0)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"nonce": "0" * 32}, "does not belong to this run"),
        ({"extra": 1}, "invalid evidence artifact"),
        ({"pytest_exit_code": "0"}, "invalid evidence artifact"),
        ({"counts": {}}, "invalid evidence artifact"),
        ({"outcomes": {"passed": 1}}, "malformed test outcomes"),
        ({"outcomes": {**_GOOD_OUTCOMES, "failed": -1}}, "malformed test outcomes"),
        ({"outcomes": {**_GOOD_OUTCOMES, "passed": True}}, "malformed test outcomes"),
    ],
)
def test_foreign_or_malformed_trace_artifacts_raise(
    monkeypatch: pytest.MonkeyPatch,
    probe: tuple[Path, Path, Path],
    overrides: dict[str, object],
    message: str,
) -> None:
    root, module, target = probe
    _fake_child(monkeypatch, _artifact(module, **overrides))
    with pytest.raises(RuntimeError, match=message):
        measure([str(target)], pack_root=root, threshold=0)


@pytest.mark.parametrize(
    ("outcomes", "exit_code", "issue"),
    [
        ({"deselected": 2}, 0, "deselected 2 test(s)"),
        ({"failed": 1}, 0, "contradicts 1 failed"),
        ({"errors": 1}, 0, "contradicts 0 failed and 1 errored"),
        ({"collected": 0, "passed": 0}, 5, "pytest collected no tests"),
        ({"passed": 0, "skipped": 1}, 0, "no test passed"),
    ],
)
def test_run_outcomes_that_are_not_a_full_green_run_are_issues(
    monkeypatch: pytest.MonkeyPatch,
    probe: tuple[Path, Path, Path],
    outcomes: dict[str, int],
    exit_code: int,
    issue: str,
) -> None:
    root, module, target = probe
    artifact = _artifact(
        module, outcomes={**_GOOD_OUTCOMES, **outcomes}, pytest_exit_code=exit_code
    )
    _fake_child(monkeypatch, artifact)
    report = measure([str(target)], pack_root=root, threshold=0)
    assert any(issue in entry for entry in report.measurement_issues), report.measurement_issues
    assert report.verified is False


def test_collection_failure_is_recorded_with_zero_collected(
    monkeypatch: pytest.MonkeyPatch, probe: tuple[Path, Path, Path]
) -> None:
    root, module, target = probe
    outcomes = {**_GOOD_OUTCOMES, "collected": None, "passed": 0}
    _fake_child(monkeypatch, _artifact(module, outcomes=outcomes, pytest_exit_code=2))
    report = measure([str(target)], pack_root=root, threshold=0)
    assert report.outcomes is not None and report.outcomes.collected == 0
    assert "pytest exited with 2" in coverage_failures(report)


def test_the_trace_child_is_hermetic(
    monkeypatch: pytest.MonkeyPatch, probe: tuple[Path, Path, Path]
) -> None:
    root, module, target = probe
    seen: dict[str, object] = {}
    real_run = subprocess.run

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if not _is_trace_child(command):
            return real_run(command, **kwargs)  # type: ignore[call-overload,no-any-return]
        seen.update(kwargs)
        seen["args"] = json.loads(command[5])
        Path(command[3]).write_text(json.dumps(_artifact(module)(command[4])), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing_matches")
    monkeypatch.setattr(coverage.subprocess, "run", run)
    measure([str(target)], pack_root=root, threshold=0)
    environment = seen["env"]
    assert isinstance(environment, dict)
    assert "PYTEST_ADDOPTS" not in environment
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert environment["PYTHONPATH"].split(":")[0] == str(REPO_ROOT / "src")
    args = seen["args"]
    assert isinstance(args, list)
    assert args[args.index("-c") + 1] == str(REPO_ROOT / "pyproject.toml")
    assert args[-1] == str(target)


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def test_uncommitted_evidence_changes_block_acceptance(
    monkeypatch: pytest.MonkeyPatch, probe: tuple[Path, Path, Path]
) -> None:
    root, module, target = probe
    _fake_child(monkeypatch, _artifact(module))
    monkeypatch.setattr(
        coverage.provenance, "working_tree_drift", lambda *_a, **_k: ("?? src/forged.py",)
    )
    report = measure([str(target)], pack_root=root, threshold=0)
    assert any("working tree differs" in issue for issue in report.measurement_issues)
    assert report.provenance is not None
    assert report.provenance.worktree_drift == ("?? src/forged.py",)


def test_evidence_without_git_is_unbound(
    monkeypatch: pytest.MonkeyPatch, probe: tuple[Path, Path, Path]
) -> None:
    root, module, target = probe
    _fake_child(monkeypatch, _artifact(module))

    def no_git(_root: Path) -> str:
        raise RuntimeError("git executable absent")

    monkeypatch.setattr(coverage.provenance, "current_commit", no_git)
    report = measure([str(target)], pack_root=root, threshold=0)
    assert report.provenance is not None and report.provenance.git_commit is None
    assert "measurement is not bound to a commit" in coverage_failures(report)


def test_source_digest_changes_with_measured_bytes(
    monkeypatch: pytest.MonkeyPatch, probe: tuple[Path, Path, Path]
) -> None:
    root, module, target = probe
    _fake_child(monkeypatch, _artifact(module))
    first = measure([str(target)], pack_root=root, threshold=0)
    module.write_text(PROBE_SOURCE + "# edited\n", encoding="utf-8")
    second = measure([str(target)], pack_root=root, threshold=0)
    assert first.provenance is not None and second.provenance is not None
    assert first.provenance.source_digest != second.provenance.source_digest


# ---------------------------------------------------------------------------
# determinism and artifact reuse
# ---------------------------------------------------------------------------


def test_identical_inputs_give_identical_canonical_bytes(
    monkeypatch: pytest.MonkeyPatch, probe: tuple[Path, Path, Path]
) -> None:
    root, module, target = probe
    _fake_child(monkeypatch, _artifact(module))
    first = canonical_json(measure([str(target)], pack_root=root, threshold=0))
    second = canonical_json(measure([str(target)], pack_root=root, threshold=0))
    assert first == second
    assert first.endswith("}\n") and json.loads(first)["schema"] == "nexus.pack-coverage/2"


def test_pack_order_does_not_change_the_artifact() -> None:
    alpha = PackCoverage(pack="alpha", modules=(ModuleCoverage("a.py", 1, 1),))
    beta = PackCoverage(pack="beta", modules=(ModuleCoverage("b.py", 1, 1),))
    forward = _report(1, 1, packs=(alpha, beta))
    backward = _report(1, 1, packs=(beta, alpha))
    assert canonical_json(forward) == canonical_json(backward)
    assert [pack["pack"] for pack in forward.as_dict()["packs"]] == ["alpha", "beta"]  # type: ignore[index]


def test_real_trace_child_reports_what_pytest_actually_did(
    probe: tuple[Path, Path, Path],
) -> None:
    root, _module, target = probe
    target.write_text(
        "import pytest, runpy\n"
        f"MODULE = {str(root / 'probe.py')!r}\n"
        "def test_pass():\n    assert runpy.run_path(MODULE)['answer']() == 42\n"
        "def test_skip():\n    pytest.skip('not here')\n"
        "@pytest.mark.xfail(strict=True)\ndef test_xfail():\n    assert False\n"
        "def test_fail():\n    assert False\n",
        encoding="utf-8",
    )
    report = measure([str(target)], pack_root=root, threshold=0)
    assert report.outcomes == RunOutcomes(
        collected=4, deselected=0, passed=1, failed=1, skipped=1, errors=0, xfailed=1
    )
    assert report.pytest_exit_code == 1
    assert report.executed == 3  # the child really traced the probe module


def test_real_trace_child_counts_deselection_by_a_conftest(
    probe: tuple[Path, Path, Path],
) -> None:
    root, _module, target = probe
    target.write_text(
        "def test_kept():\n    assert True\ndef test_dropped():\n    assert True\n",
        encoding="utf-8",
    )
    (target.parent / "conftest.py").write_text(
        "def pytest_collection_modifyitems(config, items):\n"
        "    dropped = [item for item in items if item.name == 'test_dropped']\n"
        "    items[:] = [item for item in items if item.name != 'test_dropped']\n"
        "    config.hook.pytest_deselected(items=dropped)\n",
        encoding="utf-8",
    )
    report = measure([str(target)], pack_root=root, threshold=0)
    assert report.outcomes is not None
    assert (report.outcomes.collected, report.outcomes.deselected) == (1, 1)
    assert any("deselected 1 test(s)" in issue for issue in report.measurement_issues)


def _stub_measure(monkeypatch: pytest.MonkeyPatch, report: CoverageReport) -> list[float]:
    calls: list[float] = []

    def fake(tests: object = None, **kwargs: float) -> CoverageReport:
        assert tests is None and set(kwargs) == {"threshold"}  # always canonical
        calls.append(kwargs["threshold"])
        return report

    monkeypatch.setattr(coverage, "measure", fake)
    return calls


def test_verify_artifact_accepts_only_a_byte_identical_reproduction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _report(100, 100)
    calls = _stub_measure(monkeypatch, fresh)
    raw = canonical_json(fresh).encode("utf-8")
    assert verify_artifact(raw) == []
    assert calls == [95.0]

    forged = json.loads(raw)
    forged["total"]["percent"] = 99.0
    problems = verify_artifact(coverage_bytes(forged))
    assert problems and "differs in: total.percent" in problems[0]

    respaced = json.dumps(json.loads(raw), indent=4, sort_keys=True).encode("utf-8")
    assert verify_artifact(respaced) == ["artifact bytes are not canonical"]


def test_verify_artifact_rejects_a_reproduced_but_unaccepted_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _report(90, 100)
    _stub_measure(monkeypatch, fresh)
    problems = verify_artifact(canonical_json(fresh).encode("utf-8"))
    assert problems
    assert all(problem.startswith("fresh measurement not accepted") for problem in problems)


def coverage_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        (b"{", "artifact unreadable"),
        (b"\xff", "artifact unreadable"),
        (b"[]", "root must be a JSON object"),
        (b'{"schema": "nexus.pack-coverage/2", "schema": "x"}', "duplicate key"),
        (b'{"schema": "nexus.pack-coverage/2", "threshold": NaN}', "non-finite"),
        (b'{"schema": "nexus.pack-coverage/1", "threshold": 95.0}', "is not"),
        (b'{"schema": "nexus.pack-coverage/2", "threshold": true}', "threshold invalid"),
        (b'{"schema": "nexus.pack-coverage/2", "threshold": -1}', "threshold invalid"),
    ],
)
def test_verify_artifact_rejects_malformed_artifacts_without_measuring(
    monkeypatch: pytest.MonkeyPatch, raw: bytes, problem: str
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> CoverageReport:
        raise AssertionError("malformed artifacts must be refused before measuring")

    monkeypatch.setattr(coverage, "measure", refuse)
    (found,) = verify_artifact(raw)
    assert problem in found


def test_verify_artifact_reports_an_unavailable_re_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_args: object, **_kwargs: object) -> CoverageReport:
        raise RuntimeError("tracer crashed")

    monkeypatch.setattr(coverage, "measure", broken)
    raw = canonical_json(_report(100, 100)).encode("utf-8")
    assert verify_artifact(raw) == ["re-measurement failed: tracer crashed"]


# ---------------------------------------------------------------------------
# the script: exit codes and stale artifacts
# ---------------------------------------------------------------------------


def _script(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        timeout=600,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ("--threshold", "nan"),
        ("--threshold", "-5"),
        ("--pack", "no-such-pack"),
        ("--tests", "tests/unit/test_absent_module.py"),
        (
            "--tests",
            "tests/unit/test_slideshow_invariants.py",
            "tests/unit/test_slideshow_invariants.py",
        ),
    ],
)
def test_script_invalid_requests_exit_two_and_remove_stale_artifacts(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    stale = tmp_path / "coverage.json"
    stale.write_text('{"accepted": true}\n', encoding="utf-8")
    result = _script(*arguments, "--json-out", str(stale), "--quiet")
    assert result.returncode == 2, result.stderr
    assert "invalid measurement request" in result.stderr
    assert not stale.exists()


def test_script_defaults_to_the_contract_threshold() -> None:
    result = _script(
        "--tests",
        "tests/unit/test_pack_runtime_composition.py",
        "--pack",
        "core",
        "--json",
        "--quiet",
    )
    assert result.returncode == 1  # a focused run is diagnostic, never accepted
    payload = json.loads(result.stdout)
    assert payload["threshold"] == ACCEPTANCE_THRESHOLD
    assert payload["accepted"] is False


@pytest.mark.parametrize("content", [None, b"{", b'{"schema": "nexus.pack-coverage/1"}'])
def test_script_verify_artifact_fails_closed(tmp_path: Path, content: bytes | None) -> None:
    artifact = tmp_path / "coverage.json"
    if content is not None:
        artifact.write_bytes(content)
    result = _script("--verify-artifact", str(artifact))
    assert result.returncode == 1
    assert "✗" in result.stderr


def test_script_verify_artifact_refuses_diagnostic_options(tmp_path: Path) -> None:
    result = _script("--verify-artifact", str(tmp_path / "a.json"), "--pack", "core")
    assert result.returncode == 2
    assert "re-measures canonically" in result.stderr
