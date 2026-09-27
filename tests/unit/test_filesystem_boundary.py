"""Adversarial tests for the physical workspace boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.tools.files import ListDirTool, ReadFileTool, WriteFileTool
from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError, WorkspaceFilesystem
from nexus_ai_agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_tools_reject_symlink_escape_for_read_write_and_list(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (outside / "nested").mkdir()

    try:
        (workspace / "secret.txt").symlink_to(outside / "secret.txt")
        (workspace / "nested").symlink_to(outside / "nested", target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows without symlink privilege
        pytest.skip(f"symlinks unavailable: {exc}")

    read = await ReadFileTool(workspace).execute({"path": "secret.txt"})
    write = await WriteFileTool(workspace).execute(
        {"path": "nested/new.txt", "content": "must not escape"}
    )
    listed = await ListDirTool(workspace).execute({"path": "nested"})

    assert read["success"] is False
    assert write["success"] is False
    assert listed["success"] is False
    assert not (outside / "nested" / "new.txt").exists()


@pytest.mark.asyncio
async def test_registry_workspace_is_instance_local(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "value.txt").write_text("first", encoding="utf-8")
    (second / "value.txt").write_text("second", encoding="utf-8")

    # Constructing the second registry must not redirect the first registry.
    registry_a = ToolRegistry(workspace_root=first)
    registry_b = ToolRegistry(workspace_root=second)
    assert registry_a.workspace_root == first
    assert registry_b.workspace_root == second

    # Registration binds each tool to its own root; no process-global
    # environment is needed to make two compositions deterministic.
    tool_a = ReadFileTool(first)
    tool_b = ReadFileTool(first)
    registry_a.register(tool_a)
    registry_b.register(tool_b)
    a = await tool_a.execute({"path": "value.txt"})
    b = await tool_b.execute({"path": "value.txt"})
    assert a["output"] == "first"
    assert b["output"] == "second"


def test_policy_rejects_absolute_windows_and_traversal_spellings(tmp_path: Path) -> None:
    policy = WorkspaceFilesystem(tmp_path)
    for raw in ("../secret", "/etc/passwd", r"..\secret", r"C:\Windows\secret", r"\\server\share"):
        with pytest.raises(FilesystemBoundaryError):
            policy.resolve(raw)


def test_policy_does_not_follow_leaf_symlink_on_unlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("keep", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:  # pragma: no cover - Windows without symlink privilege
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(FilesystemBoundaryError):
        WorkspaceFilesystem(workspace).unlink("link.txt")
    assert outside.read_text(encoding="utf-8") == "keep"
    assert link.is_symlink()


def test_symlink_is_refused_even_when_its_target_is_inside_the_workspace(
    tmp_path: Path,
) -> None:
    """The boundary rejects *symlinks*, not merely symlinks that escape.

    A link pointing outside is already caught by the ``is_relative_to`` check on
    the resolved path, so that case proves nothing about the no-symlink rule.
    This test covers the case only the symlink-component check can catch: a link
    whose target is a perfectly ordinary file inside the same workspace.

    It is also load-bearing for TOCTOU: validation and use are two separate
    syscalls, so a path that involves a link can be re-pointed between them.  A
    boundary that only rejects escaping links is a boundary that can be moved.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")
    link = workspace / "inner_link"
    try:
        link.symlink_to("notes.txt")
    except OSError as exc:  # pragma: no cover - Windows without symlink privilege
        pytest.skip(f"symlinks unavailable: {exc}")

    boundary = WorkspaceFilesystem(workspace)
    with pytest.raises(FilesystemBoundaryError):
        boundary.read_text("inner_link")
    assert boundary.read_text("notes.txt") == "hello"
