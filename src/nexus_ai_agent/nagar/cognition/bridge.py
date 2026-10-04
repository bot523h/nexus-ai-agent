"""Bridge: a proposal becomes a *candidate* canonical command — nothing more.

This is the one place where the cognition layer touches the execution layer,
and it does so in the safest possible direction: it **builds a
``TypedCommand``** from a proposal, which then has to pass the *entire*
``CommandBus`` pipeline (envelope schema, operation schema, actor/project
authorization, capability version, pack lifecycle, execution policy,
reference resolution, idempotency, revision, atomic apply).

Key properties:

* the bridge is **pure** — no I/O, no dispatch, no side effects;
* the bridge **never invents authority**: the actor, project, permissions
  and idempotency key are supplied by the *caller* (the composition root),
  not by the proposal.  A proposal cannot choose who it runs as;
* the bridge produces ``schema_version=2`` commands by default (explicit
  actor/target/provenance), so a proposal-derived command is authorized by
  the same trusted authorizer as any other command;
* the bridge is **optional**: a caller may ignore it entirely and hand-build
  a command.  It exists so the wiring is one obvious, testable seam.

Refusals and malformed proposals never reach here: callers pass a
``TypedProposal``, which only :func:`parse_proposal` can produce.
"""

from __future__ import annotations

from uuid import uuid4

from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    CommandProvenance,
    RequestContext,
    TargetRef,
    TypedCommand,
)
from nexus_ai_agent.nagar.cognition.proposal import TypedProposal


def proposal_to_command(
    proposal: TypedProposal,
    *,
    actor: ActorIdentity,
    project_id: str,
    operation_schema_version: int,
    idempotency_key: str | None = None,
    reason: str | None = None,
) -> TypedCommand:
    """Build a candidate ``nagar.command.v1`` command from a proposal.

    The proposal supplies only *what* to do (``operation`` + ``input``).  The
    caller supplies *who* and *where* (``actor``, ``project_id``).  The
    command is schema 2, so the bus will demand a trusted authorizer that
    binds that actor to that project — a proposal can never grant itself
    access by naming an actor.
    """
    return TypedCommand(
        schema_version=2,
        operation_schema_version=operation_schema_version,
        command_id=f"cmd_{uuid4().hex}",
        actor=actor,
        operation=proposal.operation,
        target=TargetRef(project_id=project_id),
        input=dict(proposal.input),
        provenance=CommandProvenance(
            source="agent",
            source_id=proposal.provenance.producer.name,
            reason=(reason or proposal.rationale)[:500] or None,
        ),
        request_context=RequestContext(channel="ai"),
        idempotency_key=idempotency_key,
    )


__all__ = ["proposal_to_command"]
