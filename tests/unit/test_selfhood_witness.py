"""Two-witness check: a paired device's head receipts versus the Self-Ledger."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nexus_ai_agent.selfhood.ledger import SelfEntryKind, SelfLedger
from nexus_ai_agent.selfhood.witness import (
    MAX_RECEIPTS,
    WitnessReceipt,
    WitnessStatus,
    check_witnesses,
    issue_receipt,
    parse_receipt,
)

HASH = "sha256:" + "a" * 64


def _ledger(
    tmp_path: Path, name: str = "self.sqlite3", *, count: int = 6, salt: str = "x"
) -> SelfLedger:
    ledger = SelfLedger(tmp_path / name)
    for index in range(count):
        ledger.append(SelfEntryKind.OBSERVATION, subject="s", detail={"i": index, "salt": salt})
    return ledger


def _receipt(ledger: SelfLedger, seq: int) -> WitnessReceipt:
    record = ledger.records()[seq - 1]
    return WitnessReceipt(record.seq, record.record_hash)


def _truncate(ledger: SelfLedger, keep: int) -> None:
    with sqlite3.connect(ledger.db_path) as raw:
        raw.execute("DELETE FROM nexus_self_ledger WHERE seq > ?", (keep,))


def test_empty_ledger_issues_no_receipt(tmp_path: Path) -> None:
    assert issue_receipt(SelfLedger(tmp_path / "e.sqlite3")) is None


def test_issued_receipt_is_consistent_with_its_own_ledger(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    receipt = issue_receipt(ledger)
    assert receipt is not None and receipt.seq == 6
    verdict = check_witnesses(ledger, [receipt])
    assert verdict.ok and verdict.status is WitnessStatus.CONSISTENT
    assert verdict.receipts_checked == 1 and verdict.ledger_length == 6


def test_honest_growth_after_the_receipt_is_not_a_false_alarm(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, count=3)
    receipt = issue_receipt(ledger)
    assert receipt is not None
    # The failure mode of verify(expected_head=...): the head moved, so it cries wolf.
    for index in range(4):
        ledger.append(SelfEntryKind.OBSERVATION, subject="later", detail={"i": index})
    assert not ledger.verify(expected_head=receipt.record_hash).ok
    assert check_witnesses(ledger, [receipt]).ok


def test_no_receipts_is_never_reported_as_consistent(tmp_path: Path) -> None:
    verdict = check_witnesses(_ledger(tmp_path), [])
    assert verdict.status is WitnessStatus.NO_WITNESS and not verdict.ok


@pytest.mark.parametrize("witnessed", range(1, 7))
@pytest.mark.parametrize("kept", range(0, 7))
def test_every_truncation_point_is_decided_exactly(
    tmp_path: Path, witnessed: int, kept: int
) -> None:
    ledger = _ledger(tmp_path, count=6)
    receipt = _receipt(ledger, witnessed)
    _truncate(ledger, kept)
    assert ledger.verify().ok  # the bare chain cannot see tail loss
    verdict = check_witnesses(ledger, [receipt])
    if witnessed > kept:
        assert verdict.status is WitnessStatus.TRUNCATED
        assert verdict.failing_seq == witnessed
    else:
        assert verdict.ok


def test_rewritten_history_is_diverged_even_when_its_own_chain_verifies(tmp_path: Path) -> None:
    honest = _ledger(tmp_path, "honest.sqlite3", count=5, salt="real")
    forged = _ledger(tmp_path, "forged.sqlite3", count=5, salt="forged")
    assert forged.verify().ok
    verdict = check_witnesses(forged, [issue_receipt(honest)])
    assert verdict.status is WitnessStatus.DIVERGED and verdict.failing_seq == 5


def test_an_early_receipt_pins_the_prefix_under_a_later_rewrite(tmp_path: Path) -> None:
    honest = _ledger(tmp_path, "honest.sqlite3", count=5, salt="real")
    early = _receipt(honest, 2)
    forged = _ledger(tmp_path, "forged.sqlite3", count=5, salt="forged")
    verdict = check_witnesses(forged, [early])
    assert verdict.status is WitnessStatus.DIVERGED and verdict.failing_seq == 2


def test_two_witnesses_that_saw_different_histories_are_a_fork(tmp_path: Path) -> None:
    a = _ledger(tmp_path, "a.sqlite3", count=4, salt="a")
    b = _ledger(tmp_path, "b.sqlite3", count=4, salt="b")
    verdict = check_witnesses(a, [_receipt(a, 3), _receipt(b, 3)])
    assert verdict.status is WitnessStatus.CONFLICTING and verdict.failing_seq == 3


def test_identical_receipts_are_deduplicated_not_conflicting(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    receipt = _receipt(ledger, 4)
    verdict = check_witnesses(ledger, [receipt, receipt, receipt.to_dict()])
    assert verdict.ok and verdict.receipts_checked == 1


def test_a_broken_chain_is_reported_before_any_witness_comparison(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    receipt = issue_receipt(ledger)
    with sqlite3.connect(ledger.db_path) as raw:
        raw.execute("DELETE FROM nexus_self_ledger WHERE seq = 3")
    verdict = check_witnesses(ledger, [receipt])
    assert verdict.status is WitnessStatus.LEDGER_TAMPERED and verdict.failing_seq is not None


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "x",
        42,
        [],
        {},
        {"seq": 1},
        {"record_hash": HASH},
        {"seq": 0, "record_hash": HASH},
        {"seq": -3, "record_hash": HASH},
        {"seq": True, "record_hash": HASH},
        {"seq": 1.0, "record_hash": HASH},
        {"seq": "1", "record_hash": HASH},
        {"seq": 1, "record_hash": HASH[:-1]},
        {"seq": 1, "record_hash": "a" * 64},
        {"seq": 1, "record_hash": "sha256:" + "A" * 64},
        {"seq": 1, "record_hash": "sha256:" + "g" * 64},
        {"seq": 1, "record_hash": 7},
    ],
)
def test_malformed_receipts_never_raise_and_never_pass(tmp_path: Path, bad: object) -> None:
    assert parse_receipt(bad) is None
    ledger = _ledger(tmp_path)
    verdict = check_witnesses(ledger, [issue_receipt(ledger), bad])
    assert verdict.status is WitnessStatus.MALFORMED and not verdict.ok


def test_an_absurd_number_of_receipts_is_rejected_without_work(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    flood = (WitnessReceipt(1 + (i % 6), HASH) for i in range(MAX_RECEIPTS + 1))
    verdict = check_witnesses(ledger, flood)
    assert verdict.status is WitnessStatus.MALFORMED and "too many" in verdict.detail


def test_receipts_round_trip_through_json_transport(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    receipt = issue_receipt(ledger)
    assert receipt is not None
    wire = json.loads(json.dumps([receipt.to_dict()]))
    assert check_witnesses(ledger, wire).ok
