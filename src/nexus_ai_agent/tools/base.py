from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum


class RiskLevel(str, Enum):
    SAFE = "safe"
    GUARDED = "guarded"
    BLOCKED = "blocked"


class BaseTool(ABC):
    name: str
    description: str
    risk_level: RiskLevel

    @abstractmethod
    async def execute(self, inputs: dict) -> dict:
        """
        Execute tool with structured inputs.

        Return schema:
          {"success": bool, "output": str, "error": str|None}
        """

        raise NotImplementedError

