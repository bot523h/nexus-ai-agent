#!/usr/bin/env python3
"""Multi-agent claim board CLI for NEXUS AI Agent.

Coordination channel for agents working on this repository from separate
sandboxes. The only shared medium between sandboxes is the git repo itself,
so a claim is only real once it is committed AND pushed.

Commands
--------
  show                          Print the board (auto-releases expired leases)
  claim TASK --branch BRANCH    Claim a queued/expired task (refuses if actively
                                claimed by another branch)
  release TASK --branch BRANCH  Mark a finished claim done (free the zone)
  defer TASK --branch BRANCH --reason "..." [--fa "..."]
                                Record a deferral note ("I stopped because
                                another agent was working; resume later")
  next --branch BRANCH          Suggest the first claimable task
  check --files a,b,c --branch BRANCH
                                Exit 1 if any file overlaps another branch's
                                active or active-in-review exclusive paths
                                (pre-push / CI referee)
  praudit [--pr-json F | --repo owner/name] [--fail-on-invisible] [--json]
                                Read-only audit: compare open GitHub PR changed
                                files against board exclusive_paths and report
                                PRs whose scope is invisible (no claim for the
                                head branch, or files outside every fence).

All state lives in .agents/board.json (schema 1). Pure stdlib.

Typical loop for an arriving agent:
    python scripts/agent_board.py show
    python scripts/agent_board.py next --branch $MY_BRANCH
    python scripts/agent_board.py claim feature-wiring-batch --branch $MY_BRANCH
    # ... git pull --rebase, commit board change, push IMMEDIATELY ...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / ".agents" / "board.json"

# Files every PR is expected to touch (coordination medium) — never counted as
# "uncovered scope" by praudit, because no claim exclusively owns them.
COORDINATION_FILES = frozenset({".agents/board.json"})

STOP_BANNER = """
╔══════════════════════════════════════════════════════════════════╗
║  STOP — zone actively claimed by another agent                    ║
╠══════════════════════════════════════════════════════════════════╣
║  Pick a DIFFERENT task (scripts/agent_board.py next), or record  ║
║  your deferral: scripts/agent_board.py defer <task> ...          ║
║  Persian note template:                                          ║
║  «چون عامل دیگری روی این محدوده کار می‌کرد متوقف شدم؛ این کار    ║
║   پس از آزاد شدن ناحیه انجام خواهد شد تا فراموش نشود.»           ║
╚══════════════════════════════════════════════════════════════════╝
"""

_DEFAULT_FA = (
    "چون عامل دیگری روی این محدوده کار می‌کرد متوقف شدم؛ "
    "این کار پس از آزاد شدن ناحیه انجام خواهد شد تا فراموش نشود."
)

ACTIVE_STATUSES = frozenset({"active", "active_in_review"})


def _is_active_claim(claim: dict) -> bool:
    """True when a claim owns its zone and exclusive paths right now.

    ``active_in_review`` is intentionally treated as an active lease: an open PR
    can still conflict even though the author has stopped coding.  Older boards
    only checked the literal string ``active``, which made review-phase PRs
    invisible to the referee.
    """

    return claim.get("status") in ACTIVE_STATUSES


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_board() -> dict:
    if not BOARD.exists():
        sys.exit(f"board not found: {BOARD}")
    return json.loads(BOARD.read_text(encoding="utf-8"))


def save_board(board: dict) -> None:
    board["updated_at"] = _iso(_now())
    BOARD.write_text(json.dumps(board, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def gc_expired(board: dict) -> list[str]:
    """Auto-release expired leases. Returns list of freed task names."""
    freed: list[str] = []
    for claim in board.get("claims", []):
        claimed = _parse(claim.get("claimed_at"))
        if _is_active_claim(claim) and claimed is not None:
            expires = claimed + timedelta(hours=int(claim.get("ttl_hours", 24)))
            if _now() > expires:
                previous_status = claim.get("status")
                claim["status"] = "expired"
                claim["note"] = (
                    f"auto-released by gc at {_iso(_now())} (stale {previous_status} lease)"
                )
                freed.append(claim["task"])
    return freed


def _find(board: dict, task: str) -> dict | None:
    return next((c for c in board.get("claims", []) if c["task"] == task), None)


def _path_matches(excl: str, fp: str) -> bool:
    """Directory (`dir/`) or exact-file membership of *fp* in an exclusive path."""
    if excl.endswith("/"):
        return fp.startswith(excl) or fp == excl.rstrip("/")
    return fp == excl


def _claim_live(claim: dict) -> bool:
    """True when the claim currently fences its exclusive_paths (unexpired)."""
    if not _is_active_claim(claim):
        return False
    claimed = _parse(claim.get("claimed_at"))
    if claimed is None:
        return False
    return _now() <= claimed + timedelta(hours=int(claim.get("ttl_hours", 24)))


# ── cross-PR board visibility (task-207) ─────────────────────────────────
#
# `cmd_check` used to read ONE board: the local working tree's.  A claim that
# lives only on an unmerged PR branch was therefore invisible, and the tool
# printed "no overlap — safe to proceed" for a file somebody else had leased.
# That is the enforcement mechanism failing OPEN, in the most reassuring
# wording it owns.  It was observed live: task-202 (graph.py, PR #119) and
# task-203 (memory/, PR #121) were both leased while check reported no overlap.
#
# Visibility alone is not the fix.  The moment GitHub is unreachable the same
# false negative returns, so `check` now also reports WHETHER it managed to
# consult every open PR, and exits 2 when it did not.  "Could not verify" and
# "verified clear" are different answers and must not share an exit code.

#: Exit codes, documented in AGENTS.md and in `--help`.
CHECK_CLEAR = 0  # every open PR consulted; no live lease touches these files
CHECK_OVERLAP = 1  # a live foreign lease does touch them
CHECK_UNVERIFIED = 2  # the cross-PR view could not be established — NOT a pass


def _board_from_ref(ref: str) -> dict | None:
    """Read ``.agents/board.json`` from a local remote-tracking ref, if fetched."""
    import subprocess

    proc = subprocess.run(  # noqa: S603 (fixed argv, no shell)
        ["git", "show", f"{ref}:.agents/board.json"],
        cwd=BOARD.parent,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _board_from_github(repo: str, branch: str, token: str | None) -> dict | None:
    """Read a PR branch's board through the contents API."""
    import base64

    try:
        payload = _gh_get(  # type: ignore[arg-type]
            f"https://api.github.com/repos/{repo}/contents/.agents/board.json?ref={branch}",
            token,
        )
        return json.loads(base64.b64decode(payload["content"]).decode("utf-8"))  # type: ignore[index]
    except Exception:  # noqa: BLE001 - any failure means "could not consult"
        return None


def collect_open_pr_boards(
    repo: str, token: str | None = None, my_branch: str = ""
) -> tuple[list[tuple[str, dict]], list[str]]:
    """Every open PR's board, plus the branches that could not be consulted.

    Git is tried before the network: the branches are usually already fetched,
    which keeps the common case fast and rate-limit-free.  GitHub is the
    fallback so correctness does not depend on somebody having fetched first.
    """
    boards: list[tuple[str, dict]] = []
    unconsulted: list[str] = []
    try:
        prs = fetch_open_prs(repo, token)
    except Exception as exc:  # noqa: BLE001
        return [], [f"<open-PR list unavailable: {type(exc).__name__}>"]

    for pr in prs:
        head = pr.get("head_branch") or ""
        if not head or (my_branch and head == my_branch):
            continue
        board = _board_from_ref(f"origin/{head}")
        source = f"origin/{head}"
        if board is None:
            board = _board_from_github(repo, head, token)
            source = f"PR#{pr.get('number')} ({head})"
        if board is None:
            unconsulted.append(head)
        else:
            boards.append((source, board))
    return boards, unconsulted


def conflicting_paths_across(
    boards: list[tuple[str, dict]], branch: str, files: list[str]
) -> list[tuple[str, str, str]]:
    """Live-lease conflicts for *files* across many boards.

    Pure and side-effect free, so the whole rule can be tested without a
    network, a git remote, or a real claim.  Returns ``(task, file, source)``.
    """
    seen: set[tuple[str, str, str]] = set()
    hits: list[tuple[str, str, str]] = []
    for source, board in boards:
        for task, path in _conflicting_paths(board, branch, files):
            key = (task, path, source)
            if key not in seen:
                seen.add(key)
                hits.append(key)
    return hits


def _conflicting_paths(board: dict, branch: str, files: list[str]) -> list[tuple[str, str]]:
    """Return overlaps between *files* and other branches' live exclusive paths."""
    hits: list[tuple[str, str]] = []
    for claim in board.get("claims", []):
        if not _is_active_claim(claim):
            continue
        if branch and claim.get("agent_branch") == branch:
            continue
        claimed = _parse(claim.get("claimed_at"))
        if claimed is None:
            continue
        expires = claimed + timedelta(hours=int(claim.get("ttl_hours", 24)))
        if _now() > expires:
            continue
        for excl in claim.get("exclusive_paths", []):
            for f in files:
                fp = f.strip()
                if not fp:
                    continue
                if _path_matches(excl, fp):
                    hits.append((claim["task"], fp))
    return hits


# ── commands ──────────────────────────────────────────────────────────


def cmd_show(_args: argparse.Namespace) -> int:
    board = load_board()
    freed = gc_expired(board)
    if freed:
        save_board(board)
        print(f"[gc] auto-released expired leases: {', '.join(freed)}")
    print(f"board updated_at: {board['updated_at']}")
    for claim in board.get("claims", []):
        claimed = _parse(claim.get("claimed_at"))
        expires = (
            _iso(claimed + timedelta(hours=int(claim.get("ttl_hours", 24)))) if claimed else "—"
        )
        owner = claim.get("agent_branch") or "(unclaimed)"
        print(
            f"\n● {claim['task']}  [{claim['status']}]\n"
            f"  zone: {claim.get('zone')} · owner: {owner}\n"
            f"  claimed: {claim.get('claimed_at') or '—'} · expires: {expires}\n"
            f"  scope: {claim.get('scope', '')}"
        )
    for entry in board.get("deferred_log", []):
        print(f"\n⏸ DEFERRED {entry['task']} by {entry.get('deferred_by_branch')}")
        print(f"   fa: {entry.get('reason_fa', '')}")
        print(f"   en: {entry.get('reason_en', '')}")
        print(f"   resume_when: {entry.get('resume_when', '—')}")
    print(f"\ngates rule: {board.get('gates_rule', '—')}")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    board = load_board()
    gc_expired(board)
    claim = _find(board, args.task)
    if claim is None:
        print(f"task not found: {args.task}")
        return 2
    if _is_active_claim(claim):
        claimed = _parse(claim.get("claimed_at"))
        expires = (
            _iso(claimed + timedelta(hours=int(claim.get("ttl_hours", 24)))) if claimed else "?"
        )
        if claim.get("agent_branch") == args.branch:
            claim["claimed_at"] = _iso(_now())  # renewal (heartbeat)
            save_board(board)
            print(f"renewed lease for {args.task} (owner: {args.branch})")
            return 0
        print(
            f"task {args.task} is ACTIVELY claimed by"
            f" {claim.get('agent_branch')} (status {claim.get('status')}, expires {expires})"
        )
        print(STOP_BANNER)
        return 2
    claim.update(
        status="active",
        agent_branch=args.branch,
        claimed_at=_iso(_now()),
        ttl_hours=int(args.ttl),
        gates_owner=bool(args.gates),
        note="",
    )
    if args.gates:
        for other in board["claims"]:
            if other["task"] != args.task and other.get("gates_owner"):
                other["gates_owner"] = False
    save_board(board)
    print(f"CLAIMED {args.task} for {args.branch} (ttl {args.ttl}h, gates_owner={args.gates})")
    print("NOW: git add .agents/board.json && git commit && git push IMMEDIATELY —")
    print("an unpushed claim does not exist for the other sandbox.")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    board = load_board()
    claim = _find(board, args.task)
    if claim is None:
        print(f"task not found: {args.task}")
        return 2
    if claim.get("agent_branch") != args.branch:
        print(f"refusing: {args.task} belongs to {claim.get('agent_branch')}, not {args.branch}")
        return 2
    claim.update(status="done", agent_branch="", claimed_at=None, gates_owner=False)
    save_board(board)
    print(f"RELEASED {args.task}. Zone free — the next agent can claim it.")
    return 0


def cmd_defer(args: argparse.Namespace) -> int:
    board = load_board()
    claim = _find(board, args.task)
    board.setdefault("deferred_log", []).append(
        {
            "task": args.task,
            "deferred_by_branch": args.branch,
            "at": _iso(_now()),
            "reason_fa": args.fa or _DEFAULT_FA,
            "reason_en": args.reason
            or "Stopped because another agent held the active lease; resume when the zone frees.",
            "resume_when": args.resume_when or (f"claim {args.task} is free"),
        }
    )
    if claim is not None and claim.get("agent_branch") == args.branch and _is_active_claim(claim):
        claim.update(status="deferred", claimed_at=None, gates_owner=False)
    save_board(board)
    print(f"DEFERRED {args.task} by {args.branch} — note recorded so nothing is forgotten.")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    board = load_board()
    gc_expired(board)
    save_board(board)
    for claim in board.get("claims", []):
        if claim["status"] in ("queued", "expired", "deferred"):
            blockers = []
            for entry in board.get("deferred_log", []):
                if entry["task"] != claim["task"]:
                    continue
                blocked_claim = _find(board, entry["task"])
                if blocked_claim is not None and _is_active_claim(blocked_claim):
                    blockers.append(entry["task"])
            if blockers:
                print(
                    f"BLOCKED {claim['task']} — waiting on active claim(s): {', '.join(blockers)}"
                )
                continue
            print(f"NEXT: {claim['task']} (zone {claim.get('zone')}) — {claim.get('scope', '')}")
            print(
                f"claim it:  python scripts/agent_board.py claim"
                f" {claim['task']} --branch {args.branch}"
            )
            return 0
    print("no claimable task — all done or blocked. Propose a new task in .agents/board.json.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    files = [f.strip() for f in args.files.split(",") if f.strip()]
    branch = args.branch or ""
    # getattr: `cmd_check` is called programmatically by the board tests with a
    # hand-built namespace, and an AttributeError here would read as a crash
    # rather than as "this caller predates the cross-PR options".
    # None means "the operator did not name any boards"; [] means "consult an
    # empty set, successfully". Collapsing the two would make it impossible to
    # test the clean case hermetically, and would silently fall back to a network
    # call the caller thought they had disabled.
    _raw_boards = getattr(args, "board_json", None)
    explicit_boards: list[str] | None = None if _raw_boards is None else list(_raw_boards)
    no_remote: bool = bool(getattr(args, "no_remote", False))
    repo_arg: str = getattr(args, "repo", "") or ""
    token_arg: str = getattr(args, "token", "") or ""
    boards: list[tuple[str, dict]] = []
    unconsulted: list[str] = []

    # The local board is ALWAYS consulted. Explicit/remote boards are additional
    # views of the same repository, never a replacement for the one on disk —
    # an earlier draft let `--board-json` suppress the local board entirely, and
    # the regression suite caught it immediately.
    boards.append(("local board", load_board()))

    if explicit_boards is not None:
        # Hermetic path: these boards are named explicitly. Used by the regression
        # tests, and by an operator who has already snapshotted them.
        for path in explicit_boards:
            try:
                boards.append((path, json.loads(Path(path).read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"could not read board {path}: {exc}", file=sys.stderr)
                unconsulted.append(path)
    elif not no_remote:
        repo = repo_arg or os.environ.get("GITHUB_REPOSITORY", "")
        token = token_arg or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if "/" not in repo:
            print(
                "check: no repository known, so foreign PR boards were NOT consulted.\n"
                "       Pass --repo owner/name (or set GITHUB_REPOSITORY), or --no-remote\n"
                "       to accept a local-only check explicitly.",
                file=sys.stderr,
            )
            unconsulted.append("<no repository>")
        else:
            remote, missed = collect_open_pr_boards(repo, token, my_branch=branch)
            boards.extend(remote)
            unconsulted.extend(missed)

    hits = conflicting_paths_across(boards, branch, files)

    if hits:
        print("OVERLAP with another agent's live exclusive paths:")
        for task, path, source in hits:
            print(f"  {path}  <- claimed by {task}  [seen in {source}]")
        print(STOP_BANNER)
        return CHECK_OVERLAP

    if unconsulted:
        # The critical branch. `check` exists to stop a collision; a verdict it
        # could not actually earn must never be phrased as permission.
        print(
            f"INCOMPLETE — {len(unconsulted)} open-PR board(s) could NOT be consulted:",
            file=sys.stderr,
        )
        for head in unconsulted[:10]:
            print(f"  {head}", file=sys.stderr)
        if len(unconsulted) > 10:
            print(f"  ... and {len(unconsulted) - 10} more", file=sys.stderr)
        print(
            "\nThis is NOT a pass. A lease held on an unmerged PR branch is exactly the\n"
            "case this check exists to catch, and it is invisible from the local board.\n"
            "Retry with --repo owner/name, or run\n"
            "  python scripts/praudit --repo owner/name --fail-on-invisible\n"
            "and treat a network failure as a blocker, not as clearance.",
            file=sys.stderr,
        )
        return CHECK_CLEAR if no_remote else CHECK_UNVERIFIED

    consulted = len(boards) - 1  # the local board is not a PR
    suffix = f" (local + {consulted} open-PR board(s))" if consulted else ""
    if no_remote:
        print(
            "\n!!  --no-remote: FOREIGN PR BOARDS WERE NOT CONSULTED.\n"
            "!!  A lease held only on an unmerged PR branch is invisible from here, and\n"
            "!!  that is the exact case this check exists to catch. This exit 0 means\n"
            "!!  'no overlap in the LOCAL board', not 'no overlap on the repository'.",
            file=sys.stderr,
        )
    print(f"no overlap — safe to proceed{suffix}.")
    return CHECK_CLEAR


# ── praudit: open-PR × board visibility (read-only) ───────────────────


def audit_pr_visibility(board: dict, prs: list[dict]) -> dict:
    """Compare open PR changed files with the board's live exclusive paths.

    A PR is **invisible** when the board cannot tell who owns its scope:
      * no live claim exists for the PR's head branch, or
      * at least one changed file (coordination files excluded) sits outside
        every live claim's ``exclusive_paths`` — the claim exists but does not
        actually fence what the PR is rewriting.

    Foreign fences (hits from *other* branches' claims) are reported per PR so
    collisions are visible before a push, not after a conflicting merge.
    """
    results: list[dict] = []
    live_claims = [c for c in board.get("claims", []) if _claim_live(c)]
    for pr in prs:
        number = pr.get("number")
        title = pr.get("title", "")
        head = pr.get("head_branch") or (pr.get("head") or {}).get("ref") or ""
        raw_files = pr.get("files") or []
        files = [f if isinstance(f, str) else str(f.get("filename", "")) for f in raw_files]
        files = [f for f in files if f]

        own_claims = [c["task"] for c in live_claims if head and c.get("agent_branch") == head]
        foreign = sorted({task for task, _path in _conflicting_paths(board, head, files)})

        def covered(fp: str) -> bool:
            return any(
                any(_path_matches(excl, fp) for excl in claim.get("exclusive_paths", []))
                for claim in live_claims
            )

        audited_files = [f for f in files if f not in COORDINATION_FILES]
        uncovered = [f for f in audited_files if not covered(f)]

        reasons: list[str] = []
        if not head:
            reasons.append("missing head branch")
        if not own_claims:
            reasons.append("no live board claim for head branch")
        if uncovered:
            reasons.append(f"{len(uncovered)} file(s) outside every exclusive_paths")

        results.append(
            {
                "number": number,
                "title": title,
                "head_branch": head,
                "files": files,
                "own_claims": own_claims,
                "foreign_fences": foreign,
                "uncovered_files": uncovered,
                "invisible": bool(reasons),
                "reasons": reasons,
            }
        )
    invisible = [r for r in results if r["invisible"]]
    return {
        "prs": results,
        "total": len(results),
        "invisible_count": len(invisible),
        "invisible_numbers": [r["number"] for r in invisible],
    }


def _gh_get(url: str, token: str | None) -> object:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "nexus-agent-board",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (github api only)
        return json.load(response)


def fetch_open_prs(repo: str, token: str | None = None) -> list[dict]:
    """Live open-PR snapshot (GitHub REST, stdlib urllib)."""
    pulls = _gh_get(f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=100", token)
    out: list[dict] = []
    for pr in pulls:  # type: ignore[assignment]
        number = pr["number"]
        rows = _gh_get(
            f"https://api.github.com/repos/{repo}/pulls/{number}/files?per_page=100", token
        )
        out.append(
            {
                "number": number,
                "title": pr.get("title", ""),
                "head_branch": (pr.get("head") or {}).get("ref", ""),
                "files": [row["filename"] for row in rows],  # type: ignore[index]
                "files_truncated": len(rows) >= 100,  # type: ignore[arg-type]
            }
        )
    return out


def _load_pr_fixture(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data["prs"]
    if not isinstance(data, list):
        sys.exit(f"invalid --pr-json fixture: {path}")
    return data


def cmd_praudit(args: argparse.Namespace) -> int:
    """Read-only visibility audit — never writes the board."""
    board = load_board()
    gc_expired(board)  # in-memory only: no save_board() in this command

    if args.pr_json:
        prs = _load_pr_fixture(args.pr_json)
    else:
        repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
        if "/" not in repo:
            print("praudit: provide --repo owner/name (or GITHUB_REPOSITORY) or --pr-json")
            return 2
        token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None
        try:
            prs = fetch_open_prs(repo, token)
        except (urllib.error.URLError, OSError) as exc:
            # URLError covers HTTP errors; OSError covers socket timeouts —
            # both must degrade to an actionable exit, never an unhandled trace.
            print(f"praudit: GitHub API error: {exc}")
            return 2

    result = audit_pr_visibility(board, prs)

    if args.as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"praudit: {result['total']} open PR(s), "
            f"{result['invisible_count']} invisible on the board"
        )
        for row in result["prs"]:
            mark = "INVISIBLE" if row["invisible"] else "visible"
            own = ", ".join(row["own_claims"]) or "—"
            print(f"  PR#{row['number']} [{mark}] head={row['head_branch'] or '?'} claims={own}")
            if row["reasons"]:
                print(f"    reasons: {'; '.join(row['reasons'])}")
            if row["foreign_fences"]:
                print(f"    fences hit from other branches: {', '.join(row['foreign_fences'])}")
            for fp in row["uncovered_files"][:10]:
                print(f"    uncovered: {fp}")
            if len(row["uncovered_files"]) > 10:
                print(f"    … +{len(row['uncovered_files']) - 10} more uncovered")

    if args.fail_on_invisible and result["invisible_count"]:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NEXUS multi-agent claim board")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show").set_defaults(func=cmd_show)

    p = sub.add_parser("claim")
    p.add_argument("task")
    p.add_argument("--branch", required=True)
    p.add_argument("--ttl", type=int, default=24)
    p.add_argument("--gates", action="store_true", help="this agent owns CI gates while active")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("release")
    p.add_argument("task")
    p.add_argument("--branch", required=True)
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("defer")
    p.add_argument("task")
    p.add_argument("--branch", required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--fa", default="")
    p.add_argument("--resume-when", dest="resume_when", default="")
    p.set_defaults(func=cmd_defer)

    p = sub.add_parser("next")
    p.add_argument("--branch", required=True)
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("check")
    p.add_argument("--files", required=True, help="comma-separated changed file paths")
    p.add_argument("--branch", default="")
    p.add_argument("--repo", default="", help="owner/name; defaults to $GITHUB_REPOSITORY")
    p.add_argument("--token", default="", help="defaults to $GITHUB_TOKEN or $GH_TOKEN")
    p.add_argument(
        "--board-json",
        action="append",
        default=None,
        help="consult this board file instead of GitHub (repeatable; hermetic). "
        "Pass once with no value semantics: an empty list means 'consult none, "
        "and that succeeded'.",
    )
    p.add_argument(
        "--no-remote",
        action="store_true",
        help="local board ONLY. Accepts that foreign PR leases are invisible; "
        "the exit-0 verdict is then a local-only statement, not clearance.",
    )
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("praudit")
    p.add_argument(
        "--pr-json",
        dest="pr_json",
        default="",
        help="offline fixture: JSON list of {number,title,head_branch,files:[...]}",
    )
    p.add_argument("--repo", default="", help="GitHub repo owner/name for live mode")
    p.add_argument("--token", default="", help="GitHub token (else GITHUB_TOKEN env)")
    p.add_argument(
        "--fail-on-invisible",
        action="store_true",
        help="exit 1 when any open PR scope is invisible on the board",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_praudit)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
