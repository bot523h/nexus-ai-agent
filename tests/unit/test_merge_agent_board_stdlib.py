"""Stdlib-only tests for scripts/merge_agent_board.py.

Run with: python3 -m unittest tests/unit/test_merge_agent_board_stdlib.py
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "merge_agent_board.py"
_spec = importlib.util.spec_from_file_location("merge_agent_board", _SCRIPT)
assert _spec is not None and _spec.loader is not None
mab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mab)


def _board(updated_at: str, *claims: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"schema": 2, "updated_at": updated_at, "claims": list(claims), **extra}


def _claim(task: str, status: str, **fields: Any) -> dict[str, Any]:
    return {"task": task, "status": status, "agent_branch": "arena/x", **fields}


class MergeAgentBoardTests(unittest.TestCase):
    def test_disjoint_claims_from_every_branch_are_all_kept(self) -> None:
        a = _board("2026-11-12T01:00:00Z", _claim("task-a", "done"))
        b = _board("2026-11-12T02:00:00Z", _claim("task-b", "queued"))
        c = _board("2026-11-12T03:00:00Z", _claim("task-c", "active"))
        merged = mab.merge_boards([("a", a), ("b", b), ("c", c)])
        self.assertEqual([x["task"] for x in merged["claims"]], ["task-a", "task-b", "task-c"])
        self.assertEqual(mab.verify_no_loss([("a", a), ("b", b), ("c", c)], merged), 3)

    def test_higher_status_wins_and_the_loser_is_kept_as_alternate(self) -> None:
        queued = _board("2026-11-12T01:00:00Z", _claim("t", "queued", note="old"))
        done = _board("2026-11-12T02:00:00Z", _claim("t", "done", note="new"))
        merged = mab.merge_boards([("q", queued), ("d", done)])
        (claim,) = merged["claims"]
        self.assertEqual(claim["status"], "done")
        self.assertEqual([alt["status"] for alt in claim["merge_alternates"]], ["queued"])
        mab.verify_no_loss([("q", queued), ("d", done)], merged)

    def test_evidence_and_path_lists_are_unioned_and_sorted(self) -> None:
        one = _board("t1", _claim("t", "active", files=["z.py", "a.py"], evidence=["e2"]))
        two = _board("t2", _claim("t", "active", files=["m.py", "a.py"], evidence=["e1"]))
        (claim,) = mab.merge_boards([("1", one), ("2", two)])["claims"]
        self.assertEqual(claim["files"], ["a.py", "m.py", "z.py"])
        self.assertEqual(claim["evidence"], ["e1", "e2"])

    def test_unknown_status_is_refused(self) -> None:
        bad = _board("t", _claim("t", "completed-ish"))
        with self.assertRaisesRegex(mab.MergeError, "unknown claim status"):
            mab.merge_boards([("bad", bad)])

    def test_duplicate_task_inside_one_board_is_refused(self) -> None:
        dup = _board("t", _claim("t", "done"), _claim("t", "queued"))
        with self.assertRaisesRegex(mab.MergeError, "duplicate task"):
            mab.merge_boards([("dup", dup)])

    def test_merge_does_not_depend_on_input_order(self) -> None:
        a = _board("2026-11-12T01:00:00Z", _claim("t", "queued"), _claim("u", "done"))
        b = _board("2026-11-12T02:00:00Z", _claim("t", "done", files=["x"]), _claim("v", "active"))
        forward = mab.render(mab.merge_boards([("a", a), ("b", b)]))
        reverse = mab.render(mab.merge_boards([("b", b), ("a", a)]))
        self.assertEqual(forward, reverse)

    def test_remerging_a_merged_board_is_idempotent(self) -> None:
        a = _board("2026-11-12T01:00:00Z", _claim("t", "queued"))
        b = _board("2026-11-12T02:00:00Z", _claim("t", "done"), _claim("u", "active"))
        merged = mab.merge_boards([("a", a), ("b", b)])
        self.assertEqual(mab.merge_boards([("m", merged)]), merged)
        self.assertEqual(mab.merge_boards([("m", merged), ("a", a), ("b", b)]), merged)

    def test_scalar_conflict_keeps_latest_updated_at_and_is_recorded(self) -> None:
        old = _board("2026-11-12T01:00:00Z", protocol={"version": 1})
        new = _board("2026-11-12T09:00:00Z", protocol={"version": 2})
        merged = mab.merge_boards([("old", old), ("new", new)])
        self.assertEqual(merged["protocol"], {"version": 2})
        paths = {c["path"] for c in merged["merge_conflicts"]}
        self.assertIn("protocol.version", paths)

    def test_output_is_canonical_json_with_trailing_newline(self) -> None:
        board = _board("t", _claim("t", "done"), history={"b": 2, "a": 1})
        text = mab.render(mab.merge_boards([("b", board)]))
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(json.loads(text), mab.merge_boards([("b", board)]))
        self.assertLess(text.index('"a"'), text.index('"b"'))

    def test_verify_no_loss_detects_a_dropped_claim(self) -> None:
        a = _board("t", _claim("t", "done"), _claim("u", "queued"))
        merged = mab.merge_boards([("a", a)])
        merged["claims"] = [c for c in merged["claims"] if c["task"] != "u"]
        with self.assertRaisesRegex(mab.MergeError, "was lost"):
            mab.verify_no_loss([("a", a)], merged)


if __name__ == "__main__":
    unittest.main()
