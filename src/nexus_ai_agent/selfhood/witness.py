"""Two-witness check: does the Self-Ledger still extend what a paired device saw?

A bare hash chain cannot see loss of its newest records (see ``ledger.py``), and
``SelfLedger.verify(expected_head=...)`` only works while the head has not moved:
the first honest append after the anchor turns it into a false alarm. A
**witness receipt** fixes both. It is ``(seq, record_hash)`` — what a paired
device (the application) observed at some moment. Because the hash at ``seq``
commits to every earlier record, one receipt proves the whole prefix up to
``seq``; the ledger may legitimately grow past it.

Verdicts, in the order they are decided:

* ``MALFORMED``     — a receipt is not ``(positive int, ``sha256:`` + 64 lowercase hex)`` or
                      there are absurdly many; untrusted input never raises and
                      never passes.
* ``NO_WITNESS``    — no receipts: nothing was proven. Never reported as OK.
* ``CONFLICTING``   — two receipts name the same ``seq`` with different hashes:
                      the witnesses saw different histories (a fork).
* ``LEDGER_TAMPERED`` — the chain itself no longer verifies.
* ``TRUNCATED``     — a receipt is beyond the ledger's end: the tail was lost.
* ``DIVERGED``      — the record at a receipt's ``seq`` has another hash: history
                      was rewritten (even if the rewritten chain verifies).
* ``CONSISTENT``    — every receipt is a prefix of the ledger.

Honest limit: a witness only protects the prefix it has seen. Records written
and rewritten *between* two receipts are invisible to this check, and a lost
device is a lost witness. This is evidence of tampering, never authority.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from nexus_ai_agent.selfhood.ledger import SelfLedger, SelfRecord

MAX_RECEIPTS = 4096
_PREFIX = "sha256:"
_HEX = frozenset("0123456789abcdef")


class WitnessStatus(str, Enum):
    CONSISTENT = "consistent"
    NO_WITNESS = "no_witness"
    TRUNCATED = "truncated"
    DIVERGED = "diverged"
    CONFLICTING = "conflicting"
    LEDGER_TAMPERED = "ledger_tampered"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class WitnessReceipt:
    """What a paired device observed: the record hash at ``seq``."""

    seq: int
    record_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "record_hash": self.record_hash}


@dataclass(frozen=True)
class WitnessVerdict:
    status: WitnessStatus
    receipts_checked: int
    ledger_length: int
    failing_seq: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is WitnessStatus.CONSISTENT


def parse_receipt(raw: object) -> WitnessReceipt | None:
    """Validate one untrusted receipt (e.g. decoded device JSON); ``None`` if bad."""
    seq: object
    digest: object
    if isinstance(raw, WitnessReceipt):
        seq, digest = raw.seq, raw.record_hash
    elif isinstance(raw, dict):
        seq, digest = raw.get("seq"), raw.get("record_hash")
    else:
        return None
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        return None
    if not isinstance(digest, str) or not digest.startswith(_PREFIX):
        return None
    body = digest[len(_PREFIX) :]
    if len(body) != 64 or not _HEX.issuperset(body):
        return None
    return WitnessReceipt(seq, digest)


def issue_receipt(ledger: SelfLedger) -> WitnessReceipt | None:
    """The receipt for the current head, or ``None`` for an empty ledger."""
    records = ledger.records()
    if not records:
        return None
    head = records[-1]
    return WitnessReceipt(head.seq, head.record_hash)


def _verdict(
    status: WitnessStatus,
    checked: int,
    length: int,
    failing: int | None = None,
    detail: str = "",
) -> WitnessVerdict:
    return WitnessVerdict(status, checked, length, failing, detail)


def compare_witnesses(records: Sequence[SelfRecord], receipts: Iterable[object]) -> WitnessVerdict:
    """Compare receipts with records whose chain the caller has ALREADY verified.

    Pure: no I/O. Prefer :func:`check_witnesses`, which verifies the chain first.
    """
    length = len(records)
    parsed: dict[tuple[int, str], WitnessReceipt] = {}
    seen = 0
    for raw in receipts:
        seen += 1
        if seen > MAX_RECEIPTS:
            return _verdict(WitnessStatus.MALFORMED, 0, length, None, "too many receipts")
        receipt = parse_receipt(raw)
        if receipt is None:
            return _verdict(WitnessStatus.MALFORMED, 0, length, None, f"receipt #{seen} is invalid")
        parsed[(receipt.seq, receipt.record_hash)] = receipt
    if not parsed:
        return _verdict(WitnessStatus.NO_WITNESS, 0, length, None, "no receipts: nothing proven")

    by_seq: dict[int, str] = {}
    for seq, digest in sorted(parsed):
        if by_seq.setdefault(seq, digest) != digest:
            return _verdict(
                WitnessStatus.CONFLICTING,
                len(parsed),
                length,
                seq,
                "two receipts name the same seq with different hashes (fork)",
            )
    for seq, digest in sorted(by_seq.items()):
        if seq > length:
            return _verdict(
                WitnessStatus.TRUNCATED,
                len(by_seq),
                length,
                seq,
                f"witnessed seq {seq} but the ledger ends at {length}",
            )
        if records[seq - 1].record_hash != digest:
            return _verdict(
                WitnessStatus.DIVERGED,
                len(by_seq),
                length,
                seq,
                f"record {seq} differs from what the witness saw (history rewritten)",
            )
    return _verdict(WitnessStatus.CONSISTENT, len(by_seq), length)


def check_witnesses(ledger: SelfLedger, receipts: Iterable[object]) -> WitnessVerdict:
    """Verify the chain, then prove every receipt is a prefix of the ledger."""
    chain = ledger.verify()
    if not chain.ok:
        return _verdict(
            WitnessStatus.LEDGER_TAMPERED,
            0,
            chain.record_count,
            chain.first_broken_seq,
            chain.reason or "chain does not verify",
        )
    return compare_witnesses(ledger.records(), receipts)


__all__ = [
    "MAX_RECEIPTS",
    "WitnessReceipt",
    "WitnessStatus",
    "WitnessVerdict",
    "check_witnesses",
    "compare_witnesses",
    "issue_receipt",
    "parse_receipt",
]
