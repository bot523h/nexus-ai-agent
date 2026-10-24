"""Fail-closed boot: verify the ledger, then anchor/check the charter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nexus_ai_agent.selfhood.charter import CHARTER, Charter
from nexus_ai_agent.selfhood.ledger import SelfEntryKind, SelfLedger


class SelfIntegrityError(RuntimeError):
    """The Self-Ledger failed verification; the system must not start."""


class CharterDriftError(RuntimeError):
    """The running charter differs from the anchored one without an amendment."""


@dataclass(frozen=True)
class SelfState:
    ledger: SelfLedger
    charter_digest: str


def _anchored_digest(ledger: SelfLedger) -> str | None:
    digest = None
    for record in ledger.records():
        if record.kind is SelfEntryKind.CHARTER_ADOPTED:
            digest = str(record.payload["detail"]["digest"])
    return digest


def boot_self(
    db_path: Path | str,
    *,
    charter: Charter = CHARTER,
    expected_head: str | None = None,
) -> SelfState:
    ledger = SelfLedger(db_path)
    verdict = ledger.verify(expected_head=expected_head)
    if not verdict.ok:
        raise SelfIntegrityError(
            f"self-ledger failed verification at seq={verdict.first_broken_seq}: {verdict.reason}"
        )
    anchored = _anchored_digest(ledger)
    digest = charter.digest()
    if anchored is None:
        ledger.append(
            SelfEntryKind.CHARTER_ADOPTED,
            subject="charter",
            detail={"digest": digest, "version": charter.version, "genesis": True},
        )
    elif anchored != digest:
        raise CharterDriftError(
            f"charter digest {digest} != anchored {anchored}; "
            "a human-approved amendment is required"
        )
    return SelfState(ledger=ledger, charter_digest=digest)


__all__ = ["CharterDriftError", "SelfIntegrityError", "SelfState", "boot_self"]
