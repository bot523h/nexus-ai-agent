"""Snapshot threat model, attacked against real Git repositories.

Each test is one row of the threat model in
``nexus_ai_agent.continuum.snapshot`` (ATTACK -> EXPECTED).  Git is real;
only the two expensive, machine-bound measurements (the collected test count
and the dependency fingerprint) are pinned so the attack under test is the
only difference between a green and a red verdict.  The end-to-end version of
the same matrix — real CLI, real collection, real environment — is
``nexus_ai_agent.continuum.gate`` (CI job ``continuum-evidence``).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import nexus_ai_agent.continuum.snapshot as snapshot
from nexus_ai_agent.cli import app
from nexus_ai_agent.continuum import provenance
from nexus_ai_agent.continuum.snapshot import (
    ContinuumSnapshot,
    EnvFingerprint,
    parse_snapshot_bytes,
)

ENVIRONMENT = EnvFingerprint("3.11.2", "1.13.0", "2.0.30")
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "audit",
    "GIT_AUTHOR_EMAIL": "audit@example.invalid",
    "GIT_COMMITTER_NAME": "audit",
    "GIT_COMMITTER_EMAIL": "audit@example.invalid",
}


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        env={**os.environ, **_GIT_IDENTITY},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _snapshot(step: str, *, count: int = 3, ledger: list[dict[str, object]] | None = None):
    return ContinuumSnapshot(
        schema_version=2,
        plan="audit",
        step=step,
        next="verify",
        ledger=ledger or [],
        test_count_expected=count,
        env_fingerprint=ENVIRONMENT,
    )


class Repository:
    """A git checkout with the repository's layout and a published snapshot."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        _git(root, "init", "-q")
        (root / ".gitignore").write_text("models/\n__pycache__/\n", encoding="utf-8")
        for relative in ("src/pkg/service.py", "tests/test_a.py", "migrations/env.py"):
            self.write(relative, "value = 1\n")
        self.write("pyproject.toml", "[project]\nname = 'audit'\n")
        self.step = self.commit("baseline")

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def commit(self, message: str) -> str:
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", message)
        return _git(self.root, "rev-parse", "HEAD")

    def publish(self, value: ContinuumSnapshot) -> str:
        snapshot.write_snapshot(value)
        return self.commit("publish snapshot")

    def snapshot_path(self) -> Path:
        return self.root / ".nexus" / "continuum.json"


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "repository")
    monkeypatch.setattr(snapshot, "_REPO_ROOT", repository.root)
    monkeypatch.setattr(snapshot, "SNAPSHOT_PATH", repository.snapshot_path())
    monkeypatch.setattr(snapshot, "_test_case_count", lambda: 3)
    monkeypatch.setattr(snapshot, "_environment", lambda: ENVIRONMENT)
    repository.publish(_snapshot(repository.step))
    return repository


def test_control_a_published_snapshot_verifies(repo: Repository) -> None:
    assert snapshot.verify_snapshot() == []


# --- stale, drift, dirty ----------------------------------------------------


def test_stale_snapshot_from_another_history_is_state_loss(repo: Repository) -> None:
    orphan = _git(repo.root, "commit-tree", "HEAD^{tree}", "-m", "orphan")
    repo.publish(_snapshot(orphan))
    (problem,) = snapshot.verify_snapshot()
    assert problem.startswith(f"state loss detected: recorded good commit {orphan}")


def test_nonexistent_step_in_a_complete_clone_is_state_loss(repo: Repository) -> None:
    repo.publish(_snapshot("0" * 40))
    (problem,) = snapshot.verify_snapshot()
    assert problem.startswith("state loss detected")


@pytest.mark.parametrize(
    "relative", ["src/pkg/service.py", "tests/test_a.py", "migrations/env.py", "pyproject.toml"]
)
def test_later_committed_drift_in_any_evidence_root_is_detected(
    repo: Repository, relative: str
) -> None:
    repo.write(relative, "value = 2\n")
    repo.commit("drift")
    assert snapshot.verify_snapshot() == [
        "source state drift detected: executable, test, or dependency roots "
        "changed after the recorded good commit"
    ]


def test_later_commits_outside_evidence_roots_keep_the_snapshot_valid(repo: Repository) -> None:
    repo.write("docs/notes.md", "prose\n")
    repo.commit("docs only")
    assert snapshot.verify_snapshot() == []


@pytest.mark.parametrize(
    "relative",
    [
        "src/pkg/service.py",  # tracked edit
        "src/pkg/new_module.py",  # untracked importable source
        "docs/untracked.md",  # untracked anywhere in the checkout
        "src/pkg/models/hidden.py",  # ignored but importable
        ".nexus/.continuum.json.abc.tmp",  # leftover partial publication
    ],
)
def test_dirty_untracked_or_ignored_importable_files_are_drift(
    repo: Repository, relative: str
) -> None:
    repo.write(relative, "value = 99\n")
    assert snapshot.verify_snapshot() == [
        "working tree drift detected: snapshot verification requires a clean checkout"
    ]


def test_regenerated_caches_are_not_drift(repo: Repository) -> None:
    repo.write("src/pkg/__pycache__/service.cpython-311.pyc", "cache")
    assert snapshot.verify_snapshot() == []


def test_ignored_files_outside_source_roots_are_not_drift(repo: Repository) -> None:
    repo.write("models/weights.bin", "large")
    assert snapshot.verify_snapshot() == []


# --- history availability ----------------------------------------------------


def test_shallow_clone_cannot_prove_ancestry(
    repo: Repository, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo.write("docs/later.md", "later\n")
    repo.commit("later commit outside evidence roots")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{repo.root}", str(shallow)], check=True
    )
    monkeypatch.setattr(snapshot, "_REPO_ROOT", shallow)
    monkeypatch.setattr(snapshot, "SNAPSHOT_PATH", shallow / ".nexus" / "continuum.json")
    problems = snapshot.verify_snapshot()
    assert problems == [
        "git verification unavailable: cannot verify snapshot ancestry in a shallow repository"
    ]
    assert not any(problem.startswith("state loss") for problem in problems)


def test_missing_git_is_unavailable_never_green(
    repo: Repository, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    problems = snapshot.verify_snapshot()
    assert problems
    assert all(problem.startswith("git verification unavailable") for problem in problems)


def test_a_directory_that_is_not_a_repository_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plain = tmp_path / "plain"
    (plain / ".nexus").mkdir(parents=True)
    (plain / ".nexus" / "continuum.json").write_text(_snapshot("a" * 40).to_json())
    monkeypatch.setattr(snapshot, "_REPO_ROOT", plain)
    monkeypatch.setattr(snapshot, "SNAPSHOT_PATH", plain / ".nexus" / "continuum.json")
    monkeypatch.setattr(snapshot, "_test_case_count", lambda: 3)
    monkeypatch.setattr(snapshot, "_environment", lambda: ENVIRONMENT)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    problems = snapshot.verify_snapshot()
    assert problems and all(p.startswith("git verification unavailable") for p in problems)


# --- measured facts ----------------------------------------------------------


def test_test_count_and_environment_drift_are_reported_together(
    repo: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snapshot, "_test_case_count", lambda: 4)
    monkeypatch.setattr(snapshot, "_environment", lambda: EnvFingerprint("3.12.4", "1", "2"))
    problems = snapshot.verify_snapshot()
    assert "test count mismatch: expected 3, found 4" in problems
    assert any(problem.startswith("environment fingerprint mismatch") for problem in problems)
    assert len(problems) == 2


def test_collection_that_deselects_items_is_not_a_count(monkeypatch: pytest.MonkeyPatch) -> None:
    real_run = subprocess.run

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1:3] != ["-c", snapshot._COLLECTION_RUNNER]:
            return real_run(command, **kwargs)  # type: ignore[call-overload,no-any-return]
        Path(command[3]).write_text(
            json.dumps({"count": 7, "deselected": 2, "exit_code": 0}), encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(snapshot.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="deselected 2 item"):
        snapshot._test_case_count()


@pytest.mark.parametrize(
    "payload",
    [
        {"count": 7, "exit_code": 0},
        {"count": 7, "deselected": 0, "exit_code": 0, "verified": True},
        {"count": -1, "deselected": 0, "exit_code": 0},
    ],
)
def test_collection_artifact_must_be_exact(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    real_run = subprocess.run

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1:3] != ["-c", snapshot._COLLECTION_RUNNER]:
            return real_run(command, **kwargs)  # type: ignore[call-overload,no-any-return]
        Path(command[3]).write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(snapshot.subprocess, "run", run)
    with pytest.raises(RuntimeError):
        snapshot._test_case_count()


def test_collection_ignores_the_callers_pytest_steering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    real_run = subprocess.run

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1:3] != ["-c", snapshot._COLLECTION_RUNNER]:
            return real_run(command, **kwargs)  # type: ignore[call-overload,no-any-return]
        seen.update(kwargs)
        Path(command[3]).write_text(
            json.dumps({"count": 1, "deselected": 0, "exit_code": 0}), encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing")
    monkeypatch.setattr(snapshot.subprocess, "run", run)
    assert snapshot._test_case_count() == 1
    environment = seen["env"]
    assert isinstance(environment, dict) and "PYTEST_ADDOPTS" not in environment


# --- document integrity ------------------------------------------------------


def _canonical(data: dict[str, object]) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _valid() -> dict[str, object]:
    return json.loads(_snapshot("a" * 40).to_json())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(step="HEAD"), "full lowercase hexadecimal commit id"),
        (lambda d: d.update(step="a" * 12), "full lowercase hexadecimal commit id"),
        (lambda d: d.update(step="A" * 40), "full lowercase hexadecimal commit id"),
        (lambda d: d.update(step=""), "non-empty Git revision"),
        (lambda d: d.update(verified=True), "unexpected keys: verified"),
        (lambda d: d.pop("ledger"), "missing keys: ledger"),
        (lambda d: d.update(test_count_expected="5"), "non-negative integer"),
        (lambda d: d.update(test_count_expected=True), "non-negative integer"),
        (lambda d: d.update(test_count_expected=-1), "non-negative integer"),
        (lambda d: d.update(schema_version=3), "unsupported snapshot schema_version"),
        (lambda d: d.update(schema_version=True), "must be an integer"),
        (lambda d: d.update(ledger=["x"]), "list of JSON objects"),
        (lambda d: d.update(env_fingerprint={"python": "3"}), "missing keys"),
    ],
)
def test_schema_violations_are_unreadable(mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    data = _valid()
    mutate(data)
    with pytest.raises(ValueError, match=message):
        parse_snapshot_bytes(_canonical(data))


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"{", "invalid JSON"),
        (b"\xff\xfe", "not UTF-8"),
        (b"[]", "root must be a JSON object"),
    ],
)
def test_malformed_bytes_are_unreadable(raw: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_snapshot_bytes(raw)


def test_non_canonical_duplicate_key_and_nan_documents_are_unreadable() -> None:
    canonical = _canonical(_valid())
    assert parse_snapshot_bytes(canonical).step == "a" * 40
    with pytest.raises(ValueError, match="not canonical"):
        parse_snapshot_bytes(json.dumps(_valid(), indent=4, sort_keys=True).encode() + b"\n")
    with pytest.raises(ValueError, match="not canonical"):
        parse_snapshot_bytes(canonical.rstrip(b"\n"))
    shadowed = canonical.replace(b'  "plan":', b'  "plan": "shadow",\n  "plan":', 1)
    with pytest.raises(ValueError, match="duplicate key 'plan'"):
        parse_snapshot_bytes(shadowed)
    poisoned = canonical.replace(b'"ledger": []', b'"ledger": [{"x": NaN}]', 1)
    with pytest.raises(ValueError, match="non-finite"):
        parse_snapshot_bytes(poisoned)
    with pytest.raises(ValueError):
        _snapshot("a" * 40, ledger=[{"x": float("inf")}]).to_json()


def test_tampered_document_in_a_repository_is_reported_unreadable(repo: Repository) -> None:
    data = json.loads(repo.snapshot_path().read_text(encoding="utf-8"))
    data["forged"] = True
    repo.snapshot_path().write_bytes(_canonical(data))
    (problem,) = snapshot.verify_snapshot()
    assert problem.startswith("snapshot unreadable") and "unexpected keys: forged" in problem


def test_missing_snapshot_is_unreadable(repo: Repository) -> None:
    repo.snapshot_path().unlink()
    (problem,) = snapshot.verify_snapshot()
    assert problem.startswith("snapshot unreadable")


# --- publication -------------------------------------------------------------


def test_interrupted_publication_keeps_the_prior_snapshot(
    repo: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = repo.snapshot_path().read_bytes()

    def interrupted(_source: object, _destination: object) -> None:
        raise OSError("power loss")

    monkeypatch.setattr(provenance.os, "replace", interrupted)
    with pytest.raises(OSError, match="power loss"):
        snapshot.write_snapshot(_snapshot(repo.step, count=9))
    assert repo.snapshot_path().read_bytes() == before
    assert list(repo.snapshot_path().parent.glob("*.tmp")) == []
    assert snapshot.verify_snapshot() == []


def test_capture_measures_facts_and_refuses_a_dirty_checkout(repo: Repository) -> None:
    head = _git(repo.root, "rev-parse", "HEAD")
    captured = snapshot.capture_snapshot(plan="p", next_step="n", ledger=[{"id": "x"}])
    assert (captured.step, captured.test_count_expected) == (head, 3)
    assert captured.env_fingerprint == ENVIRONMENT

    repo.write("src/pkg/service.py", "value = 5\n")
    with pytest.raises(RuntimeError, match="dirty checkout"):
        snapshot.capture_snapshot(plan="p", next_step="n", ledger=[])


# --- CLI consumers -----------------------------------------------------------


def test_cli_show_validates_instead_of_echoing_raw_bytes(repo: Repository) -> None:
    runner = CliRunner()
    shown = runner.invoke(app, ["continuum", "show"])
    assert shown.exit_code == 0
    assert repo.step in shown.output

    repo.snapshot_path().write_text('{"forged": ', encoding="utf-8")
    broken = runner.invoke(app, ["continuum", "show"])
    assert broken.exit_code == 1
    assert "forged" not in broken.stdout


def test_cli_publish_round_trip_inherits_the_ledger_and_verifies(repo: Repository) -> None:
    runner = CliRunner()
    repo.publish(_snapshot(repo.step, ledger=[{"id": "C1", "status": "complete"}]))
    published = runner.invoke(app, ["continuum", "publish", "--next", "ship"])
    assert published.exit_code == 0, published.output
    value = snapshot.read_snapshot()
    assert value.ledger == [{"id": "C1", "status": "complete"}]
    assert (value.plan, value.next) == ("audit", "ship")
    repo.commit("publish via cli")
    assert runner.invoke(app, ["continuum", "verify"]).exit_code == 0


def test_cli_publish_refuses_dirty_trees_and_missing_narrative(repo: Repository) -> None:
    runner = CliRunner()
    repo.write("src/pkg/service.py", "value = 7\n")
    dirty = runner.invoke(app, ["continuum", "publish"])
    assert dirty.exit_code == 1
    _git(repo.root, "checkout", "--", "src/pkg/service.py")

    repo.snapshot_path().write_text("{", encoding="utf-8")
    orphaned = runner.invoke(app, ["continuum", "publish", "--plan", "p"])
    assert orphaned.exit_code == 2
    assert repo.snapshot_path().read_text(encoding="utf-8") == "{"


def test_cli_rejects_unknown_modes() -> None:
    assert CliRunner().invoke(app, ["continuum", "bless"]).exit_code != 0
