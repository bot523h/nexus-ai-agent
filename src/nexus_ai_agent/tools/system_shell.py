"""Restricted shell tool: allowlisted commands sandboxed to the workspace.

Security model (Phase 0):
- Only commands on the ALLOWLIST may run.
- Commands execute with ``cwd`` set to the workspace root
  (``NEXUS_WORKSPACE_ROOT``, same default as ``tools/files.py``).
- Any argument that is (or resolves to) a path outside the workspace is
  rejected: absolute paths, ``..`` segments, and symlinks pointing out.
- ``find`` may not use execution/deletion flags (``-exec``, ``-execdir``,
  ``-delete``, ``-ok``, ``-okdir``).  Flags whose next argument is a file
  (``-fprintf``, ``-fprint``, ``-fprint0``, ``-fls``, ``-newer``) are only
  allowed with paths inside the workspace.  ``-printf`` takes a format
  string, not a path, so it is not path-checked (it cannot write files).
- ``grep``'s ``-f`` argument (pattern file) is path-checked; the first
  non-flag argument is the pattern and may be any regex text.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from pathlib import Path
from typing import ClassVar

from nexus_ai_agent.tools.base import BaseTool, RiskLevel
from nexus_ai_agent.tools.files import _workspace_root


class ShellTool(BaseTool):
    name = "shell"
    description = "Run a restricted shell command (allowlisted, workspace-only)."
    risk_level = RiskLevel.GUARDED

    ALLOWLIST: ClassVar[list[str]] = ["ls", "pwd", "echo", "cat", "grep", "find", "date"]

    # Commands whose non-flag arguments are file paths (relative to workspace).
    _PATH_COMMANDS: ClassVar[frozenset[str]] = frozenset({"ls", "cat", "find"})

    # find: flags that execute commands or delete files — always blocked.
    _FIND_BLOCKED_FLAGS: ClassVar[frozenset[str]] = frozenset(
        {"-exec", "-execdir", "-delete", "-ok", "-okdir"}
    )

    # find: flags whose next argument is a file path (containment-checked).
    _FIND_PATH_FLAGS: ClassVar[frozenset[str]] = frozenset(
        {"-fprintf", "-fprint", "-fprint0", "-fls", "-newer"}
    )

    # grep: flags whose next argument is a file path (containment-checked).
    _GREP_PATH_FLAGS: ClassVar[frozenset[str]] = frozenset({"-f"})

    def __init__(self, enable_shell: bool):
        if not enable_shell:
            self.risk_level = RiskLevel.BLOCKED

    # ── validation ──────────────────────────────────────────────────

    @staticmethod
    def _validate_path(root: Path, arg: str) -> None:
        """Reject absolute paths, ``..`` segments, and escapes outside *root*."""
        if arg.startswith("/"):
            raise ValueError(f"Absolute paths are not allowed: {arg}")
        if ".." in Path(arg).parts:
            raise ValueError(f"Path traversal is not allowed: {arg}")
        resolved = (root / arg).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"Path escapes the workspace: {arg}")

    def _validate_args(self, command: str, args: list[str]) -> None:
        root = _workspace_root()

        if command == "find":
            for arg in args:
                if arg in self._FIND_BLOCKED_FLAGS:
                    raise ValueError(f"Flag not allowed for find: {arg}")

        i = 0
        nonflag_index = 0  # for grep: 1st non-flag arg is the pattern, rest are paths
        while i < len(args):
            arg = args[i]
            if arg.startswith("-"):
                if command == "find" and arg in self._FIND_PATH_FLAGS:
                    if i + 1 >= len(args):
                        raise ValueError(f"Flag {arg} requires a path argument")
                    self._validate_path(root, args[i + 1])
                    i += 2
                elif command == "grep" and arg in self._GREP_PATH_FLAGS:
                    if i + 1 >= len(args):
                        raise ValueError(f"Flag {arg} requires a path argument")
                    self._validate_path(root, args[i + 1])
                    i += 2
                else:
                    i += 1
                continue

            if command in self._PATH_COMMANDS:
                self._validate_path(root, arg)
            elif command == "grep" and nonflag_index >= 1:
                self._validate_path(root, arg)
            nonflag_index += 1
            i += 1

    # ── execution ───────────────────────────────────────────────────

    async def execute(self, inputs: dict) -> dict:
        command = str(inputs.get("command", "")).strip()
        if not command:
            return {"success": False, "output": "", "error": "Missing command"}

        try:
            parts = shlex.split(command)
        except ValueError:
            return {"success": False, "output": "", "error": "Invalid command (bad quoting)"}
        if not parts:
            return {"success": False, "output": "", "error": "Invalid command"}

        if parts[0] not in self.ALLOWLIST:
            return {
                "success": False,
                "output": "",
                "error": f"Command not allowed: {parts[0]}",
            }

        try:
            self._validate_args(parts[0], parts[1:])
        except ValueError as exc:
            return {"success": False, "output": "", "error": str(exc)}

        root = _workspace_root()

        def _run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                parts,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                cwd=root,
            )

        try:
            proc = await asyncio.to_thread(_run)
            out = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode != 0:
                return {"success": False, "output": out, "error": f"Exit {proc.returncode}"}
            return {"success": True, "output": out, "error": None}
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "", "error": "Command timed out"}
        except OSError as exc:
            return {"success": False, "output": "", "error": f"Execution error: {exc}"}
