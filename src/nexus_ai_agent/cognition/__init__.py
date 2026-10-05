"""Cognition boundary — one authoritative path from intent to model proposal.

Law (Gate B):
  AI proposes. Compiler/policy decides. Bus executes. Verifier judges.

Cognition never executes creative commands, never mutates project state,
and never elevates retrieved memory to system instructions without a
policy transform.
"""

from nexus_ai_agent.cognition.memory_policy import MemoryContextPolicy, UntrustedMemory
from nexus_ai_agent.cognition.port import (
    CognitionError,
    CognitionPort,
    CognitionRequest,
    CognitionResult,
    PrivacyClass,
    TaskClass,
)
from nexus_ai_agent.cognition.router import CognitionRoute, DeterministicRouter, RouteDecision
from nexus_ai_agent.cognition.service import LocalCognition

__all__ = [
    "CognitionError",
    "CognitionPort",
    "CognitionRequest",
    "CognitionResult",
    "CognitionRoute",
    "DeterministicRouter",
    "LocalCognition",
    "MemoryContextPolicy",
    "PrivacyClass",
    "RouteDecision",
    "TaskClass",
    "UntrustedMemory",
]
