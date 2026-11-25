"""Verify durable plan approvals with the existing IBPC envelope authority.

This adapter verifies a MAC over the *entire* W4 approval binding. It neither
issues approvals nor authenticates a caller: a trusted composition root must
supply both the key provider and independently obtained envelopes, authenticate
the principal, and authorize project access. The IBPC issuer must only seal
bindings following an authorized human decision. MAC validity is not a human
decision and does not itself grant permission to perform an effect.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import datetime, timezone

from nexus_ai_agent.creative.studio.durable_dedupe import ApprovalBinding
from nexus_ai_agent.nagar.ibpc.approval_mac import ApprovalEnvelope, KeyProvider, verify_envelope


class IBPCPlanApprovalVerifier:
    """Adapter from trusted IBPC HMAC envelopes to the studio approval port.

    No request-controlled key material or identity is accepted here. The nonce
    consumption and same-plan replay checks remain the responsibility of the
    durable plan store, in the same transaction as reservation.
    """

    def __init__(
        self,
        provider: KeyProvider,
        envelopes: Mapping[str, ApprovalEnvelope],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._envelopes = dict(envelopes)
        self._now = now if now is not None else lambda: datetime.now(timezone.utc)

    def verify(self, binding: object) -> bool:
        if not isinstance(binding, ApprovalBinding):
            return False
        envelope = self._envelopes.get(binding.approval_id)
        if (
            not isinstance(envelope, ApprovalEnvelope)
            or envelope.approval_id != binding.approval_id
        ):
            return False
        try:
            now = self._now()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                return False
            # Require the envelope and binding to describe the same instants;
            # both fields are also included in the authenticated scope payload.
            issued = datetime.fromisoformat(envelope.issued_at.replace("Z", "+00:00"))
            expires = datetime.fromisoformat(envelope.expires_at.replace("Z", "+00:00"))
            if (
                issued.tzinfo is None
                or issued.utcoffset() is None
                or expires.tzinfo is None
                or expires.utcoffset() is None
                or issued != datetime.fromtimestamp(binding.issued_at, timezone.utc)
                or expires != datetime.fromtimestamp(binding.expires_at, timezone.utc)
                or not issued <= now < expires
            ):
                return False
            return verify_envelope(self._provider, envelope, asdict(binding), now=now.isoformat())
        except (ValueError, TypeError, OverflowError, OSError):
            return False
