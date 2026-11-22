"""Shared bounded plan and project identity validation."""

from __future__ import annotations


def validate_plan_identity(value: str, *, name: str) -> None:
    """Require a nonblank identity with a bounded UTF-8 representation.

    The identity is not normalized: whitespace can be significant to the
    caller, but whitespace-only and oversized values are rejected.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value.encode("utf-8")) > 256:
        raise ValueError(f"{name} must contain at most 256 UTF-8 bytes")
