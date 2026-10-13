#!/usr/bin/env python3
"""Adversarial mutation harness for the multi-source board referee (task-207).

Evidence rule: a guard that has never been attacked is not evidence.  The
``check`` referee now consults the local board, every sibling git worktree's
board, and ``origin/main``'s board, and must answer 0 (clean) / 1 (proven
overlap) / 2 (unverifiable).  This script weakens that logic one invariant at a
time, re-runs the referee suite, and requires it to turn RED — a survivor is a
fake guard.

Every mutation is applied to ``scripts/agent_board.py`` in place and restored
from an in-memory copy in a ``finally`` block; the file's sha256 is checked
before and after so a crash can never leave the coordination tool altered.

Usage::

    python scripts/board_referee_mutations.py
    python scripts/board_referee_mutations.py --list

Exit code 0 means "N/N mutants killed"; anything else means a guard is fake.
"""

# ruff: noqa: E501 - the MUTATIONS table holds verbatim source snippets; they must
# match the shipped file byte-for-byte, so they cannot be re-wrapped.
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "agent_board.py"
TESTS = ("tests/unit/test_agent_board.py",)


@dataclass(frozen=True)
class Mutation:
    name: str
    old: str
    new: str
    why: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "unreadable_source_becomes_a_pass",
        '    if unreadable:\n        print("UNVERIFIABLE: cannot establish the absence of overlap',
        '    if False:\n        print("UNVERIFIABLE: cannot establish the absence of overlap',
        "an unreadable remote/worktree must never be reported as 'no overlap'",
    ),
    Mutation(
        "no_remote_flag_ignored",
        '    no_remote = bool(getattr(args, "no_remote", False))',
        "    no_remote = False  # mutation: ignore --no-remote and always hit git",
        "--no-remote must actually narrow the check to the local board",
    ),
    Mutation(
        "foreign_worktree_boards_skipped",
        "    if result is None or result.returncode != 0:\n        return [], False",
        "    if result is None or result.returncode != 0:\n        return [], True  # mutation: pretend enumeration succeeded",
        "a foreign lease living in a sibling worktree must still be seen",
    ),
    Mutation(
        "proven_conflict_downgraded_to_unverifiable",
        "        print(STOP_BANNER)\n        return 1",
        "        print(STOP_BANNER)\n        return 2  # mutation: a proven overlap becomes 'unverifiable'",
        "a proven conflict must be exit 1, not hidden behind exit 2",
    ),
    Mutation(
        "unreadable_local_board_becomes_empty_pass",
        '    except (OSError, ValueError) as exc:\n        print(f"UNVERIFIABLE: local board cannot be read: {exc}")\n        return 2',
        '    except (OSError, ValueError):\n        local_board = {"claims": []}  # mutation: unreadable local board becomes an empty pass',
        "an unreadable local board must be exit 2, never a fail-open empty board",
    ),
    Mutation(
        "generation_fencing_guard_ignored",
        '    expected = getattr(args, "expected_generation", None)\n    if expected is None:',
        '    return None  # mutation: ignore the fencing guard\n    expected = getattr(args, "expected_generation", None)\n    if expected is None:',
        "a stale generation must be refused, never ignored (task-219)",
    ),
    Mutation(
        "mandatory_token_guard_removed",
        "        if required:\n            return (\n                f\"REFUSED: mutating the live lease for {claim['task']} requires \"",
        "        if False:  # mutation: do not require a token on existing-lease mutations\n            return (\n                f\"REFUSED: mutating the live lease for {claim['task']} requires \"",
        "mutating an existing lease without a fencing token must be refused (task-219)",
    ),
    Mutation(
        "claim_takeover_does_not_advance_generation",
        '    claim["generation"] = _generation(claim) + 1\n    if args.gates:',
        '    claim["generation"] = _generation(claim)  # mutation: takeover must advance the epoch\n    if args.gates:',
        "taking a lease over must advance its fencing epoch",
    ),
    Mutation(
        "release_does_not_advance_generation",
        '    claim["generation"] = _generation(claim) + 1\n    save_board(board)\n    print(\n        f"RELEASED',
        '    claim["generation"] = _generation(claim)  # mutation: release must advance the epoch\n    save_board(board)\n    print(\n        f"RELEASED',
        "releasing a lease must advance its fencing epoch",
    ),
    Mutation(
        "expired_lease_keeps_the_gate",
        '                claim["gates_owner"] = False\n                claim["release_reason"] = (',
        '                claim["release_reason"] = (  # mutation: an expired holder keeps the gate',
        "an expired lease must lose gates_owner (exactly one live gate holder)",
    ),
    Mutation(
        "gc_overwrites_the_owners_note",
        '                claim["release_reason"] = (\n                    f"auto-released by gc at {_iso(_now())} (stale {previous_status} lease)"\n                )',
        '                claim["note"] = (\n                    f"auto-released by gc at {_iso(_now())} (stale {previous_status} lease)"\n                )',
        "gc must record the release reason without clobbering the owner's evidence note",
    ),
    Mutation(
        "pr_only_lease_source_skipped",
        "        pr_heads, pr_reason = _open_pr_heads(repo, token)",
        '        pr_heads, pr_reason = [], ""  # mutation: skip the pushed open-PR head source',
        "a lease living only on a pushed PR head must be seen, not skipped",
    ),
    Mutation(
        "pr_branch_self_exclusion_removed",
        "            and head.ref == exclude_branch",
        "            and False  # mutation: no self-exclusion by identity",
        "a branch must not conflict with its own pushed lease (exclusion is by identity)",
    ),
    Mutation(
        "stale_pr_branch_ref_read_as_fresh",
        "        if _head_key(head) not in fresh:\n            continue",
        "        if False:  # mutation: read stale tracking refs as fresh\n            continue",
        "a tracking ref not fetched in this run must never be read as a live lease",
    ),
    Mutation(
        "heartbeat_ignores_the_gates_flag",
        '            if args.gates:\n                took_gate = not claim.get("gates_owner")\n                _take_gate(board, claim)',
        '            if False:  # mutation: a heartbeat ignores --gates (the original defect)\n                took_gate = not claim.get("gates_owner")\n                _take_gate(board, claim)',
        "`claim --gates` on a live own lease must take the gate, not print success (task-266)",
    ),
    Mutation(
        "gate_grant_does_not_clear_the_other_holders",
        '    for other in board.get("claims", []):\n        if other.get("task") != claim.get("task") and other.get("gates_owner"):\n            other["gates_owner"] = False',
        '    for other in []:  # mutation: leave every other holder in place\n        other["gates_owner"] = False',
        "granting the gate must clear every other holder: at most one live gate (task-266)",
    ),
    Mutation(
        "heartbeat_releases_the_gate",
        "            else:\n                took_gate = False",
        '            else:\n                took_gate = False\n                claim["gates_owner"] = False  # mutation: renewing releases the gate',
        "a heartbeat without --gates must not silently drop the gate (task-266)",
    ),
    Mutation(
        "open_pr_listing_stops_at_page_one",
        '        if len(rows) < _OPEN_PR_PAGE_SIZE:\n            return heads, ""\n        page += 1',
        '        return heads, ""  # mutation: page one is the whole listing\n        page += 1',
        "with more than one page of open PRs, a page-one-only listing hides every later lease (task-266 review A)",
    ),
    Mutation(
        "failed_listing_page_degrades_to_a_partial_list",
        '            return [], f"open-PR API page {page} unavailable ({exc})"',
        "            break  # mutation: keep the pages already read and call it complete",
        "a page that cannot be read must make the whole listing unverifiable, never partial (task-266 review A)",
    ),
    Mutation(
        "repo_identity_validation_disabled",
        '    return all(_REPO_PART.match(part) and part not in (".", "..") for part in parts)',
        "    return True  # mutation: accept any repository name",
        "the fetch URL is derived from this identity, so it must be a plausible owner/name (task-266 review B)",
    ),
    Mutation(
        "ref_validation_disabled",
        "    return not any(bad in ref for bad in _REF_FORBIDDEN)",
        "    return True  # mutation: accept any ref name",
        "a ref reaches a refspec and git show, so it is validated first (task-266 review B)",
    ),
    Mutation(
        "fork_head_resolved_through_origin",
        "        source_ref = _head_source_ref(head, coordination_repo)\n        label = _head_source_label(head, coordination_repo)",
        '        source_ref = f"refs/remotes/origin/{head.ref}"  # mutation: resolve a fork head via origin\n        label = _head_source_label(head, coordination_repo)',
        "a fork's head must be read from the fork's own ref namespace, never from a same-named origin branch (task-266 review B)",
    ),
    Mutation(
        "foreign_head_fetched_from_origin",
        "        url = _head_fetch_url(head)",
        '        url = "origin"  # mutation: fetch a foreign head from the local default remote',
        "a foreign PR head is fetched from its own repository; origin is never a substitute (task-266 review B)",
    ),
    Mutation(
        "failed_fork_fetch_counted_as_readable",
        "        if fetched is not None and fetched.returncode == 0:\n"
        "            fresh.add(_head_key(head))\n"
        "        else:\n"
        '            unreadable.append(\n                f"pr-head {_head_key(head)}: git fetch from {url} failed "',
        "        if True:  # mutation: a failed fork fetch still counts as readable\n"
        "            fresh.add(_head_key(head))\n"
        "        else:\n"
        '            unreadable.append(\n                f"pr-head {_head_key(head)}: git fetch from {url} failed "',
        "a fork head that could not be fetched must not be read: its verdict is unverifiable (task-266 review B, round 2)",
    ),
    Mutation(
        "open_pr_heads_deduped_by_ref_name",
        "        key = _head_key(head)",
        "        key = head.ref  # mutation: dedupe by branch name, ignoring the repository",
        "two forks sharing a ref name are two sources, not one (task-266 review B)",
    ),
    Mutation(
        "exactly_one_gate_owner_ignored",
        "    if not no_remote and (malformed_owners or len(owners) != 1):",
        "    if False:  # mutation: ignore the exactly-one-gates-owner invariant\n        pass\n    if not no_remote and (malformed_owners or len(owners) != 1) and False:",
        "the referee must refuse to certify anything without exactly one live gate holder",
    ),
    Mutation(
        "open_pr_source_set_truncated",
        "    for start in range(0, len(local), _REMOTE_BRANCH_FETCH_LIMIT):",
        "    for start in range(0, min(len(local), _REMOTE_BRANCH_FETCH_LIMIT), _REMOTE_BRANCH_FETCH_LIMIT):  # mutation: cap the source set",
        "every open-PR head must be consulted; a capped source set is a false clean pass (task-266)",
    ),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_tests() -> bool:
    # ``--noconftest``: test_agent_board.py loads the CLI module directly and uses
    # only builtin fixtures, so the harness stays dependency-free (no structlog,
    # no package install) — the same targeted invocation the repo documents for
    # diagnostic runs.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "--noconftest", *TESTS],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}: {mutation.why}")
        return 0

    original = SCRIPT.read_text(encoding="utf-8")
    before = _sha256(SCRIPT)

    print("baseline ... ", end="", flush=True)
    if not run_tests():
        print("RED — refusing to mutate a suite that is already failing")
        return 2
    print("GREEN")

    killed = 0
    survivors: list[str] = []
    try:
        for mutation in MUTATIONS:
            if mutation.old not in original:
                print(f"{mutation.name}: MUTATION DOES NOT APPLY (source drifted)")
                survivors.append(mutation.name)
                continue
            SCRIPT.write_text(original.replace(mutation.old, mutation.new, 1), encoding="utf-8")
            try:
                green = run_tests()
            finally:
                SCRIPT.write_text(original, encoding="utf-8")
            if green:
                print(f"{mutation.name}: SURVIVED — {mutation.why}")
                survivors.append(mutation.name)
            else:
                killed += 1
                print(f"{mutation.name}: killed")
    finally:
        # Belt and braces: never leave the shared coordination tool mutated.
        SCRIPT.write_text(original, encoding="utf-8")

    after = _sha256(SCRIPT)
    print("restored baseline ... ", end="", flush=True)
    if after != before:
        print("RED — restore changed the file (sha256 mismatch)")
        return 3
    if not run_tests():
        print("RED — restore failed")
        return 3
    print("GREEN")
    print(f"{killed}/{len(MUTATIONS)} killed")
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
