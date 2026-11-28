#!/usr/bin/env python3
"""Close every non-merged PR with its documented terminal disposition.

Reads ``PR_CONVERGENCE_DISPOSITION_MATRIX.json``, posts one substantive comment
per PR (exact SHAs, delta, conflicts, preservation anchor, resume condition),
then closes the PR. Writes ``CLOSURE_RESULT.json`` so the final report can prove
what was closed and where the evidence lives.

Four safety properties make re-runs safe:

* **safe by default** — run with no flags and the tool is a *dry run*: it prints
  what it would post and calls nothing. Live mutation (``POST`` comment,
  ``PATCH`` state=closed) requires an explicit ``--apply``. ``--dry-run`` stays
  as an alias for the default so existing callers keep working.
* **compare-and-swap** — :func:`verify_live_pr_head_matches_snapshot` re-reads
  the PR immediately before acting and refuses (``StalePRSnapshotError``) when
  the live state is not ``open`` or ``head.sha`` no longer equals the SHA the
  matrix was computed against. A disposition is a statement about a *specific*
  head; acting on a different head would close work nobody audited.
* **idempotency** — :func:`has_existing_closure_comment` detects the stable
  ``CLOSURE_MARKER`` so a retry never posts a duplicate termination comment.
* **crash recovery** — every closure walks
  ``PREPARED -> COMMENTED -> CLOSED -> VERIFIED -> RECORDED`` and each step is
  persisted atomically (:class:`ClosureJournal`). A run killed between the
  comment and the close, or between the close and the result write, resumes at
  the state it reached instead of posting twice or skipping the record.

Usage::

    python close_prs.py                 # dry run (the default)
    python close_prs.py --dry-run       # explicit alias of the default
    python close_prs.py --apply         # the only mutating mode
    python close_prs.py --apply --only 135
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

TOKEN = os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"]
REPO = "bot523h/nexus-ai-agent"
ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "PR_CONVERGENCE_DISPOSITION_MATRIX.json"
RESULT = ROOT / "CLOSURE_RESULT.json"
JOURNAL = ROOT / "CLOSURE_JOURNAL.json"

#: Stable, machine-detectable marker. Never change it without a migration.
CLOSURE_MARKER = "<!-- nexus-convergence:2026-11-27:closure -->"
SKIP_DISPOSITIONS = ("MERGED", "MERGE_CANDIDATE", "BLOCKED_ACTIVE_OWNER")

#: The durable states of one PR closure, in order. Each transition is persisted
#: before the next side effect, so a crash can only ever repeat a step that is
#: already idempotent (comment detection, ``PATCH state=closed``, result append).
PREPARED = "PREPARED"
COMMENTED = "COMMENTED"
CLOSED = "CLOSED"
VERIFIED = "VERIFIED"
RECORDED = "RECORDED"
STATES: tuple[str, ...] = (PREPARED, COMMENTED, CLOSED, VERIFIED, RECORDED)
_NEXT_STATE = dict(zip(STATES, STATES[1:], strict=False))


class StalePRSnapshotError(RuntimeError):
    """The live PR no longer matches the head the disposition was computed on."""


class JournalStateError(RuntimeError):
    """A journal transition that the state machine does not allow."""


def atomic_write_json(path: Path, payload: Any) -> None:
    """Replace *path* with *payload* atomically (temp file + fsync + rename).

    A journal or result file that is half-written after a crash is worse than no
    file at all: the next run would read a truncated document and either abort
    or, worse, believe a PR was recorded when it was not.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8", prefix=path.name, suffix=".tmp"
    )
    with handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


class ClosureJournal:
    """Durable ``PREPARED -> ... -> RECORDED`` record of every closure.

    Keyed by PR number (stored as a string, because JSON object keys are), and
    rewritten atomically on every transition. Re-entering the current state is a
    no-op so a resumed run can replay the step it already completed; skipping a
    state forwards is refused, because that would mean a side effect was never
    performed.
    """

    def __init__(self, path: Path = JOURNAL) -> None:
        self.path = path
        self._records: dict[str, dict[str, Any]] = {}
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = payload.get("records", {}) if isinstance(payload, Mapping) else {}
            self._records = {str(key): dict(value) for key, value in records.items()}

    def state(self, number: int) -> str | None:
        record = self._records.get(str(number))
        return None if record is None else str(record.get("state"))

    def comment_url(self, number: int) -> str | None:
        value = self._records.get(str(number), {}).get("comment_url")
        return value if isinstance(value, str) else None

    def reached(self, number: int, state: str) -> bool:
        """True when *state* or a later one has already been reached."""
        current = self.state(number)
        if current is None or current not in STATES:
            return False
        return STATES.index(current) >= STATES.index(state)

    def transition(self, number: int, state: str, **extra: Any) -> None:
        if state not in STATES:
            raise JournalStateError(f"#{number}: unknown state {state!r}")
        current = self.state(number)
        if current == state:
            if extra:
                self._records[str(number)].update(extra)
                self._flush()
            return
        allowed = _NEXT_STATE.get(current) if current else PREPARED
        if state != allowed:
            raise JournalStateError(f"#{number}: {current!r} -> {state!r} is not a valid step")
        record = self._records.setdefault(str(number), {})
        record.update(extra)
        record["state"] = state
        record["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._flush()

    def _flush(self) -> None:
        atomic_write_json(
            self.path,
            {"version": 1, "comment_marker": CLOSURE_MARKER, "records": self._records},
        )


def verify_live_pr_head_matches_snapshot(
    pr_number: int, expected_head_sha: str, live_pr_payload: Mapping[str, Any]
) -> bool:
    """Fail closed unless the live PR is open at exactly *expected_head_sha*.

    Returns ``True`` when the compare-and-swap precondition holds and raises
    :class:`StalePRSnapshotError` otherwise, so a caller can never comment on
    or close a head it did not audit.
    """
    if not expected_head_sha:
        raise StalePRSnapshotError(f"#{pr_number}: the snapshot carries no head SHA")
    state = live_pr_payload.get("state")
    if state != "open":
        raise StalePRSnapshotError(f"#{pr_number}: live state is {state!r}, not 'open'")
    head = live_pr_payload.get("head") or {}
    live_sha = head.get("sha") if isinstance(head, Mapping) else None
    if live_sha != expected_head_sha:
        raise StalePRSnapshotError(
            f"#{pr_number}: live head {live_sha!r} != audited head {expected_head_sha!r}"
        )
    return True


def has_existing_closure_comment(
    comments: Sequence[Mapping[str, Any]], marker: str = CLOSURE_MARKER
) -> bool:
    """True when a previous run already posted the termination comment."""
    for entry in comments:
        body = entry.get("body")
        if isinstance(body, str) and marker in body:
            return True
    return False


def api(path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "nexus-convergence",
        },
        method=method,
    )
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and attempt < 5:
                time.sleep(2**attempt)
                continue
            raise
    raise RuntimeError("unreachable")


def comment(row: Mapping[str, Any]) -> str:
    disposition = row["disposition"]
    head = (
        "This comment was created by an AI agent (OpenHands) on behalf of the user.\n\n"
        f"**Closed by the 2026-11-27 PR backlog convergence** "
        f"(`NEXUS-PR-BACKLOG-CONVERGENCE-2026-11-27`).\n\n"
        f"| | |\n|---|---|\n"
        f"| Disposition | `{disposition}` |\n"
        f"| Head | `{row['head_sha']}` |\n"
        f"| Main at closure | `{row['main_sha']}` |\n"
        f"| Delta vs main | {row['ahead']} commits ahead, {row['behind']} behind, "
        f"{row['changed_files']} files, +{row['additions']}/-{row['deletions']} |\n"
        f"| Merge-tree conflicts | {row['merge_tree_conflicts']} |\n"
        f"| Unresolved review threads | {row['unresolved_threads']} |\n"
        f"| Added identifiers already on main | {row['symbols_on_main']}/{row['added_symbols']} |\n"
    )
    body = [head, f"\n**Why `{disposition}`**\n\n{row['rationale']}\n"]
    if row.get("evidence"):
        body.append(
            "\n**Evidence**\n\n" + "\n".join(f"- `{item}`" for item in row["evidence"]) + "\n"
        )
    if disposition == "SUPERSEDED_AND_CLOSED":
        body.append(
            "\nThe capability this PR introduced already exists on canonical `main`, so "
            "replaying the branch would create a duplicate implementation. The branch is "
            "**kept** for history and this PR stays reopenable; no code is deleted.\n"
        )
    elif disposition == "EXTRACTED_AND_CLOSED":
        body.append(
            "\nThe unique artefact this PR carried was extracted into the convergence branch "
            "(`.agents/evidence/pr-convergence-2026-11-27/`), so the work is preserved on "
            "main-bound history. The branch is **kept** and this PR stays reopenable.\n"
        )
    else:
        body.append(
            f"\n**Preservation anchor**\n\n"
            f"- Branch `{row['head_ref']}` is kept and this PR is reopenable.\n"
            f"- Durable board task `{row['board_task']}` (queued) is the resume anchor.\n"
            f"- Convergence PR: #369 (merges the disposition matrix and board anchors).\n"
            f"- Convergence evidence: "
            f"`.agents/evidence/pr-convergence-2026-11-27/PR_CONVERGENCE_DISPOSITION_MATRIX.json`.\n"
            f"\n**Resume condition**\n\n{row['resume_condition']}\n"
        )
    body.append(
        "\nThis is a deliberate backlog retirement, not a judgement that the work is worthless: "
        "the delta is preserved and the resume condition is explicit.\n"
        f"\n{CLOSURE_MARKER}\n"
    )
    return "".join(body)


def _live_pr(number: int, call: Callable[..., Any]) -> Mapping[str, Any]:
    payload = call(f"/pulls/{number}")
    return payload if isinstance(payload, Mapping) else {}


def close_pr(
    row: Mapping[str, Any],
    *,
    journal: ClosureJournal,
    call: Callable[..., Any] | None = None,
    result_path: Path = RESULT,
) -> str:
    """Drive one PR to ``RECORDED``, resuming from whatever state it reached.

    Every mutation is preceded by the journal transition that proves *why* the
    mutation is safe to (re)perform, so the only steps a resume can repeat are
    the idempotent ones: the comment is guarded by ``CLOSURE_MARKER`` detection
    and ``PATCH state=closed`` is a no-op on an already-closed PR.

    Returns a short outcome label for the run log.
    """
    api_call = call or api
    number = int(row["number"])
    if journal.reached(number, RECORDED):
        return "already-recorded"

    state = journal.state(number)
    if state is None:
        journal.transition(number, PREPARED)
        state = PREPARED

    live = _live_pr(number, api_call)
    if state == COMMENTED:
        # The comment is durable; only the close (and the proof of it) is left.
        if live.get("state") == "open":
            verify_live_pr_head_matches_snapshot(number, str(row["head_sha"]), live)
            api_call(f"/pulls/{number}", "PATCH", {"state": "closed"})
        journal.transition(number, CLOSED)
        state = CLOSED
    elif state == PREPARED:
        try:
            verify_live_pr_head_matches_snapshot(number, str(row["head_sha"]), live)
        except StalePRSnapshotError:
            # A closure posted by a run that predates the journal (the 43 PRs of
            # the first convergence pass) is complete, not stale: the marker
            # proves the comment and ``state=closed`` proves the close. Adopt it
            # instead of refusing, or a re-run could never converge.
            comments = api_call(f"/issues/{number}/comments?per_page=100")
            if live.get("state") != "closed" or not has_existing_closure_comment(comments):
                raise
            journal.transition(number, COMMENTED, recovered=True, legacy=True)
            journal.transition(number, CLOSED)
            journal.transition(number, VERIFIED)
            state = VERIFIED
        else:
            comments = api_call(f"/issues/{number}/comments?per_page=100")
            if has_existing_closure_comment(comments):
                journal.transition(number, COMMENTED, comment_url=None, recovered=True)
            else:
                posted = api_call(f"/issues/{number}/comments", "POST", {"body": comment(row)})
                journal.transition(number, COMMENTED, comment_url=posted.get("html_url"))
            api_call(f"/pulls/{number}", "PATCH", {"state": "closed"})
            journal.transition(number, CLOSED)
            state = CLOSED

    if state == CLOSED:
        if _live_pr(number, api_call).get("state") != "closed":
            raise StalePRSnapshotError(f"#{number}: close did not take effect")
        journal.transition(number, VERIFIED)

    results = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else []
    if number not in {entry.get("number") for entry in results}:
        results.append(
            {
                "number": number,
                "disposition": row["disposition"],
                "comment_url": journal.comment_url(number),
                "closed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        atomic_write_json(result_path, results)
    journal.transition(number, RECORDED)
    return "closed"


def _adopt_previous_closures(
    journal: ClosureJournal, results_path: Path, matrix: Sequence[Mapping[str, Any]]
) -> int:
    """Seed the journal from an existing ``CLOSURE_RESULT.json``.

    The 43 closures of the first convergence run predate the journal; without
    this they would look like unstarted work and be re-commented. A PR that is
    already in the result *is* recorded, so its journal is advanced to
    ``RECORDED`` through the real state machine (never written out of order).
    """
    if not results_path.exists():
        return 0
    heads = {int(row["number"]): row for row in matrix}
    adopted = 0
    for entry in json.loads(results_path.read_text(encoding="utf-8")):
        number = int(entry["number"])
        if number not in heads or journal.reached(number, RECORDED):
            continue
        for state in STATES:
            journal.transition(number, state, comment_url=entry.get("comment_url"))
        adopted += 1
    return adopted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Close non-merged PRs with their documented disposition (dry run by default)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually post the comment and close the PR (the only mutating mode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit alias of the default; wins if combined with --apply",
    )
    parser.add_argument("--only", type=int, action="append")
    parser.add_argument("--matrix", default=str(MATRIX))
    parser.add_argument("--result", default=str(RESULT))
    parser.add_argument("--journal", default=str(JOURNAL))
    args = parser.parse_args(list(argv) if argv is not None else None)
    apply = bool(args.apply) and not bool(args.dry_run)

    matrix = json.loads(Path(args.matrix).read_text(encoding="utf-8"))["records"]
    result_path = Path(args.result)
    journal = ClosureJournal(Path(args.journal))

    if not apply:
        # A dry run must be safe on a machine with no network and no token use:
        # it renders the exact comment text and calls nothing at all.
        for row in matrix:
            if row["disposition"] in SKIP_DISPOSITIONS:
                continue
            if args.only and row["number"] not in args.only:
                continue
            print(f"--- #{row['number']} {row['disposition']} ---")
            print(comment(row))
        print("dry run: 0 comments posted, 0 PRs closed (pass --apply to execute)")
        return 0

    adopted = _adopt_previous_closures(journal, result_path, matrix)
    if adopted:
        print(f"adopted {adopted} previously closed PR(s) into the journal")
    for row in matrix:
        number = row["number"]
        if row["disposition"] in SKIP_DISPOSITIONS:
            continue
        if args.only and number not in args.only:
            continue
        try:
            outcome = close_pr(row, journal=journal, result_path=result_path)
        except StalePRSnapshotError as exc:
            print(f"REFUSING #{number}: {exc}")
            return 1
        print(f"#{number}: {outcome} ({row['disposition']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
