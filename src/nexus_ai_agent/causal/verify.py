"""Independent verification of a passport (the producer is never the judge).

``causal.passport`` *builds* projections; this module *judges* them, and it
judges them against the world, not against themselves:

1. **The report is intact.**  The passport carries its own content digest
   (``passport_id``); recomputing it detects a passport that was edited after
   it was issued, or one that was assembled inconsistently.
2. **The completeness claim is honest.**  A passport that says ``COMPLETE``
   must carry a measured artifact whose digest equals the artifact identity,
   must not carry a ``critical`` divergence, and must have walked a chain
   whose links all resolve.
3. **The bytes still agree.**  The artifact path recorded in the passport is
   re-measured *now*, by this module, and must hash to the passport's
   artifact identity.  This is the check that makes a passport more than a
   stored opinion: bytes that changed after the fact are caught here even if
   every stored record is internally consistent.

The verdict is a typed refusal when anything fails; nothing here repairs,
rewrites or annotates durable evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.causal.models import PassportRefused, Stage
from nexus_ai_agent.causal.passport import ArtifactPassport, default_measure

OK = "ok"
PASSPORT_EDITED = "passport_edited"
PASSPORT_INCOMPLETE = "passport_incomplete"
CHAIN_LINK_BROKEN = "chain_link_broken"
ARTIFACT_UNREADABLE = "artifact_unreadable"
ARTIFACT_DIGEST_MISMATCH = "artifact_digest_mismatch"


class PassportVerification(BaseModel):
    """Verdict of the independent re-check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    reason_code: str
    detail: str = ""
    passport_id: str
    checked_artifact_id: str
    measured_sha256: str | None = None
    measured_size_bytes: int | None = Field(default=None, ge=0)


def verify_passport(
    passport: ArtifactPassport,
    *,
    measure: Callable[[Path], tuple[str, int]] = default_measure,
) -> PassportVerification:
    """Re-check a passport against the artifact bytes and its own identity."""

    def verdict(
        reason: str, detail: str, measured: tuple[str, int] | None = None
    ) -> PassportVerification:
        return PassportVerification(
            ok=reason == OK,
            reason_code=reason,
            detail=detail,
            passport_id=passport.passport_id,
            checked_artifact_id=passport.artifact_id,
            measured_sha256=measured[0] if measured else None,
            measured_size_bytes=measured[1] if measured else None,
        )

    if passport.compute_passport_id() != passport.passport_id:
        return verdict(
            PASSPORT_EDITED,
            "the passport body was modified after it was issued (id recomputation differs)",
        )

    if passport.completeness != "COMPLETE":
        # Not a pass at any severity: an unsettled, partial or diverged
        # projection proves nothing about the artifact, and the verifier must
        # never upgrade a passport the builder refused to certify.
        return verdict(
            PASSPORT_INCOMPLETE,
            f"completeness={passport.completeness} "
            f"(divergences={[item.code for item in passport.divergences]})",
        )

    critical = [item for item in passport.divergences if item.severity == "critical"]
    if critical:
        return verdict(
            PASSPORT_INCOMPLETE,
            f"a COMPLETE passport carries critical divergences: {[i.code for i in critical]}",
        )
    if passport.measured_artifact is None:
        return verdict(
            PASSPORT_INCOMPLETE,
            "a COMPLETE passport carries no independent measurement of the artifact",
        )
    if passport.measured_artifact.sha256 != passport.artifact_id:
        return verdict(
            PASSPORT_INCOMPLETE,
            "the passport's measured digest does not equal its artifact identity",
        )

    for record in passport.chain:
        present = {item.subject.node_id for item in passport.chain}
        for parent in record.parents:
            if parent.stage is Stage.REQUEST:
                continue
            if parent.node_id not in present:
                return verdict(
                    CHAIN_LINK_BROKEN,
                    f"seq={record.seq} names unrecorded parent {parent.node_id}",
                )

    measured_source = passport.measured_artifact
    try:
        digest, size = measure(Path(measured_source.path))
    except OSError as exc:
        return verdict(ARTIFACT_UNREADABLE, f"cannot re-measure the artifact: {exc}")
    if digest != passport.artifact_id:
        return verdict(
            ARTIFACT_DIGEST_MISMATCH,
            f"bytes at {measured_source.path} hash to {digest}, not {passport.artifact_id}",
            (digest, size),
        )
    return verdict(OK, "passport and artifact bytes agree", (digest, size))


def require_complete(passport: ArtifactPassport) -> ArtifactPassport:
    """Passport-or-refuse: raise unless the projection is evidence-complete.

    Call sites that must not proceed on partial evidence (delivery gates,
    receipts, downstream compilation) use this instead of inspecting
    ``completeness`` by hand — a hand-rolled check is where an honest refusal
    usually turns into an optimistic one.
    """
    if passport.completeness != "COMPLETE":
        raise PassportRefused(
            "passport_not_complete",
            f"completeness={passport.completeness} "
            f"divergences={[item.code for item in passport.divergences]}",
        )
    return passport


__all__ = [
    "ARTIFACT_DIGEST_MISMATCH",
    "ARTIFACT_UNREADABLE",
    "CHAIN_LINK_BROKEN",
    "OK",
    "PASSPORT_EDITED",
    "PASSPORT_INCOMPLETE",
    "PassportVerification",
    "require_complete",
    "verify_passport",
]
