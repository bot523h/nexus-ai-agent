#!/usr/bin/env python3
"""Fail-closed pre-merge guard: a main-bound merge requires base == main.

The **#143 incident**: a pull request whose base was
``arena/01a0f986-nexus-ai-agent`` (a *feature* branch, **not** ``main``) was
merged, producing merge commit ``d7c463e``.  That commit never landed on
``main`` — GitHub happily reported the PR as ``MERGEABLE`` / ``CLEAN`` because
the merge was valid *against its own (wrong) base*.  The content was later
recovered on ``main`` via #186, but the wrong-base merge was invisible to every
existing check.

This guard makes the base branch a **hard precondition**, not a review habit:

* ``check --base <ref>`` — pure decision, no network.  Exit ``1`` unless the
  base ref equals the expected branch (default ``main``).  Called by CI on every
  ``pull_request`` and by an agent immediately before ``gh pr merge``.
* ``check-event --event <name> --base <ref>`` — CI entry point: a non-PR event
  (``push``, ``schedule``) has no PR base, so it passes; a ``pull_request`` event
  with a non-main base fails closed.
* ``check-pr <number>`` — reads a live PR via the ``gh`` CLI and fails closed if
  ``baseRefName != main`` **or** the recorded base SHA is not the live ``main``
  head (a stale base is a REBASE signal, reported separately).

Exit codes: ``0`` main-bound; ``1`` violation; ``2`` a source was unreadable —
which is **never** a pass.

Persian note: ادغام روی پایهٔ نادرست (#143) باید مکانیکی و fail-closed مسدود شود؛
پایهٔ نادرست = قرمز، منبع ناخوانا = کد ۲ (هرگز PASS نیست).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

DEFAULT_EXPECTED = "main"

#: PR events that carry a base branch.  Everything else has no PR base and the
#: guard is a no-op pass (there is nothing to protect).
_PR_EVENTS = {"pull_request", "pull_request_target"}


def base_is_acceptable(base_ref: str | None, expected: str = DEFAULT_EXPECTED) -> bool:
    """The whole decision, in one testable place.

    A missing base ref is not a verifiable base and is therefore not acceptable.
    A present base ref must equal ``expected`` exactly.
    """
    if base_ref is None:
        return False
    return base_ref.strip() == expected


def check_event(event: str, base_ref: str | None, expected: str = DEFAULT_EXPECTED) -> int:
    """CI entry point.  Non-PR events pass; PR events must be main-bound."""
    if event not in _PR_EVENTS:
        print(f"merge-base-guard: event {event!r} carries no PR base — pass")
        return 0
    if not base_ref:
        # A pull_request event always carries a base; an empty one is an
        # unreadable source, which is never a pass.
        print(
            f"merge-base-guard: {event!r} event has no base ref — cannot verify, "
            "refusing to pass (exit 2)",
            file=sys.stderr,
        )
        return 2
    return _report(base_ref, expected)


def _report(base_ref: str | None, expected: str) -> int:
    if not base_ref:
        print(
            "merge-base-guard: missing base ref — cannot verify, refusing to pass (exit 2)",
            file=sys.stderr,
        )
        return 2
    if base_is_acceptable(base_ref, expected):
        print(f"merge-base-guard: base {base_ref!r} == {expected!r} — main-bound, pass")
        return 0
    print(
        f"merge-base-guard: FAIL-CLOSED — base {base_ref!r} != {expected!r}. "
        "A main-bound merge requires base == main (incident #143). "
        "Retarget the PR to main and rebase before merging.",
        file=sys.stderr,
    )
    return 1


def _gh_json(args: list[str]) -> tuple[object | None, str | None]:
    """Run ``gh api`` and parse JSON.  Returns (data, error)."""
    try:
        proc = subprocess.run(
            ["gh", "api", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None, "gh CLI not found"
    if proc.returncode != 0:
        return None, (proc.stderr.strip() or f"gh exited {proc.returncode}")
    try:
        return json.loads(proc.stdout), None
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        return None, f"gh returned non-JSON: {exc}"


def _resolve_repo(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    import os

    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    # https://github.com/owner/repo.git  |  git@github.com:owner/repo.git
    tail = url.split("github.com", 1)[-1].lstrip(":/")
    tail = tail.removesuffix(".git")
    return tail if "/" in tail else None


def check_pr(number: int, repo: str | None, expected: str = DEFAULT_EXPECTED) -> int:
    """Live-PR check via the ``gh`` CLI.  Unreadable source → exit 2."""
    slug = _resolve_repo(repo)
    if not slug:
        print("merge-base-guard: cannot resolve repo slug (use --repo)", file=sys.stderr)
        return 2
    pr, err = _gh_json([f"repos/{slug}/pulls/{number}"])
    if err or not isinstance(pr, dict):
        print(f"merge-base-guard: cannot read PR #{number}: {err}", file=sys.stderr)
        return 2
    base = pr.get("base")
    head = pr.get("head")
    if not isinstance(base, dict) or not isinstance(head, dict):
        print(
            f"merge-base-guard: PR #{number} has unreadable base/head data — refusing to pass "
            "(exit 2)",
            file=sys.stderr,
        )
        return 2
    base_ref = base.get("ref")
    base_sha = base.get("sha")
    head_sha = head.get("sha")
    if not isinstance(base_ref, str) or not base_ref.strip():
        print(
            f"merge-base-guard: PR #{number} is missing base.ref — refusing to pass (exit 2)",
            file=sys.stderr,
        )
        return 2
    if not isinstance(base_sha, str) or not base_sha.strip():
        print(
            f"merge-base-guard: PR #{number} is missing base.sha — refusing to pass (exit 2)",
            file=sys.stderr,
        )
        return 2
    if not isinstance(head_sha, str) or not head_sha.strip():
        print(
            f"merge-base-guard: PR #{number} is missing head.sha — refusing to pass (exit 2)",
            file=sys.stderr,
        )
        return 2
    verdict = _report(base_ref, expected)
    if verdict != 0:
        return verdict

    ref, err = _gh_json([f"repos/{slug}/git/ref/heads/{expected}"])
    if err or not isinstance(ref, dict):
        print(f"merge-base-guard: cannot read live {expected} head: {err}", file=sys.stderr)
        return 2
    live_main = (ref.get("object") or {}).get("sha")
    print(f"merge-base-guard: PR #{number} head={head_sha} base={base_ref} base_sha={base_sha}")
    print(f"merge-base-guard: live {expected} head={live_main}")
    if base_sha and live_main and base_sha != live_main:
        # Not a hard fail — a stale base is a rebase signal, reported loudly.
        print(
            f"merge-base-guard: WARNING — base SHA {str(base_sha)[:10]} is behind live "
            f"{expected} {str(live_main)[:10]}; rebase before merging (REBASE_REQUIRED)."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="decide on a base ref (no network)")
    p_check.add_argument("--base", default=None)
    p_check.add_argument("--expected", default=DEFAULT_EXPECTED)

    p_event = sub.add_parser("check-event", help="CI entry point for an event")
    p_event.add_argument("--event", required=True)
    p_event.add_argument("--base", default=None)
    p_event.add_argument("--expected", default=DEFAULT_EXPECTED)

    p_pr = sub.add_parser("check-pr", help="read a live PR via gh")
    p_pr.add_argument("number", type=int)
    p_pr.add_argument("--repo", default=None)
    p_pr.add_argument("--expected", default=DEFAULT_EXPECTED)

    args = parser.parse_args(argv)
    if args.command == "check":
        return _report(args.base, args.expected)
    if args.command == "check-event":
        return check_event(args.event, args.base, args.expected)
    if args.command == "check-pr":
        return check_pr(args.number, args.repo, args.expected)
    return 2  # pragma: no cover - argparse enforces the subcommand


if __name__ == "__main__":
    raise SystemExit(main())
