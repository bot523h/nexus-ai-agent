from __future__ import annotations

import os
from typing import Any

from nexus_ai_agent.tools.base import BaseTool, RiskLevel


class ToolRegistry:
    def __init__(self, enable_shell: bool = False, workspace_root: str | None = None):
        self.enable_shell = enable_shell
        self._tools: dict[str, BaseTool] = {}
        self.workspace_root = workspace_root

        # Backwards-compatible convenience: allow the CLI/runtime to configure the
        # sandbox root via ToolRegistry, since file tools resolve via env var.
        if workspace_root:
            os.environ["NEXUS_WORKSPACE_ROOT"] = str(workspace_root)

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    async def run(self, name: str, inputs: dict, policy: dict[str, Any] | None = None) -> dict:
        policy = policy or {}
        tool = self.get(name)
        if tool is None:
            return {"success": False, "output": "", "error": f"Tool not found: {name}"}

        if tool.risk_level == RiskLevel.BLOCKED:
            return {"success": False, "output": "", "error": "Tool is disabled"}

        if tool.risk_level == RiskLevel.GUARDED and policy.get("confirmed") is not True:
            return {"needs_confirmation": True, "tool": name, "inputs": inputs}

        try:
            result = await tool.execute(inputs)
            return result
        except Exception as e:  # noqa: BLE001
            return {"success": False, "output": "", "error": str(e)}
