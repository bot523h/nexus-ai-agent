#!/usr/bin/env python3
"""Multi-agent claim board CLI for NEXUS AI Agent.

Coordination channel for agents working on this repository from separate
sandboxes. The only shared medium between sandboxes is the git repo itself.
Since protocol 11 (ADR 0012, audit A-04) the live board is the coordination
ref ``refs/nexus/board`` on origin: every mutation below fetches the ref,
applies the change and publishes it with a compare-and-swap push in the same
command, so a claim is real the moment the command reports it published — and
no code branch or pull request ever carries board state.  ``.agents/board.json``
is a frozen archive (``guard``), used only with ``--store tree``.

Commands
--------
  show                          Print the board (auto-releases expired leases)
  claim TASK --branch BRANCH    Claim a queued/expired task (refuses if actively
                                claimed by another branch). A same-owner call is a
                                heartbeat renewal; taking a lease over advances the
                                lease's fencing generation.
  release TASK --branch BRANCH  Mark a finished claim done (free the zone) and
                                advance its generation.
  defer TASK --branch BRANCH --reason "..." [--fa "..."]
                                Record a deferral note ("I stopped because
                                another agent was working; resume later")
  next --branch BRANCH          Suggest the first claimable task
  check --files a,b,c --branch BRANCH
                                Overlap referee against refs/nexus/board — the
                                single authority (code trees are not sources).
                                Exit 0 when the ref is readable, has exactly one
                                live gates owner and no overlap; 1 on a proven
                                overlap (or a modified board archive in the
                                tree); 2 when the ref could not be read or is
                                inconsistent — never 0 on unverified data.
                                With --store tree the pre-protocol-11
                                multi-source referee runs instead ([--no-remote]
                                [--repo owner/name] [--token]).
  guard [--print]               Exit 1 when .agents/board.json differs from the
                                frozen archive digest (runs in CI on every PR).
  init-ref [--from FILE]        Bootstrap refs/nexus/board once from a board file;
                                refuses (exit 2) when the ref already exists.
  praudit [--pr-json F | --repo owner/name] [--fail-on-invisible] [--json]
                                Read-only audit: compare open GitHub PR changed
                                files against board exclusive_paths and report
                                PRs whose scope is invisible (no claim for the
                                head branch, or files outside every fence).

Lease fencing (task-219)
------------------------
Every claim carries a ``generation`` epoch. It advances whenever ownership
*transfers* (a new branch takes the lease, or the holder releases/defers it)
and stays stable across a same-owner heartbeat renewal. Mutating an *existing*
lease (``claim`` renewal, takeover of an expired lease, ``release``, ``defer``)
**requires** ``--expected-generation N`` and refuses with exit 2 when it is
absent or differs from the live generation: a stale owner that resumes after
the zone changed hands cannot mutate it. Taking a *free* lease (a fresh task, or
a legacy record at generation 0) needs no token — there is nothing to fence
against. The check is deterministic — a refusal, never a warning or a
best-effort log.

Board authority (protocol 11)
-----------------------------
``--store ref`` (default; env ``NEXUS_BOARD_STORE``): board = ``board.json`` on
``refs/nexus/board`` (override ``NEXUS_BOARD_REF``).  Writes are
``git push origin <sha>:<ref> --force-with-lease=<ref>:<tip we read>``; a lost
race is redone from a fresh read (3 attempts), then refused.  An unreadable or
unwritable ref is exit 2 — never a pass, never a fallback to the tree file.
``--store tree``: the pre-protocol-11 file + flock behaviour on
``.agents/board.json`` for fixtures and tests.  Pure stdlib + git.

Typical loop for an arriving agent:
    python scripts/agent_board.py show
    python scripts/agent_board.py next --branch $MY_BRANCH
    python scripts/agent_board.py claim feature-wiring-batch --branch $MY_BRANCH
    # published on refs/nexus/board by the command itself — nothing to commit
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / ".agents" / "board.json"

# Files every PR is expected to touch (coordination medium) — never counted as
# "uncovered scope" by praudit, because no claim exclusively owns them.
COORDINATION_FILES = frozenset({".agents/board.json"})

# ── Board authority (protocol 11, audit A-04) ─────────────────────────
# The live board is a *coordination ref*, not a file in the code tree.  Every
# claim/release/defer is one commit on that ref, written with a compare-and-swap
# push (``--force-with-lease=<ref>:<tip we read>``), so two sandboxes can never
# overwrite each other and a code PR never carries board state again.  The tree
# file ``.agents/board.json`` is a frozen archive of the last tree-resident
# board (pinned by ``FROZEN_BOARD_SHA256`` and the ``guard`` command); it is
# consulted only in ``--store tree`` mode (tests, offline fixtures).
BOARD_REF = os.environ.get("NEXUS_BOARD_REF", "refs/nexus/board")
BOARD_REF_FILE = "board.json"
FROZEN_BOARD_SHA256 = "94a7642f64606b4ec69dcfd2b255755d20cf27c0a706797de6e237ad25b0a160"
_STORE_ENV = "NEXUS_BOARD_STORE"
_CAS_ATTEMPTS = 3


class BoardSourceUnavailable(RuntimeError):
    """The authoritative board could not be read or written — never a pass."""


class BoardCASConflict(RuntimeError):
    """The ref moved between our read and our write; the mutation must be redone."""


def resolve_store(args: argparse.Namespace | None = None) -> str:
    """``ref`` (authoritative, default) or ``tree`` (frozen file; tests/offline).

    Precedence: explicit ``--store`` > ``NEXUS_BOARD_STORE`` env > ``ref``.
    """
    explicit = getattr(args, "store", None) if args is not None else None
    choice = explicit or os.environ.get(_STORE_ENV, "") or "ref"
    if choice not in ("ref", "tree"):
        raise SystemExit(f"unknown board store {choice!r} (expected 'ref' or 'tree')")
    return choice


def _plumbing(args: list[str], *, stdin: str | None = None) -> str:
    """Run a git plumbing command against ROOT; raise BoardSourceUnavailable on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            input=stdin,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BoardSourceUnavailable(f"git {args[0]} could not run: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise BoardSourceUnavailable(
            f"git {' '.join(args[:2])} failed: {detail[-1] if detail else 'no output'}"
        )
    return result.stdout


def fetch_ref_board() -> tuple[dict[str, Any], str]:
    """Fetch the coordination ref and return ``(board, tip_sha)``.

    The ref is fetched into the same local name so a stale local copy can never
    be mistaken for the remote tip: the tip used for compare-and-swap is the one
    this very fetch produced.  Any failure is ``BoardSourceUnavailable``.
    """
    _plumbing(["fetch", "--quiet", "origin", f"+{BOARD_REF}:{BOARD_REF}"])
    tip = _plumbing(["rev-parse", "--verify", f"{BOARD_REF}^{{commit}}"]).strip()
    raw = _plumbing(["show", f"{tip}:{BOARD_REF_FILE}"])
    try:
        board = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BoardSourceUnavailable(f"{BOARD_REF}@{tip[:7]} board.json is not valid JSON") from exc
    if not isinstance(board, dict) or board.get("schema") != 2:
        raise BoardSourceUnavailable(
            f"{BOARD_REF}@{tip[:7]} is not a schema-2 claim board (incompatible generation)"
        )
    if not isinstance(board.get("claims", []), list):
        raise BoardSourceUnavailable(f"{BOARD_REF}@{tip[:7]} claims is not a list")
    return board, tip


def _board_blob(board: dict[str, Any]) -> str:
    text = json.dumps(board, indent=2, ensure_ascii=False) + "\n"
    return _plumbing(["hash-object", "-w", "--stdin"], stdin=text).strip()


def _commit_board(board: dict[str, Any], parent: str | None, message: str) -> str:
    """Write ``board`` as a commit object (no working tree, no index) and return its sha."""
    blob = _board_blob(board)
    entries: list[str] = []
    if parent is not None:
        for line in _plumbing(["ls-tree", parent]).splitlines():
            meta, _, name = line.partition("\t")
            if name != BOARD_REF_FILE:
                entries.append(line)
    entries.append(f"100644 blob {blob}\t{BOARD_REF_FILE}")
    tree = _plumbing(["mktree"], stdin="\n".join(entries) + "\n").strip()
    commit_args = [*_identity_args(), "commit-tree", tree, "-m", message]
    if parent is not None:
        commit_args += ["-p", parent]
    return _plumbing(commit_args).strip()


def _identity_args() -> list[str]:
    """Keep the sandbox's own git identity; fall back to a marker identity if none is set."""
    probe = subprocess.run(
        ["git", "config", "--get", "user.email"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0 and probe.stdout.strip():
        return []
    return ["-c", "user.name=nexus-agent-board", "-c", "user.email=agent-board@nexus.invalid"]


def push_ref_board(board: dict[str, Any], expected_tip: str | None, message: str) -> str:
    """Compare-and-swap write: publish ``board`` on top of ``expected_tip``.

    ``--force-with-lease=<ref>:<expected>`` makes the remote reject the update
    when the ref no longer points at the tip we read (``BoardCASConflict``);
    an empty expectation means "the ref must not exist yet" (bootstrap).
    """
    commit = _commit_board(board, expected_tip, message)
    lease = f"--force-with-lease={BOARD_REF}:{expected_tip or ''}"
    try:
        result = subprocess.run(
            ["git", "push", "--quiet", "origin", f"{commit}:{BOARD_REF}", lease],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BoardSourceUnavailable(f"git push could not run: {exc}") from exc
    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip()
        if "stale info" in err or "rejected" in err or "fetch first" in err:
            raise BoardCASConflict(err.splitlines()[-1] if err else "push rejected")
        raise BoardSourceUnavailable(f"git push failed: {err.splitlines()[-1] if err else '?'}")
    # keep the local ref in step with what the remote accepted
    _plumbing(["update-ref", BOARD_REF, commit])
    return commit


# Active ref transaction: while set, load_board()/save_board()/board_lock()
# operate on this in-memory board instead of the tree file.  The runner below
# publishes it with compare-and-swap once the command returns.
_REF_TX: dict[str, Any] | None = None


def _ref_message(args: argparse.Namespace) -> str:
    cmd = getattr(args, "cmd", None) or getattr(getattr(args, "func", None), "__name__", "board")
    task = getattr(args, "task", "") or ""
    branch = getattr(args, "branch", "") or ""
    return f"board: {cmd} {task} ({branch})".strip()


def run_with_store(args: argparse.Namespace, fn: Callable[[argparse.Namespace], int]) -> int:
    """Execute a board command against the resolved store.

    ``tree``: the command runs exactly as before (file + flock).  ``ref``: the
    board is fetched, the command mutates it in memory, and the result is
    published with compare-and-swap; a lost race is *redone from a fresh read*
    (so a claim that became foreign in the meantime is refused, never
    overwritten).  Unreadable/unwritable authority is exit 2 — never a pass,
    never a silent fallback to the frozen tree file.
    """
    global _REF_TX
    if resolve_store(args) == "tree":
        return fn(args)
    for attempt in range(1, _CAS_ATTEMPTS + 1):
        try:
            board, tip = fetch_ref_board()
        except BoardSourceUnavailable as exc:
            print(f"UNVERIFIABLE: board authority {BOARD_REF} unreadable: {exc}")
            return 2
        _REF_TX = {"board": board, "tip": tip, "dirty": False}
        try:
            rc = int(fn(args))
            tx = _REF_TX
        finally:
            _REF_TX = None
        if not tx or not tx["dirty"]:
            return rc
        try:
            new_tip = push_ref_board(tx["board"], tip, _ref_message(args))
        except BoardCASConflict:
            print(f"[cas] {BOARD_REF} moved during attempt {attempt}; re-reading and redoing")
            continue
        except BoardSourceUnavailable as exc:
            print(f"UNVERIFIABLE: board authority {BOARD_REF} not writable: {exc}")
            return 2
        print(f"[board] published {BOARD_REF} {tip[:7]} → {new_tip[:7]}")
        return rc
    print(f"REFUSED: {BOARD_REF} kept moving for {_CAS_ATTEMPTS} attempts; retry later (exit 2)")
    return 2


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
    if _REF_TX is not None:
        return _REF_TX["board"]
    if not BOARD.exists():
        sys.exit(f"board not found: {BOARD}")
    return json.loads(BOARD.read_text(encoding="utf-8"))


def save_board(board: dict) -> None:
    board["updated_at"] = _iso(_now())
    if _REF_TX is not None:
        _REF_TX["board"] = board
        _REF_TX["dirty"] = True
        return
    BOARD.write_text(json.dumps(board, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class BoardLockedError(RuntimeError):
    """Another agent holds the board lock; the caller must retry, not proceed."""


@contextlib.contextmanager
def board_lock() -> Iterator[None]:
    """Hold the inter-process board lock for the duration of one mutation.

    Read-validate-write on a JSON file is a TOCTOU race: two agents can read the
    same generation, both validate, and both write — the second silently undoing
    the first (and defeating fencing, since the loser's token was valid when it
    read). This exclusive ``flock`` (the same primitive ``storage/migrations.py``
    uses) serialises board mutations across processes on one host. It does *not*
    span hosts: two sandboxes mutate separate clones and reconcile through git,
    where the fencing generation is the guard (a stale epoch is refused on push).
    Non-POSIX degrades to best-effort, exactly like the migration lock.

    Inside a ref transaction the compare-and-swap push *is* the lock, so no
    file lock is taken (and nothing under ``.agents/`` is touched).
    """
    if _REF_TX is not None:
        yield
        return
    BOARD.parent.mkdir(parents=True, exist_ok=True)
    lock_path = BOARD.with_suffix(".lock")
    try:
        import fcntl  # POSIX only
    except ImportError:  # pragma: no cover - non-POSIX best-effort
        yield
        return
    with lock_path.open("a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BoardLockedError(
                "the board is locked by another agent; retry in a moment"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def gc_expired(board: dict) -> list[str]:
    """Auto-release expired leases. Returns list of freed task names.

    The release is recorded in ``release_reason`` and the owner's ``note`` is
    left untouched: the note holds that session's unpushed evidence and handoff,
    so overwriting it here would silently destroy work (a real regression the
    previous hardening session had to work around). ``gc`` only ever adds the
    structured release reason.
    """
    freed: list[str] = []
    for claim in board.get("claims", []):
        claimed = _parse(claim.get("claimed_at"))
        if _is_active_claim(claim) and claimed is not None:
            expires = claimed + timedelta(hours=int(claim.get("ttl_hours", 24)))
            if _now() > expires:
                previous_status = claim.get("status")
                claim["status"] = "expired"
                # A departed lease holder also loses the gates: an expired
                # gates owner must not keep the single-gate role. Provenance
                # (agent_branch, claimed_at) is kept — the record stays honest.
                claim["gates_owner"] = False
                claim["release_reason"] = (
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


def _generation(claim: dict) -> int:
    """Fencing epoch of a lease. Legacy boards without the field read as 0.

    The epoch increments on every *transfer of ownership* (a new branch taking
    the lease, or the holder releasing/deferring it) and stays stable across a
    same-owner heartbeat renewal. A superseded owner that resumes after its
    lease changed hands therefore holds a stale epoch, which
    ``_stale_generation`` uses to refuse its mutation (task-219).
    """
    raw = claim.get("generation", 0)
    return raw if isinstance(raw, int) and raw >= 0 else 0


def _stale_generation(
    args: argparse.Namespace, claim: dict, *, required: bool = False
) -> str | None:
    """Refusal message when the caller's ``--expected-generation`` no longer matches.

    A fencing check, not a warning: a resumed stale owner must be rejected
    deterministically, never best-effort. When ``required`` is set (any mutation
    of an *existing* live lease) an absent ``--expected-generation`` is itself a
    refusal: without a token to compare, a stale owner that never recorded the
    epoch it held could still mutate a zone that changed hands (task-219).
    """
    expected = getattr(args, "expected_generation", None)
    if expected is None:
        if required:
            return (
                f"REFUSED: mutating the live lease for {claim['task']} requires "
                "--expected-generation N (a fencing token). Read the current generation with "
                "`show` and pass it; taking a *free* lease needs no token (task-219)."
            )
        return None
    current = _generation(claim)
    if int(expected) == current:
        return None
    return (
        f"REFUSED: stale lease generation for {claim['task']} — you hold gen {expected}, "
        f"the live lease is gen {current}. Another holder took or released this zone after "
        "you started; your mutation is fenced out (task-219)."
    )


def _conflicting_paths(board: dict, branch: str, files: list[str]) -> list[tuple[str, str]]:
    """Return overlaps between *files* and other branches' live exclusive paths."""
    hits: list[tuple[str, str]] = []
    for claim in board.get("claims", []):
        if not _claim_live(claim):
            continue
        if branch and claim.get("agent_branch") == branch:
            continue
        for excl in claim.get("exclusive_paths", []):
            for f in files:
                fp = f.strip()
                if not fp:
                    continue
                if _path_matches(excl, fp):
                    hits.append((claim["task"], fp))
    return hits


def _active_gates_owners(board: dict) -> tuple[list[dict], list[str]]:
    """Return live gates owners and malformed claims that block verification."""
    owners: list[dict] = []
    malformed: list[str] = []
    for claim in board.get("claims", []):
        if not claim.get("gates_owner"):
            continue
        try:
            live = _claim_live(claim)
        except (TypeError, ValueError, OverflowError) as exc:
            malformed.append(f"{claim.get('task', '<unknown>')}: {exc}")
            continue
        if live:
            owners.append(claim)
    return owners, malformed


# ── commands ──────────────────────────────────────────────────────────


def cmd_show(_args: argparse.Namespace) -> int:
    with board_lock():
        board = load_board()
        freed = gc_expired(board)
        if freed:
            save_board(board)
    if freed:
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
            f"  generation: {_generation(claim)} (fencing epoch)\n"
            f"  scope: {claim.get('scope', '')}"
        )
        if claim.get("release_reason"):
            print(f"  released: {claim['release_reason']}")
    for entry in board.get("deferred_log", []):
        print(f"\n⏸ DEFERRED {entry['task']} by {entry.get('deferred_by_branch')}")
        print(f"   fa: {entry.get('reason_fa', '')}")
        print(f"   en: {entry.get('reason_en', '')}")
        print(f"   resume_when: {entry.get('resume_when', '—')}")
    print(f"\ngates rule: {board.get('gates_rule', '—')}")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    with board_lock():
        return _claim_locked(args)


def _claim_locked(args: argparse.Namespace) -> int:
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
            refusal = _stale_generation(args, claim, required=True)
            if refusal:
                print(refusal)
                return 2
            claim["claimed_at"] = _iso(_now())  # renewal (heartbeat)
            save_board(board)
            print(
                f"renewed lease for {args.task} (owner: {args.branch}, "
                f"generation {_generation(claim)} — heartbeat keeps the epoch)"
            )
            return 0
        print(
            f"task {args.task} is ACTIVELY claimed by"
            f" {claim.get('agent_branch')} (status {claim.get('status')}, expires {expires})"
        )
        print(STOP_BANNER)
        return 2
    # Not currently active: this is a brand-new claim, a takeover of an expired
    # lease, or a re-claim of a deferred/done one. A takeover/re-claim rewrites
    # an *existing* lease record, so it must carry the fencing token too —
    # otherwise a resumed stale owner could re-claim (and effectively seize) a
    # zone that changed hands while it was away. A truly fresh task (no record,
    # or generation 0) needs no token: there is nothing to fence against.
    if _generation(claim) > 0:
        refusal = _stale_generation(args, claim, required=True)
        if refusal:
            print(refusal)
            return 2
    claim.update(
        status="active",
        agent_branch=args.branch,
        claimed_at=_iso(_now()),
        ttl_hours=int(args.ttl),
        gates_owner=bool(args.gates),
        note="",
    )
    # A takeover/re-claim of a lease that ``gc`` freed must not keep gc's
    # "auto-released" reason on what is now a live lease. Retire it into
    # ``previous_release_reason`` so the record stays honest (task-220).
    retired = claim.pop("release_reason", None)
    if retired:
        claim["previous_release_reason"] = retired
    # Fencing epoch: strictly monotonic across every (re)claim. A brand-new or
    # legacy claim (generation 0) becomes 1; a takeover or re-claim advances it
    # so any superseded holder's recorded expectation is invalidated — including
    # a second session sharing the same branch name.
    claim["generation"] = _generation(claim) + 1
    if args.gates:
        for other in board["claims"]:
            if other["task"] != args.task and other.get("gates_owner"):
                other["gates_owner"] = False
    save_board(board)
    print(
        f"CLAIMED {args.task} for {args.branch} (ttl {args.ttl}h, gates_owner={args.gates}, "
        f"generation {claim['generation']})"
    )
    if _REF_TX is not None:
        print(f"the claim is published on {BOARD_REF} by this command (compare-and-swap);")
        print("nothing in the code tree changes — do NOT add .agents/board.json to your PR.")
    else:
        print("NOW: git add .agents/board.json && git commit && git push IMMEDIATELY —")
        print("an unpushed claim does not exist for the other sandbox.")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    with board_lock():
        return _release_locked(args)


def _release_locked(args: argparse.Namespace) -> int:
    board = load_board()
    claim = _find(board, args.task)
    if claim is None:
        print(f"task not found: {args.task}")
        return 2
    if claim.get("agent_branch") != args.branch:
        print(f"refusing: {args.task} belongs to {claim.get('agent_branch')}, not {args.branch}")
        return 2
    refusal = _stale_generation(args, claim, required=True)
    if refusal:
        print(refusal)
        return 2
    claim.update(status="done", agent_branch="", claimed_at=None, gates_owner=False)
    # Releasing transfers the zone: advance the epoch so a stale holder cannot
    # act as if it still owns the lease.
    claim["generation"] = _generation(claim) + 1
    save_board(board)
    print(
        f"RELEASED {args.task} (generation {claim['generation']}). "
        "Zone free — next agent can claim."
    )
    return 0


def cmd_defer(args: argparse.Namespace) -> int:
    with board_lock():
        return _defer_locked(args)


def _defer_locked(args: argparse.Namespace) -> int:
    board = load_board()
    claim = _find(board, args.task)
    if claim is not None and claim.get("agent_branch") == args.branch and _is_active_claim(claim):
        refusal = _stale_generation(args, claim, required=True)
        if refusal:
            print(refusal)
            return 2
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
        claim["generation"] = _generation(claim) + 1
    save_board(board)
    print(f"DEFERRED {args.task} by {args.branch} — note recorded so nothing is forgotten.")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    with board_lock():
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


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    """Run a git command; ``None`` when git is missing or the call cannot start.

    Callers treat ``None`` as "unverifiable", never as "clean".
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _worktree_boards(exclude: Path) -> tuple[list[Path], bool]:
    """Sibling worktree board paths, and whether enumeration was reliable.

    ``git worktree list --porcelain`` is the only reliable way to see other
    checkouts sharing this repository. A non-zero exit (not a repo, git absent)
    is *unverifiable*, so the caller must not claim a global pass.
    """
    result = _git(["worktree", "list", "--porcelain"], ROOT)
    if result is None or result.returncode != 0:
        return [], False
    boards: list[Path] = []
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        try:
            root = Path(line[len("worktree ") :].strip()).resolve()
        except OSError:
            continue
        if root == exclude.resolve():
            continue
        candidate = root / ".agents" / "board.json"
        if candidate.exists():
            boards.append(candidate)
    return boards, True


def _load_board_from(path: Path) -> dict:
    """Load and shape-check a board file. Raises ``OSError``/``ValueError``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("claims", []), list):
        raise ValueError(f"not a claim board: {path}")
    return data


def _read_remote_board() -> tuple[dict | None, str]:
    """Best-effort read of ``origin/main``'s board without mutating the tree.

    Returns ``(board_or_None, reason)``. ``None`` always carries a reason the
    caller prints before exiting 2 — a remote we cannot read is never a pass.
    """
    remotes = _git(["remote"], ROOT)
    if remotes is None:
        return None, "git is unavailable"
    if "origin" not in remotes.stdout.split():
        return None, "no 'origin' remote is configured"
    fetched = _git(["fetch", "--quiet", "origin", "main"], ROOT)
    if fetched is None or fetched.returncode != 0:
        return None, "git fetch origin main failed (network or remote unavailable)"
    show = _git(["show", "origin/main:.agents/board.json"], ROOT)
    if show is None or show.returncode != 0:
        return None, "origin/main has no readable .agents/board.json"
    try:
        data = json.loads(show.stdout)
    except json.JSONDecodeError:
        return None, "origin/main board is not valid JSON"
    if not isinstance(data, dict):
        return None, "origin/main board is not a JSON object"
    return data, ""


_REMOTE_BRANCH_FETCH_LIMIT = 25


def _open_pr_branches(repo: str, token: str | None) -> tuple[list[str], str]:
    """Head branch names of every open PR. ``([], reason)`` when unavailable.

    A claim is real once pushed, so a lease that lives *only* on a pushed branch
    with an open PR is a real lease — yet such a branch is invisible to every
    other source: it is not in the local tree, not a sibling worktree, and not
    ``origin/main``. The open-PR list is how the referee learns those branch
    names. It is a *required* source: an unreadable list degrades to exit 2,
    never to a clean pass.
    """
    if "/" not in repo:
        return [], "GITHUB_REPOSITORY unset and --repo not given (cannot enumerate PR branches)"
    if not token:
        return [], "no GitHub token in the environment (cannot enumerate open-PR branches)"
    try:
        pulls = _gh_get(f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=100", token)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return [], f"open-PR API unavailable ({exc})"
    if not isinstance(pulls, list):
        return [], "open-PR API returned an unexpected payload"
    branches: list[str] = []
    for pr in pulls:
        head = (pr.get("head") or {}).get("ref") if isinstance(pr, dict) else None
        if isinstance(head, str) and head:
            branches.append(head)
    return branches, ""


def _remote_branch_boards(
    branches: list[str], exclude_branch: str = ""
) -> tuple[list[tuple[str, dict]], list[str]]:
    """Read the board of each *pushed* branch (read-only).

    Each branch is fetched with an explicit refspec into its own
    ``refs/remotes/origin/<branch>`` tracking ref (a ref write that never
    touches the working tree) and its board is read with ``git show``. A board
    is read **only** when that branch's fetch succeeded in this invocation, so a
    stale ref left by an earlier run can never masquerade as fresh. A branch
    that cannot be fetched, or whose board cannot be parsed, is *unverifiable*,
    never skipped.
    """
    targets: list[str] = []
    for branch in branches:
        if branch and branch != exclude_branch and branch not in targets:
            targets.append(branch)
    targets = targets[:_REMOTE_BRANCH_FETCH_LIMIT]
    if not targets:
        return [], []

    unreadable: list[str] = []
    # One batched round trip is the fast path; a single dead ref (e.g. a branch
    # deleted between the PR list and the fetch) fails the whole batch, so fall
    # back to per-branch fetches rather than losing every other branch.
    batch = _git(
        [
            "fetch",
            "--quiet",
            "origin",
            *[f"+refs/heads/{b}:refs/remotes/origin/{b}" for b in targets],
        ],
        ROOT,
    )
    if batch is not None and batch.returncode == 0:
        fresh = set(targets)
    else:
        fresh = set()
        for branch in targets:
            fetched = _git(
                [
                    "fetch",
                    "--quiet",
                    "origin",
                    f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                ],
                ROOT,
            )
            if fetched is not None and fetched.returncode == 0:
                fresh.add(branch)
            else:
                unreadable.append(f"pushed branch {branch}: git fetch failed (unreachable?)")

    boards: list[tuple[str, dict]] = []
    for branch in targets:
        if branch not in fresh:
            continue
        show = _git(["show", f"refs/remotes/origin/{branch}:.agents/board.json"], ROOT)
        if show is None or show.returncode != 0:
            unreadable.append(f"pushed branch {branch}: no readable .agents/board.json")
            continue
        try:
            data = json.loads(show.stdout)
        except json.JSONDecodeError:
            unreadable.append(f"pushed branch {branch}: board is not valid JSON")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("claims", []), list):
            unreadable.append(f"pushed branch {branch}: board is not a claim board")
            continue
        boards.append((f"branch:{branch}", data))
    return boards, unreadable


def cmd_check(args: argparse.Namespace) -> int:
    """Multi-source referee: exit 0 clean, 1 overlap, 2 unverifiable.

    The local board is always the first source. Unless ``--no-remote`` is set,
    every *other* live git worktree's board, ``origin/main``'s board, and every
    open-PR branch's pushed board are also consulted — a claim is only real once
    pushed, and a lease can live only on a pushed branch (open PR, not yet in
    ``origin/main``, not checked out locally). A source that cannot be read is a
    loud exit 2: "no overlap" is only honest when every consulted source was
    actually read.
    """
    if resolve_store(args) == "ref":
        return _check_ref(args)
    files = [f for f in args.files.split(",") if f.strip()]
    sources: list[tuple[str, dict]] = []
    unreadable: list[str] = []
    no_remote = bool(getattr(args, "no_remote", False))

    # 1. local board (mandatory). A missing or unparseable local board is
    #    UNVERIFIABLE (exit 2), never a crash and never a silent pass: the whole
    #    point of the referee is that "no overlap" is a claim about read data.
    if not BOARD.exists():
        print(f"UNVERIFIABLE: board not found: {BOARD}")
        return 2
    try:
        local_board = _load_board_from(BOARD)
    except (OSError, ValueError) as exc:
        print(f"UNVERIFIABLE: local board cannot be read: {exc}")
        return 2
    sources.append(("local", local_board))

    # 2. sibling worktree boards + origin/main + pushed open-PR branches
    #    (unless narrowed)
    if no_remote:
        scope = "local (--no-remote: local board only; NOT a global pass)"
    else:
        scope = "local + sibling worktrees + origin/main + pushed open-PR branches"
        worktree_boards, reliable = _worktree_boards(exclude=ROOT)
        if not reliable:
            unreadable.append("git worktree enumeration (git unavailable or not a repository)")
        for board_path in worktree_boards:
            try:
                sources.append((str(board_path), _load_board_from(board_path)))
            except (OSError, ValueError) as exc:
                unreadable.append(f"{board_path}: {exc}")
        remote_board, reason = _read_remote_board()
        if remote_board is None:
            unreadable.append(f"origin/main: {reason}")
        else:
            sources.append(("origin/main", remote_board))

        # A lease can live *only* on a pushed branch with an open PR (not in
        # main, not a worktree). That source is required: if the open-PR list or
        # any branch board cannot be read, exit 2 — never a false clear.
        repo = getattr(args, "repo", "") or os.environ.get("GITHUB_REPOSITORY", "")
        token = getattr(args, "token", "") or os.environ.get("GITHUB_TOKEN", "")
        pr_branches, pr_reason = _open_pr_branches(repo, token)
        if pr_reason:
            unreadable.append(f"open-PR branches: {pr_reason}")
        else:
            branch_boards, branch_unreadable = _remote_branch_boards(
                pr_branches, exclude_branch=args.branch or ""
            )
            sources.extend(branch_boards)
            unreadable.extend(branch_unreadable)

    if unreadable:
        print("UNVERIFIABLE sources (will only matter if no conflict is proven):")
        for reason in unreadable:
            print(f"  - {reason}")

    # 3. overlap across every readable source (deduped by task+path, sources merged).
    #    A *proven* conflict is exit 1 even when another source is unreadable:
    #    the conflict is confirmed, and hiding it behind exit 2 would be worse.
    aggregate: dict[tuple[str, str], set[str]] = {}
    for source, board in sources:
        try:
            hits = _conflicting_paths(board, args.branch or "", files)
        except (TypeError, ValueError) as exc:
            # A malformed board must not crash the referee: it degrades to
            # "this source is unverifiable" (exit 2), never to a pass.
            unreadable.append(f"{source}: malformed claim data ({exc})")
            continue
        for task, path in hits:
            aggregate.setdefault((task, path), set()).add(source)

    print(f"check scope: {scope} — {len(sources)} source(s) read")
    if no_remote:
        print("  NOTE: --no-remote is a LOCAL verdict (this board only); it is NOT a global pass.")
    if aggregate:
        print("OVERLAP with another agent's active or active-in-review exclusive paths:")
        for (task, path), srcs in sorted(aggregate.items()):
            print(f"  {path}  ← claimed by {task} (seen in: {', '.join(sorted(srcs))})")
        print(STOP_BANNER)
        return 1

    owners, malformed_owners = _active_gates_owners(local_board)
    if not no_remote and (malformed_owners or len(owners) != 1):
        print(
            "UNVERIFIABLE: exactly one active, unexpired gates_owner is required; "
            f"found {len(owners)} (zero or multiple is fail-closed)"
        )
        for reason in malformed_owners:
            print(f"  malformed gates-owner claim: {reason}")
        return 2

    if unreadable:
        print("UNVERIFIABLE: cannot establish the absence of overlap — source(s) unreadable:")
        for reason in unreadable:
            print(f"  - {reason}")
        print("  (exit 2 — do NOT treat this as a pass; rerun once the source is reachable)")
        return 2

    print("no overlap — safe to proceed (every consulted source was readable).")
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
        files_complete = bool(pr.get("files_complete", not pr.get("files_truncated", False)))
        if not head:
            reasons.append("missing head branch")
        if not files_complete:
            reasons.append("changed-file source is incomplete")
        if not own_claims:
            reasons.append("no live board claim for head branch")
        if uncovered:
            reasons.append(f"{len(uncovered)} file(s) outside every exclusive_paths")

        if not head or not files_complete:
            classification = "BLOCKED"
        elif own_claims and not uncovered:
            classification = "OWNED"
        elif not uncovered:
            classification = "SCOPED"
        else:
            classification = "BLOCKED"

        results.append(
            {
                "number": number,
                "title": title,
                "head_branch": head,
                "files": files,
                "own_claims": own_claims,
                "foreign_fences": foreign,
                "uncovered_files": uncovered,
                "files_complete": files_complete,
                "classification": classification,
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
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_open_prs(repo: str, token: str | None = None) -> list[dict]:
    """Live open-PR snapshot (GitHub REST, stdlib urllib)."""
    pulls: list[dict] = []
    page = 1
    while True:
        rows = _gh_get(
            f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=100&page={page}",
            token,
        )
        if not isinstance(rows, list):
            raise ValueError("GitHub open-PR response is not a list")
        pulls.extend(rows)
        if len(rows) < 100:
            break
        page += 1
    out: list[dict] = []
    for pr in pulls:
        number = pr["number"]
        detail = _gh_get(f"https://api.github.com/repos/{repo}/pulls/{number}", token)
        expected_files = detail.get("changed_files") if isinstance(detail, dict) else None
        file_rows: list[dict] = []
        page = 1
        files_complete = True
        while True:
            rows = _gh_get(
                f"https://api.github.com/repos/{repo}/pulls/{number}/files?"
                f"per_page=100&page={page}",
                token,
            )
            if not isinstance(rows, list):
                raise ValueError(f"GitHub files response for PR #{number} is not a list")
            file_rows.extend(rows)
            if len(rows) < 100:
                break
            if len(file_rows) >= 3000:
                files_complete = isinstance(expected_files, int) and expected_files <= len(
                    file_rows
                )
                break
            page += 1
        out.append(
            {
                "number": number,
                "title": pr.get("title", ""),
                "head_branch": (pr.get("head") or {}).get("ref", ""),
                "files": [row["filename"] for row in file_rows],
                "files_complete": files_complete,
                "base_ref": (pr.get("base") or {}).get("ref", ""),
                "base_sha": (pr.get("base") or {}).get("sha", ""),
                "head_sha": (pr.get("head") or {}).get("sha", ""),
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


def _archive_drift() -> str | None:
    """Return a message when the frozen tree archive was modified, else None."""
    if not BOARD.exists():
        return f"{BOARD.relative_to(ROOT)} is missing (expected frozen archive)"
    digest = hashlib.sha256(BOARD.read_bytes()).hexdigest()
    if digest != FROZEN_BOARD_SHA256:
        return (
            f"{BOARD.relative_to(ROOT)} differs from the frozen archive "
            f"(sha256 {digest[:12]}… ≠ {FROZEN_BOARD_SHA256[:12]}…)"
        )
    return None


def _check_ref(args: argparse.Namespace) -> int:
    """Overlap referee against the coordination ref — the *only* source.

    Worktrees, origin/main and PR branches are not consulted: with the board on
    its own ref there is exactly one place a live claim can exist, so a stale or
    hostile ``.agents/board.json`` in any code tree (including ours) cannot grant
    or hide a claim.  Exit 0 clear, 1 overlap, 2 unverifiable.
    """
    files = [f.strip() for f in args.files.split(",") if f.strip()]
    branch = args.branch or ""
    try:
        board, tip = fetch_ref_board()
    except BoardSourceUnavailable as exc:
        print(f"UNVERIFIABLE: board authority {BOARD_REF} unreadable: {exc}")
        print(STOP_BANNER)
        return 2
    print(f"check scope: {BOARD_REF}@{tip[:7]} (single authority; code trees are not sources)")
    try:
        hits = _conflicting_paths(board, branch, files)
    except (TypeError, ValueError, AttributeError) as exc:
        print(f"UNVERIFIABLE: {BOARD_REF}@{tip[:7]} has a malformed claim: {exc}")
        print(STOP_BANNER)
        return 2
    rc = 0
    if hits:
        print(f"OVERLAP with {len(hits)} active claim(s) on {BOARD_REF}:")
        for task, path in hits:
            print(f"  {path}  ← {task}")
        rc = 1
    owners, malformed = _active_gates_owners(board)
    if malformed:
        print(f"UNVERIFIABLE: malformed gates_owner claims on {BOARD_REF}: {', '.join(malformed)}")
        rc = 2
    elif len(owners) != 1:
        names = ", ".join(str(o.get("task", "?")) for o in owners) or "-"
        print(
            f"UNVERIFIABLE: expected exactly one live gates_owner on {BOARD_REF}, "
            f"found {len(owners)}: {names}"
        )
        rc = 2
    drift = _archive_drift()
    if drift is not None:
        print(f"ARCHIVE MODIFIED: {drift}")
        print("fix: git checkout origin/main -- .agents/board.json  (board state lives on the ref)")
        rc = max(rc, 1)
    if rc == 0:
        print(f"no overlap on {BOARD_REF}@{tip[:7]} — safe to proceed")
    else:
        print(STOP_BANNER)
    return rc


def cmd_guard(args: argparse.Namespace) -> int:
    """Fail (exit 1) when the frozen tree archive ``.agents/board.json`` was edited.

    Run in CI on every pull request: after the move to the coordination ref,
    *no* code PR may carry board state.  ``--print`` shows the digests.
    """
    drift = _archive_drift()
    if getattr(args, "print_digest", False):
        actual = hashlib.sha256(BOARD.read_bytes()).hexdigest() if BOARD.exists() else "-"
        print(f"frozen={FROZEN_BOARD_SHA256}\nactual={actual}")
    if drift is not None:
        print(f"BOARD ARCHIVE MODIFIED: {drift}")
        print("The live board is the coordination ref; code PRs must not carry board edits.")
        print("fix: git checkout origin/main -- .agents/board.json")
        return 1
    print("board archive frozen (sha256 matches) — OK")
    return 0


def cmd_init_ref(args: argparse.Namespace) -> int:
    """Bootstrap the coordination ref from a board file (refuses if the ref exists)."""
    source = Path(args.source) if args.source else BOARD
    try:
        board = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"UNVERIFIABLE: cannot read seed board {source}: {exc}")
        return 2
    if not isinstance(board, dict) or board.get("schema") != 2:
        print(f"REFUSED: {source} is not a schema-2 board")
        return 2
    protocol = board.setdefault("protocol", {})
    if isinstance(protocol, dict):
        protocol["board_authority"] = {
            "ref": BOARD_REF,
            "file": BOARD_REF_FILE,
            "write": "scripts/agent_board.py claim|release|defer (compare-and-swap push)",
            "tree_copy": ".agents/board.json is a frozen archive (see `guard`)",
        }
    try:
        existing = _plumbing(["ls-remote", "--exit-code", "origin", BOARD_REF]).strip()
    except BoardSourceUnavailable:
        existing = ""  # ls-remote --exit-code returns 2 when the ref is absent
    if existing:
        print(f"REFUSED: {BOARD_REF} already exists on origin ({existing.split()[0][:7]}); ")
        print("init-ref never overwrites it — use claim/release/defer, which compare-and-swap")
        return 2
    try:
        tip = push_ref_board(board, None, "board: bootstrap coordination ref (A-04, protocol 11)")
    except BoardCASConflict:
        print(f"REFUSED: {BOARD_REF} already exists on origin; init-ref never overwrites it")
        return 2
    except BoardSourceUnavailable as exc:
        print(f"UNVERIFIABLE: {exc}")
        return 2
    print(f"[board] {BOARD_REF} created at {tip} from {source}")
    return 0


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
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
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
    parser.add_argument(
        "--store",
        choices=("ref", "tree"),
        default=None,
        help=(
            f"board authority: 'ref' = coordination ref {BOARD_REF} (default, fetch + "
            "compare-and-swap push); 'tree' = frozen .agents/board.json (tests/offline only). "
            f"Env fallback: {_STORE_ENV}."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show").set_defaults(func=cmd_show)

    p = sub.add_parser("claim")
    p.add_argument("task")
    p.add_argument("--branch", required=True)
    p.add_argument("--ttl", type=int, default=24)
    p.add_argument("--gates", action="store_true", help="this agent owns CI gates while active")
    p.add_argument(
        "--expected-generation",
        dest="expected_generation",
        type=int,
        default=None,
        help="fencing guard: refuse unless the lease's generation matches (task-219)",
    )
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("release")
    p.add_argument("task")
    p.add_argument("--branch", required=True)
    p.add_argument(
        "--expected-generation",
        dest="expected_generation",
        type=int,
        default=None,
        help="fencing guard: refuse unless the lease's generation matches (task-219)",
    )
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("defer")
    p.add_argument("task")
    p.add_argument("--branch", required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--fa", default="")
    p.add_argument("--resume-when", dest="resume_when", default="")
    p.add_argument(
        "--expected-generation",
        dest="expected_generation",
        type=int,
        default=None,
        help="fencing guard: refuse unless the lease's generation matches (task-219)",
    )
    p.set_defaults(func=cmd_defer)

    p = sub.add_parser("next")
    p.add_argument("--branch", required=True)
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("check")
    p.add_argument("--files", required=True, help="comma-separated changed file paths")
    p.add_argument("--branch", default="")
    p.add_argument(
        "--no-remote",
        dest="no_remote",
        action="store_true",
        help="narrow to the local board only (exit 0 is then a LOCAL verdict, not a global pass)",
    )
    p.add_argument(
        "--repo",
        default="",
        help="GitHub repo owner/name for the open-PR branch source (else GITHUB_REPOSITORY)",
    )
    p.add_argument("--token", default="", help="GitHub token (else GITHUB_TOKEN env)")
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

    p = sub.add_parser("guard", help="fail when the frozen tree archive was modified")
    p.add_argument("--print", dest="print_digest", action="store_true", help="show digests")
    p.set_defaults(func=cmd_guard)

    p = sub.add_parser("init-ref", help=f"bootstrap {BOARD_REF} once from a board file")
    p.add_argument("--from", dest="source", default="", help="seed board path (default tree)")
    p.set_defaults(func=cmd_init_ref)

    args = parser.parse_args()
    direct = {cmd_check, cmd_guard, cmd_init_ref}
    try:
        if args.func in direct:
            return int(args.func(args))
        return int(run_with_store(args, args.func))
    except BoardLockedError as exc:
        print(f"UNVERIFIABLE: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
