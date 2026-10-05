"""Deterministic, policy-driven cognition router — not an LLM."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from nexus_ai_agent.cognition.port import CognitionRequest, PrivacyClass, TaskClass


class CognitionRoute(str, Enum):
    GEMINI = "gemini"
    LOCAL = "local"
    FALLBACK = "fallback"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: CognitionRoute
    reason: str
    provider_id: str


class DeterministicRouter:
    """Maps typed request + availability → provider route.

    Intent text is never used for authority. Privacy and availability win.
    """

    def __init__(
        self,
        *,
        gemini_available: bool = False,
        local_available: bool = False,
        allow_cloud: bool = True,
    ) -> None:
        self._gemini_available = gemini_available
        self._local_available = local_available
        self._allow_cloud = allow_cloud

    def decide(self, request: CognitionRequest) -> RouteDecision:
        if request.cancel_requested:
            return RouteDecision(
                CognitionRoute.DENIED,
                "cancel_requested",
                provider_id="none",
            )

        if request.privacy is PrivacyClass.STRICT_LOCAL:
            if self._local_available:
                return RouteDecision(
                    CognitionRoute.LOCAL,
                    "privacy=strict_local",
                    provider_id="local",
                )
            return RouteDecision(
                CognitionRoute.DENIED,
                "strict_local_but_no_local_provider",
                provider_id="none",
            )

        if not self._allow_cloud:
            if self._local_available:
                return RouteDecision(
                    CognitionRoute.LOCAL,
                    "cloud_disabled",
                    provider_id="local",
                )
            return RouteDecision(
                CognitionRoute.DENIED,
                "cloud_disabled_and_no_local",
                provider_id="none",
            )

        # Cloud path: prefer Gemini when configured; else local; else fallback.
        if self._gemini_available and request.task_class is not TaskClass.EMBED:
            return RouteDecision(
                CognitionRoute.GEMINI,
                f"task={request.task_class.value};gemini_available",
                provider_id="gemini",
            )

        if self._local_available:
            return RouteDecision(
                CognitionRoute.LOCAL,
                "gemini_unavailable;local",
                provider_id="local",
            )

        return RouteDecision(
            CognitionRoute.FALLBACK,
            "no_primary_provider",
            provider_id="fallback",
        )
