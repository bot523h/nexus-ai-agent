"""WAVE 31 — the Self-Healing Linear Ledger.

The atlas records its evidence in an append-only ledger. Two failure modes must
be survivable:

* **bit-rot** — a stored byte quietly flips. The ledger must notice, locate the
  damaged shard, and rebuild the record from the surviving shards.
* **tampering** — a record's bytes are rewritten to say something else. The
  ledger must refuse to heal that: a self-healing ledger that "repairs" a
  forgery into a valid entry is worse than no ledger at all.

Both are handled by the same construction. Each record is a node in a SHA-256
hash chain *and* is stored as ``k`` data + ``parity`` parity shards under the
Wave 30 systematic MDS code over ``GF(2^8)``. Each shard carries its own digest,
so a damaged shard is *erased* rather than trusted; any ``k`` surviving shards
rebuild the record exactly.

The distinction is the seal:

* if the rebuilt record hashes back to the digest already in the chain, the
  damage was random — **heal**;
* if it does not, the bytes were rewritten — **refuse**, and report a tamper.

The public surface is :class:`LinearEvidenceLedger` with
:meth:`~LinearEvidenceLedger.append`,
:meth:`~LinearEvidenceLedger.verify` and
:meth:`~LinearEvidenceLedger.heal`, plus the deterministic fault-injection
seams :meth:`~LinearEvidenceLedger.inject_shard_fault` and
:meth:`~LinearEvidenceLedger.inject_tamper` used by the falsification wave.
Pure Python + stdlib, offline, deterministic.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from nexus_ai_agent.creative.atlas.atlas_spine_governance import jcs_canonical_bytes
from nexus_ai_agent.creative.atlas.holographic_equation_vault import (
    DEFAULT_PARITY,
    VaultError,
    decode_shards,
    encode_shards,
)

__all__ = [
    "LEDGER_SCHEMA",
    "DEFAULT_DATA_SHARDS",
    "GENESIS",
    "ENTRY_DOMAIN",
    "LedgerError",
    "LedgerEntry",
    "LedgerIntegrityReport",
    "HealingReport",
    "LedgerAnchor",
    "LinearEvidenceLedger",
]

LEDGER_SCHEMA: Final[str] = "atlas.self-healing-linear-ledger/1"
DEFAULT_DATA_SHARDS: Final[int] = 4
#: The genesis link, so record 0 has a well-defined ``prev_digest``.
GENESIS: Final[str] = "0" * 64
#: Domain separator for the predecessor-bound entry digest.
ENTRY_DOMAIN: Final[bytes] = b"NAGAR_LEDGER_V1\x00"


class LedgerError(ValueError):
    """Raised for a malformed ledger operation or an unhealable record."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _entry_digest(*, seq: int, prev_digest: str, data: bytes) -> str:
    """The predecessor-bound entry digest.

    Every entry's digest cryptographically commits to its position (``seq``), its
    predecessor link (``prev_digest``) and its payload, so recomputing or
    mutating any predecessor link or sequence number invalidates ``digest_i`` and
    every subsequent ``digest_j`` (``j > i``).
    """
    return _sha256(
        ENTRY_DOMAIN
        + struct.pack(">Q", seq)
        + bytes.fromhex(prev_digest)
        + struct.pack(">Q", len(data))
        + data
    )


def _payload_bytes(payload: bytes | bytearray | str) -> bytes:
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    raise LedgerError("payload must be bytes or str")


@dataclass(frozen=True)
class LedgerEntry:
    """One sealed ledger record: its chain link and its erasure-coded shards."""

    seq: int
    length: int
    digest: str
    prev_digest: str
    shards: tuple[bytes, ...]
    shard_digests: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "length": self.length,
            "digest": self.digest,
            "prev_digest": self.prev_digest,
            "shards": [s.hex() for s in self.shards],
            "shard_digests": list(self.shard_digests),
        }

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> LedgerEntry:
        return cls(
            seq=int(document["seq"]),
            length=int(document["length"]),
            digest=str(document["digest"]),
            prev_digest=str(document["prev_digest"]),
            shards=tuple(bytes.fromhex(s) for s in document["shards"]),
            shard_digests=tuple(str(d) for d in document["shard_digests"]),
        )


@dataclass(frozen=True)
class LedgerIntegrityReport:
    """What :meth:`LinearEvidenceLedger.verify` found, without changing it."""

    entry_count: int
    chain_intact: bool
    damaged_seqs: tuple[int, ...]
    tampered_seqs: tuple[int, ...]
    unreadable_seqs: tuple[int, ...]

    @property
    def intact(self) -> bool:
        return self.chain_intact and not self.damaged_seqs and not self.tampered_seqs

    @property
    def healable(self) -> bool:
        return not self.tampered_seqs and not self.unreadable_seqs

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_count": self.entry_count,
            "chain_intact": self.chain_intact,
            "damaged_seqs": list(self.damaged_seqs),
            "tampered_seqs": list(self.tampered_seqs),
            "unreadable_seqs": list(self.unreadable_seqs),
            "intact": self.intact,
            "healable": self.healable,
        }


@dataclass(frozen=True)
class HealingReport:
    """The outcome of a heal pass over the ledger."""

    healed_seqs: tuple[int, ...]
    tampered_seqs: tuple[int, ...]
    unreadable_seqs: tuple[int, ...]
    intact_after: bool

    @property
    def all_recovered(self) -> bool:
        return self.intact_after and not self.tampered_seqs and not self.unreadable_seqs

    def to_dict(self) -> dict[str, Any]:
        return {
            "healed_seqs": list(self.healed_seqs),
            "tampered_seqs": list(self.tampered_seqs),
            "unreadable_seqs": list(self.unreadable_seqs),
            "intact_after": self.intact_after,
            "all_recovered": self.all_recovered,
        }


@dataclass(frozen=True)
class LedgerAnchor:
    """An independent external witness for the ledger head.

    Written to a *separate* file (``anchor_path``) from the ledger JSONL, so an
    attacker who can rewrite the whole ledger from genesis cannot also update the
    witness — verification against the anchor refuses a full-history rewrite.
    """

    seq_count: int
    head_digest: str
    schema: str = "atlas.ledger-anchor/1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "seq_count": self.seq_count,
            "head_digest": self.head_digest,
        }

    def canonical_bytes(self) -> bytes:
        return jcs_canonical_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> LedgerAnchor:
        if document.get("schema") != "atlas.ledger-anchor/1":
            raise LedgerError(f"unknown ledger anchor schema {document.get('schema')!r}")
        head = str(document["head_digest"])
        if len(head) != 64:
            raise LedgerError("anchor head_digest must be a 64-hex digest")
        count = int(document["seq_count"])
        if count < 0:
            raise LedgerError("anchor seq_count must be non-negative")
        return cls(seq_count=count, head_digest=head)


class LinearEvidenceLedger:
    """An append-only hash-chained ledger whose records rebuild from parity.

    Construct with ``k`` data shards and ``parity`` extra shards; the ledger
    tolerates up to ``parity`` damaged shards per record.
    """

    def __init__(self, *, k: int = DEFAULT_DATA_SHARDS, parity: int = DEFAULT_PARITY) -> None:
        if not isinstance(k, int) or k < 1:
            raise LedgerError("k must be an integer >= 1")
        if not isinstance(parity, int) or parity < 1:
            raise LedgerError("parity must be an integer >= 1")
        # Reuse the vault's validated MDS generator to fail fast on bad params.
        try:
            encode_shards(b"", k=k, n=k + parity)
        except VaultError as exc:  # pragma: no cover - defensive re-raise
            raise LedgerError(str(exc)) from exc
        self._k = k
        self._parity = parity
        self._n = k + parity
        self._entries: list[LedgerEntry] = []

    @property
    def k(self) -> int:
        return self._k

    @property
    def n(self) -> int:
        return self._n

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def append(self, payload: bytes | bytearray | str) -> LedgerEntry:
        """Seal ``payload`` as the next record; returns the new entry."""
        data = _payload_bytes(payload)
        prev_digest = self._entries[-1].digest if self._entries else GENESIS
        entry = self._seal(seq=len(self._entries), data=data, prev_digest=prev_digest)
        self._entries.append(entry)
        return entry

    def _seal(self, *, seq: int, data: bytes, prev_digest: str) -> LedgerEntry:
        shards = encode_shards(data, k=self._k, n=self._n)
        return LedgerEntry(
            seq=seq,
            length=len(data),
            digest=_entry_digest(seq=seq, prev_digest=prev_digest, data=data),
            prev_digest=prev_digest,
            shards=shards,
            shard_digests=tuple(_sha256(s) for s in shards),
        )

    def payload(self, seq: int) -> bytes:
        """Recover a record's payload from its surviving shards."""
        entry = self._entry(seq)
        good = {
            i: entry.shards[i]
            for i, digest in enumerate(entry.shard_digests)
            if _sha256(entry.shards[i]) == digest
        }
        if len(good) < self._k:
            raise LedgerError(f"record {seq} has only {len(good)} readable shards; need {self._k}")
        return decode_shards(good, k=self._k, n=self._n)[: entry.length]

    def _entry(self, seq: int) -> LedgerEntry:
        if not isinstance(seq, int) or not 0 <= seq < len(self._entries):
            raise LedgerError(f"no record at seq {seq!r}")
        return self._entries[seq]

    # --- verification ---------------------------------------------------------

    def _rebuilt_matches_seal(self, entry: LedgerEntry, rebuilt: bytes) -> bool:
        """Whether ``rebuilt`` reproduces the entry's predecessor-bound digest."""
        return _entry_digest(seq=entry.seq, prev_digest=entry.prev_digest, data=rebuilt) == (
            entry.digest
        )

    def _anchor_conflict(self, anchor: LedgerAnchor | None) -> str | None:
        """Return a refusal reason when the ledger disagrees with its witness."""
        if anchor is None:
            return None
        if anchor.seq_count != len(self._entries):
            return (
                f"external witness records {anchor.seq_count} entries but the ledger has "
                f"{len(self._entries)} (a rewrite truncated or extended the history)"
            )
        if anchor.head_digest != self.head_digest:
            return (
                "ledger head digest does not match the external witness anchor "
                "(a rewrite recalculated the chain from genesis)"
            )
        return None

    def verify(self, *, anchor: LedgerAnchor | None = None) -> LedgerIntegrityReport:
        """Classify every record without modifying the ledger.

        When ``anchor`` (an independent external witness) is supplied, the
        computed head digest and entry count are compared against it; a mismatch
        marks the chain broken so a full-history rewrite is refused even when the
        attacker recalculated every ``prev_digest`` and ``digest`` value.
        """
        damaged: list[int] = []
        tampered: list[int] = []
        unreadable: list[int] = []
        chain_intact = self._anchor_conflict(anchor) is None
        expected_prev = GENESIS
        for entry in self._entries:
            if entry.prev_digest != expected_prev:
                chain_intact = False
            expected_prev = entry.digest
            readable = sum(
                1
                for i, digest in enumerate(entry.shard_digests)
                if _sha256(entry.shards[i]) == digest
            )
            if readable < self._k:
                unreadable.append(entry.seq)
                continue
            rebuilt = decode_shards(
                {
                    i: entry.shards[i]
                    for i, digest in enumerate(entry.shard_digests)
                    if _sha256(entry.shards[i]) == digest
                },
                k=self._k,
                n=self._n,
            )[: entry.length]
            if self._rebuilt_matches_seal(entry, rebuilt):
                if readable < self._n:
                    damaged.append(entry.seq)
            else:
                tampered.append(entry.seq)
        return LedgerIntegrityReport(
            entry_count=len(self._entries),
            chain_intact=chain_intact,
            damaged_seqs=tuple(damaged),
            tampered_seqs=tuple(tampered),
            unreadable_seqs=tuple(unreadable),
        )

    # --- healing --------------------------------------------------------------

    def heal(self, *, anchor: LedgerAnchor | None = None) -> HealingReport:
        """Repair every bit-rotted record that still has ``k`` readable shards.

        Tampered records are refused, never laundered: a record whose readable
        shards do not rebuild to the sealed predecessor-bound digest is left
        exactly as found. When ``anchor`` is supplied the final integrity check
        is made against the external witness, so healing can never silently bless
        a rewritten history.
        """
        healed: list[int] = []
        tampered: list[int] = []
        unreadable: list[int] = []
        for entry in self._entries:
            readable = {
                i: entry.shards[i]
                for i, digest in enumerate(entry.shard_digests)
                if _sha256(entry.shards[i]) == digest
            }
            if len(readable) < self._k:
                unreadable.append(entry.seq)
                continue
            rebuilt = decode_shards(readable, k=self._k, n=self._n)[: entry.length]
            if not self._rebuilt_matches_seal(entry, rebuilt):
                tampered.append(entry.seq)
                continue
            if len(readable) == self._n:
                continue  # already whole
            self._entries[entry.seq] = self._seal(
                seq=entry.seq, data=rebuilt, prev_digest=entry.prev_digest
            )
            healed.append(entry.seq)
        return HealingReport(
            healed_seqs=tuple(healed),
            tampered_seqs=tuple(tampered),
            unreadable_seqs=tuple(unreadable),
            intact_after=self.verify(anchor=anchor).intact,
        )

    # --- fault-injection seams (used by tests and the falsification wave) -----

    def inject_shard_fault(self, seq: int, shard_index: int, *, bit: int = 0) -> None:
        """Flip one byte of a stored shard to simulate bit-rot."""
        entry = self._entry(seq)
        if not 0 <= shard_index < self._n:
            raise LedgerError(f"shard index {shard_index} out of range")
        shards = list(entry.shards)
        shard = shards[shard_index]
        if bit < 0 or bit >= 8 * max(1, len(shard)):
            raise LedgerError(f"bit {bit} out of range for a {len(shard)}-byte shard")
        target = bit // 8
        mask = 1 << (bit % 8)
        mutated = bytearray(shard)
        mutated[target] ^= mask
        shards[shard_index] = bytes(mutated)
        self._entries[seq] = self._replace_shards(entry, tuple(shards))

    def inject_tamper(self, seq: int, new_payload: bytes | bytearray | str) -> None:
        """Rewrite a record's bytes consistently, but leave its chain digest.

        This models an adversary who can rewrite the stored shards and their
        shard digests but cannot forge the chain link: the healer must refuse,
        not repair, because the rebuilt payload no longer matches the seal.
        """
        entry = self._entry(seq)
        data = _payload_bytes(new_payload)
        if len(data) != entry.length:
            raise LedgerError("a tamper must preserve the record length")
        forged = encode_shards(data, k=self._k, n=self._n)
        self._entries[seq] = LedgerEntry(
            seq=entry.seq,
            length=entry.length,
            digest=entry.digest,
            prev_digest=entry.prev_digest,
            shards=forged,
            shard_digests=tuple(_sha256(s) for s in forged),
        )

    @staticmethod
    def _replace_shards(entry: LedgerEntry, shards: tuple[bytes, ...]) -> LedgerEntry:
        return LedgerEntry(
            seq=entry.seq,
            length=entry.length,
            digest=entry.digest,
            prev_digest=entry.prev_digest,
            shards=shards,
            shard_digests=entry.shard_digests,
        )

    # --- persistence ----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": LEDGER_SCHEMA,
            "k": self._k,
            "parity": self._parity,
            "entries": [e.to_dict() for e in self._entries],
        }

    def canonical_bytes(self) -> bytes:
        return jcs_canonical_bytes(self.to_dict())

    @property
    def head_digest(self) -> str:
        """The sealed head: a digest over the whole canonical ledger."""
        return _sha256(self.canonical_bytes())

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> LinearEvidenceLedger:
        if document.get("schema") != LEDGER_SCHEMA:
            raise LedgerError(f"unknown ledger schema {document.get('schema')!r}")
        ledger = cls(k=int(document["k"]), parity=int(document["parity"]))
        entries = [LedgerEntry.from_dict(e) for e in document["entries"]]
        for expected, entry in enumerate(entries):
            if entry.seq != expected:
                raise LedgerError(f"entry {entry.seq} is out of order (expected {expected})")
        ledger._entries = entries
        return ledger

    # --- on-disk persistence (JSONL log + external witness anchor) -------------

    def save_to_disk(self, log_path: str | Path, anchor_path: str | Path) -> LedgerAnchor:
        """Persist the ledger as an append-only JSONL log plus a witness anchor.

        The log is rewritten atomically (temp + fsync + ``os.replace``); the
        anchor is written to a *separate* file so it is an independent witness of
        the head. Returns the :class:`LedgerAnchor` written.
        """
        log = Path(log_path)
        lines = [json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)]
        _atomic_write(log, ("\n".join(lines) + "\n").encode("utf-8"))
        anchor = LedgerAnchor(seq_count=len(self._entries), head_digest=self.head_digest)
        _atomic_write(Path(anchor_path), anchor.canonical_bytes())
        return anchor

    @classmethod
    def load_from_disk(
        cls, log_path: str | Path, anchor_path: str | Path | None = None
    ) -> LinearEvidenceLedger:
        """Reload a ledger from its JSONL log and, if given, verify the anchor."""
        log = Path(log_path)
        if not log.is_file():
            raise LedgerError(f"no ledger log at {log}")
        try:
            document = json.loads(log.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise LedgerError(f"ledger log is not valid JSON: {exc}") from exc
        ledger = cls.from_dict(document)
        if anchor_path is not None:
            anchor = cls.load_anchor(anchor_path)
            conflict = ledger._anchor_conflict(anchor)
            if conflict is not None:
                raise LedgerError(conflict)
        return ledger

    @staticmethod
    def load_anchor(anchor_path: str | Path) -> LedgerAnchor:
        path = Path(anchor_path)
        if not path.is_file():
            raise LedgerError(f"no ledger anchor at {path}")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise LedgerError(f"ledger anchor is not valid JSON: {exc}") from exc
        return LedgerAnchor.from_dict(document)


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write ``payload`` to ``path`` atomically (temp file + fsync + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
