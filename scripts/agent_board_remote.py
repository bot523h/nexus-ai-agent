"""Read-only publication preflight using the existing board's owner-issued leases.

SUCCESS is scoped to an observed outgoing commit range, never to production readiness.
Remote-tracking refs are refreshed; worktree, index, branch and boards are not mutated.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ACTIVE = {"active", "active_in_review", "active_in_review_PR33"}
INACTIVE = {
    "queued",
    "released",
    "done",
    "deferred",
    "expired",
    "blocked",
    "cancelled",
    "done_partial_residue_queued",
    "queued_reserved_for_B",
    "available",
    "completed_released",
    "completed_merged",
    "completed_delivered_via_PR34",
    "superseded_by_PR33",
    "assigned_to_B",
    "assigned_to_B_next",
    "assigned_to_E_pr33",
    "available_sequenced_post_33",
    "available_sequenced_post_32",
    "available_sequenced_post_32_33",
}
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
BOARD = ".agents/board.json"


class ObservationError(Exception):
    """A missing premise, not permission to publish."""


class Git:
    def __init__(self, root: Path):
        self.root = root

    def run(self, *args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "--no-replace-objects", "-C", str(self.root), *args],
                check=False,
                capture_output=True,
                timeout=45,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ObservationError(f"git_{args[0]}_unavailable") from exc
        if completed.returncode:
            # stderr may contain a configured URL or credential: never copy it to evidence.
            raise ObservationError(f"git_{args[0]}_failed")
        if len(completed.stdout) > 8 * 1024 * 1024:
            raise ObservationError("git_output_limit")
        try:
            return completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ObservationError("non_utf8_git_evidence") from exc

    def remote_heads(self, remote: str) -> dict[str, str]:
        lines = self.run("ls-remote", "--heads", remote, "main", "arena/*").splitlines()
        heads: dict[str, str] = {}
        for line in lines:
            parts = line.split("\t")
            if len(parts) != 2 or not OID.fullmatch(parts[0]):
                raise ObservationError("malformed_remote_frontier")
            oid, ref = parts
            if ref != "refs/heads/main" and not ref.startswith("refs/heads/arena/"):
                raise ObservationError("unexpected_remote_ref")
            if ref in heads:
                raise ObservationError("duplicate_remote_ref")
            heads[ref] = oid
        if "refs/heads/main" not in heads or len(heads) > 512:
            raise ObservationError("missing_main_or_frontier_limit")
        return heads

    def board(self, oid: str) -> dict[str, Any] | None:
        # A missing board on a legacy branch is recorded, never substituted with local cwd.
        if not self.run("ls-tree", "--name-only", oid, "--", BOARD).strip():
            return None
        if int(self.run("cat-file", "-s", f"{oid}:{BOARD}")) > 2 * 1024 * 1024:
            raise ObservationError("board_size_limit")
        try:
            value = json.loads(self.run("show", f"{oid}:{BOARD}"))
        except json.JSONDecodeError as exc:
            raise ObservationError("invalid_remote_board_json") from exc
        if not isinstance(value, dict):
            raise ObservationError("invalid_remote_board_shape")
        return value


def _path_valid(path: Any) -> bool:
    return (
        isinstance(path, str)
        and bool(path)
        and not path.startswith("/")
        and "\\" not in path
        and "\x00" not in path
        and all(part not in {"", ".", ".."} for part in path.rstrip("/").split("/"))
        and not any(char in path for char in "*?[]")
    )


def _matches(path: str, pattern: str) -> bool:
    return path == pattern or (pattern.endswith("/") and path.startswith(pattern))


def evaluate_claims(
    snapshots: dict[str, dict[str, Any]],
    files: list[str],
    branch: str,
    now: datetime,
) -> dict[str, Any]:
    """Owner-head precedence: an inherited release cannot release another owner's lease.

    No caller's claimed_at timestamp can override a different owner's immutable tip.
    Missing owner heads with live inherited claims require explicit reconciliation.
    """
    conflicts: list[dict[str, Any]] = []
    errors: list[str] = []
    leases: list[dict[str, Any]] = []
    missing_boards = []
    for source, snapshot in sorted(snapshots.items()):
        board = snapshot["board"]
        if board is None:
            missing_boards.append(source)
            continue
        claims = board.get("claims")
        if not isinstance(claims, list):
            errors.append(f"{source}:invalid_claims")
            continue
        seen = set()
        for claim in claims:
            if not isinstance(claim, dict):
                errors.append(f"{source}:invalid_claim")
                continue
            status, owner = claim.get("status"), claim.get("agent_branch")
            if status in INACTIVE:
                continue
            # Ignore inherited copies when their owner head can speak for itself.
            if (
                isinstance(owner, str)
                and owner != source
                and owner in snapshots
                and snapshots[owner]["board"] is not None
            ):
                continue
            task = claim.get("task")
            label = f"{source}:{task if isinstance(task, str) else 'unknown-task'}"
            if status not in ACTIVE or not isinstance(owner, str) or not owner:
                errors.append(f"{label}:invalid_active_identity")
                continue
            try:
                stamp = claim["claimed_at"]
                if not isinstance(stamp, str):
                    raise ValueError("timestamp")
                at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                ttl = claim["ttl_hours"]
                if at.tzinfo is None or isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
                    raise ValueError("ttl")
                if not math.isfinite(ttl) or ttl <= 0 or at > now:
                    raise ValueError("clock_or_ttl")
                expires = at + timedelta(hours=ttl)
            except (KeyError, ValueError, TypeError, OverflowError):
                errors.append(f"{label}:invalid_lease_time")
                continue
            if expires <= now:
                continue
            paths = claim.get("exclusive_paths")
            if not isinstance(paths, list) or not all(_path_valid(p) for p in paths):
                errors.append(f"{label}:invalid_exclusive_paths")
                continue
            if not isinstance(task, str) or not task or task in seen:
                errors.append(f"{label}:invalid_or_duplicate_task")
                continue
            seen.add(task)
            if owner != source:
                errors.append(f"{label}:owner_head_missing")
                continue
            entry = {
                "owner": owner,
                "task": task,
                "source_oid": snapshot["oid"],
                "expires_at": expires.isoformat(),
                "exclusive_paths": paths,
            }
            leases.append(entry)
            overlap = sorted({f for f in files if any(_matches(f, p) for p in paths)})
            if owner != branch and overlap:
                conflicts.append(dict(entry, files=overlap))
    # Uncertain evidence has priority; a known conflict is still retained in the report.
    outcome = "NOT_VERIFIED" if errors else ("REJECTED" if conflicts else "SUCCESS")
    return dict(
        outcome=outcome,
        errors=errors,
        conflicts=conflicts,
        leases=leases,
        missing_boards=missing_boards,
    )


def inspect_publication(
    root: Path,
    branch: str,
    remote: str = "origin",
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {
        "schema_version": 1,
        "outcome": "NOT_VERIFIED",
        "scope": "outgoing_commits_only",
        "observed_at": now.isoformat(),
        "branch": branch,
        "remote": remote,
        "errors": [],
        "production_verified": False,
        "limitations": ["not_atomic_with_push", "does_not_verify_CI_or_uncommitted_changes"],
    }
    git = Git(root)
    try:
        if now.tzinfo is None or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", remote):
            raise ObservationError("invalid_clock_or_remote_name")
        if remote not in git.run("remote").splitlines():
            raise ObservationError("remote_not_configured")
        fetch_urls = git.run("remote", "get-url", "--all", remote).splitlines()
        push_urls = git.run("remote", "get-url", "--push", "--all", remote).splitlines()
        if len(fetch_urls) != 1 or push_urls != fetch_urls:
            raise ObservationError("fetch_push_destination_mismatch")
        current = git.run("symbolic-ref", "--short", "HEAD").strip()
        if not branch.startswith("arena/") or current != branch:
            raise ObservationError("branch_identity_mismatch")
        if git.run("rev-parse", "--is-shallow-repository").strip() != "false":
            raise ObservationError("shallow_history")
        # A graft file can hide ancestors independently of replacement objects.
        grafts = Path(git.run("rev-parse", "--git-path", "info/grafts").strip())
        if not grafts.is_absolute():
            grafts = root / grafts
        if grafts.exists():
            raise ObservationError("grafted_history")
        head = git.run("rev-parse", "HEAD").strip()
        frontier = git.remote_heads(remote)
        result.update(head_oid=head, frontier=frontier)
        target = frontier.get(f"refs/heads/{branch}")
        if target is None:
            raise ObservationError("publish_claim_before_preflight")
        # No arbitrary refspec or URL is accepted from the caller.
        git.run(
            "fetch",
            "--prune",
            "--no-tags",
            remote,
            f"+refs/heads/main:refs/remotes/{remote}/main",
            f"+refs/heads/arena/*:refs/remotes/{remote}/arena/*",
        )
        for ref, oid in frontier.items():
            tracking = ref.replace("refs/heads/", f"refs/remotes/{remote}/", 1)
            if git.run("rev-parse", "--verify", tracking).strip() != oid:
                raise ObservationError("remote_moved_during_fetch")
        git.run("merge-base", "--is-ancestor", target, head)
        commits = git.run("rev-list", "--reverse", f"{target}..{head}").splitlines()
        if len(commits) > 10000:
            raise ObservationError("outgoing_history_limit")
        files: set[str] = set()
        for commit in commits:
            # -m exposes differences against EVERY merge parent; no rename elision.
            paths = git.run(
                "diff-tree",
                "--root",
                "-m",
                "--no-renames",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                commit,
            )
            files.update(path for path in paths.split("\x00") if path)
        snapshots = {
            ref.removeprefix("refs/heads/"): {"oid": oid, "board": git.board(oid)}
            for ref, oid in frontier.items()
        }
        status = git.run("status", "--porcelain", "--untracked-files=normal")
        result.update(
            remote_tip=target,
            outgoing_commits=commits,
            outgoing_files=sorted(files),
            worktree_dirty=bool(status),
        )
        result.update(evaluate_claims(snapshots, sorted(files), branch, now))
        if git.remote_heads(remote) != frontier:
            raise ObservationError("remote_frontier_changed")
        unchanged = (
            git.run("rev-parse", "HEAD").strip() == head
            and git.run("symbolic-ref", "--short", "HEAD").strip() == branch
        )
        if not unchanged:
            raise ObservationError("local_head_changed")
        binding = {
            "frontier": frontier,
            "head": head,
            "branch": branch,
            "commits": commits,
            "files": sorted(files),
            "observed_at": now.isoformat(),
        }
        result["frontier_sha256"] = hashlib.sha256(
            json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    except (ObservationError, ValueError) as exc:
        result["outcome"] = "NOT_VERIFIED"
        result["errors"].append(str(exc))
    return result
