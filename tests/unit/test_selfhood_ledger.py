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
