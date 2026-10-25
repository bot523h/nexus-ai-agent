"""Phase 1 of the Living Core: charter anchoring + Self-Ledger (fail-closed boot)."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from nexus_ai_agent.selfhood.amend import adopt_amendment
from nexus_ai_agent.selfhood.boot import (
    CharterDriftError,
    SelfIntegrityError,
    boot_self,
)
from nexus_ai_agent.selfhood.charter import CHARTER
from nexus_ai_agent.selfhood.ledger import SelfEntryKind, SelfLedger

ROOT = Path(__file__).resolve().parents[2]


def _ledger(tmp_path: Path) -> SelfLedger:
    return SelfLedger(tmp_path / "self.sqlite3")


def test_append_builds_a_gapless_verifiable_chain(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    for index in range(5):
        ledger.append(SelfEntryKind.OBSERVATION, subject=f"s{index}", detail={"i": index})
    verdict = ledger.verify()
    assert verdict.ok and verdict.record_count == 5
    assert [r.seq for r in ledger.records()] == [1, 2, 3, 4, 5]


def test_content_tampering_is_pinpointed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    for index in range(4):
        ledger.append(SelfEntryKind.OBSERVATION, subject="s", detail={"i": index})
    with sqlite3.connect(ledger.db_path) as raw:
        raw.execute(
            "UPDATE nexus_self_ledger SET record_json = replace(record_json, '\"i\":2', '\"i\":9')"
            " WHERE seq = 3"
        )
    verdict = ledger.verify()
    assert not verdict.ok and verdict.first_broken_seq == 3


def test_deleting_a_record_is_detected(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    for index in range(4):
        ledger.append(SelfEntryKind.OBSERVATION, subject="s", detail={"i": index})
    with sqlite3.connect(ledger.db_path) as raw:
        raw.execute("DELETE FROM nexus_self_ledger WHERE seq = 2")
    verdict = ledger.verify()
    assert not verdict.ok and verdict.first_broken_seq == 3


def test_anchored_head_detects_tail_truncation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    for index in range(3):
        ledger.append(SelfEntryKind.OBSERVATION, subject="s", detail={"i": index})
    anchor = ledger.verify().head_hash
    with sqlite3.connect(ledger.db_path) as raw:
        raw.execute("DELETE FROM nexus_self_ledger WHERE seq = 3")
    assert ledger.verify().ok  # a bare chain cannot see tail loss ...
    assert not ledger.verify(expected_head=anchor).ok  # ... an external anchor can


def test_commitments_are_a_projection_of_the_ledger(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.open_commitment("c1", "ship phase 1")
    ledger.open_commitment("c2", "verify backups")
    ledger.close_commitment("c1", outcome="done")
    assert [c.commitment_id for c in ledger.open_commitments()] == ["c2"]
    with pytest.raises(ValueError):
        ledger.open_commitment("c2", "duplicate")
    with pytest.raises(ValueError):
        ledger.close_commitment("nope", outcome="x")
    with pytest.raises(ValueError):
        ledger.close_commitment("c1", outcome="again")


def test_first_boot_anchors_the_charter_and_reboot_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "self.sqlite3"
    state = boot_self(path)
    assert state.charter_digest == CHARTER.digest()
    again = boot_self(path)
    adopted = [r for r in again.ledger.records() if r.kind is SelfEntryKind.CHARTER_ADOPTED]
    assert len(adopted) == 1 and again.charter_digest == state.charter_digest


def test_tampered_ledger_fails_closed_at_boot(tmp_path: Path) -> None:
    path = tmp_path / "self.sqlite3"
    boot_self(path)
    with sqlite3.connect(path) as raw:
        raw.execute("UPDATE nexus_self_ledger SET record_json = record_json || ' ' WHERE seq = 1")
    with pytest.raises(SelfIntegrityError):
        boot_self(path)


def test_charter_drift_fails_closed_without_human_amendment(tmp_path: Path) -> None:
    path = tmp_path / "self.sqlite3"
    boot_self(path)
    edited = replace(CHARTER, version=CHARTER.version + 1)
    with pytest.raises(CharterDriftError):
        boot_self(path, charter=edited)


def test_amendment_requires_a_named_human_and_then_boots(tmp_path: Path) -> None:
    path = tmp_path / "self.sqlite3"
    state = boot_self(path)
    edited = replace(CHARTER, version=CHARTER.version + 1)
    with pytest.raises(ValueError):
        adopt_amendment(state.ledger, edited, approved_by="", reason="x")
    adopt_amendment(state.ledger, edited, approved_by="majid", reason="test amendment")
    assert boot_self(path, charter=edited).charter_digest == edited.digest()


def test_crash_restores_every_charter_entry_and_open_commitment(tmp_path: Path) -> None:
    path = tmp_path / "self.sqlite3"
    child = textwrap.dedent(
        f"""
        import os
        from nexus_ai_agent.selfhood.boot import boot_self
        state = boot_self({str(path)!r})
        for i in range(6):
            state.ledger.open_commitment(f"c{{i}}", f"goal {{i}}")
        for i in range(2):
            state.ledger.close_commitment(f"c{{i}}", outcome="done")
        os._exit(137)  # hard kill: no atexit, no flush, no close
        """
    )
    proc = subprocess.run([sys.executable, "-I", "-c", child], cwd=ROOT, check=False)
    assert proc.returncode == 137
    state = boot_self(path)
    assert state.ledger.verify().ok
    assert sorted(c.commitment_id for c in state.ledger.open_commitments()) == [
        "c2",
        "c3",
        "c4",
        "c5",
    ]
    assert state.charter_digest == CHARTER.digest()


def test_created_at_tampering_is_detected(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.append(SelfEntryKind.OBSERVATION, subject="s", detail={})
    with sqlite3.connect(ledger.db_path) as raw:
        raw.execute("UPDATE nexus_self_ledger SET created_at = '1999-01-01T00:00:00+00:00'")
    verdict = ledger.verify()
    assert not verdict.ok and verdict.first_broken_seq == 1


def test_concurrent_open_commitment_admits_exactly_one_writer(tmp_path: Path) -> None:
    import threading

    ledger = _ledger(tmp_path)
    outcomes: list[str] = []

    def attempt() -> None:
        try:
            ledger.open_commitment("race", "only one")
            outcomes.append("ok")
        except ValueError:
            outcomes.append("refused")

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1 and outcomes.count("refused") == 7
    assert [c.commitment_id for c in ledger.open_commitments()] == ["race"]
    assert ledger.verify().ok


def test_two_processes_cannot_both_open_the_same_commitment(tmp_path: Path) -> None:
    path = tmp_path / "self.sqlite3"
    SelfLedger(path)
    child = textwrap.dedent(
        f"""
        import sys
        from nexus_ai_agent.selfhood.ledger import SelfLedger
        try:
            SelfLedger({str(path)!r}).open_commitment("xproc", "t")
            sys.exit(0)
        except ValueError:
            sys.exit(3)
        """
    )
    procs = [subprocess.Popen([sys.executable, "-I", "-c", child], cwd=ROOT) for _ in range(4)]
    codes = sorted(p.wait() for p in procs)
    assert codes == [0, 3, 3, 3]
    assert len(SelfLedger(path).open_commitments()) == 1


def test_precondition_runs_while_the_write_lock_is_held(tmp_path: Path) -> None:
    """Deterministic contention proof: a second writer is locked out during the check."""
    ledger = _ledger(tmp_path)
    seen: list[str] = []

    def precondition(_records: list) -> None:
        other = sqlite3.connect(ledger.db_path, timeout=0.05, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            seen.append("second writer got in")
        except sqlite3.OperationalError as exc:
            seen.append(str(exc))
        finally:
            other.close()

    ledger.append(SelfEntryKind.OBSERVATION, subject="s", precondition=precondition)
    assert seen and "locked" in seen[0], seen


def test_append_many_is_all_or_nothing_and_chained(tmp_path: Path) -> None:
    from nexus_ai_agent.selfhood.ledger import PendingEntry

    ledger = _ledger(tmp_path)
    records = ledger.append_many(
        [
            PendingEntry(SelfEntryKind.OBSERVATION, "a", {"i": 1}),
            PendingEntry(SelfEntryKind.OBSERVATION, "b", {"i": 2}),
        ]
    )
    assert [r.seq for r in records] == [1, 2] and ledger.verify().ok
    with pytest.raises(ValueError):
        ledger.append_many(
            [
                PendingEntry(SelfEntryKind.OBSERVATION, "c", {}),
                PendingEntry(SelfEntryKind.OBSERVATION, "", {}),  # invalid -> whole batch refused
            ]
        )
    assert [r.seq for r in ledger.records()] == [1, 2]
