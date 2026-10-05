"""LocalCognition — default CognitionPort implementation."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from nexus_ai_agent.cognition.memory_policy import MemoryContextPolicy
from nexus_ai_agent.cognition.port import (
    CognitionError,
    CognitionRequest,
    CognitionResult,
)
from nexus_ai_agent.cognition.router import CognitionRoute, DeterministicRouter

GenerateFn = Callable[[str, str], Awaitable[str]]


class LocalCognition:
    """One authority: route → adapter → typed result.

    Does not execute tools, CommandBus, or filesystem operations.
    Provider absence and failures are fail-closed (CognitionError).
    """

    def __init__(
        self,
        router: DeterministicRouter,
        *,
        gemini_generate: GenerateFn | None = None,
        local_generate: GenerateFn | None = None,
        memory_policy: MemoryContextPolicy | None = None,
    ) -> None:
        self._router = router
        self._gemini = gemini_generate
        self._local = local_generate
        self._memory = memory_policy or MemoryContextPolicy()

    async def propose(self, request: CognitionRequest) -> CognitionResult:
        correlation_id = request.correlation_id or str(uuid.uuid4())

        if request.cancel_requested:
            raise CognitionError(
                "cancelled",
                "cognition cancelled before dispatch",
                correlation_id=correlation_id,
            )

        decision = self._router.decide(request)
        if decision.route is CognitionRoute.DENIED:
            raise CognitionError(
                "policy_denied",
                decision.reason,
                correlation_id=correlation_id,
            )

        if decision.route is CognitionRoute.FALLBACK:
            raise CognitionError(
                "provider_unavailable",
                decision.reason,
                correlation_id=correlation_id,
            )

        user_prompt = self._memory.merge_into_user_prompt(
            request.prompt, request.memory_fragments
        )
        system = request.system

        generate = self._select_generate(decision.route)
        if generate is None:
            raise CognitionError(
                "provider_unavailable",
                f"no adapter for route={decision.route.value}",
                correlation_id=correlation_id,
            )

        async def _call() -> str:
            return await generate(user_prompt, system)

        try:
            if request.timeout_s is not None and request.timeout_s > 0:
                text = await asyncio.wait_for(_call(), timeout=request.timeout_s)
            else:
                text = await _call()
        except TimeoutError as exc:
            raise CognitionError(
                "timeout",
                f"cognition exceeded timeout_s={request.timeout_s}",
                correlation_id=correlation_id,
            ) from exc
        except CognitionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CognitionError(
                "provider_error",
                str(exc),
                correlation_id=correlation_id,
            ) from exc

        return CognitionResult(
            text=text,
            correlation_id=correlation_id,
            provider_id=decision.provider_id,
            route_reason=decision.reason,
            degraded=False,
            usage={},
        )

    def _select_generate(self, route: CognitionRoute) -> GenerateFn | None:
        if route is CognitionRoute.GEMINI:
            return self._gemini
        if route is CognitionRoute.LOCAL:
            return self._local
        return None
