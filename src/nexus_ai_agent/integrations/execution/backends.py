"""Provider-neutral execution backend port + the always-available in-process one.

The fabric asks a backend to *execute* an already-decided request and return a
raw witness. A backend never verifies, never publishes to a final destination
and never touches Nexus authority. The in-process backend is the honest
reference implementation (it runs the canonical worker handler directly); a
provider backend (Hatchet) is a thin adapter over the same port.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from .contract import (
    ExecutionAttempt,
    ExecutionIdentity,
    ExecutionRequest,
    ExecutionResult,
    OutcomeState,
)

#: A canonical Nexus job handler, e.g. ``creative.render_jobs.creative_render_job``.
JobHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ExecutionBackend(Protocol):
    """Hand a decided request to an execution provider; return a raw witness."""

    kind: str

    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...

    async def cancel(self, identity: ExecutionIdentity) -> bool: ...


def _sha256_of(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class InProcessBackend:
    """Reference backend: run the canonical handler in this process.

    No provider retries, no network. It writes nothing outside the request's
    ``staging_dir`` (rule 10) and returns a witness the Nexus verifier checks.
    """

    kind = "in_process"

    def __init__(self, handler: JobHandler) -> None:
        self._handler = handler

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        payload = dict(request.payload)
        witness = await self._handler(payload)
        ok = bool(witness.get("success"))
        staged = witness.get("artifact_path")
        sha = witness.get("sha256")
        if ok and staged and not sha:
            sha = _sha256_of(Path(str(staged)))
        return ExecutionResult(
            identity=request.identity,
            attempt=ExecutionAttempt(identity=request.identity, provider_attempt=1),
            state=OutcomeState.SUCCEEDED if ok else OutcomeState.FAILED,
            witness=witness,
            staged_artifact_path=str(staged) if staged else None,
            artifact_sha256=str(sha) if sha else None,
        )

    async def cancel(self, identity: ExecutionIdentity) -> bool:
        # In-process work is not interruptible across an await boundary here.
        return False
