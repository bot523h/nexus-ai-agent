"""Composition safety: two correct PRs must not silently become one unsafe merge.

task-227.  ``creative/studio/capabilities.py`` is rewritten by two open PRs at
the same time (identity-addressed undo vs. staged plan transactions).  Each PR is
correct alone, but a three-way merge of the file produces conflict hunks, and a
naive "take ours / take theirs" resolution silently drops one side's invariant —
the TOCTOU identity gate *or* the plan-selection semantics.

This module pins the *detector* that makes that composition visible before a
push: ``scripts/agent_board.py collision``.  It never merges and never resolves;
it classifies a pair of refs as safe, as requiring manual reconciliation, or as a
security-sensitive collision.  The git fixtures are built in ``tmp_path`` so the
suite is hermetic and independent of the live repository's refs.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "agent_board.py"


def _load_board_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_board_collision_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _write(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")


@pytest.fixture()
def module() -> ModuleType:
    return _load_board_cli()


@pytest.fixture()
def collision_repo(tmp_path: Path) -> tuple[Path, dict]:
    """A tiny repo with deterministic refs exercising every classification."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    filler = "".join(f"line_{i:02d}\n" for i in range(40))
    _write(repo, "shared.py", 'def guard():\n    return "v1"\n')
    _write(repo, "other.py", filler)
    _write(repo, "conflict.py", filler)
    _write(repo, "solo.py", "solo = 0\n")
    (repo / ".agents").mkdir()
    _write(repo, ".agents/board.json", "{}\n")
    _write(repo, "AGENTS.md", "line_00\nline_01\nline_02\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "tag", "base")

    def branch(name: str, edits: dict[str, str], message: str) -> None:
        _git(repo, "checkout", "-q", "-b", name, "base")
        for filename, text in edits.items():
            _write(repo, filename, text)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", message)

    # a: rewrites the security-sensitive file + two others
    branch(
        "a",
        {
            "shared.py": 'def guard():\n    return "A"\n',
            "other.py": filler.replace("line_01\n", "A_01\n"),
            "conflict.py": filler.replace("line_20\n", "A_20\n"),
        },
        "a",
    )
    _git(repo, "tag", "a")
    # b: rewrites the same security-sensitive file + overlapping others
    branch(
        "b",
        {
            "shared.py": 'def guard():\n    return "B"\n',
            "other.py": filler.replace("line_38\n", "B_38\n"),
            "conflict.py": filler.replace("line_20\n", "B_20\n"),
        },
        "b",
    )
    _git(repo, "tag", "b")
    # c: disjoint — only a brand-new file
    branch("c", {"solo.py": "solo = 1\n"}, "c")
    _git(repo, "tag", "c")
    # d/e: same non-security file, same line -> conflict
    branch("d", {"conflict.py": filler.replace("line_20\n", "D_20\n")}, "d")
    _git(repo, "tag", "d")
    branch("e", {"conflict.py": filler.replace("line_20\n", "E_20\n")}, "e")
    _git(repo, "tag", "e")
    # f/g: same non-security file, distant lines -> clean overlap
    branch("f", {"other.py": filler.replace("line_01\n", "F_01\n")}, "f")
    _git(repo, "tag", "f")
    branch("g", {"other.py": filler.replace("line_38\n", "G_38\n")}, "g")
    _git(repo, "tag", "g")
    # h/i: only the coordination file changes
    branch("h", {".agents/board.json": '{"h": 1}\n'}, "h")
    _git(repo, "tag", "h")
    branch("i", {".agents/board.json": '{"i": 1}\n'}, "i")
    _git(repo, "tag", "i")
    # s1/s2: stacked lineage
    branch("s", {"solo.py": "solo = 2\n"}, "s1")
    _git(repo, "tag", "s1")
    _git(repo, "checkout", "-q", "s")
    _write(repo, "solo.py", "solo = 3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "s2")
    _git(repo, "tag", "s2")

    # j/k: only the coordination file AGENTS.md changes (must stay SAFE_INDEPENDENT)
    branch("j", {"AGENTS.md": "line_00\nline_01-j\nline_02\n"}, "j")
    _git(repo, "tag", "j")
    branch("k", {"AGENTS.md": "line_00\nline_01-k\nline_02\n"}, "k")
    _git(repo, "tag", "k")

    # orphan: an unrelated root commit, no merge base with the base lineage
    _git(repo, "checkout", "-q", "--orphan", "orphan")
    _git(repo, "rm", "-q", "-rf", ".")
    _write(repo, "orphan.py", "orphan = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "orphan")
    _git(repo, "tag", "orphan")

    board = {
        "zones": [
            {"id": "nagar-contract-gate", "paths": ["shared.py"]},
            {"id": "misc", "paths": ["other.py", "conflict.py", "solo.py"]},
        ]
    }
    return repo, board


def _detect(module: ModuleType, repo: Path, board: dict, refs: list[str]) -> dict:
    return module.detect_collisions(board, refs, repo=repo)


def _pair(result: dict) -> dict:
    assert len(result["pairs"]) == 1
    return result["pairs"][0]


# --------------------------------------------------------------------------- #
# security-sensitive zones come from the board, not a hardcoded file list
# --------------------------------------------------------------------------- #
def test_security_sensitive_zones_are_board_derived(module: ModuleType) -> None:
    board = {
        "zones": [
            {"id": "nagar-contract-gate", "paths": ["shared.py"]},
            {"id": "misc", "paths": ["other.py"]},
        ]
    }
    assert module.security_sensitive_zones(board) == ["nagar-contract-gate"]


def test_security_zones_come_from_real_board(module: ModuleType) -> None:
    board = json.loads((REPO_ROOT / ".agents" / "board.json").read_text(encoding="utf-8"))
    zones = module.security_sensitive_zones(board)
    assert "nagar-contract-gate" in zones
    assert "security-boundary" in zones


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def test_disjoint_refs_are_safe(module: ModuleType, collision_repo) -> None:
    repo, board = collision_repo
    assert _pair(_detect(module, repo, board, ["a", "c"]))["classification"] == (
        module.COLLISION_SAFE
    )


def test_same_security_sensitive_file_is_a_security_collision(
    module: ModuleType, collision_repo
) -> None:
    repo, board = collision_repo
    pair = _pair(_detect(module, repo, board, ["a", "b"]))
    assert pair["classification"] == module.COLLISION_SECURITY
    shared = next(row for row in pair["files"] if row["path"] == "shared.py")
    assert shared["security_sensitive"] is True
    assert shared["zones"] == ["nagar-contract-gate"]


def test_conflicting_non_security_file_requires_reconciliation(
    module: ModuleType, collision_repo
) -> None:
    repo, board = collision_repo
    pair = _pair(_detect(module, repo, board, ["d", "e"]))
    assert pair["classification"] == module.COLLISION_RECONCILE
    assert pair["files"][0]["conflict_hunks"] == 1


def test_clean_overlap_of_a_non_security_file_is_safe_overlap(
    module: ModuleType, collision_repo
) -> None:
    repo, board = collision_repo
    pair = _pair(_detect(module, repo, board, ["f", "g"]))
    assert pair["classification"] == module.COLLISION_SAFE_OVERLAP
    assert pair["files"][0]["conflict_hunks"] == 0


def test_coordination_file_alone_is_never_a_hazard(module: ModuleType, collision_repo) -> None:
    repo, board = collision_repo
    pair = _pair(_detect(module, repo, board, ["h", "i"]))
    assert pair["classification"] == module.COLLISION_SAFE
    assert all(row["path"] != ".agents/board.json" for row in pair["files"])


def test_agents_md_alone_is_never_a_hazard(module: ModuleType, collision_repo) -> None:
    """AGENTS.md is a coordination file (protocol SAFE_INDEPENDENT rule): two refs
    that only edit it must be SAFE, not REQUIRES_MANUAL_RECONCILIATION."""
    repo, board = collision_repo
    pair = _pair(_detect(module, repo, board, ["j", "k"]))
    assert pair["classification"] == module.COLLISION_SAFE
    assert pair["overlap_files"] == ["AGENTS.md"]
    assert all(row["path"] != "AGENTS.md" for row in pair["files"])


def test_unknown_ref_is_unverifiable_not_safe(module: ModuleType, collision_repo) -> None:
    """A ref git cannot resolve must never be reported as independent."""
    repo, board = collision_repo
    result = _detect(module, repo, board, ["a", "no-such-ref"])
    pair = _pair(result)
    assert pair["classification"] == module.COLLISION_UNKNOWN
    assert pair["classification"] in module._COLLISION_BAD
    assert result["collision_count"] == 1


def test_no_merge_base_without_base_is_unverifiable(
    module: ModuleType, collision_repo
) -> None:
    repo, board = collision_repo
    pair = _pair(_detect(module, repo, board, ["a", "orphan"]))
    assert pair["classification"] == module.COLLISION_UNKNOWN


def test_no_merge_base_with_base_uses_the_supplied_root(
    module: ModuleType, collision_repo
) -> None:
    repo, board = collision_repo
    result = module.detect_collisions(board, ["a", "orphan"], base="base", repo=repo)
    assert result["pairs"][0]["classification"] != module.COLLISION_UNKNOWN


def test_changed_files_returns_none_on_git_failure(
    module: ModuleType, collision_repo
) -> None:
    """The None-vs-[] contract is what makes the fail-closed path possible."""
    repo, _ = collision_repo
    assert module._changed_files("base", "no-such-ref", repo) is None
    assert module._changed_files("base", "a", repo) is not None


def test_cli_fails_on_unknown_ref(
    module: ModuleType, collision_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, board = collision_repo
    monkeypatch.setattr(module, "load_board", lambda: board)
    args = type(
        "A",
        (),
        {
            "refs": "a,no-such-ref",
            "base": "",
            "repo": str(repo),
            "fail_on_collision": True,
            "as_json": False,
        },
    )()
    assert module.cmd_collision(args) == 1


def test_stacked_lineage_is_reported(module: ModuleType, collision_repo) -> None:
    repo, board = collision_repo
    assert _pair(_detect(module, repo, board, ["s1", "s2"]))["stacked"] is True
    assert _pair(_detect(module, repo, board, ["a", "b"]))["stacked"] is False


def test_multi_ref_report_counts_only_dangerous_pairs(module: ModuleType, collision_repo) -> None:
    repo, board = collision_repo
    result = _detect(module, repo, board, ["a", "b", "c"])
    # pairs: (a,b) SECURITY, (a,c) SAFE, (b,c) SAFE
    assert len(result["pairs"]) == 3
    assert result["collision_count"] == 1
    assert [p["classification"] for p in result["pairs"]] == [
        module.COLLISION_SECURITY,
        module.COLLISION_SAFE,
        module.COLLISION_SAFE,
    ]


# --------------------------------------------------------------------------- #
# determinism: no set/dict iteration order leaks into the report
# --------------------------------------------------------------------------- #
def test_report_is_deterministic_regardless_of_ref_order(
    module: ModuleType, collision_repo
) -> None:
    repo, board = collision_repo
    forward = _detect(module, repo, board, ["a", "b"])
    reversed_ = _detect(module, repo, board, ["b", "a"])
    assert forward["pairs"][0]["classification"] == reversed_["pairs"][0]["classification"]
    assert forward["pairs"][0]["overlap_files"] == reversed_["pairs"][0]["overlap_files"]
    # and the same call twice yields byte-identical JSON
    assert json.dumps(forward, sort_keys=False) == json.dumps(
        _detect(module, repo, board, ["a", "b"]), sort_keys=False
    )


# --------------------------------------------------------------------------- #
# CLI wiring: exit codes
# --------------------------------------------------------------------------- #
def test_cli_fails_on_a_dangerous_pair(
    module: ModuleType, collision_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, board = collision_repo
    monkeypatch.setattr(module, "load_board", lambda: board)
    args = type(
        "A",
        (),
        {
            "refs": "a,b",
            "base": "",
            "repo": str(repo),
            "fail_on_collision": True,
            "as_json": False,
        },
    )()
    assert module.cmd_collision(args) == 1


def test_cli_passes_on_a_safe_pair(
    module: ModuleType, collision_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, board = collision_repo
    monkeypatch.setattr(module, "load_board", lambda: board)
    args = type(
        "A",
        (),
        {
            "refs": "a,c",
            "base": "",
            "repo": str(repo),
            "fail_on_collision": True,
            "as_json": False,
        },
    )()
    assert module.cmd_collision(args) == 0
