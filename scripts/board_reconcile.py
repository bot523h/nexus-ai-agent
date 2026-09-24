#!/usr/bin/env python3
"""Board-Git truth reconciler (task-163, D-0010 / ADR 0005).

``.agents/board.json`` is a *cache*; the truth is ``git ls-remote`` plus
the GitHub PR matrix.  This tool reconciles the two and reports drift.
It is **write-free** — it never mutates the board — so it is safe to run
in CI on every push/PR and on a ``*/15`` schedule sweep (the
``board-reconcile`` job), where a blocking mismatch turns the build red.
The owner (or the claiming agent) repairs the board; this tool only
witnesses the truth.

Blocking drift (``--check`` ⇒ exit 1):

* ``MERGED_STILL_OPEN`` — a claim whose branch was merged (a merged PR
  for that branch) is still active on the board.
* ``BRANCH_GONE`` — an active claim's branch no longer exists on the
  remote and is not the current working branch.
* ``UNCLAIMED_OPEN_PR`` — an open agent-branch PR has no live claim.
* ``INVISIBLE_FILE`` — a file changed by an open PR is fenced by no live
  claim (the split-brain blind spot: two agents, one file, zero fences).

Warnings (exit 0 unless ``--strict``):

* ``LEASE_EXPIRED`` — a live claim's TTL has passed (the referee,
  ``agent_board.py``, releases dead fences; the gate must not pre-empt
  it, so this stays a warning).
* ``SCOPE_TRUNCATED`` — a PR's file list hit ``gh``'s 100-file cap:
  partial visibility, never a false clean.
* ``OVERLAP`` — a file changed by one open PR is fenced by another live
  claim (coordinate before merging).

Exit codes: ``0`` clean (or warnings only), ``1`` blocking drift with
``--check`` (or any finding with ``--strict --check``), ``2`` usage or
environment error (board missing/unreadable, git/gh failure).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: The coordination medium itself is exempt from file-fence checks: every
#: claim edits the board to record itself, so fencing it against claims
#: would be a tautology, not a signal.
COORDINATION_FILES = frozenset({".agents/board.json"})

#: Statuses that own their exclusive paths right now (same set as the
#: referee, ``scripts/agent_board.py``: an open PR can still conflict
#: even though its author has stopped coding).
ACTIVE_STATUSES = frozenset({"active", "active_in_review"})

#: gh's PR.files cap — a list at this length may be incomplete.
GH_FILES_CAP = 100

_DEFAULT_REMOTE = "origin"
_DEFAULT_BRANCH_PREFIX = "arena/"
_DEFAULT_MERGED_LIMIT = 100
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str  # "blocking" | "warning"
    subject: str  # task id or "PR#<n>"
    detail: str
    files: tuple[str, ...] = field(default=())


@dataclass
class Report:
    generated_at: str
    blocking: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.blocking

    def as_dict(self) -> dict[str, object]:
        def _entry(f: Finding) -> dict[str, object]:
            entry: dict[str, object] = {
                "kind": f.kind,
                "severity": f.severity,
                "subject": f.subject,
                "detail": f.detail,
            }
            if f.files:
                entry["files"] = list(f.files)
            return entry

        return {
            "generated_at": self.generated_at,
            "blocking": [_entry(f) for f in self.blocking],
            "warnings": [_entry(f) for f in self.warnings],
            "counts": {"blocking": len(self.blocking), "warning": len(self.warnings)},
        }


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _path_matches(fence: str, file_path: str) -> bool:
    """True when *file_path* is inside the fenced *fence* (exact or prefix)."""
    fence = fence.rstrip("/")
    if not fence:
        return False
    if fence.endswith("/"):
        return file_path.startswith(fence.rstrip("/"))
    return file_path == fence or file_path.startswith(fence + "/")


def _pr_files(raw_files: list[object]) -> list[str]:
    """gh returns file *objects* ({"path": ...}) on newer APIs — accept both."""
    paths: list[str] = []
    for item in raw_files:
        if isinstance(item, dict):
            path = item.get("path")
            if isinstance(path, str) and path:
                paths.append(path)
        elif isinstance(item, str) and item:
            paths.append(item)
    return paths


def reconcile(
    board: dict,
    live_branches: set[str],
    open_prs: list[dict],
    merged_prs: list[dict],
    *,
    now: datetime,
    branch_prefix: str = _DEFAULT_BRANCH_PREFIX,
) -> Report:
    """Pure reconciliation: board + truth in, verdicts out (no I/O).

    ``live_branches`` must already include the current working branch
    (a push in flight has a live branch that ``ls-remote`` has not seen
    yet).  Each PR dict: ``{"number", "branch", "files" (list[str])}``.
    """
    report = Report(generated_at=now.strftime(_TS_FORMAT))

    claims = board.get("claims", [])
    live_claims = [c for c in claims if c.get("status") in ACTIVE_STATUSES]

    merged_branches = {pr["branch"]: pr["number"] for pr in merged_prs if pr.get("branch")}

    # ── claim-side blocking drift ──────────────────────────────────────
    for claim in live_claims:
        task = str(claim.get("task", "?"))
        branch = claim.get("agent_branch")
        if not branch:
            continue
        claimed = _parse_ts(claim.get("claimed_at"))
        ttl = int(claim.get("ttl_hours", 24))

        if branch in merged_branches:
            report.blocking.append(
                Finding(
                    kind="MERGED_STILL_OPEN",
                    severity="blocking",
                    subject=task,
                    detail=(
                        f"branch={branch} merged (PR#{merged_branches[branch]}) "
                        f"but the board claim is still {claim.get('status')}"
                    ),
                )
            )
            continue
        if branch not in live_branches:
            report.blocking.append(
                Finding(
                    kind="BRANCH_GONE",
                    severity="blocking",
                    subject=task,
                    detail=(
                        f"branch={branch} no longer exists on the remote "
                        f"and is not the current working branch"
                    ),
                )
            )
        if claimed is not None and now > claimed + timedelta(hours=ttl):
            report.warnings.append(
                Finding(
                    kind="LEASE_EXPIRED",
                    severity="warning",
                    subject=task,
                    detail=(
                        f"lease expired (claimed {claim.get('claimed_at')} + {ttl}h) — "
                        "the referee will release it"
                    ),
                )
            )

    # ── PR-side blocking drift ─────────────────────────────────────────
    fenced = [
        (claim.get("agent_branch"), str(p))
        for claim in live_claims
        for p in claim.get("exclusive_paths", [])
        if isinstance(p, str)
    ]
    for pr in open_prs:
        number = int(pr.get("number", 0))
        branch = pr.get("branch") or ""
        files = _pr_files(pr.get("files", []))
        if not branch:
            continue
        # The board is the *agent* coordination medium: only agent-branch
        # PRs must be claimed and fenced.  Human PRs go through the normal
        # review process and are out of scope (flagging them would turn
        # every ordinary PR into blocking drift).
        if not branch.startswith(branch_prefix):
            continue

        if not any(claim.get("agent_branch") == branch for claim in live_claims):
            report.blocking.append(
                Finding(
                    kind="UNCLAIMED_OPEN_PR",
                    severity="blocking",
                    subject=f"PR#{number}",
                    detail=f"branch={branch} is open but no live claim covers it",
                )
            )

        visible = [f for f in files if f not in COORDINATION_FILES]
        invisible = [f for f in visible if not any(_path_matches(fence, f) for _, fence in fenced)]
        if invisible:
            report.blocking.append(
                Finding(
                    kind="INVISIBLE_FILE",
                    severity="blocking",
                    subject=f"PR#{number}",
                    detail="changed files fenced by no live claim (split-brain blind spot)",
                    files=tuple(sorted(invisible)),
                )
            )

        if len(files) >= GH_FILES_CAP:
            report.warnings.append(
                Finding(
                    kind="SCOPE_TRUNCATED",
                    severity="warning",
                    subject=f"PR#{number}",
                    detail=(
                        f"file list hit the {GH_FILES_CAP}-file gh cap — "
                        "partial visibility, never a false clean"
                    ),
                )
            )

        other_fence_hits: set[str] = set()
        for claim_branch, fence in fenced:
            if claim_branch == branch:
                continue
            for f in visible:
                if _path_matches(fence, f):
                    other_fence_hits.add(f)
        if other_fence_hits:
            report.warnings.append(
                Finding(
                    kind="OVERLAP",
                    severity="warning",
                    subject=f"PR#{number}",
                    detail=(
                        "changed files are fenced by another live claim — "
                        "coordinate before merging (split-brain risk)"
                    ),
                    files=tuple(sorted(other_fence_hits)),
                )
            )

    return report


# ── I/O layer (monkeypatched in tests) ─────────────────────────────────


def _run_git(repo: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _current_branch(repo: Path) -> str | None:
    try:
        out = _run_git(repo, ["branch", "--show-current"]).strip()
        return out or None
    except subprocess.CalledProcessError:
        return None


def _live_branches(repo: Path, remote: str) -> set[str]:
    out = _run_git(repo, ["ls-remote", "--heads", remote])
    branches: set[str] = set()
    for line in out.splitlines():
        if "\t" not in line:
            continue
        ref = line.split("\t", 1)[1].strip()
        if ref.startswith("refs/heads/"):
            ref = ref[len("refs/heads/") :]
        if ref:
            branches.add(ref)
    current = _current_branch(repo)
    if current:
        branches.add(current)
    return branches


def _open_prs() -> list[dict]:
    raw = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,headRefName,files",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data = json.loads(raw or "[]")
    return [
        {
            "number": item.get("number"),
            "branch": item.get("headRefName"),
            "files": item.get("files", []),
        }
        for item in data
    ]


def _merged_prs(limit: int) -> list[dict]:
    raw = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "merged",
            "--limit",
            str(limit),
            "--json",
            "number,headRefName",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data = json.loads(raw or "[]")
    return [{"number": item.get("number"), "branch": item.get("headRefName")} for item in data]


def load_board(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"board not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _render(report: Report) -> str:
    lines = [f"board-reconcile @ {report.generated_at}"]
    for finding in report.blocking:
        lines.append(f"⛔ {finding.kind}\t{finding.subject}")
        detail = f"  {finding.detail}"
        if finding.files:
            detail += f"  files: {', '.join(finding.files)}"
        lines.append(detail)
    for finding in report.warnings:
        lines.append(f"⚠ {finding.kind}\t{finding.subject}")
        detail = f"  {finding.detail}"
        if finding.files:
            detail += f"  files: {', '.join(finding.files)}"
        lines.append(detail)
    if not report.blocking and not report.warnings:
        lines.append("drift: clean")
    else:
        lines.append(f"drift: {len(report.blocking)} blocking, {len(report.warnings)} warning")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile .agents/board.json against git + PR truth."
    )
    parser.add_argument("--check", action="store_true", help="exit 1 on blocking drift (CI mode)")
    parser.add_argument(
        "--strict", action="store_true", help="treat warnings as blocking for exit code"
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--now", help="pin 'now' (ISO 8601 Z) — tests/diagnostics")
    parser.add_argument(
        "--board", default=".agents/board.json", help="board path (repo-relative or absolute)"
    )
    parser.add_argument("--repo", default=".", help="repository root for git calls")
    parser.add_argument("--remote", default=_DEFAULT_REMOTE, help="git remote to query")
    parser.add_argument(
        "--branch-prefix",
        default=_DEFAULT_BRANCH_PREFIX,
        help="agent-branch prefix for open-PR checks",
    )
    parser.add_argument(
        "--merged-limit", type=int, default=_DEFAULT_MERGED_LIMIT, help="merged PRs to inspect"
    )
    args = parser.parse_args(argv)

    now = _parse_ts(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        print(
            f"error: --now must be ISO 8601 (e.g. 2026-09-23T12:00:00Z), got {args.now!r}",
            file=sys.stderr,
        )
        return 2

    repo = Path(args.repo).resolve()
    board_path = Path(args.board)
    if not board_path.is_absolute():
        board_path = repo / board_path

    try:
        board = load_board(board_path)
        live_branches = _live_branches(repo, args.remote)
        open_prs = _open_prs()
        merged_prs = _merged_prs(args.merged_limit)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"error: truth source unavailable: {exc}", file=sys.stderr)
        return 2

    report = reconcile(
        board,
        live_branches,
        open_prs,
        merged_prs,
        now=now,
        branch_prefix=args.branch_prefix,
    )

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(_render(report))

    if not args.check:
        return 0
    failing = report.blocking + (report.warnings if args.strict else [])
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
