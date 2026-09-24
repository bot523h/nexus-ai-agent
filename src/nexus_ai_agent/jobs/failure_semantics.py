"""Failure classification for the canonical job lifecycle (task-181, Gate 5).

One axis, one principle:

    A failure is **RETRYABLE** iff the world (environment, dependency,
    resource, transient damage) can change to make the identical request
    succeed.  It is **TERMINAL** iff the identical request must fail again
    unless the request, the credentials, or the code changes.

The classifier is total over the three ways a job can fail:

* :func:`classify_typed_code` — the repository's typed user-failure dialect
  (``{"success": False, "error_code": <code>}``); the vocabulary is the union
  of the creative (``creative/render_jobs.py::ERROR_CODES``) and slideshow
  (``creative/slideshow/worker_adapter.py::ERROR_CODES``) closed sets;
* :func:`classify_exception` — an exception escaping the handler (worker
  crash / fail-closed conversion), matched most-specific-first and duck-typed
  on ``exc.code`` so this module stays import-light (no creative imports);
* :func:`classify_verification_reason` — the independent verifier's typed
  ``reason_code`` (and the queue's publish/re-probe failure codes).

``failure_status`` maps the class to the durable state
(``FAILED_RETRYABLE`` / ``FAILED_TERMINAL``).  There is **no retry
scheduler** in this repository (``docs/architecture/JOB_LIFECYCLE.md`` §6):
``FAILED_RETRYABLE`` records retry-*eligibility*; it is terminal for the
queue as implemented and the ``failed_retryable → pending`` edge stays
outside the transition matrix (fail-closed) until a scheduler exists.
"""

from __future__ import annotations

import errno
from enum import Enum

from nexus_ai_agent.application.ports.job_queue import JobStatus


class FailureClass(str, Enum):
    """Retryability classification — orthogonal to the user-facing code."""

    RETRYABLE = "retryable"
    TERMINAL = "terminal"


def failure_status(failure: FailureClass) -> JobStatus:
    """The durable status a classified failure must persist as."""
    if failure is FailureClass.RETRYABLE:
        return JobStatus.FAILED_RETRYABLE
    return JobStatus.FAILED_TERMINAL


#: Typed user-failure codes → class.  Rationale is per-code, on the record:
#:
#: ================  ========  =====================================================
#: code              class     why
#: ================  ========  =====================================================
#: invalid_request   TERMINAL  the payload must change; a retry repeats the refusal
#: media_missing     TERMINAL  the referenced input must change (request-level)
#: unsupported       TERMINAL  the capability set must change (a deploy, not a retry)
#: render_failed     TERMINAL  the render of THESE inputs failed; same inputs, same
#:                             binary ⇒ same failure (change the request)
#: unusable_image    TERMINAL  the input media must change
#: caption_profile   RETRYABLE installing the caption engine makes the identical job
#:                             succeed (dependency unavailable)
#: ffmpeg_unavailable RETRYABLE installing FFmpeg makes the identical job succeed
#: image_generation  RETRYABLE generator/service failures are environment-side and
#:                             transient often enough to justify one bounded retry
#: internal          RETRYABLE the environment-side catch-all (OSError family);
#:                             conservative bounded retry
#: (unknown code)    TERMINAL  a typed code without a classification row is contract
#:                             drift: fail closed toward visibility, never silent
#:                             retry-spam
#: ================  ========  =====================================================
TYPED_CODE_CLASSES: dict[str, FailureClass] = {
    "invalid_request": FailureClass.TERMINAL,
    "media_missing": FailureClass.TERMINAL,
    "unsupported_operation": FailureClass.TERMINAL,
    "render_failed": FailureClass.TERMINAL,
    "unusable_image": FailureClass.TERMINAL,
    "caption_profile_unavailable": FailureClass.RETRYABLE,
    "ffmpeg_unavailable": FailureClass.RETRYABLE,
    "image_generation_failed": FailureClass.RETRYABLE,
    "internal": FailureClass.RETRYABLE,
}

#: Independent-verification ``reason_code``s (``jobs/verification.py`` plus the
#: queue's own publish/re-probe codes) → class.
#:
#: Artifact *measurement disagreements* (missing/empty/sha/size/duration/probe)
#: are RETRYABLE: execution may have been transiently damaged (staging, disk,
#: encoder glitch) and one clean re-execution re-verifies from scratch
#: (verification is read-only and idempotent).  Deterministic handler bugs
#: (no claim, malformed sha, mis-routed publication) are TERMINAL: the
#: identical retry repeats the lie.  ``verifier_crashed`` is infrastructure
#: (the artifact was never judged) — RETRYABLE.
VERIFICATION_REASON_CLASSES: dict[str, FailureClass] = {
    "missing_artifact": FailureClass.RETRYABLE,
    "empty_artifact": FailureClass.RETRYABLE,
    "sha256_mismatch": FailureClass.RETRYABLE,
    "size_mismatch": FailureClass.RETRYABLE,
    "duration_mismatch": FailureClass.RETRYABLE,
    "probe_failed": FailureClass.RETRYABLE,
    "verifier_crashed": FailureClass.RETRYABLE,
    "publish_failed": FailureClass.RETRYABLE,
    "reprobe_failed": FailureClass.RETRYABLE,
    "success_without_artifact_claim": FailureClass.TERMINAL,
    "invalid_sha256_claim": FailureClass.TERMINAL,
    "outside_expected_root": FailureClass.TERMINAL,
    "unexpected_artifact_path": FailureClass.TERMINAL,
    "typed_user_failure": FailureClass.TERMINAL,
}

#: Transient OS errors — the "temporary IO" row of the classification table.
_TRANSIENT_ERRNOS: frozenset[int] = frozenset(
    {
        errno.EAGAIN,
        errno.EWOULDBLOCK,
        errno.EBUSY,
        errno.ETIMEDOUT,
        errno.EINTR,
        errno.ENOSPC,
        getattr(errno, "ETIME", errno.ETIMEDOUT),
    }
)

_PERMISSION_ERRNOS: frozenset[int] = frozenset({errno.EACCES, errno.EPERM})


def classify_typed_code(code: str) -> FailureClass:
    """Classify a typed user-failure code (unknown ⇒ TERMINAL, fail-closed)."""
    return TYPED_CODE_CLASSES.get(code, FailureClass.TERMINAL)


def classify_verification_reason(reason_code: str) -> FailureClass:
    """Classify a verification/publish refusal code (unknown ⇒ TERMINAL)."""
    return VERIFICATION_REASON_CLASSES.get(reason_code, FailureClass.TERMINAL)


def classify_exception(exc: BaseException) -> FailureClass:
    """Classify an exception escaping a job handler (most specific first).

    Order matters: permission errors before the OSError family, typed
    (``exc.code``) dialects before the generic value-error family.  Anything
    unrecognised is an unexpected worker crash ⇒ RETRYABLE (conservative:
    cause unknown and potentially transient; a deterministic crash
    re-classifies identically on the next attempt and stays visible).
    """
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return classify_typed_code(code)
    if isinstance(exc, PermissionError):
        return FailureClass.TERMINAL
    if isinstance(exc, OSError):
        number = getattr(exc, "errno", None)
        if number in _PERMISSION_ERRNOS:
            return FailureClass.TERMINAL
        if number in _TRANSIENT_ERRNOS:
            return FailureClass.RETRYABLE
        if isinstance(exc, FileNotFoundError):
            return FailureClass.TERMINAL
        return FailureClass.RETRYABLE
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return FailureClass.RETRYABLE
    if isinstance(exc, NotImplementedError):
        return FailureClass.TERMINAL
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return FailureClass.TERMINAL
    return FailureClass.RETRYABLE


def is_typed_user_failure(result: dict[str, object]) -> bool:
    """True for the repository's typed user-failure dialect.

    The canonical dialect is exactly ``{"success": False, "error_code":
    <str>, ...}``.  A result in this shape is a **failure of the job**
    (task-181, GAP-A): it must reach a failure status — never ``COMPLETED`` —
    whatever any verifier would answer about it.

    Gate-5 repair (NEW LAW 1 explicit item): a bare ``{"success": False}``
    with a missing/empty ``error_code`` is *also* a failure (the handler
    announced failure; nothing may turn it into success).  It classifies as
    ``typed_failure:untyped_failure`` — an unknown code is TERMINAL
    (fail-closed) — instead of falling through to verification/completion.
    """
    return result.get("success") is False


TYPED_FAILURE_ERROR_PREFIX = "typed_failure:"


def typed_failure_error(code: str) -> str:
    """Persisted ``error`` spelling for a typed user failure."""
    return f"{TYPED_FAILURE_ERROR_PREFIX}{code}"


def parse_typed_failure_error(error: str | None) -> str | None:
    """Inverse of :func:`typed_failure_error`; ``None`` if not that shape."""
    if error and error.startswith(TYPED_FAILURE_ERROR_PREFIX):
        return error[len(TYPED_FAILURE_ERROR_PREFIX) :]
    return None


__all__ = [
    "TYPED_CODE_CLASSES",
    "TYPED_FAILURE_ERROR_PREFIX",
    "VERIFICATION_REASON_CLASSES",
    "FailureClass",
    "classify_exception",
    "classify_typed_code",
    "classify_verification_reason",
    "failure_status",
    "is_typed_user_failure",
    "parse_typed_failure_error",
    "typed_failure_error",
]
