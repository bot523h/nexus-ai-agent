"""Thin Hatchet execution adapter (task-255).

Nexus hands a decided, already-fenced request to Hatchet for *workflow
execution* and receives back a raw witness. Hatchet gets workflow execution
capacity — retry, concurrency, run lifecycle, cancellation propagation — and
nothing else. This adapter owns no authority: it never commits an artifact,
validates a passport, mutates lineage, owns project state or mints an
idempotency key independent of Nexus.

Path (rules 6/7/10)::

    Nexus (fenced request)
      → Execution Fabric contract
      → this adapter            (run + witness; no commit)
      → Hatchet worker          (canonical repo handler)
      → per-attempt staging dir (never the final destination)
      → Nexus verifier          (independent re-measurement)
      → Nexus queue fenced CAS   (status AND attempt = fencing_token)
      → passport

Provider retries are metadata (``provider_attempt``), never a Nexus try
(rule 9). A result for a superseded execution is refused (rule 7).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

from nexus_ai_agent.integrations.execution.contract import (
    BackendKind,
    ExecutionAttempt,
    ExecutionFailure,
    ExecutionIdentity,
    ExecutionRequest,
    ExecutionResult,
    OutcomeState,
)
from nexus_ai_agent.integrations.execution.reconcile import (
    is_stale,
    state_from_provider,
)
from nexus_ai_agent.integrations.execution.staging import assert_staged, staging_dir_for
from nexus_ai_agent.observability.logging import get_logger

from ._sdk import new_client
from .worker_task import WORKFLOW_NAME

logger = get_logger(__name__)


class HatchetExecutionAdapter:
    """Provider backend that runs canonical handlers via Hatchet workflows."""

    kind = BackendKind.HATCHET.value

    def __init__(
        self,
        *,
        base_staging_dir: str,
        client: Any | None = None,
        result_timeout: float = 120.0,
    ) -> None:
        self._client = client
        self._base_staging_dir = base_staging_dir
        self._run_ids: dict[str, str] = {}
        self._result_timeout = result_timeout

    # -- client -----------------------------------------------------------------

    def _get_client(self) -> Any:  # SDK type is optional
        if self._client is None:
            self._client = new_client()
        return self._client

    # -- backend port -----------------------------------------------------------

    async def execute(self, request: ExecutionRequest) -> ExecutionResult | ExecutionFailure:
        """Trigger the Hatchet workflow and reconcile its result, fail-closed."""
        attempt_dir = staging_dir_for(self._base_staging_dir, request.identity, provider_attempt=1)
        run = await self._trigger(request, attempt_dir)
        provider_run_id = _run_id(run)
        if provider_run_id:
            self._run_ids[request.identity.try_id] = provider_run_id

        raw = await self._await_result(run)
        result = reconcile_provider_output(request.identity, raw, request)
        # Record the real provider run as *evidence* on the returned identity
        # (never as an authority key: identity_matches ignores it).
        if provider_run_id:
            recorded = result.identity.with_provider_run(provider_run_id)
            if isinstance(result, ExecutionResult | ExecutionFailure):
                result = replace(result, identity=recorded)
        return result

    async def cancel(self, identity: ExecutionIdentity) -> bool:
        """Best-effort propagation of a Nexus cancellation to Hatchet.

        Nexus remains the authority: a failed provider cancel does not clear a
        Nexus cancellation.
        """
        run_id = self._run_ids.get(identity.try_id)
        if not run_id:
            return False
        try:
            client = self._get_client()
            await client.runs.cancel(run_id)
            return True
        except Exception as exc:  # pragma: no cover - provider-dependent
            logger.warning("hatchet_cancel_failed", run_id=run_id, error=str(exc))
            return False

    # -- internals --------------------------------------------------------------

    async def _trigger(
        self, request: ExecutionRequest, attempt_dir: Path
    ) -> Any:
        client = self._get_client()
        try:
            workflow = client.workflow(name=WORKFLOW_NAME)
            return await workflow.aio_run_no_wait(
                {
                    "operation": request.operation,
                    "payload": dict(request.payload),
                    "staging_dir": str(attempt_dir),
                    "metadata": request.identity.to_metadata(),
                }
            )
        except Exception as exc:  # pragma: no cover - provider-dependent
            raise HatchetTriggerError(str(exc)) from exc

    async def _await_result(self, run: Any) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(run.aio_result(), timeout=self._result_timeout)
        except Exception as exc:  # pragma: no cover - provider-dependent
            logger.warning("hatchet_result_await_failed", error=str(exc))
            return None


class HatchetTriggerError(RuntimeError):
    """The provider refused to accept the run (network/provider failure)."""


def reconcile_provider_output(
    identity: ExecutionIdentity,
    raw: dict[str, Any] | None,
    request: ExecutionRequest,
) -> ExecutionResult | ExecutionFailure:
    """Map a Hatchet result to a safe Nexus verdict (fail-closed).

    Missing → ``UNKNOWN`` (retryable, no commit). Stale identity → refused.
    A staged path outside the request's staging dir → refused (rule 10).
    """
    if raw is None:
        return ExecutionFailure(
            identity=identity,
            code="provider_result_missing",
            detail="Hatchet returned no result for this run",
            retryable=True,
            state=OutcomeState.UNKNOWN,
        )
    if isinstance(raw, dict) and "status" in raw:
        state = state_from_provider(str(raw.get("status")))
        meta = dict(raw.get("witness", {}) or {})
    else:
        ok = bool(isinstance(raw, dict) and raw.get("ok"))
        state = OutcomeState.SUCCEEDED if ok else OutcomeState.FAILED
        meta = raw if isinstance(raw, dict) else {}
    result = ExecutionResult(
        identity=identity,
        attempt=_attempt(identity, meta),
        state=state,
        witness=meta,
        staged_artifact_path=meta.get("staged_artifact_path"),
        artifact_sha256=meta.get("artifact_sha256"),
    )
    if is_stale(identity, result):
        return ExecutionFailure(
            identity=identity,
            code="stale_provider_result",
            detail="provider result does not match the fenced execution",
            state=OutcomeState.UNKNOWN,
        )
    if result.staged_artifact_path:
        try:
            assert_staged(request, result.staged_artifact_path)
        except ValueError as exc:
            return ExecutionFailure(
                identity=identity,
                code="staged_path_escape",
                detail=str(exc),
                state=OutcomeState.UNKNOWN,
            )
    return result


def _run_id(run: Any) -> str | None:
    for attr in ("workflow_run_id", "run_id", "id"):
        value = getattr(run, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _attempt(identity: ExecutionIdentity, meta: dict[str, Any]) -> ExecutionAttempt:
    raw_attempt = meta.get("provider_attempt")
    try:
        n = int(raw_attempt) if raw_attempt is not None else 1
    except (TypeError, ValueError):
        n = 1
    return ExecutionAttempt(identity=identity, provider_attempt=max(1, n))
