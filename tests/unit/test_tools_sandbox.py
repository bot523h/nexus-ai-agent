from __future__ import annotations

import pytest

from nexus_ai_agent.tools.files import ReadFileTool, WriteFileTool
from nexus_ai_agent.tools.registry import ToolRegistry
from nexus_ai_agent.tools.system_shell import ShellTool


@pytest.mark.asyncio
async def test_read_file_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_WORKSPACE_ROOT", str(tmp_path))
    tool = ReadFileTool()
    result = await tool.execute({"path": "../../../etc/passwd"})
    assert result["success"] is False
    assert result["error"]


@pytest.mark.asyncio
async def test_write_file_sandboxed(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_WORKSPACE_ROOT", str(tmp_path))
    tool = WriteFileTool()
    result = await tool.execute({"path": "notes/hello.txt", "content": "hi"})
    assert result["success"] is True
    assert (tmp_path / "notes" / "hello.txt").exists()


@pytest.mark.asyncio
async def test_shell_blocked_by_default():
    registry = ToolRegistry(enable_shell=False)
    registry.register(ShellTool(enable_shell=False))
    result = await registry.run("shell", {"command": "ls"})
    assert result["success"] is False
    assert "disabled" in (result["error"] or "").lower()


@pytest.mark.asyncio
async def test_shell_allowlist():
    registry = ToolRegistry(enable_shell=True)
    registry.register(ShellTool(enable_shell=True))
    result = await registry.run("shell", {"command": "rm -rf /"}, policy={"confirmed": True})
    assert result["success"] is False
    assert "not allowed" in (result["error"] or "").lower()
