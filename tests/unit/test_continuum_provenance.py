"""Provenance primitives shared by coverage evidence and the snapshot verifier."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.continuum import provenance
from nexus_ai_agent.continuum.provenance import (
    EVIDENCE_SOURCE_PATHS,
    REPO_ROOT,
    atomic_write_bytes,
    isolated_python_environment,
    source_digest,
    working_tree_drift,
)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=a@b.invalid", "-c", "user.name=a", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    for relative in ("src/a.py", "src/b.py", "docs/guide.md"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text("x = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text("models/\n__pycache__/\n*.egg-info/\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def test_every_evidence_source_path_exists_in_this_repository() -> None:
    """A stale root (the historical ``alembic`` entry) silently disables drift checks."""
    missing = [path for path in EVIDENCE_SOURCE_PATHS if not (REPO_ROOT / path).exists()]
    assert missing == []
    tracked = set(
        subprocess.run(
            ["git", "ls-files"], cwd=REPO_ROOT, check=True, capture_output=True, text=True
        ).stdout.split()
    )
    for path in EVIDENCE_SOURCE_PATHS:
        assert any(name == path or name.startswith(path + "/") for name in tracked), path


def test_clean_checkout_has_no_drift(checkout: Path) -> None:
    assert working_tree_drift(checkout) == ()


def test_a_tracked_edit_in_the_first_status_record_keeps_its_path(checkout: Path) -> None:
    """Regression: stripping git output corrupted the first ``" M path"`` record."""
    (checkout / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert working_tree_drift(checkout, scope=("src",)) == ("M src/a.py",)
    assert working_tree_drift(checkout) == ("M src/a.py",)


def test_scope_limits_tracked_and_untracked_changes(checkout: Path) -> None:
    (checkout / "docs" / "guide.md").write_text("changed\n", encoding="utf-8")
    (checkout / "notes.txt").write_text("new\n", encoding="utf-8")
    assert working_tree_drift(checkout, scope=("src",)) == ()
    assert working_tree_drift(checkout) == ("?? notes.txt", "M docs/guide.md")


def test_renames_and_staged_changes_are_reported(checkout: Path) -> None:
    _git(checkout, "mv", "src/b.py", "src/c.py")
    assert working_tree_drift(checkout, scope=("src",)) == ("R src/c.py",)


def test_ignored_importable_source_is_drift_but_caches_are_not(checkout: Path) -> None:
    (checkout / "src" / "models").mkdir()
    (checkout / "src" / "models" / "hidden.py").write_text("x = 1\n", encoding="utf-8")
    (checkout / "src" / "__pycache__").mkdir()
    (checkout / "src" / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"\0")
    (checkout / "src" / "pkg.egg-info").mkdir()
    (checkout / "models").mkdir()
    (checkout / "models" / "weights.bin").write_bytes(b"\0")
    assert working_tree_drift(checkout, scope=("src",)) == ("ignored src/models/",)


def test_git_failures_raise_instead_of_reporting_clean(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="cannot inspect working tree"):
        working_tree_drift(tmp_path / "missing")


def test_source_digest_is_order_independent_and_content_sensitive(checkout: Path) -> None:
    a, b = checkout / "src" / "a.py", checkout / "src" / "b.py"
    first = source_digest(checkout, [a, b])
    assert first == source_digest(checkout, [b, a, a])
    assert first.startswith("sha256:") and len(first) == len("sha256:") + 64
    b.write_text("x = 3\n", encoding="utf-8")
    assert source_digest(checkout, [a, b]) != first


def test_isolated_environment_prefers_the_checkout_and_drops_steering(tmp_path: Path) -> None:
    environment = isolated_python_environment(
        tmp_path,
        {"PYTEST_ADDOPTS": "-k x", "PYTEST_PLUGINS": "evil", "PYTHONPATH": "/elsewhere"},
    )
    assert "PYTEST_ADDOPTS" not in environment and "PYTEST_PLUGINS" not in environment
    assert environment["PYTHONPATH"].split(":") == [str(tmp_path / "src"), "/elsewhere"]
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_atomic_write_replaces_whole_files_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out" / "artifact.json"
    atomic_write_bytes(target, b"first\n")
    assert target.read_bytes() == b"first\n"

    def interrupted(_source: object, _destination: object) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr(provenance.os, "replace", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        atomic_write_bytes(target, b"second\n")
    assert target.read_bytes() == b"first\n"
    assert sorted(path.name for path in target.parent.iterdir()) == ["artifact.json"]


def test_shallow_ancestry_is_unknown_not_false(tmp_path: Path, checkout: Path) -> None:
    (checkout / "docs" / "guide.md").write_text("two\n", encoding="utf-8")
    _git(checkout, "commit", "-qam", "second")
    base = _git(checkout, "rev-list", "--max-parents=0", "HEAD").strip()
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{checkout}", str(shallow)], check=True
    )
    head = provenance.current_commit(shallow)
    assert provenance.is_shallow_repository(shallow)
    assert not provenance.commit_exists(shallow, base)
    assert provenance.is_ancestor(shallow, head, head) is True
    orphan = _git(shallow, "commit-tree", "HEAD^{tree}", "-m", "orphan").strip()
    with pytest.raises(RuntimeError, match="shallow repository"):
        provenance.is_ancestor(shallow, orphan, head)
