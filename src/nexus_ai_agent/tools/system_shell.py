from __future__ import annotations

import asyncio
import shlex
import subprocess
from typing import ClassVar

from nexus_ai_agent.tools.base import BaseTool, RiskLevel


class ShellTool(BaseTool):
    name = "shell"
    description = "Run a restricted shell command (allowlisted)."
    risk_level = RiskLevel.GUARDED

    ALLOWLIST: ClassVar[list[str]] = ["ls", "pwd", "echo", "cat", "grep", "find", "date"]

    def __init__(self, enable_shell: bool):
        if not enable_shell:
            self.risk_level = RiskLevel.BLOCKED

    async def execute(self, inputs: dict) -> dict:
        command = str(inputs.get("command", "")).strip()
        if not command:
            return {"success": False, "output": "", "error": "Missing command"}

        parts = shlex.split(command)
        if not parts:
            return {"success": False, "output": "", "error": "Invalid command"}

        if parts[0] not in self.ALLOWLIST:
            return {
                "success": False,
                "output": "",
                "error": f"Command not allowed: {parts[0]}",
            }

        def _run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                parts,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

        try:
            proc = await asyncio.to_thread(_run)
            out = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode != 0:
                return {"success": False, "output": out, "error": f"Exit {proc.returncode}"}
            return {"success": True, "output": out, "error": None}
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "", "error": "Command timed out"}
