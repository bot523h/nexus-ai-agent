from __future__ import annotations

import os
from pathlib import Path

from nexus_ai_agent.tools.base import BaseTool, RiskLevel


def _workspace_root() -> Path:
    # Default to current working directory; can be overridden for deployments/tests.
    return Path(os.environ.get("NEXUS_WORKSPACE_ROOT", ".")).resolve()


def _resolve_sandboxed(path: str) -> Path:
    if path.startswith("/") or ".." in Path(path).parts:
        raise ValueError("Path must be relative and must not contain '..'")
    root = _workspace_root()
    return (root / path).resolve()


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "Read a text file from the sandboxed workspace."
    risk_level = RiskLevel.SAFE

    async def execute(self, inputs: dict) -> dict:
        try:
            p = _resolve_sandboxed(str(inputs.get("path", "")))
            if not p.exists() or not p.is_file():
                return {"success": False, "output": "", "error": "File not found"}
            return {"success": True, "output": p.read_text(encoding="utf-8"), "error": None}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "output": "", "error": str(e)}


class WriteFileTool(BaseTool):
    name = "write_file"
    description = "Write a text file into the sandboxed workspace."
    risk_level = RiskLevel.GUARDED

    async def execute(self, inputs: dict) -> dict:
        try:
            p = _resolve_sandboxed(str(inputs.get("path", "")))
            content = str(inputs.get("content", ""))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return {"success": True, "output": f"Wrote {p}", "error": None}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "output": "", "error": str(e)}


class ListDirTool(BaseTool):
    name = "list_dir"
    description = "List directory contents in the sandboxed workspace."
    risk_level = RiskLevel.SAFE

    async def execute(self, inputs: dict) -> dict:
        try:
            p = _resolve_sandboxed(str(inputs.get("path", "")) or ".")
            if not p.exists() or not p.is_dir():
                return {"success": False, "output": "", "error": "Directory not found"}
            items = sorted([child.name for child in p.iterdir()])
            return {"success": True, "output": "\n".join(items), "error": None}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "output": "", "error": str(e)}

