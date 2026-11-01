"""Operator-actionable failure taxonomy for disaster-recovery operations.

A bare ``R2 upload failed: <boto traceback>`` answers none of the questions
an operator has at 03:17 UTC when the nightly job goes red: *what* failed,
*where*, *why*, *is it retryable*, and *what must I do*. This module
classifies storage-layer exceptions into all five, without importing
botocore at module import time (the taxonomy works on any injected client
error that carries S3-style fields, and degrades to message inspection —
never to silence).

Design rule (board/task-137): this is a *classification* layer used by the
maintenance lane. The provider itself is untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActionableFailure:
    """A storage/DR failure translated into an operator decision."""

    what: str  # the operation that failed (upload/download/list/delete)
    where: str  # the object/bucket it failed on
    why: str  # classified root cause
    # stable machine class: credentials/bucket/key/permission/network/
    # transient/integrity/config/unknown
    category: str
    retryable: bool  # may a re-run succeed with NO config change?
    operator_action: str  # the single most useful next step

    def render(self) -> str:
        return (
            f"{self.what} failed for {self.where}: {self.why} "
            f"[category={self.category}, retryable={str(self.retryable).lower()}]. "
            f"Operator action: {self.operator_action}"
        )


# S3/R2 error codes → (category, retryable, operator action).
_ERROR_CODE_MAP: dict[str, tuple[str, bool, str]] = {
    "InvalidAccessKeyId": (
        "credentials",
        False,
        "R2_ACCESS_KEY_ID is invalid — rotate or correct the API token in the repo/service secrets",
    ),
    "SignatureDoesNotMatch": (
        "credentials",
        False,
        "R2_SECRET_ACCESS_KEY does not match the access key — "
        "correct the API token pair in secrets",
    ),
    "TokenRefreshRequired": (
        "credentials",
        False,
        "the R2 API token expired or was revoked — issue a new token and update secrets",
    ),
    "AccessDenied": (
        "permission",
        False,
        "the API token lacks Object Read & Write on this bucket — "
        "grant the bucket-scoped permission",
    ),
    "NoSuchBucket": (
        "bucket",
        False,
        "R2_BUCKET names a bucket that does not exist under R2_ACCOUNT_ID — "
        "fix the bucket name or account",
    ),
    "NoSuchKey": (
        "key",
        False,
        "the requested object key does not exist — check the backup stamp/key "
        "(housekeeping may have pruned it)",
    ),
    "SlowDown": (
        "transient",
        True,
        "R2 rate-limited the request — re-run with backoff; no configuration change needed",
    ),
    "InternalError": (
        "transient",
        True,
        "R2 returned a 5xx — transient service error; retry the job",
    ),
    "ServiceUnavailable": (
        "transient",
        True,
        "R2 is temporarily unavailable — retry; escalate if the run keeps failing",
    ),
}

_NETWORK_MARKERS = (
    "endpointconnectionerror",
    "connecttimeout",
    "readtimeouterror",
    "connectionerror",
    "temporary failure in name resolution",
    "name or service not known",
    "econnrefused",
    "network is unreachable",
)


def _walk_causes(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _client_error_fields(chain: list[BaseException]) -> dict[str, Any] | None:
    """Best-effort extraction of botocore's ``ClientError.response['Error']``.

    Works for real botocore errors AND for test doubles that reproduce the
    field shape (``response = {'Error': {'Code': ..., 'Message': ...}}``),
    without importing botocore here.
    """
    for item in chain:
        response = getattr(item, "response", None)
        if isinstance(response, dict) and isinstance(response.get("Error"), dict):
            return dict(response["Error"])
    return None


def classify_storage_failure(
    *,
    operation: str,
    where: str,
    exc: BaseException,
    bucket: str | None = None,
) -> ActionableFailure:
    """Turn any storage-layer exception into an :class:`ActionableFailure`."""
    chain = _walk_causes(exc)
    error = _client_error_fields(chain)
    code = str(error.get("Code", "")) if error else ""
    remote_message = str(error.get("Message", "")) if error else ""

    if code in _ERROR_CODE_MAP:
        category, retryable, action = _ERROR_CODE_MAP[code]
        why = remote_message or f"R2 answered {code}"
        return ActionableFailure(
            what=operation,
            where=where,
            why=f"{code}: {why}",
            category=category,
            retryable=retryable,
            operator_action=action,
        )

    blob = " ".join(str(c) for c in chain).lower()
    if any(marker in blob for marker in _NETWORK_MARKERS):
        return ActionableFailure(
            what=operation,
            where=where,
            why="network/endpoint failure talking to the R2 endpoint — "
            "check R2_ACCOUNT_ID (the endpoint host is derived from it) and outbound connectivity",
            category="network",
            retryable=True,
            operator_action=(
                "verify R2_ACCOUNT_ID (the endpoint host is derived from it) and the "
                "network path, then re-run; a persistent failure with a wrong account id "
                "is a config error, not a network error"
            ),
        )

    return ActionableFailure(
        what=operation,
        where=where,
        why=f"{type(exc).__name__}: {exc}"[:300],
        category="unknown",
        retryable=True,
        operator_action=f"capture the full job log and classify manually — bucket={bucket!r}"
        if bucket
        else "capture the full job log and classify manually",
    )


class MaintenanceOperationError(RuntimeError):
    """An actionable maintenance failure (carries .actionable traceback-free text)."""

    def __init__(self, failure: ActionableFailure) -> None:
        self.failure = failure
        super().__init__(failure.render())


__all__ = ["ActionableFailure", "MaintenanceOperationError", "classify_storage_failure"]
