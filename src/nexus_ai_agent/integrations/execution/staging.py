"""A staged, per-attempt provider workspace (rule 10).

The provider may only ever write inside the staging directory handed to it; the
*final* artifact destination, the verifier and the fenced commit live in Nexus
and are never reachable from here. The staging directory name embeds both the
Nexus try identity and the provider attempt, so concurrent provider attempts
cannot collide.
"""

from __future__ import annotations

from pathlib import Path

from .contract import ExecutionIdentity, ExecutionRequest

STAGING_ROOT_NAME = "nexus_execution_staging"


def staging_dir_for(
    base_dir: str | Path, identity: ExecutionIdentity, provider_attempt: int
) -> Path:
    """Create (idempotently) and return the per-attempt staging directory."""
    if provider_attempt < 1:
        raise ValueError("provider_attempt must be >= 1")
    root = Path(base_dir) / STAGING_ROOT_NAME
    safe = _slug(identity.try_id)
    attempt_dir = root / f"{safe}.p{provider_attempt}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    return attempt_dir


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value)


def assert_staged(request: ExecutionRequest, candidate: str | Path) -> Path:
    """Fail closed unless ``candidate`` is inside the request's staging dir.

    This is the provider-side guard: even a provider that tries to report a
    path outside its own staging area is refused before anything is read or
    published by Nexus.
    """
    staging = Path(request.staging_dir).resolve()
    resolved = Path(candidate).resolve()
    try:
        resolved.relative_to(staging)
    except ValueError as exc:  # not inside staging
        raise ValueError(
            f"provider reported an artifact outside its staging dir: {resolved} !∈ {staging}"
        ) from exc
    return resolved
