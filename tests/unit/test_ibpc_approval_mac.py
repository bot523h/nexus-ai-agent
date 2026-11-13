from __future__ import annotations

from dataclasses import replace

from nexus_ai_agent.connect.key_provider import StaticKeyRing
from nexus_ai_agent.nagar.ibpc.approval_mac import ApprovalEnvelope, issue_envelope, verify_envelope


def _issue(ring: StaticKeyRing, approval_id: str, payload: object) -> ApprovalEnvelope:
    return issue_envelope(
        ring,
        approval_id=approval_id,
        payload=payload,
        issued_at="2026-11-13T00:00:00Z",
        expires_at="2026-11-13T00:10:00Z",
    )


def test_approval_envelope_is_keyed_and_scope_bound() -> None:
    ring = StaticKeyRing({"test": b"k" * 32}, active_key_id="test")
    scope = {"proposal": "p-1", "revision": 2, "range": [100, 900]}
    envelope = _issue(ring, "a-1", scope)
    assert envelope.key_id == "test"
    assert envelope.scope_digest.startswith("sha256:")
    assert verify_envelope(ring, envelope, scope, now="2026-11-13T00:01:00Z")
    assert not verify_envelope(
        ring, envelope, {**scope, "range": [100, 901]}, now="2026-11-13T00:01:00Z"
    )
    assert not verify_envelope(
        ring, replace(envelope, approval_id="a-2"), scope, now="2026-11-13T00:01:00Z"
    )
    assert not verify_envelope(
        ring, replace(envelope, mac="0" * 64), scope, now="2026-11-13T00:01:00Z"
    )


def test_unknown_key_and_expiry_fail_closed() -> None:
    ring = StaticKeyRing({"known": b"k" * 32}, active_key_id="known")
    envelope = _issue(ring, "a-1", {"x": 1})
    assert not verify_envelope(
        ring, replace(envelope, key_id="unknown"), {"x": 1}, now="2026-11-13T00:01:00Z"
    )
    wrong_key = StaticKeyRing({"known": b"x" * 32}, active_key_id="known")
    assert not verify_envelope(wrong_key, envelope, {"x": 1}, now="2026-11-13T00:01:00Z")
    assert not verify_envelope(ring, envelope, {"x": 1}, now="2026-11-13T00:10:00Z")
