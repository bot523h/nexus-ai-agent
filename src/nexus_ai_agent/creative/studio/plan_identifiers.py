"""Shared bounded plan and project identity validation."""

from __future__ import annotations

import hashlib
import json
import re


def validate_plan_identity(value: str, *, name: str) -> None:
    """Require a nonblank identity with a bounded UTF-8 representation.

    The identity is not normalized: whitespace can be significant to the
    caller, but whitespace-only and oversized values are rejected.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value.encode("utf-8")) > 256:
        raise ValueError(f"{name} must contain at most 256 UTF-8 bytes")


def derive_effect_key(
    *,
    project_id: str,
    plan_id: str,
    node_id: str,
    idempotency_key: str,
    fingerprint: str,
) -> str:
    """Derive a versioned key for a particular authorized plan-node effect.

    All components are required: the key is only an identifier, not proof of
    authorization, execution, destination-side deduplication or a receipt.
    The fingerprint is the caller's canonical SHA-256 command fingerprint.
    """
    for name, value in (
        ("project_id", project_id),
        ("plan_id", plan_id),
        ("node_id", node_id),
        ("idempotency_key", idempotency_key),
    ):
        validate_plan_identity(value, name=name)
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError("fingerprint must be lowercase SHA-256 hex")
    encoded = json.dumps(
        ["nexus.w4.effect.v1", project_id, plan_id, node_id, idempotency_key, fingerprint],
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()
