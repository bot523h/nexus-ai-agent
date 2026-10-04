"""The cognition port — one boundary, three replaceable providers.

``CognitionPort.propose`` returns a :class:`TypedProposal` or a
:class:`Refusal`.  The port contract is deliberately small and strict:

* **typed** — the return is one of two Pydantic models, never ``Any``;
* **schema-versioned** — the caller passes a :class:`ProposalSchema` and the
  result is validated against it;
* **provenance-carrying** — every result names its producer;
* **explicitly refusable** — an honest "I cannot" is a first-class answer;
* **bounded** — the caller passes a :class:`CognitionBudget`; the port
  enforces the wall-clock bound by refusing, and never retries unboundedly;
* **authority-free** — a proposal cannot carry a grant (enforced in
  :mod:`nexus_ai_agent.nagar.cognition.proposal`).

Provider-specific logic lives behind the port: :class:`NullCognition` (no
model), and — in a deployment — local/cloud adapters that wrap an existing
``LLMProvider``.  Nothing above the port knows which is installed.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from nexus_ai_agent.nagar.cognition.context import (
    CognitionBudget,
    CognitionContext,
    ProposalSchema,
)
from nexus_ai_agent.nagar.cognition.proposal import Refusal, TypedProposal


@runtime_checkable
class CognitionPort(Protocol):
    """The canonical, model-optional reasoning boundary."""

    async def propose(
        self,
        context: CognitionContext,
        schema: ProposalSchema,
        budget: CognitionBudget,
    ) -> TypedProposal | Refusal: ...


__all__ = ["CognitionPort"]
