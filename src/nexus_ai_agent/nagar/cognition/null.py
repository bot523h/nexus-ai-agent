"""NullCognition — the honest no-model provider.

This is the foundational **Model Kill Test** in code: with no model
available, Nagar must stay alive, keep its deterministic paths intact, and
answer an explicit :class:`Refusal` — never a fabricated proposal, never a
crash.  It always refuses, with a stable reason code, and never invents an
operation or an input.

NullCognition is not a stub that "will be filled in": it is a legitimate,
permanent provider for deployments that want deterministic-only operation
and for the test that proves the substrate does not secretly depend on a
model.
"""

from __future__ import annotations

import time

from nexus_ai_agent.nagar.cognition.context import (
    CognitionBudget,
    CognitionContext,
    ProducerIdentity,
    ProposalSchema,
)
from nexus_ai_agent.nagar.cognition.proposal import (
    ProposalProvenance,
    Refusal,
    RefusalReason,
    TypedProposal,
)

#: The producer identity every NullCognition result carries.
NULL_PRODUCER_NAME = "nagar.null-cognition"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class NullCognition:
    """A provider that proposes nothing and refuses everything, honestly."""

    def __init__(self, *, reason_detail: str = "no cognition provider is configured") -> None:
        self._detail = reason_detail

    def _provenance(self) -> ProposalProvenance:
        return ProposalProvenance(
            producer=ProducerIdentity(kind="service", name=NULL_PRODUCER_NAME),
            created_at=_utc_now(),
            source="null",
        )

    async def propose(
        self,
        context: CognitionContext,
        schema: ProposalSchema,
        budget: CognitionBudget,
    ) -> TypedProposal | Refusal:
        # NullCognition does no work, so it can never exceed its budget; the
        # signature is honoured so it is a drop-in for any other provider.
        _ = (context, schema, budget)
        return Refusal(
            reason=RefusalReason.PRODUCER_REFUSED,
            detail=self._detail,
            provenance=self._provenance(),
        )


__all__ = ["NULL_PRODUCER_NAME", "NullCognition"]
