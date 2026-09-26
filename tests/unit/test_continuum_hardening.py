"""Adversarial contract tests for Continuum evidence and snapshot truth.

The tests intentionally attack the measurer and the snapshot rather than a
business feature.  A green result must require real evidence, not merely a
well-shaped percentage or JSON document.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import nexus_ai_agent.continuum.pack_coverage as coverage
import nexus_ai_agent.continuum.snapshot as snapshot
from nexus_ai_agent.continuum.pack_coverage import (
    CoverageReport,
    ModuleCoverage,
    PackCoverage,
    coverage_failures,
    format_table,
    measure,
)
from nexus_ai_agent.continuum.snapshot import ContinuumSnapshot, EnvFingerprint


def _module(path: str = "core.py", executed: int = 1, executable: int = 1) -> ModuleCoverage:
    return ModuleCoverage(path=path, executed=executed, executable=executable)


def _snapshot(test_count: int = 3) -> ContinuumSnapshot:
    return ContinuumSnapshot(
        schema_version=2,
        plan="forensic",
        step="recorded-good-commit",
        next="verify",
        ledger=[{"status": "complete", "id": "C1"}],
        test_count_expected=test_count,
        env_fingerprint=EnvFingerprint("3.11.2", "1.20.0", "2.0.54"),
    )


def _write_probe(tmp_path: Path) -> tuple[Path, Path]:
    """Make a pack module and a pytest target that executes it in a child."""

    root = tmp_path / "packs"
    root.mkdir()
    module = root / "probe.py"
    module.write_text(
        "value = 40\ndef answer():\n    return value + 2\n",
        encoding="utf-8",
    )
    target = tmp_path / "test_probe.py"
    target.write_text(
        "import runpy\n"
        f"MODULE = {str(module)!r}\n"
        "def test_probe_runs_real_pack_source():\n"
        "    namespace = runpy.run_path(MODULE)\n"
        "    assert namespace['answer']() == 42\n",
        encoding="utf-8",
    )
    return root, target


# ---------------------------------------------------------------------------
# Coverage: invalid surfaces and fake numerators must never be green.
# ---------------------------------------------------------------------------


def test_measure_rejects_invalid_unknown_empty_and_zero_line_surfaces(tmp_path: Path) -> None:
    target = "tests/unit/test_pack_coverage_harness.py"
    with pytest.raises(ValueError, match="pack root does not exist"):
        measure([target], pack_root=tmp_path / "missing")

    root = tmp_path / "packs"
    root.mkdir()
    (root / "module.py").write_text("value = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown pack selection"):
        measure([target], pack_root=root, packs=["missing-pack"])

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    (empty_root / "only_comments.py").write_text("# no executable source\n", encoding="utf-8")
    with pytest.raises(ValueError, match="zero traceable executable lines"):
        measure([target], pack_root=empty_root)


def test_measure_rejects_empty_or_duplicate_targets_before_running_pytest(tmp_path: Path) -> None:
    root = tmp_path / "packs"
    root.mkdir()
    (root / "module.py").write_text("value = 1\n", encoding="utf-8")
    target = "tests/unit/test_pack_coverage_harness.py"
    with pytest.raises(ValueError, match="empty target set"):
        measure([], pack_root=root)
    with pytest.raises(ValueError, match="duplicate pytest target"):
        measure([target, target], pack_root=root)


def test_scoped_measurement_is_repeatable_but_cannot_claim_green(tmp_path: Path) -> None:
    root, target = _write_probe(tmp_path)
    first = measure([str(target)], pack_root=root, threshold=0)
    second = measure([str(target)], pack_root=root, threshold=0)

    assert first.as_dict() == second.as_dict()
    assert first.pytest_exit_code == 0
    assert first.executed > 0
    assert first.verified is False
    failures = coverage_failures(first)
    assert any("partial test target set" in failure for failure in failures)
    assert any("non-canonical pack root" in failure for failure in failures)
    assert "UNVERIFIED" in format_table(first)


def test_measure_records_a_real_pytest_failure_as_unverified_evidence(tmp_path: Path) -> None:
    root, target = _write_probe(tmp_path)
    target.write_text("def test_intentional_failure():\n    assert False\n", encoding="utf-8")

    report = measure([str(target)], pack_root=root, threshold=0)

    assert report.pytest_exit_code == 1
    assert report.verified is False
    assert "pytest exited with 1" in coverage_failures(report)
    assert "UNVERIFIED" in format_table(report)


def test_measure_fails_observably_when_the_trace_subprocess_breaks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "packs"
    root.mkdir()
    (root / "module.py").write_text("value = 1\n", encoding="utf-8")

    def broken_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 23, stdout="", stderr="broken tracer")

    monkeypatch.setattr(coverage.subprocess, "run", broken_run)
    with pytest.raises(RuntimeError, match="process exit 23"):
        measure(["tests/unit/test_pack_coverage_harness.py"], pack_root=root)


def test_fake_numerator_nonfinite_threshold_and_pytest_failure_are_rejected() -> None:
    with pytest.raises(ValueError, match="executed <= executable"):
        _module(executed=2, executable=1)
    with pytest.raises(ValueError, match="finite"):
        CoverageReport(packs=(), threshold=float("nan"))

    failed = CoverageReport(
        packs=(PackCoverage(pack="core", modules=(_module(),)),),
        pytest_exit_code=1,
    )
    assert failed.verified is False
    assert "pytest exited with 1" in coverage_failures(failed)
    assert "UNVERIFIED" in format_table(failed)


# ---------------------------------------------------------------------------
# Snapshots: schema and publication failures are explicit and recoverable.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("{", "invalid JSON"),
        (json.dumps({"schema_version": 2}), "invalid keys"),
        (
            json.dumps(
                {
                    **json.loads(_snapshot().to_json()),
                    "forged": True,
                }
            ),
            "unexpected keys",
        ),
        (
            json.dumps(
                {
                    **json.loads(_snapshot().to_json()),
                    "env_fingerprint": {"python": "3.11.2", "alembic": 1, "sqlalchemy": "2"},
                }
            ),
            "must be a non-empty string",
        ),
    ],
)
def test_read_snapshot_rejects_corruption_partial_schema_drift_and_invalid_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: str, message: str
) -> None:
    path = tmp_path / "continuum.json"
    path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(snapshot, "SNAPSHOT_PATH", path)
    with pytest.raises(ValueError, match=message):
        snapshot.read_snapshot()


def test_verify_snapshot_reports_missing_file_as_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(snapshot, "SNAPSHOT_PATH", tmp_path / "missing.json")

    assert snapshot.verify_snapshot()[0].startswith("snapshot unreadable")


def test_snapshot_json_is_canonical_and_atomic_write_preserves_prior_file_on_interruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "continuum.json"
    path.write_text('{"old": true}\n', encoding="utf-8")
    monkeypatch.setattr(snapshot, "SNAPSHOT_PATH", path)
    value = _snapshot()

    assert value.to_json() == value.to_json()
    assert list(json.loads(value.to_json())["ledger"][0]) == ["id", "status"]

    def interrupted_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated interrupted replacement")

    monkeypatch.setattr(snapshot.os, "replace", interrupted_replace)
    with pytest.raises(OSError, match="simulated interrupted replacement"):
        snapshot.write_snapshot(value)
    assert path.read_text(encoding="utf-8") == '{"old": true}\n'
    assert list(tmp_path.glob(".continuum.json.*.tmp")) == []


def test_verify_snapshot_distinguishes_non_ancestor_git_failure_count_and_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _snapshot(test_count=3)
    monkeypatch.setattr(snapshot, "read_snapshot", lambda: value)
    monkeypatch.setattr(snapshot, "current_commit", lambda: "current-head")
    monkeypatch.setattr(snapshot, "_is_ancestor", lambda _old, _head: False)
    monkeypatch.setattr(snapshot, "_test_case_count", lambda: 4)
    monkeypatch.setattr(
        snapshot,
        "_environment",
        lambda: EnvFingerprint("3.12.0", "1.20.0", "2.0.54"),
    )

    problems = snapshot.verify_snapshot()
    assert any(problem.startswith("state loss detected") for problem in problems)
    assert "test count mismatch: expected 3, found 4" in problems
    assert any(problem.startswith("environment fingerprint mismatch") for problem in problems)


def test_verify_snapshot_rejects_a_valid_snapshot_when_the_checkout_is_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prior committed snapshot cannot vouch for modified source files."""

    value = _snapshot(test_count=3)
    monkeypatch.setattr(snapshot, "read_snapshot", lambda: value)
    monkeypatch.setattr(snapshot, "current_commit", lambda: "current-head")
    monkeypatch.setattr(snapshot, "_is_ancestor", lambda _old, _head: True)
    monkeypatch.setattr(snapshot, "_working_tree_clean", lambda: False)
    monkeypatch.setattr(snapshot, "_test_case_count", lambda: 3)
    monkeypatch.setattr(snapshot, "_environment", lambda: value.env_fingerprint)

    assert snapshot.verify_snapshot() == [
        "working tree drift detected: snapshot verification requires a clean checkout"
    ]


def test_verify_snapshot_fails_closed_when_worktree_provenance_cannot_be_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _snapshot(test_count=3)
    monkeypatch.setattr(snapshot, "read_snapshot", lambda: value)
    monkeypatch.setattr(snapshot, "current_commit", lambda: "current-head")
    monkeypatch.setattr(snapshot, "_is_ancestor", lambda _old, _head: True)

    def unavailable_worktree() -> bool:
        raise RuntimeError("git status is unavailable")

    monkeypatch.setattr(snapshot, "_working_tree_clean", unavailable_worktree)
    monkeypatch.setattr(snapshot, "_test_case_count", lambda: 3)
    monkeypatch.setattr(snapshot, "_environment", lambda: value.env_fingerprint)

    assert snapshot.verify_snapshot() == ["git verification unavailable: git status is unavailable"]


def test_verify_snapshot_reports_unavailable_git_and_test_discovery_without_false_state_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _snapshot(test_count=3)
    monkeypatch.setattr(snapshot, "read_snapshot", lambda: value)

    def unavailable_git() -> str:
        raise RuntimeError("git executable absent")

    def unavailable_tests() -> int:
        raise RuntimeError("pytest collection subprocess failed")

    monkeypatch.setattr(snapshot, "current_commit", unavailable_git)
    monkeypatch.setattr(snapshot, "_test_case_count", unavailable_tests)
    monkeypatch.setattr(snapshot, "_environment", lambda: value.env_fingerprint)

    problems = snapshot.verify_snapshot()
    assert any(problem.startswith("git verification unavailable") for problem in problems)
    assert any(problem.startswith("test discovery unavailable") for problem in problems)
    assert not any(problem.startswith("state loss detected") for problem in problems)


def test_test_count_reader_accepts_only_a_successful_integer_collection_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def collection_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(command[3]).write_text('{"count": 7, "exit_code": 0}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(snapshot.subprocess, "run", collection_run)
    assert snapshot._test_case_count() == 7

    def malformed_collection(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        Path(command[3]).write_text('{"count": true, "exit_code": 0}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(snapshot.subprocess, "run", malformed_collection)
    with pytest.raises(RuntimeError, match="invalid test count"):
        snapshot._test_case_count()


def test_test_count_reports_missing_collection_dependency_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_collection(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        Path(command[3]).write_text('{"count": null, "exit_code": 2}', encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="ModuleNotFoundError: No module named 'missing_dependency'",
            stderr="",
        )

    monkeypatch.setattr(snapshot.subprocess, "run", failed_collection)
    with pytest.raises(RuntimeError, match="missing import dependencies: missing_dependency"):
        snapshot._test_case_count()
