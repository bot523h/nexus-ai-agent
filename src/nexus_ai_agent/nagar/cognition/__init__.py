"""Nagar cognition boundary — the model-optional reasoning seam.

This package is deliberately **additive**: it does not execute anything, it
does not own state, and it carries no authority.  Its single job is to make
the *reasoning* step of Nagar replaceable and optional, so the deterministic
substrate (``creative.studio`` CommandBus → capability registry → policy →
execution → independent verification) can run with **no model at all**.

Contract (conceptually the mission's ``propose(context, schema, budget) ->
TypedProposal | Refusal``):

* a producer **proposes** — it never authorizes, never selects a privilege,
  never names a shell, never claims an identity;
* the proposal is **typed**, schema-versioned, provenance-carrying and
  explicitly refusable;
* the composition root (never the proposal) turns an accepted proposal into
  a canonical ``TypedCommand`` and the existing bus decides whether it may
  run.  ``nagar.cognition`` is upstream of authority, never a substitute
  for it.

Import direction: ``nagar.cognition`` may import ``creative.studio`` models
(one way only) so proposals can be bridged to the canonical command
envelope.  ``creative.studio`` never imports ``nagar``.
"""

from __future__ import annotations

from nexus_ai_agent.nagar.cognition.bridge import proposal_to_command
from nexus_ai_agent.nagar.cognition.context import (
    CognitionBudget,
    CognitionContext,
    ProducerIdentity,
    ProposalSchema,
)
from nexus_ai_agent.nagar.cognition.null import NullCognition
from nexus_ai_agent.nagar.cognition.port import CognitionPort
from nexus_ai_agent.nagar.cognition.proposal import (
    PROPOSAL_SCHEMA_VERSION,
    ProposalProvenance,
    Refusal,
    RefusalReason,
    TypedProposal,
    parse_proposal,
)
from nexus_ai_agent.nagar.cognition.router import (
    CognitionLevel,
    DeterministicRouter,
    IntentClass,
    RoutingDecision,
    RoutingRequest,
)

__all__ = [
    "PROPOSAL_SCHEMA_VERSION",
    "CognitionBudget",
    "CognitionContext",
    "CognitionLevel",
    "CognitionPort",
    "DeterministicRouter",
    "IntentClass",
    "NullCognition",
    "ProducerIdentity",
    "ProposalProvenance",
    "ProposalSchema",
    "Refusal",
    "RefusalReason",
    "RoutingDecision",
    "RoutingRequest",
    "TypedProposal",
    "parse_proposal",
    "proposal_to_command",
]
