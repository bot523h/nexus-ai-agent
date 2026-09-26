"""Adversarial tests for the shell tool's curated option policy.

Every test in this file corresponds to an exploit that was demonstrated to
run *through* the pre-fix validator on main (2026-09-26):

* ``date -f FILE`` / ``date --file=FILE`` read arbitrary files outside the
  workspace and echoed their contents through the stderr channel — the
  validator only path-checked arguments of ``ls``/``cat``/``find``/``grep``.
* ``grep -fFILE`` (attached), ``grep --file=FILE`` (long) and bundled
  ``grep -if FILE`` consumed an unvalidated pattern file.
* ``grep -R`` / ``find -L`` followed a symlinked directory *below* an
  already-validated argument, leaking outside-workspace content/names.

The policy is fail-closed: if any of these regress, this file goes RED.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus_ai_agent.tools.system_shell import ShellTool


@pytest.fixture()
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Workspace + a secret file OUTSIDE it + a planted symlink inside."""
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP_SECRET=1\n", encoding="utf-8")
    (workspace / "notes.txt").write_text("hello world\nTOP_SECRET guess-list\n", encoding="utf-8")
    try:
        os.symlink(outside, workspace / "linkdir")
    except OSError as exc:  # pragma: no cover - Windows without symlink privilege
        pytest.skip(f"symlinks unavailable: {exc}")
    monkeypatch.setenv("NEXUS_WORKSPACE_ROOT", str(workspace))
    return workspace, outside


# ── date: file-consuming options must be unreachable ────────────────────


@pytest.mark.asyncio
async def test_date_f_option_leaks_arbitrary_file_pre_fix(ws) -> None:
    """Pre-fix this printed ``date: invalid date 'TOP_SECRET=1'``."""
    _, outside = ws
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": f"date -f {outside}/secret.txt"})
    assert result["success"] is False
    assert "not allowed" in (result["error"] or "")
    assert "TOP_SECRET=1" not in result["output"]


@pytest.mark.asyncio
async def test_date_long_file_option_rejected(ws) -> None:
    _, outside = ws
    tool = ShellTool(enable_shell=True)
    for spelling in (f"--file={outside}/secret.txt", "--file", f"-f{outside}/secret.txt"):
        command = (
            f"date {spelling}"
            if "=" in spelling or spelling.startswith("-f/")
            else f"date {spelling} {outside}/secret.txt"
        )
        result = await tool.execute({"command": command})
        assert result["success"] is False, command
        assert "TOP_SECRET=1" not in result["output"]


@pytest.mark.asyncio
async def test_date_reference_and_set_are_rejected(ws) -> None:
    _, outside = ws
    tool = ShellTool(enable_shell=True)
    for command in (
        f"date -r {outside}/secret.txt",
        f"date --reference={outside}/secret.txt",
        f"date --reference {outside}/secret.txt",
        "date -s @1700000000",
        f"date -ur {outside}/secret.txt",  # bundled short options
    ):
        result = await tool.execute({"command": command})
        assert result["success"] is False, command
        assert "not allowed" in (result["error"] or ""), command


@pytest.mark.asyncio
async def test_date_benign_forms_still_work(ws) -> None:
    tool = ShellTool(enable_shell=True)
    ok = await tool.execute({"command": "date -u -d @1700000000"})
    assert ok["success"] is True
    assert "2023" in ok["output"]
    control = await tool.execute({"command": "date +%Y"})
    assert control["success"] is True


@pytest.mark.asyncio
async def test_date_rejects_time_setting_operand(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "date 092612302026"})
    assert result["success"] is False
    assert "+FORMAT" in (result["error"] or "")


# ── grep: every spelling of a file-consuming option is containment-checked ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_template",
    [
        "grep -f{secret} notes.txt",  # attached short form
        "grep -if {secret} notes.txt",  # bundled short options
        "grep --file={secret} notes.txt",  # long = form
        "grep --file {secret} notes.txt",  # long separate form
        "grep --exclude-from={secret} notes.txt",
        "grep -e hello -f {secret} notes.txt",  # after -e: all operands are paths
    ],
)
async def test_grep_file_options_cannot_read_outside(ws, command_template) -> None:
    _, outside = ws
    tool = ShellTool(enable_shell=True)
    result = await tool.execute(
        {"command": command_template.format(secret=f"{outside}/secret.txt")}
    )
    assert result["success"] is False, command_template
    assert "TOP_SECRET=1" not in result["output"]


@pytest.mark.asyncio
async def test_grep_pattern_file_inside_workspace_still_works(ws, tmp_path: Path) -> None:
    workspace, _ = ws
    (workspace / "patterns.txt").write_text("hello\n", encoding="utf-8")
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "grep -f patterns.txt notes.txt"})
    assert result["success"] is True
    assert "hello" in result["output"]


# ── symlink-dereferencing recursion below a validated argument ──────────


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["-R", "--dereference-recursive", "--follow"])
async def test_grep_dereference_recursion_rejected(ws, flag) -> None:
    """Pre-fix ``grep -R TOP_SECRET .`` printed the outside file's content."""
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": f"grep {flag} TOP_SECRET ."})
    assert result["success"] is False
    assert "not allowed" in (result["error"] or "")
    assert "TOP_SECRET=1" not in result["output"]


@pytest.mark.asyncio
async def test_grep_lowercase_recursive_does_not_follow_planted_symlink(ws) -> None:
    """``grep -r`` must keep working AND must not cross the planted symlink."""
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "grep -r TOP_SECRET ."})
    # notes.txt matches its own top_secret guess-list line; the outside file
    # behind linkdir/ must NOT contribute any ./linkdir/ matches.
    assert "linkdir" not in result["output"]
    assert "TOP_SECRET=1" not in result["output"]


@pytest.mark.asyncio
async def test_find_L_and_follow_predicates_rejected(ws) -> None:
    """Pre-fix ``find -L . -type f`` enumerated the outside directory."""
    tool = ShellTool(enable_shell=True)
    for command in ("find -L . -type f", "find -H . -type f", "find . -follow -type f"):
        result = await tool.execute({"command": command})
        assert result["success"] is False, command
        assert "not allowed" in (result["error"] or ""), command
        assert "secret.txt" not in result["output"]


@pytest.mark.asyncio
async def test_find_default_does_not_enumerate_through_symlink(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "find . -type f"})
    assert result["success"] is True
    assert "secret.txt" not in result["output"]


@pytest.mark.asyncio
async def test_ls_dereference_option_rejected(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "ls -L ."})
    assert result["success"] is False
    assert "not allowed" in (result["error"] or "")


# ── fail-closed posture and positive controls ───────────────────────────


@pytest.mark.asyncio
async def test_unknown_options_fail_closed_for_every_command(ws) -> None:
    tool = ShellTool(enable_shell=True)
    for command in (
        "ls --frobnicate",
        "cat --frobnicate",
        "grep --frobnicate x notes.txt",
        "echo --frobnicate hi",
        "grep -Q TOP_SECRET notes.txt",  # -Q is not a grep option / not in the policy
        "date --frobnicate",
    ):
        result = await tool.execute({"command": command})
        assert result["success"] is False, command
        assert "not allowed" in (result["error"] or ""), command


@pytest.mark.asyncio
async def test_gnu_prefix_abbreviation_cannot_reach_blocked_long_option(ws) -> None:
    """A GNU unique-prefix abbreviation (--fi for --file) must not sneak a
    path past the policy: unknown long options are refused outright."""
    _, outside = ws
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": f"grep --fi={outside}/secret.txt notes.txt"})
    assert result["success"] is False
    assert "not allowed" in (result["error"] or "")
    assert "TOP_SECRET=1" not in result["output"]


@pytest.mark.asyncio
async def test_bundled_mixed_valid_options_still_work(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "grep -in TOP_SECRET notes.txt"})
    assert result["success"] is True
    assert "2:" in result["output"] or "TOP_SECRET guess-list" in result["output"]


@pytest.mark.asyncio
async def test_nul_byte_argument_is_a_typed_failure_not_a_crash(ws) -> None:
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "echo hello\x00world"})
    assert result["success"] is False
    assert result["error"]


@pytest.mark.asyncio
async def test_output_is_capped_for_huge_files(ws) -> None:
    workspace, _ = ws
    (workspace / "big.txt").write_text("x" * (2 * 1024 * 1024), encoding="utf-8")
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": "cat big.txt"})
    assert result["success"] is True
    assert len(result["output"]) <= 200_100
    assert result["output"].endswith("[output truncated]")
