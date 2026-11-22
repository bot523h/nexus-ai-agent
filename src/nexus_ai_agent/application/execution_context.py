"""Scoped execution context for queue-minted fencing tokens.

The queue mints an :class:`ExecutionClaim` (the ``(job_id, attempt)``
fencing token of exactly one execution) when it reserves a job, and
installs it in a :class:`contextvars.ContextVar` for the duration of the
handler invocation.  The claim propagates through ``asyncio.to_thread``,
so worker code (renderers, verifiers, side-effect fences) can learn which
execution they are acting for without threading the token through every
signature.

The installed claim is *evidence of the minted token, not proof of current
ownership*: durable side effects still require the queue's fenced
ownership check (``attempt == claim.attempt`` in the expected source
state).  Outside a handler the context is empty — ``None`` — and nothing
may be inferred from it.
"""

from __future__ import annotations

from contextvars import ContextVar

from nexus_ai_agent.jobs.lifecycle import ExecutionClaim

__all__ = [
    "ExecutionClaim",
    "current_execution_claim",
    "get_current_execution_claim",
]

#: The claim of the handler invocation currently running in this context.
#: ``None`` outside a queue-driven handler (the default).
_current_claim: ContextVar[ExecutionClaim | None] = ContextVar(
    "queue_execution_claim", default=None
)


def current_execution_claim() -> ExecutionClaim | None:
    """Return the queue-minted claim in a running handler, or ``None`` outside it."""
    return _current_claim.get()


#: Backwards/forwards-compatible alias for :func:`current_execution_claim`.
get_current_execution_claim = current_execution_claim
