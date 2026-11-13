#!/usr/bin/env python3
"""Semantic, stdlib-only merger for ``.agents/board.json`` payloads.

Git's line-based three-way merge cannot reconcile this board: every agent edits the same
JSON arrays, so independent claims collide on adjacent lines.  This tool merges by identity:

* ``claims`` are keyed by ``task``.  The chosen record is the one with the highest-ranked
  status (see ``STATUS_RANK``).  Every other distinct record for that task is kept under
  ``merge_alternates``, so no claim is dropped silently.
* ``evidence``, ``evidence_required``, ``exclusive_paths`` and ``files`` are unioned and sorted.
* Other lists are unioned by canonical JSON (duplicates removed, sorted).
* Dict sections merge recursively.  A scalar conflict keeps the value from the payload with the
  latest ``updated_at`` and is recorded under ``merge_conflicts``.

Unknown statuses are refused, so a new status forces an explicit ranking decision.  Output is
canonical JSON (sorted keys, UTF-8, trailing newline), and the result does not depend on input
order.  ``verify_no_loss`` checks that every input claim and every input list item survives.

Usage::

    python3 scripts/merge_agent_board.py BOARD [BOARD ...] [-o OUT]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

#: Status precedence for claims that share a task.  Higher wins.  Unknown statuses are refused.
STATUS_RANK: dict[str, int] = {
    "done": 50,
    "completed_merged": 50,
    "completed_released": 50,
    "completed_delivered_via_PR34": 50,
    "active": 40,
    "active_in_review": 40,
    "assigned_to_B": 30,
    "assigned_to_B_next": 30,
    "assigned_to_E_pr33": 30,
    "queued": 20,
    "available_sequenced_post_32": 20,
    "available_sequenced_post_33": 20,
    "available_sequenced_post_32_33": 20,
    "deferred": 10,
    "superseded_by_PR33": 10,
    "expired": 0,
}

#: Claim fields whose lists are unioned across records for the same task.
UNION_CLAIM_FIELDS: frozenset[str] = frozenset(
    {"evidence", "evidence_required", "exclusive_paths", "files"}
)

Payload = tuple[str, dict[str, Any]]


class MergeError(ValueError):
    """Raised when a board cannot be merged without guessing."""


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _unique_sorted(items: list[Any]) -> list[Any]:
    seen: dict[str, Any] = {}
    for item in items:
        seen.setdefault(_canon(item), item)
    return [seen[key] for key in sorted(seen)]


def _rank(claim: dict[str, Any], task: str) -> int:
    status = claim.get("status")
    if not isinstance(status, str) or status not in STATUS_RANK:
        raise MergeError(f"unknown claim status {status!r} for task {task!r}")
    return STATUS_RANK[status]


def _split_alternates(claim: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    nested = claim.get("merge_alternates", [])
    base = {key: value for key, value in claim.items() if key != "merge_alternates"}
    return base, [item for item in nested if isinstance(item, dict)]


def _merge_claims(payloads: list[Payload]) -> list[dict[str, Any]]:
    by_task: dict[str, list[dict[str, Any]]] = {}
    for label, payload in payloads:
        seen_here: set[str] = set()
        for claim in payload.get("claims", []):
            task = claim.get("task")
            if not isinstance(task, str) or not task:
                raise MergeError(f"{label}: claim without a task id")
            if task in seen_here:
                raise MergeError(f"{label}: duplicate task {task!r} inside one board")
            seen_here.add(task)
            base, nested = _split_alternates(claim)
            by_task.setdefault(task, []).extend([base, *nested])

    merged: list[dict[str, Any]] = []
    for task in sorted(by_task):
        distinct: dict[str, dict[str, Any]] = {}
        for record in by_task[task]:
            distinct.setdefault(_canon(record), record)
        ordered = sorted(
            distinct.items(),
            key=lambda item: (-_rank(item[1], task), item[0]),
        )
        chosen = dict(ordered[0][1])
        for field in sorted(UNION_CLAIM_FIELDS):
            contributions = [r[field] for r in distinct.values() if isinstance(r.get(field), list)]
            if contributions:
                chosen[field] = _unique_sorted([x for values in contributions for x in values])
        alternates = [record for _, record in ordered[1:]]
        if alternates:
            chosen["merge_alternates"] = alternates
        merged.append(chosen)
    return merged


def _merge_values(path: str, values: list[tuple[str, Any]], conflicts: list[Any]) -> Any:
    """Merge ``(updated_at, value)`` pairs found at one JSON path."""
    if all(isinstance(value, dict) for _, value in values):
        keys = sorted({key for _, value in values for key in value})
        return {
            key: _merge_values(
                f"{path}.{key}",
                [(stamp, value[key]) for stamp, value in values if key in value],
                conflicts,
            )
            for key in keys
        }
    if all(isinstance(value, list) for _, value in values):
        return _unique_sorted([item for _, value in values for item in value])

    distinct: dict[str, tuple[str, Any]] = {}
    for stamp, value in values:
        distinct.setdefault(_canon(value), (stamp, value))
    if len(distinct) == 1:
        return next(iter(distinct.values()))[1]
    ordered = sorted(distinct.items(), key=lambda item: (item[1][0], item[0]), reverse=True)
    chosen = ordered[0][1][1]
    conflicts.append(
        {
            "path": path,
            "chosen": chosen,
            "alternates": [value for _, (_, value) in ordered[1:]],
        }
    )
    return chosen


def merge_boards(payloads: list[Payload]) -> dict[str, Any]:
    """Return the semantic merge of ``(label, board)`` payloads (order-independent)."""
    if not payloads:
        raise MergeError("at least one board is required")
    for label, payload in payloads:
        if not isinstance(payload, dict):
            raise MergeError(f"{label}: board must be a JSON object")
    stamped = [(str(payload.get("updated_at", "")), payload) for _, payload in payloads]
    conflicts: list[Any] = []
    for _, payload in payloads:
        conflicts.extend(payload.get("merge_conflicts", []))

    merged: dict[str, Any] = {}
    keys = sorted({key for _, payload in payloads for key in payload})
    for key in keys:
        if key in {"claims", "merge_conflicts"}:
            continue
        values = [(stamp, payload[key]) for stamp, payload in stamped if key in payload]
        merged[key] = _merge_values(key, values, conflicts)
    if any("claims" in payload for _, payload in payloads):
        merged["claims"] = _merge_claims(payloads)
    if conflicts:
        merged["merge_conflicts"] = _unique_sorted(conflicts)
    return merged


def _strip_union(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in UNION_CLAIM_FIELDS and key != "merge_alternates"
    }


def verify_no_loss(payloads: list[Payload], merged: dict[str, Any]) -> int:
    """Raise :class:`MergeError` unless every input claim and list item survives the merge.

    Returns the number of input claim records checked (including nested alternates).
    """
    out_claims = {claim["task"]: claim for claim in merged.get("claims", [])}
    checked = 0
    for label, payload in payloads:
        for claim in payload.get("claims", []):
            base, nested = _split_alternates(claim)
            for record in [base, *nested]:
                task = record["task"]
                out = out_claims.get(task)
                if out is None:
                    raise MergeError(f"{label}: task {task!r} was lost")
                survivors = [out, *out.get("merge_alternates", [])]
                target = _canon(_strip_union(record))
                if not any(_canon(_strip_union(s)) == target for s in survivors):
                    raise MergeError(f"{label}: a record for task {task!r} was lost")
                checked += 1
        for key, value in payload.items():
            if key in {"claims", "merge_conflicts"} or not isinstance(value, list):
                continue
            out_items = {_canon(item) for item in merged.get(key, [])}
            for item in value:
                if _canon(item) not in out_items:
                    raise MergeError(f"{label}: an item of {key!r} was lost")
    return checked


def render(merged: dict[str, Any]) -> str:
    """Canonical JSON text with a trailing newline."""
    return json.dumps(merged, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("boards", nargs="+", type=Path, help="board.json files to merge")
    parser.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    args = parser.parse_args(argv)
    try:
        payloads: list[Payload] = [
            (str(path), json.loads(path.read_text(encoding="utf-8"))) for path in args.boards
        ]
        merged = merge_boards(payloads)
        checked = verify_no_loss(payloads, merged)
    except (MergeError, OSError, json.JSONDecodeError) as error:
        print(f"merge_agent_board: {error}", file=sys.stderr)
        return 1
    text = render(merged)
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    alternates = sum(len(c.get("merge_alternates", [])) for c in merged.get("claims", []))
    print(
        f"merged {len(payloads)} boards: {len(merged.get('claims', []))} claims, "
        f"{alternates} alternates kept, {checked} input records verified",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
