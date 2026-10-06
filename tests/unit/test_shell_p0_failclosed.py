"""P0 shell fail-closed: unknown flags, path-flag forms, symlink/recursive escapes."""

from __future__ import annotations

import pytest

from nexus_ai_agent.tools.system_shell import ShellTool


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "ok.txt").write_text("hello\n", encoding="utf-8")
    return ShellTool(enable_shell=True, workspace_root=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "date -f /etc/passwd",
        "date --file=/etc/passwd",
        "date -r /etc/passwd",
        "date --reference=/etc/passwd",
        "grep -f/etc/passwd ok.txt",
        "grep --file=/etc/passwd ok.txt",
        "grep --exclude-from=/etc/passwd pattern ok.txt",
        "ls -L",
        "find -L .",
        "grep -R pattern .",
        "grep -r pattern .",
        "find . -exec rm {} ;",
        "cat /etc/passwd",
        "cat ../outside",
        "ls /",
        "date --unknown-flag",
        "grep --unknown-option pattern ok.txt",
    ],
)
async def test_shell_p0_rejects_escape_and_unknown(shell, command):
    result = await shell.execute({"command": command})
    assert result["success"] is False, command
    assert result["error"]


@pytest.mark.asyncio
async def test_shell_p0_allows_safe_workspace_ops(shell, tmp_path):
    ok = await shell.execute({"command": "cat ok.txt"})
    assert ok["success"] is True
    assert "hello" in (ok["output"] or "")

    ls = await shell.execute({"command": "ls -la"})
    assert ls["success"] is True

    # grep -f with workspace path
    patterns = tmp_path / "pat"
    patterns.write_text("hello\n", encoding="utf-8")
    g = await shell.execute({"command": "grep -f pat ok.txt"})
    assert g["success"] is True
