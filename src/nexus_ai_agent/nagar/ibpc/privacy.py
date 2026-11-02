"""Privacy / trust modes for live editing sessions.

Three modes, one default, explicit consent:

* ``LOCAL_VAULT`` — **default**; audio/transcript/artifacts never leave the
  process; records carry digests only, never raw transcript or audio bytes;
* ``TRUSTED_CLOUD`` / ``EXPERIMENTAL_CLOUD`` — selectable **only** with an
  explicit recorded :class:`ConsentRecord`; no cloud provider is wired in this
  slice, so any egress request under a cloud mode is a typed refusal and no
  user data leaves the vault.

Changing the mode is explicit and recorded; the mode is never silently cloud.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nexus_ai_agent.nagar.ibpc.canonical import digest_of
from nexus_ai_agent.nagar.ibpc.models import ConsentRecord, PrivacyMode

#: Fields that must never appear in a log/record as raw text — digests only.
SENSITIVE_FIELDS = frozenset({"transcript", "text", "audio", "audio_bytes", "prompt", "content"})


class PrivacyViolation(RuntimeError):
    """Typed refusal: an operation would move or expose protected data."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


def redact_mapping(record: dict[str, Any]) -> dict[str, Any]:
    """Return a log-safe copy: sensitive fields are replaced by their digest.

    A value under a sensitive key is never copied into the result — only
    ``sha256:<hex>`` of its canonical spelling (or of the raw bytes) appears.
    """
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key.lower() in SENSITIVE_FIELDS and value is not None:
            if isinstance(value, (bytes, bytearray)):
                from nexus_ai_agent.nagar.ibpc.canonical import sha256_bytes

                out[key] = sha256_bytes(bytes(value))
            else:
                out[key] = digest_of({"redacted": str(value)})
        else:
            out[key] = value
    return out


class PrivacyPolicy:
    """Mode + consent enforcement for one session."""

    DEFAULT_MODE = PrivacyMode.LOCAL_VAULT

    def __init__(self, *, now: Callable[[], str]) -> None:
        self._now = now
        self._consents: list[ConsentRecord] = []

    @property
    def consents(self) -> tuple[ConsentRecord, ...]:
        return tuple(self._consents)

    def require_local(self, mode: PrivacyMode, data_kind: str) -> None:
        """Local processing is always allowed; this is a no-op gate that
        documents the data-flow direction (nothing leaves)."""
        if not isinstance(mode, PrivacyMode):
            raise PrivacyViolation("invalid_mode", f"unknown privacy mode {mode!r}")

    def check_egress(self, mode: PrivacyMode, data_kind: str, *, consented: bool) -> None:
        """May ``data_kind`` leave the local vault under ``mode``?

        LOCAL_VAULT: egress is refused outright (there is nowhere to send).
        Cloud modes: refused unless an explicit consent record exists — and
        even then, no provider is wired in this slice, so the answer stays a
        typed refusal. User data is never sent to an unknown provider.
        """
        if mode is PrivacyMode.LOCAL_VAULT:
            raise PrivacyViolation("egress_refused", f"{data_kind} must not leave the local vault")
        if not consented:
            raise PrivacyViolation(
                "consent_missing",
                f"{mode.value} requires an explicit recorded consent for {data_kind}",
            )
        raise PrivacyViolation(
            "no_provider_wired",
            f"{mode.value} has no configured provider in this slice; {data_kind} stays local",
        )

    def record_consent(self, mode: PrivacyMode, *, principal_id: str, scope: str) -> ConsentRecord:
        """Record an explicit consent for a non-default mode (auditable)."""
        if not isinstance(mode, PrivacyMode):
            raise PrivacyViolation("invalid_mode", f"unknown privacy mode {mode!r}")
        record = ConsentRecord(
            mode=mode, principal_id=principal_id, scope=scope, granted_at=self._now()
        )
        self._consents.append(record)
        return record

    def has_consent(self, mode: PrivacyMode, *, principal_id: str, scope: str) -> bool:
        return any(
            c.mode is mode and c.principal_id == principal_id and scope in c.scope
            for c in self._consents
        )


__all__ = ["PrivacyPolicy", "PrivacyViolation", "SENSITIVE_FIELDS", "redact_mapping"]
