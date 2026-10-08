from __future__ import annotations

import os
from pathlib import Path

from nexus_ai_agent.tools.base import BaseTool, RiskLevel
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem


def _workspace_root() -> Path:
    """Resolve the legacy environment-configured workspace root.

    New composition roots should pass ``workspace_root`` to each tool. The
    environment fallback remains for direct callers and backwards-compatible
    CLI usage, but this module never mutates that process-global setting.
    """
    return Path(os.environ.get("NEXUS_WORKSPACE_ROOT", ".")).expanduser()


def _resolve_sandboxed(path: str, *, workspace_root: str | Path | None = None) -> Path:
    """Resolve a path through the same physical containment policy as I/O."""
    root = workspace_root if workspace_root is not None else _workspace_root()
    return WorkspaceFilesystem(root).resolve(path, allow_root=path in ("", "."))


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "Read a text file from the sandboxed workspace."
    risk_level = RiskLevel.SAFE

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self._workspace = WorkspaceFilesystem(workspace_root or _workspace_root())

    def bind_workspace(self, workspace_root: str | Path) -> None:
        self._workspace = WorkspaceFilesystem(workspace_root)

    async def execute(self, inputs: dict) -> dict:
        try:
            path = str(inputs.get("path", ""))
            output = self._workspace.read_text(path)
            return {"success": True, "output": output, "error": None}
        except Exception as exc:  # noqa: BLE001 - tool boundary returns typed failure
            return {"success": False, "output": "", "error": str(exc)}


class WriteFileTool(BaseTool):
    name = "write_file"
    description = "Write a text file into the sandboxed workspace."
    risk_level = RiskLevel.GUARDED

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self._workspace = WorkspaceFilesystem(workspace_root or _workspace_root())

    def bind_workspace(self, workspace_root: str | Path) -> None:
        self._workspace = WorkspaceFilesystem(workspace_root)

    async def execute(self, inputs: dict) -> dict:
        try:
            path = str(inputs.get("path", ""))
            content = str(inputs.get("content", ""))
            written = self._workspace.write_text(path, content)
            return {"success": True, "output": f"Wrote {written}", "error": None}
        except Exception as exc:  # noqa: BLE001 - tool boundary returns typed failure
            return {"success": False, "output": "", "error": str(exc)}


class ListDirTool(BaseTool):
    name = "list_dir"
    description = "List directory contents in the sandboxed workspace."
    risk_level = RiskLevel.SAFE

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self._workspace = WorkspaceFilesystem(workspace_root or _workspace_root())

    def bind_workspace(self, workspace_root: str | Path) -> None:
        self._workspace = WorkspaceFilesystem(workspace_root)

    async def execute(self, inputs: dict) -> dict:
        try:
            path = str(inputs.get("path", "")) or "."
            items = self._workspace.list_names(path)
            return {"success": True, "output": "\n".join(items), "error": None}
        except Exception as exc:  # noqa: BLE001 - tool boundary returns typed failure
            return {"success": False, "output": "", "error": str(exc)}
