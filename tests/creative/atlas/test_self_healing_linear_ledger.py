"""WAVE 31 — tests for the Self-Healing Linear Ledger.

Covers the append-only hash chain, the erasure-coded storage, the verify
classifier (intact / damaged / tampered / unreadable), the heal pass, the
refusal to launder a tamper, the fault-injection seams, and canonical
round-trip persistence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_ai_agent.creative.atlas import self_healing_linear_ledger as shl


def _ledger(k: int = 4, parity: int = 2, count: int = 5) -> shl.LinearEvidenceLedger:
    ledger = shl.LinearEvidenceLedger(k=k, parity=parity)
    for i in range(count):
        ledger.append(f"evidence-record-{i}")
    return ledger


# --- construction ----------------------------------------------------------------


def test_construction_validates_parameters() -> None:
    with pytest.raises(shl.LedgerError):
        shl.LinearEvidenceLedger(k=0)
    with pytest.raises(shl.LedgerError):
        shl.LinearEvidenceLedger(k=2, parity=0)


def test_empty_ledger_is_intact() -> None:
    ledger = shl.LinearEvidenceLedger()
    report = ledger.verify()
    assert report.entry_count == 0
    assert report.intact is True
    assert report.healable is True
    assert len(ledger) == 0


# --- append + chain --------------------------------------------------------------


def test_append_builds_a_hash_chain() -> None:
    ledger = _ledger(count=4)
    assert len(ledger) == 4
    assert ledger.entries[0].prev_digest == shl.GENESIS
    for prev, current in zip(ledger.entries, ledger.entries[1:], strict=False):
        assert current.prev_digest == prev.digest
    assert ledger.verify().chain_intact is True


def test_append_accepts_bytes_and_str() -> None:
    ledger = shl.LinearEvidenceLedger(k=2)
    entry = ledger.append(b"\x00\x01\x02")
    assert entry.length == 3
    assert ledger.append("text") is ledger.entries[1]


def test_append_refuses_non_bytes() -> None:
    ledger = shl.LinearEvidenceLedger()
    with pytest.raises(shl.LedgerError):
        ledger.append(42)  # type: ignore[arg-type]


def test_payload_round_trips() -> None:
    ledger = _ledger(count=3)
    for i in range(3):
        assert ledger.payload(i) == f"evidence-record-{i}".encode()


def test_payload_refuses_unknown_seq() -> None:
    ledger = _ledger(count=2)
    with pytest.raises(shl.LedgerError):
        ledger.payload(9)
    with pytest.raises(shl.LedgerError):
        ledger.payload(-1)


def test_chain_break_is_reported() -> None:
    ledger = _ledger(count=3)
    broken = shl.LedgerEntry(
        seq=1,
        length=ledger.entries[1].length,
        digest=ledger.entries[1].digest,
        prev_digest="f" * 64,
        shards=ledger.entries[1].shards,
        shard_digests=ledger.entries[1].shard_digests,
    )
    ledger._entries[1] = broken
    report = ledger.verify()
    assert report.chain_intact is False
    assert report.intact is False


# --- bit-rot healing -------------------------------------------------------------


def test_bit_rot_is_detected_and_healed() -> None:
    ledger = _ledger()
    ledger.inject_shard_fault(2, 1, bit=5)
    report = ledger.verify()
    assert report.damaged_seqs == (2,)
    assert report.tampered_seqs == ()
    assert report.intact is False
    assert report.healable is True
    healed = ledger.heal()
    assert healed.healed_seqs == (2,)
    assert healed.all_recovered is True
    assert ledger.payload(2) == b"evidence-record-2"


def test_heal_tolerates_up_to_parity_damaged_shards() -> None:
    ledger = _ledger(k=4, parity=2)
    ledger.inject_shard_fault(1, 0, bit=0)
    ledger.inject_shard_fault(1, 3, bit=7)
    healed = ledger.heal()
    assert healed.healed_seqs == (1,)
    assert ledger.payload(1) == b"evidence-record-1"


def test_more_than_parity_damaged_shards_is_unreadable() -> None:
    ledger = _ledger(k=4, parity=2)
    for shard in range(3):  # damage 3 of 6 -> only 3 readable < k=4
        ledger.inject_shard_fault(0, shard, bit=0)
    report = ledger.verify()
    assert report.unreadable_seqs == (0,)
    assert report.healable is False
    healed = ledger.heal()
    assert healed.unreadable_seqs == (0,)
    assert healed.all_recovered is False
    with pytest.raises(shl.LedgerError):
        ledger.payload(0)


def test_heal_on_an_intact_ledger_does_nothing() -> None:
    ledger = _ledger()
    healed = ledger.heal()
    assert healed.healed_seqs == ()
    assert healed.all_recovered is True


def test_inject_shard_fault_validates_arguments() -> None:
    ledger = _ledger(count=1)
    with pytest.raises(shl.LedgerError):
        ledger.inject_shard_fault(0, 99)
    with pytest.raises(shl.LedgerError):
        ledger.inject_shard_fault(0, 0, bit=9999)


# --- tamper refusal --------------------------------------------------------------


def test_tamper_is_refused_and_never_laundered() -> None:
    ledger = _ledger()
    ledger.inject_tamper(3, b"forged-evidence-3")
    report = ledger.verify()
    assert report.tampered_seqs == (3,)
    assert report.damaged_seqs == ()
    assert report.healable is False
    healed = ledger.heal()
    assert healed.tampered_seqs == (3,)
    assert healed.all_recovered is False
    # The forged bytes remain visible: the ledger never silently repairs them.
    assert ledger.payload(3) == b"forged-evidence-3"


def test_tamper_must_preserve_length() -> None:
    ledger = _ledger(count=1)
    with pytest.raises(shl.LedgerError):
        ledger.inject_tamper(0, b"short")


def test_heal_recovers_damage_but_still_refuses_a_concurrent_tamper() -> None:
    ledger = _ledger()
    ledger.inject_shard_fault(0, 0, bit=2)
    ledger.inject_tamper(4, b"forged-evidence-4")
    healed = ledger.heal()
    assert healed.healed_seqs == (0,)
    assert healed.tampered_seqs == (4,)
    assert healed.all_recovered is False


# --- persistence -----------------------------------------------------------------


def test_canonical_bytes_and_head_digest_are_deterministic() -> None:
    first = _ledger()
    second = _ledger()
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.head_digest == second.head_digest


def test_round_trip_through_dict() -> None:
    ledger = _ledger()
    ledger.inject_shard_fault(1, 2, bit=4)
    restored = shl.LinearEvidenceLedger.from_dict(ledger.to_dict())
    assert restored.head_digest == ledger.head_digest
    assert restored.k == ledger.k
    assert restored.n == ledger.n
    assert restored.verify().to_dict() == ledger.verify().to_dict()


def test_from_dict_refuses_unknown_schema_and_disorder() -> None:
    document = _ledger(count=2).to_dict()
    document["schema"] = "nope"
    with pytest.raises(shl.LedgerError):
        shl.LinearEvidenceLedger.from_dict(document)

    document = _ledger(count=2).to_dict()
    document["entries"].reverse()
    with pytest.raises(shl.LedgerError):
        shl.LinearEvidenceLedger.from_dict(document)


def test_entry_dict_round_trip() -> None:
    entry = _ledger(count=1).entries[0]
    assert shl.LedgerEntry.from_dict(entry.to_dict()) == entry


# --- reports ---------------------------------------------------------------------


def test_report_serialisation() -> None:
    ledger = _ledger(count=2)
    ledger.inject_shard_fault(0, 0, bit=0)
    assert set(ledger.verify().to_dict()) == {
        "entry_count",
        "chain_intact",
        "damaged_seqs",
        "tampered_seqs",
        "unreadable_seqs",
        "intact",
        "healable",
    }
    assert set(ledger.heal().to_dict()) == {
        "healed_seqs",
        "tampered_seqs",
        "unreadable_seqs",
        "intact_after",
        "all_recovered",
    }


def test_public_surface_matches_declared_exports() -> None:
    for name in shl.__all__:
        assert hasattr(shl, name), name


# --- predecessor-bound digest (Wave 34 hardening) ----------------------------------


def test_entry_digest_commits_to_position_and_predecessor() -> None:
    base = shl._entry_digest(seq=0, prev_digest=shl.GENESIS, data=b"payload")
    # a different sequence number changes the digest even with identical bytes
    assert shl._entry_digest(seq=1, prev_digest=shl.GENESIS, data=b"payload") != base
    # a different predecessor link changes the digest
    assert shl._entry_digest(seq=0, prev_digest="a" * 64, data=b"payload") != base
    # a different payload changes the digest
    assert shl._entry_digest(seq=0, prev_digest=shl.GENESIS, data=b"payloae") != base


def test_entry_digest_is_domain_separated() -> None:
    assert shl.ENTRY_DOMAIN  # a non-empty domain separator is present
    # the digest is not a bare sha256 of the payload
    import hashlib

    assert shl._entry_digest(seq=0, prev_digest=shl.GENESIS, data=b"x") != (
        hashlib.sha256(b"x").hexdigest()
    )


# --- external witness anchor (Wave 34 hardening) -----------------------------------


def test_verify_with_matching_anchor_is_intact() -> None:
    ledger = _ledger(count=4)
    anchor = shl.LedgerAnchor(seq_count=4, head_digest=ledger.head_digest)
    assert ledger.verify(anchor=anchor).intact is True


def test_verify_with_anchor_detects_a_full_history_rewrite() -> None:
    ledger = _ledger(count=4)
    anchor = shl.LedgerAnchor(seq_count=4, head_digest=ledger.head_digest)
    # an attacker rewrites the whole chain from genesis and recalculates every
    # prev_digest and digest so the internal chain still "verifies"
    rewritten = shl.LinearEvidenceLedger(k=4, parity=2)
    for i in range(4):
        rewritten.append(f"forged-record-{i}")
    assert rewritten.verify().chain_intact is True  # internally consistent
    report = rewritten.verify(anchor=anchor)
    assert report.chain_intact is False
    assert report.intact is False


def test_verify_with_anchor_detects_truncation() -> None:
    ledger = _ledger(count=5)
    anchor = shl.LedgerAnchor(seq_count=5, head_digest=ledger.head_digest)
    truncated = shl.LinearEvidenceLedger(k=4, parity=2)
    for i in range(3):
        truncated.append(f"evidence-record-{i}")
    assert truncated.verify(anchor=anchor).intact is False


def test_heal_with_anchor_reports_not_intact_after_a_rewrite() -> None:
    ledger = _ledger(count=3)
    anchor = shl.LedgerAnchor(seq_count=3, head_digest=ledger.head_digest)
    forged = shl.LinearEvidenceLedger(k=4, parity=2)
    for i in range(3):
        forged.append(f"forged-{i}")
    report = forged.heal(anchor=anchor)
    assert report.intact_after is False


def test_anchor_round_trips_and_validates() -> None:
    anchor = shl.LedgerAnchor(seq_count=7, head_digest="a" * 64)
    restored = shl.LedgerAnchor.from_dict(anchor.to_dict())
    assert restored == anchor
    with pytest.raises(shl.LedgerError):
        shl.LedgerAnchor.from_dict({"schema": "nope", "seq_count": 1, "head_digest": "a" * 64})
    with pytest.raises(shl.LedgerError):
        shl.LedgerAnchor.from_dict(
            {"schema": "atlas.ledger-anchor/1", "seq_count": 1, "head_digest": "short"}
        )
    with pytest.raises(shl.LedgerError):
        shl.LedgerAnchor.from_dict(
            {"schema": "atlas.ledger-anchor/1", "seq_count": -1, "head_digest": "a" * 64}
        )


# --- on-disk persistence (Wave 34 hardening) ---------------------------------------


def test_save_and_load_from_disk_round_trips(tmp_path: Path) -> None:
    ledger = _ledger(count=5)
    log = tmp_path / "ledger.jsonl"
    anchor_path = tmp_path / "ledger.anchor.json"
    anchor = ledger.save_to_disk(log, anchor_path)
    assert log.is_file() and anchor_path.is_file()
    restored = shl.LinearEvidenceLedger.load_from_disk(log, anchor_path)
    assert restored.head_digest == ledger.head_digest
    assert anchor.head_digest == ledger.head_digest
    assert restored.payload(2) == b"evidence-record-2"


def test_load_from_disk_refuses_a_ledger_that_disagrees_with_the_anchor(tmp_path: Path) -> None:
    ledger = _ledger(count=4)
    log = tmp_path / "ledger.jsonl"
    anchor_path = tmp_path / "ledger.anchor.json"
    ledger.save_to_disk(log, anchor_path)
    # forge a shorter ledger and re-anchor the log, but keep the original anchor
    forged = shl.LinearEvidenceLedger(k=4, parity=2)
    for i in range(3):
        forged.append(f"evidence-record-{i}")
    log.write_text(json.dumps(forged.to_dict()), encoding="utf-8")
    with pytest.raises(shl.LedgerError):
        shl.LinearEvidenceLedger.load_from_disk(log, anchor_path)


def test_load_from_disk_refuses_missing_files(tmp_path: Path) -> None:
    with pytest.raises(shl.LedgerError):
        shl.LinearEvidenceLedger.load_from_disk(tmp_path / "missing.jsonl")
    ledger = _ledger(count=2)
    log = tmp_path / "ledger.jsonl"
    ledger.save_to_disk(log, tmp_path / "a.json")
    with pytest.raises(shl.LedgerError):
        shl.LinearEvidenceLedger.load_anchor(tmp_path / "missing.anchor.json")


def test_load_from_disk_refuses_a_non_json_log(tmp_path: Path) -> None:
    log = tmp_path / "ledger.jsonl"
    log.write_text("{not json", encoding="utf-8")
    with pytest.raises(shl.LedgerError):
        shl.LinearEvidenceLedger.load_from_disk(log)


def test_load_anchor_refuses_a_non_json_anchor(tmp_path: Path) -> None:
    anchor_path = tmp_path / "ledger.anchor.json"
    anchor_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(shl.LedgerError):
        shl.LinearEvidenceLedger.load_anchor(anchor_path)


def test_load_from_disk_without_anchor_returns_the_ledger(tmp_path: Path) -> None:
    ledger = _ledger(count=3)
    log = tmp_path / "ledger.jsonl"
    ledger.save_to_disk(log, tmp_path / "a.json")
    restored = shl.LinearEvidenceLedger.load_from_disk(log)
    assert restored.head_digest == ledger.head_digest


def test_atomic_write_cleans_up_when_the_replace_fails(tmp_path: Path, monkeypatch: object) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(shl.os, "replace", boom)  # type: ignore[attr-defined]
    with pytest.raises(OSError):
        shl._atomic_write(tmp_path / "out.bin", b"payload")
    # no stray temp files are left behind in the directory
    assert [p.name for p in tmp_path.iterdir()] == []
