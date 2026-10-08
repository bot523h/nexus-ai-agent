"""Fail-closed mapping from a provider state/result to a Nexus verdict.

Nothing here trusts the provider. Unknown states are ``UNKNOWN`` (never a
success and never a terminal failure); a result whose identity does not match
the one the caller is waiting for is a *stale* result and is refused; a missing
result is ``UNKNOWN`` and must be reconciled, never committed (rule 9).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contract import (
    ExecutionAttempt,
    ExecutionFailure,
    ExecutionIdentity,
    ExecutionResult,
    OutcomeState,
    identity_matches,
)

#: Provider status spellings → provider-neutral state. Anything absent is
#: ``UNKNOWN`` (fail closed) rather than an optimistic default.
_PROVIDER_STATE: dict[str, OutcomeState] = {
    "completed": OutcomeState.SUCCEEDED,
    "succeeded": OutcomeState.SUCCEEDED,
    "success": OutcomeState.SUCCEEDED,
    "failed": OutcomeState.FAILED,
    "failure": OutcomeState.FAILED,
    "cancelled": OutcomeState.CANCELLED,
    "canceled": OutcomeState.CANCELLED,
    "timed_out": OutcomeState.TIMED_OUT,
    "timeout": OutcomeState.TIMED_OUT,
}


def state_from_provider(raw: str | None) -> OutcomeState:
    """Map a provider status to a Nexus verdict (unknown → ``UNKNOWN``)."""
    if not raw:
        return OutcomeState.UNKNOWN
    return _PROVIDER_STATE.get(raw.strip().lower(), OutcomeState.UNKNOWN)


def is_stale(
    expected: ExecutionIdentity, result_or_failure: ExecutionResult | ExecutionFailure
) -> bool:
    """A result for a superseded execution may never commit (rule 7)."""
    return not identity_matches(expected, result_or_failure.identity)


def reconcile_result(
    expected: ExecutionIdentity,
    raw_result: Mapping[str, Any] | None,
) -> ExecutionResult | ExecutionFailure:
    """Turn a (possibly missing) provider result into a safe Nexus verdict.

    * missing / indeterminate → :class:`ExecutionFailure` with state ``UNKNOWN``
      (retryable, fail-closed, no commit);
    * a success claim → passed through as an ``ExecutionResult`` the verifier
      must still check;
    * anything that cannot be tied to ``expected`` → refused as stale.
    """
    if raw_result is None:
        return ExecutionFailure(
            identity=expected,
            code="provider_result_missing",
            detail="provider returned no result for this run",
            retryable=True,
            state=OutcomeState.UNKNOWN,
        )
    return ExecutionResult(
        identity=expected,
        attempt=_attempt_of(expected, raw_result),
        state=state_from_provider(str(raw_result.get("status", ""))),
        witness=raw_result.get("witness", {}) or {},
        staged_artifact_path=(
            str(raw_result["staged_artifact_path"])
            if raw_result.get("staged_artifact_path")
            else None
        ),
        artifact_sha256=(
            str(raw_result["artifact_sha256"]) if raw_result.get("artifact_sha256") else None
        ),
    )


def _attempt_of(identity: ExecutionIdentity, raw: Mapping[str, Any]) -> ExecutionAttempt:
    raw_attempt = raw.get("provider_attempt")
    try:
        n = int(raw_attempt) if raw_attempt is not None else 1
    except (TypeError, ValueError):
        n = 1
    return ExecutionAttempt(identity=identity, provider_attempt=max(1, n))
