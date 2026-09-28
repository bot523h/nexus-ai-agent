"""Publication safety is about observed remote owners and outgoing history, not cwd diffs."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "agent_board_remote.py"
NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


@pytest.fixture
def guard():
    spec = importlib.util.spec_from_file_location("remote_guard_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def lease(owner="arena/peer", **overrides):
    return dict(
        task="peer-task",
        agent_branch=owner,
        status="active",
        claimed_at="2026-09-28T11:00:00Z",
        ttl_hours=24,
        exclusive_paths=["src/private/"],
        **overrides,
    )


def snapshot(claims, owner="arena/peer"):
    return {owner: {"oid": "a" * 40, "board": {"claims": claims}}}


def verdict(guard, views, files, branch="arena/mine"):
    return guard.evaluate_claims(views, files, branch, NOW)


def test_remote_self_claim_blocks_without_local_board(guard):
    result = verdict(guard, snapshot([lease()]), ["src/private/file.py"])
    assert result["outcome"] == "REJECTED"
    assert result["conflicts"][0]["owner"] == "arena/peer"
    assert result["conflicts"][0]["source_oid"] == "a" * 40


def test_inherited_release_cannot_cancel_owner_tip(guard):
    views = snapshot([lease()])
    released = dict(lease(), status="done")
    views["arena/third-party"] = {"oid": "b" * 40, "board": {"claims": [released]}}
    assert verdict(guard, views, ["src/private/x"])["outcome"] == "REJECTED"


@pytest.mark.parametrize(
    "changes",
    [
        {"claimed_at": None},
        {"claimed_at": "invalid"},
        {"ttl_hours": 0},
        {"ttl_hours": True},
        {"ttl_hours": "NaN"},
        {"exclusive_paths": ["../escape"]},
        {"exclusive_paths": ["/absolute"]},
        {"exclusive_paths": ["src\\private\\"]},
        {"status": "active_typo"},
        {"claimed_at": "2099-01-01T00:00:00Z"},
        {"agent_branch": None},
    ],
)
def test_ambiguous_active_metadata_is_not_safe(guard, changes):
    views = snapshot([dict(lease(), **changes)])
    result = verdict(guard, views, ["other.py"])
    assert result["outcome"] == "NOT_VERIFIED"
    assert result["errors"]


def test_prefix_boundary_expiry_and_own_claim(guard):
    views = snapshot([lease()])
    sibling = verdict(guard, views, ["src/private-sibling/x"])
    assert sibling["outcome"] == "SUCCESS"
    own = verdict(guard, views, ["src/private/x"], branch="arena/peer")
    assert own["outcome"] == "SUCCESS"
    expired = dict(lease(), claimed_at="2026-09-26T11:00:00Z")
    stale = verdict(guard, snapshot([expired]), ["src/private/x"])
    assert stale["outcome"] == "SUCCESS"


@pytest.mark.parametrize("board", [{"claims": None}, {"other": []}, {"claims": [42]}])
def test_malformed_remote_board_is_not_empty_board(guard, board):
    views = {"arena/peer": {"oid": "a" * 40, "board": board}}
    assert verdict(guard, views, [])["outcome"] == "NOT_VERIFIED"


def git(path, *args):
    out = subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.PIPE)
    return out.decode().strip()


def commit(repo, name, contents):
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents)
    git(repo, "add", name)
    git(repo, "commit", "-m", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    # Disposable test repositories only; never create/switch a branch in the checkout.
    remote, repo = tmp_path / "remote.git", tmp_path / "client"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "checkout", "-b", "main")
    commit(repo, ".agents/board.json", json.dumps({"claims": []}))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "main")
    git(repo, "checkout", "-b", "arena/peer")
    commit(repo, ".agents/board.json", json.dumps({"claims": [lease()]}))
    git(repo, "push", "origin", "arena/peer")
    git(repo, "checkout", "-b", "arena/mine", "main")
    git(repo, "push", "origin", "arena/mine")
    return repo, remote


def test_real_git_sees_old_commit_even_after_revert(guard, repository):
    repo, _ = repository
    bad = commit(repo, "src/private/owned.py", "conflicting")
    git(repo, "revert", "--no-edit", bad)
    commit(repo, "safe.py", "apparently harmless HEAD")
    before = git(repo, "rev-parse", "HEAD")
    diff = git(repo, "diff", "--name-only", "origin/arena/mine", "HEAD")
    assert "src/private/owned.py" not in diff
    result = guard.inspect_publication(repo, "arena/mine", now=NOW)
    assert result["outcome"] == "REJECTED"
    assert "src/private/owned.py" in result["outgoing_files"]
    assert len(result["outgoing_commits"]) == 3
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo, "status", "--porcelain") == ""


def test_safe_slice_reports_excluded_dirty_worktree(guard, repository):
    repo, _ = repository
    commit(repo, "safe.py", "safe")
    (repo / "private-draft.py").write_text("not committed, not being pushed")
    result = guard.inspect_publication(repo, "arena/mine", now=NOW)
    assert result["outcome"] == "SUCCESS"
    assert result["worktree_dirty"] is True
    assert result["scope"] == "outgoing_commits_only"
    assert result["outgoing_files"] == ["safe.py"]
    assert len(result["frontier_sha256"]) == 64


def test_remote_failure_is_not_no_overlap(guard, repository):
    repo, _ = repository
    git(repo, "remote", "set-url", "origin", str(repo / "missing.git"))
    result = guard.inspect_publication(repo, "arena/mine", now=NOW)
    assert result["outcome"] == "NOT_VERIFIED"
    assert result["errors"]


def test_wrong_branch_cannot_exempt_foreign_owner(guard, repository):
    repo, _ = repository
    assert guard.inspect_publication(repo, "arena/peer", now=NOW)["outcome"] == "NOT_VERIFIED"


def test_remote_movement_during_observation_is_not_safe(guard, repository, monkeypatch):
    repo, _ = repository
    original = guard.Git.remote_heads
    calls = 0

    def moving(self, remote):
        nonlocal calls
        calls += 1
        result = original(self, remote)
        if calls > 1:
            result["refs/heads/arena/new-peer"] = "b" * 40
        return result

    monkeypatch.setattr(guard.Git, "remote_heads", moving)
    assert guard.inspect_publication(repo, "arena/mine", now=NOW)["outcome"] == "NOT_VERIFIED"


# ── adversarial round (hardening-proof-01a0e742) ─────────────────────────────


def test_local_board_free_cannot_release_remote_lease(guard, repository):
    repo, _ = repository
    # The local history actively declares the peer released; the remote owner
    # head still fences the zone, and only that head is authoritative.
    released = json.dumps({"claims": [dict(lease(), status="done")]})
    (repo / ".agents/board.json").write_text(released)
    git(repo, "add", ".agents/board.json")
    git(repo, "commit", "-m", "local board says the peer released")
    commit(repo, "src/private/local-change.py", "data")
    result = guard.inspect_publication(repo, "arena/mine", now=NOW)
    assert result["outcome"] == "REJECTED"


def test_deleted_local_tracking_ref_cannot_release_remote_owner(guard, repository):
    repo, _ = repository
    commit(repo, "src/private/x.py", "data")
    git(repo, "branch", "-dr", "origin/arena/peer")
    result = guard.inspect_publication(repo, "arena/mine", now=NOW)
    assert result["outcome"] == "REJECTED"


def test_shallow_history_cannot_be_judged(guard, repository):
    repo, _ = repository
    commit(repo, "safe.py", "safe")
    subprocess.run(
        ["git", "-C", str(repo), "fetch", "--depth=1", "origin"],
        check=True,
        capture_output=True,
    )
    result = guard.inspect_publication(repo, "arena/mine", now=NOW)
    assert result["outcome"] == "NOT_VERIFIED"
    assert any("shallow" in error for error in result["errors"])


def test_rewritten_local_history_is_not_verifiable(guard, repository):
    repo, _ = repository
    commit(repo, "published.py", "published state")
    git(repo, "push", "origin", "arena/mine")
    git(repo, "reset", "--hard", "origin/main")
    commit(repo, "different.py", "rebased away from the published tip")
    result = guard.inspect_publication(repo, "arena/mine", now=NOW)
    assert result["outcome"] == "NOT_VERIFIED"
    assert result["errors"]


def test_merge_commit_introduced_file_is_caught(guard, repository):
    repo, _ = repository
    git(repo, "checkout", "-b", "side")
    commit(repo, "src/private/merged.py", "via merge")
    git(repo, "checkout", "arena/mine")
    git(repo, "merge", "--no-ff", "-m", "merge side", "side")
    result = guard.inspect_publication(repo, "arena/mine", now=NOW)
    assert result["outcome"] == "REJECTED"
    assert "src/private/merged.py" in result["outgoing_files"]


def test_cherry_pick_and_force_push_divergence_are_caught(guard, repository):
    repo, _ = repository
    git(repo, "checkout", "-b", "source", "origin/arena/peer")
    bad = commit(repo, "src/private/picked.py", "picked")
    git(repo, "checkout", "arena/mine")
    git(repo, "cherry-pick", bad)
    result = guard.inspect_publication(repo, "arena/mine", now=NOW)
    assert result["outcome"] == "REJECTED"
    assert "src/private/picked.py" in result["outgoing_files"]


def test_force_push_style_rewrite_abandons_the_claim(guard, repository):
    repo, _ = repository
    commit(repo, "published.py", "published state")
    git(repo, "push", "origin", "arena/mine")
    git(repo, "reset", "--hard", "origin/main")
    commit(repo, "rewritten.py", "force-push style divergence")
    result = guard.inspect_publication(repo, "arena/mine", now=NOW)
    assert result["outcome"] == "NOT_VERIFIED"
    assert result["errors"]


def test_local_quarantine_tag_is_not_published(guard, repository):
    repo, _ = repository
    git(repo, "checkout", "-b", "quarantine")
    bad = commit(repo, "src/private/candidate.py", "candidate content")
    git(repo, "tag", "preserved-candidate", bad)
    git(repo, "checkout", "arena/mine")
    commit(repo, "safe.py", "safe")
    result = guard.inspect_publication(repo, "arena/mine", now=NOW)
    assert result["outcome"] == "SUCCESS"
    assert "src/private/candidate.py" not in result["outgoing_files"]
    assert "preserved-candidate" not in git(repo, "ls-remote", "--heads", "origin")


def test_post_push_empty_range_is_success(guard, repository):
    repo, _ = repository
    git(repo, "push", "origin", "arena/mine")
    result = guard.inspect_publication(repo, "arena/mine", now=NOW)
    assert result["outcome"] == "SUCCESS"
    assert result["outgoing_commits"] == []
    assert result["outgoing_files"] == []
