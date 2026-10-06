"""Restricted shell tool: allowlisted commands sandboxed to the workspace.

Security model (P0 fail-closed):
- Only commands on the ALLOWLIST may run.
- Commands execute with ``cwd`` set to the workspace root.
- Any path argument outside the workspace is rejected (absolute, ``..``, symlink out).
- **Unknown flags are refused** (deny-by-default per command).
- Path-taking flags support both ``-f path`` and attached ``-fPATH`` / ``--file=PATH``.
- Symlink-follow and recursive-read flags that expand reach outside the cwd model are blocked.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from pathlib import Path
from typing import ClassVar

from nexus_ai_agent.tools.base import BaseTool, RiskLevel
from nexus_ai_agent.tools.files import _workspace_root
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem


def _flag_base(arg: str) -> str:
    """Return the option name without a short attached value or long ``=value``."""
    if arg.startswith("--"):
        return arg.split("=", 1)[0]
    if arg.startswith("-") and len(arg) > 2 and not arg.startswith("--"):
        # -fFILE / -rFILE (single-letter short option with attached operand)
        return arg[:2]
    return arg


def _attached_value(arg: str) -> str | None:
    if arg.startswith("--") and "=" in arg:
        return arg.split("=", 1)[1]
    if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
        return arg[2:]
    return None


class ShellTool(BaseTool):
    name = "shell"
    description = "Run a restricted shell command (allowlisted, workspace-only)."
    risk_level = RiskLevel.GUARDED

    ALLOWLIST: ClassVar[list[str]] = ["ls", "pwd", "echo", "cat", "grep", "find", "date"]

    _PATH_COMMANDS: ClassVar[frozenset[str]] = frozenset({"ls", "cat", "find"})

    # Per-command allowed flag bases (deny unknown).
    _ALLOWED_FLAGS: ClassVar[dict[str, frozenset[str]]] = {
        "ls": frozenset(
            {
                "-a",
                "-A",
                "-l",
                "-1",
                "-h",
                "-t",
                "-r",
                "-S",
                "-d",
                "--all",
                "--almost-all",
                "--human-readable",
                "--reverse",
            }
        ),
        "pwd": frozenset(),
        "echo": frozenset({"-n", "-e", "-E"}),
        "cat": frozenset({"-n", "-b", "-s", "--number", "--squeeze-blank"}),
        "grep": frozenset(
            {
                "-i",
                "-n",
                "-v",
                "-w",
                "-x",
                "-c",
                "-l",
                "-h",
                "-o",
                "-E",
                "-F",
                "-e",
                "-f",
                "--ignore-case",
                "--line-number",
                "--invert-match",
                "--word-regexp",
                "--count",
                "--files-with-matches",
                "--extended-regexp",
                "--fixed-strings",
                "--file",
            }
        ),
        "find": frozenset(
            {
                "-name",
                "-iname",
                "-type",
                "-maxdepth",
                "-mindepth",
                "-path",
                "-ipath",
                "-mtime",
                "-size",
                "-print",
                "-print0",
            }
        ),
        "date": frozenset({"-u", "-I", "--iso-8601", "--utc", "--universal"}),
    }

    _FIND_BLOCKED_FLAGS: ClassVar[frozenset[str]] = frozenset(
        {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-L", "-H", "-P", "-follow"}
    )

    # Flags that must never appear (symlink follow / recursive filesystem walk).
    _GLOBAL_BLOCKED: ClassVar[frozenset[str]] = frozenset(
        {
            "-L",
            "-H",
            "-R",
            "-r",  # grep recursive; ls uses -r for reverse — handled per-command
            "--recursive",
            "--dereference",
            "--follow",
        }
    )

    _FIND_PATH_FLAGS: ClassVar[frozenset[str]] = frozenset(
        {"-fprintf", "-fprint", "-fprint0", "-fls", "-newer"}
    )

    # Flags whose next token OR attached value is a path (containment-checked).
    _PATH_VALUE_FLAGS: ClassVar[dict[str, frozenset[str]]] = {
        "grep": frozenset({"-f", "--file", "--exclude-from"}),
        "find": frozenset({"-fprintf", "-fprint", "-fprint0", "-fls", "-newer"}),
        "date": frozenset({"-f", "--file", "-r", "--reference"}),
    }

    # date: these flags are always blocked (file input / set clock).
    _DATE_BLOCKED: ClassVar[frozenset[str]] = frozenset(
        {"-f", "--file", "-r", "--reference", "-s", "--set"}
    )

    def __init__(self, enable_shell: bool, workspace_root: str | Path | None = None):
        self._workspace = WorkspaceFilesystem(workspace_root or _workspace_root())
        if not enable_shell:
            self.risk_level = RiskLevel.BLOCKED

    def bind_workspace(self, workspace_root: str | Path) -> None:
        self._workspace = WorkspaceFilesystem(workspace_root)

    def _validate_path(self, arg: str) -> None:
        try:
            self._workspace.resolve(arg, allow_root=arg in ("", "."))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Path is outside the workspace: {arg}") from exc

    def _validate_args(self, command: str, args: list[str]) -> None:
        allowed = self._ALLOWED_FLAGS.get(command, frozenset())
        path_flags = self._PATH_VALUE_FLAGS.get(command, frozenset())

        i = 0
        nonflag_index = 0
        while i < len(args):
            arg = args[i]
            if arg == "--":
                i += 1
                # remainder are positional
                while i < len(args):
                    if command in self._PATH_COMMANDS or (
                        command == "grep" and nonflag_index >= 1
                    ):
                        self._validate_path(args[i])
                    nonflag_index += 1
                    i += 1
                break

            if arg.startswith("-"):
                base = _flag_base(arg)
                attached = _attached_value(arg)

                if command == "find" and base in self._FIND_BLOCKED_FLAGS:
                    raise ValueError(f"Flag not allowed for find: {base}")
                if command == "date" and base in self._DATE_BLOCKED:
                    raise ValueError(f"Flag not allowed for date: {base}")
                if command == "ls" and base in {"-L", "--dereference"}:
                    raise ValueError(f"Flag not allowed for ls: {base}")
                if command == "grep" and base in {
                    "-R",
                    "-r",
                    "--recursive",
                    "-L",
                    "--files-without-match",
                }:
                    # -r/-R recursive walk; block. (Note: -r alone is recursive for GNU grep)
                    if base in {"-R", "-r", "--recursive"}:
                        raise ValueError(f"Flag not allowed for grep: {base}")

                if base not in allowed and base not in path_flags:
                    # path flags may be allowed only via path_flags set
                    if base not in path_flags:
                        raise ValueError(f"Unknown or disallowed flag: {base}")

                if base in path_flags:
                    if attached is not None:
                        self._validate_path(attached)
                        i += 1
                        continue
                    if i + 1 >= len(args):
                        raise ValueError(f"Flag {base} requires a path argument")
                    self._validate_path(args[i + 1])
                    i += 2
                    continue

                i += 1
                continue

            # positional
            if command in self._PATH_COMMANDS:
                self._validate_path(arg)
            elif command == "grep" and nonflag_index >= 1:
                self._validate_path(arg)
            nonflag_index += 1
            i += 1

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

        root = self._workspace.root

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
