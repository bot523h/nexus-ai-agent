"""Typed, machine-readable LLM provider failures (LAW 10 / Wave D).

Provider failures must be classified by *type and status*, never by searching
free-form model text or exception messages for keywords such as ``"429"``. A
model answer that merely mentions HTTP 429 is a **success**; only a structured,
typed failure may trigger degradation, retry, or propagation.

``LLMError`` is the single typed channel. ``kind`` is one of:

``RATE_LIMIT``
    Provider throttled the caller (HTTP 429 / quota). Retryable → degrade.
``TIMEOUT``
    The provider did not answer in time. Retryable → degrade.
``UNAVAILABLE``
    Provider/transport unavailable (5xx, connection reset). Retryable → degrade.
``INVALID_RESPONSE``
    Provider returned something unusable (schema/empty). **Not** retryable:
    fail closed so a bad response never masquerades as an answer.
``UNKNOWN``
    Unclassified typed failure. **Not** retryable (fail closed).

Only :data:`RETRYABLE_KINDS` justify transparently degrading to a fallback
provider; everything else must propagate.
"""

from __future__ import annotations

# Retryable failure kinds — the only ones that justify degrading to a fallback.
RETRYABLE_KINDS: frozenset[str] = frozenset({"RATE_LIMIT", "TIMEOUT", "UNAVAILABLE"})


class LLMError(RuntimeError):
    """A typed LLM provider failure carrying a machine-readable ``kind``.

    Subclasses :class:`RuntimeError` so existing ``except RuntimeError`` sites
    keep working; classification is by :attr:`kind`/:attr:`status`, not text.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "UNKNOWN",
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status

    @property
    def retryable(self) -> bool:
        """Whether degrading to a fallback provider is the correct response."""
        return self.kind in RETRYABLE_KINDS


def is_retryable(exc: BaseException) -> bool:
    """True only for a typed :class:`LLMError` whose ``kind`` is retryable."""
    return isinstance(exc, LLMError) and exc.retryable
