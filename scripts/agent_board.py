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
  collision --refs A,B[,C...] [--fail-on-collision] [--json]
                                Read-only pre-push preflight: for every pair of
                                refs, list the files they both rewrite, the board
                                zones those files belong to, and the three-way
                                merge conflict-hunk count.  A pair is a
                                SECURITY_SENSITIVE_COLLISION when a shared file
                                is in a security-relevant zone; the tool detects
                                and classifies only — it never merges or resolves.
  validate [--strict-new] [--json]
                                Mechanical governance invariants (exit 1 on error):
                                every next_work entry and every active claim must
                                carry evidence_required (legacy claimable gaps are
                                WARN, errors under --strict-new); and exactly one
                                active claim may hold gates_owner.

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
# "uncovered scope" by praudit and never treated as a collision hazard, because
# no claim exclusively owns them.  AGENTS.md is listed in the protocol's own
# SAFE_INDEPENDENT rule, so it must be excluded here too (docs == executed rule).
COORDINATION_FILES = frozenset({".agents/board.json", "AGENTS.md"})

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
    # Documented rule (AGENTS.md §2 / protocol.verification_rule): a claimable task
    # must carry evidence_required. Enforce it here so NEW work cannot be claimed
    # without it; grandfathered legacy claims are surfaced by `validate` as WARN.
    if not claim.get("evidence_required"):
        print(
            f"refusing: {args.task} has no evidence_required — add it to .agents/board.json "
            "before claiming (AGENTS.md §2: a task without it cannot be claimed)."
        )
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
    board = load_board()
    files = [f for f in args.files.split(",") if f.strip()]
    hits = _conflicting_paths(board, args.branch or "", files)
    if hits:
        print("OVERLAP with another agent's active or active-in-review exclusive paths:")
        for task, path in hits:
            print(f"  {path}  ← claimed by {task}")
        print(STOP_BANNER)
        return 1
    print("no overlap — safe to proceed.")
    return 0


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


# ── collision preflight: two correct PRs must not become one unsafe merge ────
#
# Reads only git objects + board zone metadata; never merges and never resolves
# a conflict.  A pair of refs is a *security-sensitive collision* when they
# rewrite the same file that belongs to a security-relevant zone, or when a
# three-way merge of that file produces conflict hunks (a naive "take ours /
# take theirs" resolution would then silently drop one side's invariant).

# Board-derived, minimal: a zone is security-relevant when its id names the
# authority/boundary it enforces.  No hardcoded file list — the board already
# declares which paths each zone owns (tested by test_agent_board_collision.py).
SECURITY_ZONE_HINTS = ("security", "gate", "contract", "trust", "auth")

COLLISION_SAFE = "SAFE_INDEPENDENT"
COLLISION_SAFE_OVERLAP = "SAFE_OVERLAP"
COLLISION_RECONCILE = "REQUIRES_MANUAL_RECONCILIATION"
COLLISION_SECURITY = "SECURITY_SENSITIVE_COLLISION"
COLLISION_UNKNOWN = "UNVERIFIABLE"
# UNVERIFIABLE is dangerous on purpose: "could not inspect" must never be read as
# "independent".  It is included in the bad set so --fail-on-collision exits 1.
_COLLISION_BAD = frozenset({COLLISION_RECONCILE, COLLISION_SECURITY, COLLISION_UNKNOWN})


def _run_git(*args: str, cwd: Path | None = None) -> tuple[int, str, str]:
    import subprocess

    proc = subprocess.run(["git", *args], cwd=cwd or ROOT, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def security_sensitive_zones(board: dict) -> list[str]:
    """Zone ids that name the authority/boundary they enforce (sorted)."""
    return sorted(
        zone["id"]
        for zone in board.get("zones", [])
        if any(hint in zone["id"].lower() for hint in SECURITY_ZONE_HINTS)
    )


def zones_for_path(board: dict, path: str) -> list[str]:
    """Every board zone whose declared paths contain *path* (sorted)."""
    hits: list[str] = []
    for zone in board.get("zones", []):
        for zone_path in zone.get("paths", []):
            if path == zone_path or path.startswith(zone_path) or zone_path.startswith(path):
                hits.append(zone["id"])
                break
    return sorted(hits)


def _changed_files(base: str, ref: str, cwd: Path | None = None) -> list[str] | None:
    """Files changed between *base* and *ref*, or ``None`` when git failed.

    ``None`` (not ``[]``) is load-bearing: an empty list means "no differences",
    while ``None`` means "the ref could not be inspected".  Collapsing the two
    into ``[]`` would let a typo'd or unfetched ref classify as SAFE.
    """
    code, out, _ = _run_git("diff", "--name-only", base, ref, cwd=cwd)
    if code != 0:
        return None
    return [line for line in out.splitlines() if line.strip()]


def _unknown_refs(refs: list[str], cwd: Path | None = None) -> list[str]:
    """Refs git cannot resolve to a commit (sorted). Fail-closed input check."""
    return sorted(
        ref
        for ref in refs
        if _run_git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=cwd)[0] != 0
    )


def _is_ancestor(older: str, newer: str, cwd: Path | None = None) -> bool:
    """True when *older* is an ancestor of *newer* (stacked lineage)."""
    code, _, _ = _run_git("merge-base", "--is-ancestor", older, newer, cwd=cwd)
    return code == 0


def _conflict_hunks(base: str, a: str, b: str, path: str, cwd: Path | None = None) -> int:
    """Conflict-hunk count for a three-way merge of one file, or -1 if unknown."""
    import tempfile
    from pathlib import Path as _Path

    def blob(ref: str) -> str | None:
        code, out, _ = _run_git("show", f"{ref}:{path}", cwd=cwd)
        return out if code == 0 else None

    contents = {"base": blob(base), "a": blob(a), "b": blob(b)}
    if any(value is None for value in contents.values()):
        return -1
    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        for key, value in contents.items():
            paths[key] = _Path(tmp) / f"{key}.blob"
            paths[key].write_text(value or "", encoding="utf-8")
        proc = subprocess_run_merge(paths["a"], paths["base"], paths["b"])
        if proc is None:
            return -1
        return (
            sum(
                1
                for line in proc.splitlines()
                if line.startswith("<<<<<<<") or line.startswith(">>>>>>>")
            )
            // 2
        )


def subprocess_run_merge(ours: Path, base: Path, theirs: Path) -> str | None:
    import subprocess

    proc = subprocess.run(
        ["git", "merge-file", "-p", str(ours), str(base), str(theirs)],
        capture_output=True,
        text=True,
    )
    if proc.returncode < 0:
        return None
    return proc.stdout


def detect_collisions(
    board: dict, refs: list[str], base: str = "", repo: Path | None = None
) -> dict:
    """Pairwise collision report for *refs* (git refs or SHAs). Read-only.

    Deterministic: pairs and paths are sorted; no set/dict iteration order leaks
    into the output.  Never merges and never resolves — detect + classify only.

    Fail-closed: a ref git cannot resolve, a pair with no merge base and no
    supplied ``base``, or a diff git refuses to run all yield ``UNVERIFIABLE``
    (a bad classification) rather than ``SAFE_INDEPENDENT``.
    """
    security_zones = set(security_sensitive_zones(board))
    unknown_refs = _unknown_refs(refs, repo)
    pairs: list[dict] = []
    for i in range(len(refs)):
        for j in range(i + 1, len(refs)):
            ref_a, ref_b = refs[i], refs[j]
            if ref_a in unknown_refs or ref_b in unknown_refs:
                pairs.append(
                    {
                        "ref_a": ref_a,
                        "ref_b": ref_b,
                        "stacked": False,
                        "merge_base": None,
                        "overlap_files": [],
                        "files": [],
                        "classification": COLLISION_UNKNOWN,
                        "reason": "unknown ref (git could not resolve a commit)",
                    }
                )
                continue
            _, merge_base, _ = _run_git("merge-base", ref_a, ref_b, cwd=repo)
            stacked = _is_ancestor(ref_a, ref_b, repo) or _is_ancestor(ref_b, ref_a, repo)
            if not merge_base and base:
                # Shallow clone / unrelated histories: fall back to the supplied
                # base ref as the diff root (still detects shared rewritten files).
                merge_base = base
            diff_base = merge_base or base
            if not diff_base:
                pairs.append(
                    {
                        "ref_a": ref_a,
                        "ref_b": ref_b,
                        "stacked": stacked,
                        "merge_base": None,
                        "overlap_files": [],
                        "files": [],
                        "classification": COLLISION_UNKNOWN,
                        "reason": "no merge base; pass --base to diff unrelated histories",
                    }
                )
                continue
            changed_a = _changed_files(diff_base, ref_a, repo)
            changed_b = _changed_files(diff_base, ref_b, repo)
            if changed_a is None or changed_b is None:
                pairs.append(
                    {
                        "ref_a": ref_a,
                        "ref_b": ref_b,
                        "stacked": stacked,
                        "merge_base": merge_base or None,
                        "overlap_files": [],
                        "files": [],
                        "classification": COLLISION_UNKNOWN,
                        "reason": f"git diff failed against {diff_base}",
                    }
                )
                continue
            files_a = set(changed_a)
            files_b = set(changed_b)
            overlap = sorted(files_a & files_b)

            file_rows: list[dict] = []
            for path in overlap:
                if path in COORDINATION_FILES:
                    continue  # board.json is the shared medium; its churn is never a hazard
                zones = zones_for_path(board, path)
                security = bool(set(zones) & security_zones)
                hunks = _conflict_hunks(diff_base, ref_a, ref_b, path, repo) if diff_base else -1
                file_rows.append(
                    {
                        "path": path,
                        "zones": zones,
                        "security_sensitive": security,
                        "conflict_hunks": hunks,
                    }
                )

            if not file_rows:
                classification = COLLISION_SAFE
            elif any(row["security_sensitive"] for row in file_rows):
                classification = COLLISION_SECURITY
            elif any(row["conflict_hunks"] > 0 for row in file_rows):
                classification = COLLISION_RECONCILE
            else:
                classification = COLLISION_SAFE_OVERLAP

            pairs.append(
                {
                    "ref_a": ref_a,
                    "ref_b": ref_b,
                    "stacked": stacked,
                    "merge_base": merge_base or None,
                    "overlap_files": overlap,
                    "files": file_rows,
                    "classification": classification,
                }
            )
    bad = [p for p in pairs if p["classification"] in _COLLISION_BAD]
    return {
        "refs": list(refs),
        "security_sensitive_zones": sorted(security_zones),
        "pairs": pairs,
        "collision_count": len(bad),
    }


def cmd_collision(args: argparse.Namespace) -> int:
    board = load_board()
    refs = [r.strip() for r in args.refs.split(",") if r.strip()]
    if len(refs) < 2:
        print("collision: provide at least two refs, e.g. --refs SHA1,SHA2")
        return 2
    result = detect_collisions(
        board, refs, base=args.base, repo=Path(args.repo) if args.repo else None
    )
    if args.as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"collision preflight: {len(result['pairs'])} pair(s), "
            f"{result['collision_count']} dangerous"
        )
        print(f"  security-sensitive zones: {', '.join(result['security_sensitive_zones'])}")
        for pair in result["pairs"]:
            print(f"  {pair['ref_a'][:10]} × {pair['ref_b'][:10]} → {pair['classification']}")
            for row in pair["files"]:
                flag = " [SECURITY]" if row["security_sensitive"] else ""
                hunks = row["conflict_hunks"]
                hunk_txt = f" ({hunks} conflict hunks)" if hunks > 0 else ""
                print(f"      {row['path']}{flag}{hunk_txt}")
    if args.fail_on_collision and result["collision_count"]:
        return 1
    return 0


# ── validate: documented rule == executed rule ───────────────────────────────


def _claimable_statuses() -> set[str]:
    return {
        "queued",
        "available",
        "expired",
        "deferred",
        "available_sequenced_post_33",
        "available_sequenced_post_32",
        "available_sequenced_post_32_33",
        "assigned_to_B",
        "assigned_to_B_next",
        "assigned_to_E_pr33",
    }


def validate_board(board: dict, strict_new: bool = False) -> dict:
    """Mechanical governance invariants. Returns {errors, warnings, ok}.

    * ``evidence_required`` — required (ERROR) for the forward plan
      (``next_work``) and for every *active* claim; grandfathered open/legacy
      claims are WARN so historical board state is not rewritten.  New work
      cannot be claimed without it (``cmd_claim`` enforces that).
    * ``gates_owner`` — exactly one *active* claim may hold it.  0 or >1 is an
      ERROR (the protocol says exactly one gates steward).
    """
    errors: list[str] = []
    warnings: list[str] = []

    for entry in board.get("next_work", []):
        if not entry.get("evidence_required"):
            errors.append(f"next_work {entry.get('id')}: missing evidence_required")

    for claim in board.get("claims", []):
        status = claim.get("status")
        if not claim.get("evidence_required"):
            if _is_active_claim(claim):
                errors.append(f"active claim {claim['task']}: missing evidence_required")
            elif status in _claimable_statuses():
                message = f"legacy claimable {claim['task']}: missing evidence_required"
                (errors if strict_new else warnings).append(message)

    active_gates = [
        c["task"]
        for c in board.get("claims", [])
        if _is_active_claim(c) and c.get("gates_owner")
    ]
    if len(active_gates) != 1:
        errors.append(
            "gates_owner invariant: exactly one active gates owner required, found "
            f"{len(active_gates)} ({', '.join(active_gates) or 'none'})"
        )

    return {"errors": sorted(errors), "warnings": sorted(warnings), "ok": not errors}


def cmd_validate(args: argparse.Namespace) -> int:
    board = load_board()
    result = validate_board(board, strict_new=args.strict_new)
    if args.as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"validate: {len(result['errors'])} error(s), {len(result['warnings'])} warning(s)")
        for msg in result["errors"]:
            print(f"  ERROR   {msg}")
        for msg in result["warnings"]:
            print(f"  WARN    {msg}")
    return 0 if result["ok"] else 1


# ── evidence: a claimed test name must be derivable from a command ───────────


def resolve_evidence_names(ref: str, names: list[str], repo: Path | None = None) -> dict:
    """Resolve cited test names against a ref: file (``tests/**/NAME.py``) or
    ``def NAME(`` anywhere under ``tests/``. Read-only; deterministic output.

    Guards the PR-body failure mode where prose cites a test that never existed:
    the cited string is either present in the tree or it is not.
    """
    _, tree, _ = _run_git("ls-tree", "-r", "--name-only", ref, "tests/", cwd=repo)
    files = {Path(p).name: p for p in tree.splitlines() if p.strip()}
    results: list[dict] = []
    for name in names:
        file_path = files.get(f"{name}.py")
        code, out, _ = _run_git("grep", "-l", "-E", rf"def {name}\(", ref, "--", "tests/", cwd=repo)
        function_file = out.splitlines()[0] if code == 0 and out else None
        results.append(
            {
                "name": name,
                "file": file_path,
                "function_file": function_file,
                "found": bool(file_path or function_file),
            }
        )
    return {
        "ref": ref,
        "names": results,
        "missing": sorted(r["name"] for r in results if not r["found"]),
    }


def cmd_evidence(args: argparse.Namespace) -> int:
    names = [n.strip() for n in args.names.split(",") if n.strip()]
    if not names:
        print("evidence: provide --names test_a,test_b")
        return 2
    result = resolve_evidence_names(args.ref, names, repo=Path(args.repo) if args.repo else None)
    if args.as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"evidence: {len(result['names'])} cited name(s), {len(result['missing'])} unresolved"
        )
        for row in result["names"]:
            where = row["file"] or row["function_file"] or "—"
            mark = "OK " if row["found"] else "MISSING"
            print(f"  {mark} {row['name']}  ({where})")
    if args.fail_on_missing and result["missing"]:
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

    p = sub.add_parser(
        "collision",
        help="pre-push collision preflight: two correct PRs must not become one unsafe merge",
    )
    p.add_argument("--refs", required=True, help="comma-separated git refs/SHAs (>=2)")
    p.add_argument("--base", default="", help="fallback base ref when two refs share no history")
    p.add_argument("--repo", default="", help="repository path (default: the board repo)")
    p.add_argument(
        "--fail-on-collision",
        action="store_true",
        help="exit 1 when any pair is SECURITY_SENSITIVE or REQUIRES_MANUAL_RECONCILIATION",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_collision)

    p = sub.add_parser("validate", help="mechanical governance invariants (errors exit 1)")
    p.add_argument(
        "--strict-new",
        action="store_true",
        help="treat grandfathered legacy evidence_required gaps as errors too",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser(
        "evidence",
        help="resolve cited test names against a ref (a claimed name must exist)",
    )
    p.add_argument("--ref", required=True, help="git ref/SHA whose tree to search")
    p.add_argument("--names", required=True, help="comma-separated test names")
    p.add_argument("--repo", default="", help="repository path (default: the board repo)")
    p.add_argument(
        "--fail-on-missing",
        action="store_true",
        help="exit 1 when any cited name is not present in the tree",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_evidence)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
