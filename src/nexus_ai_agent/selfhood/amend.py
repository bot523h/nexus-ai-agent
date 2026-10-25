"""The human-only amendment door for the charter.

Deliberately imported by nothing under ``src/`` (architecture-tested): it is
invoked by a human-operated script or test, never by an agent or learner.
"""

from __future__ import annotations

from nexus_ai_agent.selfhood.charter import Charter
from nexus_ai_agent.selfhood.ledger import SelfEntryKind, SelfLedger, SelfRecord


def adopt_amendment(
    ledger: SelfLedger, charter: Charter, *, approved_by: str, reason: str
) -> SelfRecord:
    if not approved_by.strip():
        raise ValueError("an amendment needs a named human approver")
    if not reason.strip():
        raise ValueError("an amendment needs a reason")
    verdict = ledger.verify()
    if not verdict.ok:
        raise ValueError(f"refusing to amend an unverifiable ledger: {verdict.reason}")
    return ledger.append(
        SelfEntryKind.CHARTER_ADOPTED,
        subject="charter",
        detail={
            "digest": charter.digest(),
            "version": charter.version,
            "approved_by": approved_by,
            "reason": reason,
        },
    )


__all__ = ["adopt_amendment"]
