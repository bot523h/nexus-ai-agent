"""WAVE 31 — tests for the Self-Healing Linear Ledger.

Covers the append-only hash chain, the erasure-coded storage, the verify
classifier (intact / damaged / tampered / unreadable), the heal pass, the
refusal to launder a tamper, the fault-injection seams, and canonical
round-trip persistence.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.atlas import self_healing_linear_ledger as shl


def _ledger(k: int = 4, parity: int = 2, count: int = 5) -> shl.SelfHealingLinearLedger:
    ledger = shl.SelfHealingLinearLedger(k=k, parity=parity)
    for i in range(count):
        ledger.append(f"evidence-record-{i}")
    return ledger


# --- construction ----------------------------------------------------------------


def test_construction_validates_parameters() -> None:
    with pytest.raises(shl.LedgerError):
        shl.SelfHealingLinearLedger(k=0)
    with pytest.raises(shl.LedgerError):
        shl.SelfHealingLinearLedger(k=2, parity=0)


def test_empty_ledger_is_intact() -> None:
    ledger = shl.SelfHealingLinearLedger()
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
    ledger = shl.SelfHealingLinearLedger(k=2)
    entry = ledger.append(b"\x00\x01\x02")
    assert entry.length == 3
    assert ledger.append("text") is ledger.entries[1]


def test_append_refuses_non_bytes() -> None:
    ledger = shl.SelfHealingLinearLedger()
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
    restored = shl.SelfHealingLinearLedger.from_dict(ledger.to_dict())
    assert restored.head_digest == ledger.head_digest
    assert restored.k == ledger.k
    assert restored.n == ledger.n
    assert restored.verify().to_dict() == ledger.verify().to_dict()


def test_from_dict_refuses_unknown_schema_and_disorder() -> None:
    document = _ledger(count=2).to_dict()
    document["schema"] = "nope"
    with pytest.raises(shl.LedgerError):
        shl.SelfHealingLinearLedger.from_dict(document)

    document = _ledger(count=2).to_dict()
    document["entries"].reverse()
    with pytest.raises(shl.LedgerError):
        shl.SelfHealingLinearLedger.from_dict(document)


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
