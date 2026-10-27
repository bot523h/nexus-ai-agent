"""RED probes for the two reviewer findings of PR#202 (hermetic, driven via cmd_check).

The LOCAL board is clean w.r.t. the checked file, so the exit code is decided by
the PR-source path under test — the flaw the first version of this probe had.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]  # <repo>/.agents/evidence/<this file>


@dataclass(frozen=True)
class _Completed:
    """Stand-in for a finished git/gh call (subprocess.CompletedProcess shape).

    A plain value object: these probes never spawn a process, so there is no
    command to inject into — using the stdlib class name here only tripped a
    static "dangerous subprocess" heuristic.
    """

    args: list
    returncode: int
    stdout: str = ""
    stderr: str = ""


spec = importlib.util.spec_from_file_location("probe2", REPO / "scripts" / "agent_board.py")
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


def zone_board(zone, task="synthetic-gate"):
    return {
        "schema": 2,
        "protocol": {"protocol_version": 2},
        "claims": [
            {
                "task": task,
                "status": "active",
                "zone": "z",
                "agent_branch": "arena/111-other",
                "claimed_at": "2026-01-01T00:00:00Z",
                "ttl_hours": 24,
                "gates_owner": True,
                "exclusive_paths": [zone],
            }
        ],
        "zones": [{"id": "z", "paths": [zone]}],
        "deferred_log": [],
        "next_work": [],
        "history": {"waves_closed": [], "pull_requests": [], "incidents": []},
    }


CHECKED_FILE = "src/mine/engine.py"
# A private, randomly named directory (mode 0700) that is removed when the probe
# exits: a fixed path in a shared /tmp is a symlink-planting target (CWE-59/377),
# and this probe must be safe to run on any machine.
_fixture_dir = tempfile.TemporaryDirectory(prefix="task266-red-probe-")
_board_path = Path(_fixture_dir.name) / "board2.json"
_board_path.write_text(
    json.dumps(zone_board("src/other/")), encoding="utf-8"
)  # clean for CHECKED_FILE
m.BOARD = _board_path
claimed = datetime.strptime("2026-01-01T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ").replace(
    tzinfo=timezone.utc
)
m._now = lambda: claimed + timedelta(seconds=1)

CONFLICT = zone_board("src/mine/")  # a lease on the checked file, living in the PR head
CLEAN = {"claims": []}
ORIGIN = "o/r"


def head(n, ref, repo=ORIGIN, clone=None):
    return {
        "number": n,
        "head": {
            "ref": ref,
            "repo": {"full_name": repo, "clone_url": clone or f"https://github.com/{repo}.git"},
        },
    }


def run(label, gh_get, git):
    m._gh_get = gh_get
    SEEN_SHOWS.clear()

    def _recording_git(args, cwd):
        if args and args[0] == "show":
            SEEN_SHOWS.append(args)
        return git(args, cwd)

    m._git = _recording_git
    m._worktree_boards = lambda exclude: ([], True)
    m._read_remote_board = lambda: ({"claims": []}, "")  # origin/main itself: clean
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = m.cmd_check(
            SimpleNamespace(
                files=CHECKED_FILE,
                branch="arena/b561a2be-nexus-ai-agent",
                no_remote=False,
                repo=ORIGIN,
                token="t",
            )
        )
    out = buf.getvalue()
    sources = next((line for line in out.splitlines() if "source(s) read" in line), "?")
    print(f"  {label}: exit={code} | {sources.strip()}")
    return code, out


FAILS: list[str] = []
SEEN_SHOWS: list[list[str]] = []


def want(label, got, expected, note=""):
    ok = got == expected
    print(f"    want exit {expected} -> {'GREEN' if ok else 'RED — ' + note}")
    if not ok:
        FAILS.append(label)


# ── Finding A: pagination ───────────────────────────────────────────────────
def gh_paged(url, token):
    if "page=2" in url:
        return [head(101, "arena/999-pr-only")]
    return [head(i, f"arena/harmless-{i}") for i in range(1, 101)]


def git_paged(args, cwd):
    if args[:2] == ["worktree", "list"]:
        return _Completed(args, 0, "", "")
    if args[0] == "fetch":
        return _Completed(args, 0, "", "")
    if args[0] == "show":
        if "arena/999-pr-only" in args[1]:
            return _Completed(args, 0, json.dumps(CONFLICT), "")
        return _Completed(args, 0, json.dumps(CLEAN), "")
    return _Completed(args, 1, "", "")


print("FINDING A (pagination, 101 open PRs; the conflict is on PR #101)")
c, _ = run("cmd_check", gh_paged, git_paged)
want("A", c, 1, "page 2 is never requested, so PR #101 is invisible")

print("FINDING A2 (page 2 unreachable -> must be unverifiable)")


def gh_page2_fails(url, token):
    if "page=2" in url:
        raise OSError("connection reset")
    return [head(i, f"arena/harmless-{i}") for i in range(1, 101)]


c, _ = run("cmd_check", gh_page2_fails, git_paged)
want("A2", c, 2, "partial data accepted as a clean pass")

print("FINDING B (fork PR whose ref is 'main'; origin/main also exists)")


def gh_fork(url, token):
    return [head(7, "main", "attacker/nexus-ai-agent")]


fetched: list[list[str]] = []


def git_fork(args, cwd):
    if args[:2] == ["worktree", "list"]:
        return _Completed(args, 0, "", "")
    if args[0] == "fetch":
        fetched.append(args)
        return _Completed(args, 0, "", "")
    if args[0] == "show":
        if "attacker" in args[1] or "nexus-referee" in args[1]:
            return _Completed(args, 0, json.dumps(CONFLICT), "")
        return _Completed(args, 0, json.dumps(CLEAN), "")  # origin/main is clean
    return _Completed(args, 1, "", "")


c, _ = run("cmd_check", gh_fork, git_fork)
want("B", c, 1, "the fork's own board was never read")
wrong = [f for f in fetched if "origin" in f and any("refs/heads/main:" in x for x in f)]
print(
    f"    origin refspec 'refs/heads/main' used as the PR's source: {bool(wrong)} -> "
    f"{'RED — wrong repo accepted' if wrong else 'ok'}"
)
if wrong:
    FAILS.append("B-wrong-source")

print("FINDING B2 (two forks, same ref name 'release' -> two distinct sources, both read)")


def gh_two(url, token):
    return [head(8, "release", "forka/nexus-ai-agent"), head(9, "release", "forkb/nexus-ai-agent")]


def _two_fork_git(failing_fetch=None, conflicting="pr-8"):
    """Fork fixtures. ``failing_fetch`` names the fork whose fetch fails;
    ``conflicting`` names the fork whose board carries a conflict (None = neither)."""
    seen: list[list[str]] = []

    def git(args, cwd):
        if args[:2] == ["worktree", "list"]:
            return _Completed(args, 0, "", "")
        if args[0] == "fetch":
            seen.append(args)
            who = "forka" if "forka" in " ".join(args) else "forkb"
            return _Completed(args, 0 if who != failing_fetch else 1, "", "")
        if args[0] == "show":
            has_conflict = conflicting is not None and conflicting in args[1]
            body = CONFLICT if has_conflict else CLEAN
            return _Completed(args, 0, json.dumps(body), "")
        return _Completed(args, 1, "", "")

    return seen, git


# B2a — both forks readable: the referee must read BOTH boards (not one), and a
# conflict inside either of them must decide the exit code.
seen, git_both = _two_fork_git()
c, _ = run("cmd_check", gh_two, git_both)
foreign = [f for f in seen if "forka" in " ".join(f) or "forkb" in " ".join(f)]
shows = [f for f in SEEN_SHOWS if "nexus-referee" in " ".join(f)]
# A count is not evidence: two fetches of the *same* fork would satisfy ">= 2".
# Each fork's own repository URL must appear, and each fork's board must be read.
fork_urls = {f[3] for f in foreign if len(f) > 3}
want_urls = {
    "https://github.com/forka/nexus-ai-agent.git",
    "https://github.com/forkb/nexus-ai-agent.git",
}
missing = sorted(want_urls - fork_urls)
ok_fetched = not missing
ok_read = any("pr-8:" in " ".join(x) for x in shows) and any("pr-9:" in " ".join(x) for x in shows)
print(
    f"    each fork fetched from its own repository: {ok_fetched} ({len(fork_urls)} URL(s))"
    + (f" — missing {missing}" if missing else "")
    + f"; both fork boards read: {ok_read} ({len(shows)} read(s))"
)
want("B2a", c, 1, "a conflict inside one of two same-named fork heads was not found")
if not ok_fetched:
    FAILS.append("B2-deduped-by-ref-name")
if not ok_read:
    FAILS.append("B2-second-fork-not-read")

# B2b — nothing conflicts, one fork unreadable: the whole verdict is unverifiable
# (exit 2). This is the reviewer's point: an unreadable fork must never be counted
# as a clean source just because an attempt was made.
seen_b, git_one_fails = _two_fork_git(failing_fetch="forkb", conflicting=None)
c, out_b = run("cmd_check", gh_two, git_one_fails)
want("B2b", c, 2, "an unreadable fork head was treated as clean")
surfaced = "forkb" in out_b
print(f"    unreadable fork named in the output: {surfaced} -> {'ok' if surfaced else 'RED'}")
if not surfaced:
    FAILS.append("B2b-unreadable-not-surfaced")
fell_back = [f for f in seen_b if "origin" in f and any("release" in x for x in f)]
print(
    "    resolved through origin's same-named branch: "
    f"{bool(fell_back)} -> {'RED' if fell_back else 'ok'}"
)
if fell_back:
    FAILS.append("B2b-wrong-source")

# B2c — a proven conflict in a readable fork while another is unreadable: exit 1 by
# design (a confirmed conflict outranks an unreadable source) — but the unreadable
# source must still be surfaced, never silently dropped.
seen_c, git_c = _two_fork_git(failing_fetch="forkb", conflicting="pr-8")
c, out_c = run("cmd_check", gh_two, git_c)
want("B2c", c, 1, "a proven conflict was hidden behind the unreadable source")
named = "forkb" in out_c
print(
    "    unreadable fork still surfaced alongside the conflict: "
    f"{named} -> {'ok' if named else 'RED'}"
)
if not named:
    FAILS.append("B2c-unreadable-hidden")

print()
print("RED FINDINGS:", FAILS if FAILS else "none")
