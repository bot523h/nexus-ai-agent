from __future__ import annotations

from pathlib import Path
from typing import Any

from nexus_ai_agent.tools.base import BaseTool, RiskLevel


class ToolRegistry:
    def __init__(self, enable_shell: bool = False, workspace_root: str | Path | None = None):
        self.enable_shell = enable_shell
        self._tools: dict[str, BaseTool] = {}
        # Configuration is instance-owned.  Do not mutate NEXUS_WORKSPACE_ROOT
        # here: two registries in one process must not silently redirect each
        # other's filesystem operations.
        self.workspace_root = Path(workspace_root).expanduser() if workspace_root else None

    def register(self, tool: BaseTool) -> None:
        if self.workspace_root is not None:
            bind_workspace = getattr(tool, "bind_workspace", None)
            if callable(bind_workspace):
                bind_workspace(self.workspace_root)
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
