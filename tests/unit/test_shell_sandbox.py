"""Workspace-sandbox tests for ShellTool (Phase 0).

The tool must never read or write outside the workspace root
(``NEXUS_WORKSPACE_ROOT``), for any allowlisted command.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.tools.system_shell import ShellTool


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """Workspace with a couple of files, plus a secret file outside it."""
    (tmp_path.parent / "outside_secret.env").write_text("TOP_SECRET=1")
    (tmp_path / "notes.txt").write_text("hello world")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.txt").write_text("inner")
    monkeypatch.setenv("NEXUS_WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_ls_lists_workspace(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "ls"})
    assert result["success"] is True
    assert "notes.txt" in result["output"]


@pytest.mark.asyncio
async def test_cat_reads_inside_workspace(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "cat notes.txt"})
    assert result["success"] is True
    assert "hello world" in result["output"]


@pytest.mark.asyncio
async def test_cat_rejects_outside_workspace(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "cat ../outside_secret.env"})
    assert result["success"] is False
    assert result["error"]
    assert "TOP_SECRET" not in result["output"]


@pytest.mark.asyncio
async def test_cat_rejects_absolute_path(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "cat /etc/passwd"})
    assert result["success"] is False
    assert result["error"]
    assert "root:" not in result["output"]


@pytest.mark.asyncio
async def test_ls_rejects_outside_workspace(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "ls /etc"})
    assert result["success"] is False
    assert result["error"]


@pytest.mark.asyncio
async def test_grep_reads_inside_workspace(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "grep hello notes.txt"})
    assert result["success"] is True
    assert "hello" in result["output"]


@pytest.mark.asyncio
async def test_grep_rejects_outside_path(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "grep TOP_SECRET ../outside_secret.env"})
    assert result["success"] is False
    assert result["error"]
    assert "TOP_SECRET" not in result["output"]


@pytest.mark.asyncio
async def test_grep_rejects_absolute_pattern_file(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "grep -f /etc/passwd notes.txt"})
    assert result["success"] is False
    assert result["error"]


@pytest.mark.asyncio
async def test_find_blocked_flags(ws) -> None:
    tool = ShellTool(enable_shell=True)
    for flag in ["-exec", "-execdir", "-delete", "-ok", "-okdir"]:
        result = await tool.execute({"command": f"find . {flag} echo hi"})
        assert result["success"] is False, flag
        assert "not allowed" in (result["error"] or "")


@pytest.mark.asyncio
async def test_find_fprintf_outside_blocked(ws) -> None:
    tool = ShellTool(enable_shell=True)
    for command in ['find . -fprintf /tmp/evil.txt "%p"', 'find . -fprintf ../evil.txt "%p"']:
        result = await tool.execute({"command": command})
        assert result["success"] is False, command
        assert result["error"]


@pytest.mark.asyncio
async def test_find_fprintf_inside_workspace_allowed(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": 'find . -fprintf out.txt "%p\\n"'})
    assert result["success"] is True
    assert (ws / "out.txt").exists()


@pytest.mark.asyncio
async def test_find_basic_search_still_works(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": 'find . -name "notes.txt"'})
    assert result["success"] is True
    assert "notes.txt" in result["output"]


@pytest.mark.asyncio
async def test_echo_without_paths_still_works(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": 'echo "hello world"'})
    assert result["success"] is True
    assert "hello world" in result["output"]
