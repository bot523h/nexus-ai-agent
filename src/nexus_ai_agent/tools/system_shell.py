"""Restricted shell tool: allowlisted commands sandboxed to the workspace.

Security model (Phase 0, option policy hardened 2026-09-26):

- Only commands on the ALLOWLIST may run.
- Commands execute with ``cwd`` set to the workspace root
  (``NEXUS_WORKSPACE_ROOT``, same default as ``tools/files.py``).
- Any argument that is (or resolves to) a path outside the workspace is
  rejected: absolute paths, ``..`` segments, and symlinks pointing out.
- Options are parsed with a **curated, fail-closed per-command policy**
  (``_OPT_SPECS``), not by substring heuristics. Anything the policy does
  not name is rejected before the subprocess exists. This closes the
  three bypass classes that verbatim flag matching left open:

  * *file-consuming options in alternate spellings* — e.g. ``date -f`` /
    ``date --file=FILE`` (reads lines of ANY file and echoes them in
    error output) and ``grep -fFILE`` / ``grep --file=FILE`` /
    ``grep --exclude-from=FILE`` / bundled ``grep -if FILE`` (read an
    unvalidated pattern file outside the workspace). File options either
    do not exist in the policy (``date``) or are validated paths.
  * *symlink-dereferencing recursion* — ``grep -R`` /
    ``--dereference-recursive`` / ``--follow`` and ``find -L`` /
    ``find -H`` / ``find -follow`` and ``ls -L`` follow symlinked
    directories *below* an already-validated argument, which turned a
    planted symlink into an outside-workspace content/name leak. The
    lowercase ``grep -r`` (recursive, no dereference) remains allowed.
  * *unknown-option oracle* — every allowlisted command now fails closed
    on any option (short, bundled, or long) the policy does not list.

- ``find`` additionally may not use execution/deletion flags (``-exec``,
  ``-execdir``, ``-delete``, ``-ok``, ``-okdir``) or traversal-following
  options (``-H``, ``-L``, ``-follow``). Flags whose next argument is a
  file (``-fprintf``, ``-fprint``, ``-fprint0``, ``-fls``, ``-newerXY``)
  are only allowed with paths inside the workspace. ``-printf`` takes a
  format string, not a path, so it is not path-checked (it cannot write
  files).
- ``grep``'s path-typed options (``-f``/``--file`` pattern file,
  ``--exclude-from``) are containment-checked in every spelling —
  separate, ``=``, attached and bundled. The first non-flag argument is
  the pattern and may be any regex text; when ``-e``/``--regexp`` is
  used, all non-flag arguments are paths.
- ``date`` has **no path-typed options**: its operands must be format
  strings (``+...``), so ``-f``/``--file``/``--reference``/``--set``
  (which read files or mutate the clock) are unreachable.
- Output is capped (``_MAX_OUTPUT_CHARS``) so a large in-workspace file
  cannot exhaust process memory through a tool result.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from nexus_ai_agent.tools.base import BaseTool, RiskLevel
from nexus_ai_agent.tools.files import _workspace_root
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem


@dataclass(frozen=True)
class _OptSpec:
    """The complete option vocabulary of one allowlisted command.

    Short options are bundleable single characters; a character in
    ``short_opts`` consumes the rest of its token (or the next token) as
    its argument. Long options must match exactly — prefix abbreviations
    are deliberately not accepted, so an attacker cannot reach a blocked
    option through a GNU unique-prefix spelling. Every spec value is the
    argument kind: ``"value"`` (free-form data) or ``"path"`` (must stay
    inside the workspace).
    """

    short_flags: frozenset[str]
    short_opts: dict[str, str]
    long_flags: frozenset[str]
    long_opts: dict[str, str]


_LS = _OptSpec(
    short_flags=frozenset("laARhtSrF1digosk mCxQcuUvXbpq".replace(" ", "")),
    short_opts={},
    long_flags=frozenset(
        {
            "all",
            "almost-all",
            "recursive",
            "human-readable",
            "directory",
            "classify",
            "inode",
            "reverse",
            "ignore-backups",
        }
    ),
    long_opts={"ignore": "value", "width": "value"},
)

_PWD = _OptSpec(
    short_flags=frozenset("LP"),
    short_opts={},
    long_flags=frozenset({"logical", "physical"}),
    long_opts={},
)

_ECHO = _OptSpec(
    short_flags=frozenset("neE"),
    short_opts={},
    long_flags=frozenset(),
    long_opts={},
)

_CAT = _OptSpec(
    short_flags=frozenset("nbsETvetA"),
    short_opts={},
    long_flags=frozenset(
        {
            "number",
            "number-nonblank",
            "squeeze-blank",
            "show-ends",
            "show-tabs",
            "show-nonprinting",
            "show-all",
        }
    ),
    long_opts={},
)

# Symlink-dereferencing recursion (-R / --dereference-recursive / --follow)
# and the device/directory actions (-D / -d) are intentionally ABSENT: an
# unlisted option is a refusal.  -f/--file/--exclude-from are path-typed,
# so every spelling is containment-checked before execution.
_GREP = _OptSpec(
    short_flags=frozenset("ivwxyc lLnboqsraI zEFGPhH".replace(" ", "")),
    short_opts={"e": "value", "m": "value", "A": "value", "B": "value", "C": "value", "f": "path"},
    long_flags=frozenset(
        {
            "ignore-case",
            "invert-match",
            "word-regexp",
            "line-regexp",
            "count",
            "files-with-matches",
            "files-without-match",
            "line-number",
            "byte-offset",
            "only-matching",
            "quiet",
            "silent",
            "no-messages",
            "binary",
            "text",
            "no-filename",
            "with-filename",
            "recursive",
            "extended-regexp",
            "fixed-strings",
            "basic-regexp",
            "perl-regexp",
            "null-data",
            "null",
        }
    ),
    long_opts={
        "regexp": "value",
        "max-count": "value",
        "after-context": "value",
        "before-context": "value",
        "context": "value",
        "include": "value",
        "exclude": "value",
        "exclude-dir": "value",
        "label": "value",
        "color": "value",
        "colour": "value",
        "file": "path",
        "exclude-from": "path",
    },
)

# No path-typed options exist for date at all: -f/--file/-r/--reference
# (file readers whose error channel echoes file content) and -s/--set
# (clock mutation) are simply not in the vocabulary.
_DATE = _OptSpec(
    short_flags=frozenset("uIR"),
    short_opts={"d": "value"},
    long_flags=frozenset({"universal", "utc", "rfc-2822", "rfc-email", "iso-8601", "debug"}),
    long_opts={"date": "value", "rfc-3339": "value"},
)

_OPT_SPECS: dict[str, _OptSpec] = {
    "ls": _LS,
    "pwd": _PWD,
    "echo": _ECHO,
    "cat": _CAT,
    "grep": _GREP,
    "date": _DATE,
}

# find: tokens that execute commands, delete files, or follow symlinks —
# always blocked, in any position.
_FIND_BLOCKED_FLAGS: frozenset[str] = frozenset(
    {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-follow", "-H", "-L"}
)

# find: tokens whose NEXT argument is a file path (containment-checked).
_FIND_PATH_FLAGS: frozenset[str] = frozenset({"-fprintf", "-fprint", "-fprint0", "-fls", "-newer"})

# find: the -newerXY family (e.g. -newermm) also consumes a reference-file
# argument; bare match-expression spellings (``-newermt '2024-01-01'``) pass
# through the generic operand path-check unchanged.
_FIND_NEWER_FAMILY = re.compile(r"^-newer[a-zA-Z]{2}$")

_MAX_OUTPUT_CHARS = 200_000


class ShellTool(BaseTool):
    name = "shell"
    description = "Run a restricted shell command (allowlisted, workspace-only)."
    risk_level = RiskLevel.GUARDED

    ALLOWLIST: ClassVar[list[str]] = ["ls", "pwd", "echo", "cat", "grep", "find", "date"]

    # Commands whose non-flag arguments are file paths (relative to workspace).
    _PATH_COMMANDS: ClassVar[frozenset[str]] = frozenset({"ls", "cat", "find"})

    # Kept as class attributes for backwards compatibility with callers/tests
    # that introspect the policy; the authoritative text lives in
    # _FIND_BLOCKED_FLAGS / _FIND_PATH_FLAGS / _OPT_SPECS.
    _FIND_BLOCKED_FLAGS: ClassVar[frozenset[str]] = _FIND_BLOCKED_FLAGS
    _FIND_PATH_FLAGS: ClassVar[frozenset[str]] = _FIND_PATH_FLAGS

    def __init__(self, enable_shell: bool, workspace_root: str | Path | None = None):
        self._workspace = WorkspaceFilesystem(workspace_root or _workspace_root())
        if not enable_shell:
            self.risk_level = RiskLevel.BLOCKED

    def bind_workspace(self, workspace_root: str | Path) -> None:
        self._workspace = WorkspaceFilesystem(workspace_root)

    # ── validation ──────────────────────────────────────────────────

    def _validate_path(self, arg: str) -> None:
        """Reject traversal and symlink escapes before subprocess execution."""
        try:
            self._workspace.resolve(arg, allow_root=arg in ("", "."))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Path is outside the workspace: {arg}") from exc

    def _validate_find_args(self, args: list[str]) -> None:
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in _FIND_BLOCKED_FLAGS:
                raise ValueError(f"Flag not allowed for find: {arg}")
            if arg.startswith("-"):
                if arg in _FIND_PATH_FLAGS or _FIND_NEWER_FAMILY.match(arg):
                    if i + 1 >= len(args):
                        raise ValueError(f"Flag {arg} requires a path argument")
                    self._validate_path(args[i + 1])
                    i += 2
                else:
                    i += 1
                continue
            self._validate_path(arg)
            i += 1

    def _validate_opt_args(self, command: str, args: list[str]) -> None:
        """Parse options with the command's curated spec (fail-closed)."""
        spec = _OPT_SPECS[command]
        operands: list[str] = []
        seen_regexp_opt = command != "grep"  # only meaningful for grep
        end_of_options = False
        i = 0
        while i < len(args):
            token = args[i]
            if not end_of_options and token == "--":
                end_of_options = True
                i += 1
                continue
            if not end_of_options and token.startswith("--"):
                name, sep, embedded = token[2:].partition("=")
                if not sep and name in spec.long_flags:
                    i += 1
                    continue
                kind = spec.long_opts.get(name)
                if kind is None:
                    raise ValueError(f"Option not allowed for {command}: --{name}")
                if sep:
                    value = embedded
                elif i + 1 < len(args):
                    value = args[i + 1]
                    i += 1
                else:
                    raise ValueError(f"Option --{name} requires a value")
                if kind == "path":
                    self._validate_path(value)
                if command == "grep" and name == "regexp":
                    seen_regexp_opt = True
                i += 1
                continue
            if not end_of_options and token.startswith("-") and token != "-":
                bundle = token[1:]
                cursor = 0
                while cursor < len(bundle):
                    char = bundle[cursor]
                    if char in spec.short_flags:
                        cursor += 1
                        continue
                    kind = spec.short_opts.get(char)
                    if kind is None:
                        raise ValueError(f"Option not allowed for {command}: -{char}")
                    attached = bundle[cursor + 1 :]
                    if attached:
                        value = attached
                    elif i + 1 < len(args):
                        value = args[i + 1]
                        i += 1
                    else:
                        raise ValueError(f"Option -{char} requires a value")
                    if kind == "path":
                        self._validate_path(value)
                    if command == "grep" and char == "e":
                        seen_regexp_opt = True
                    break  # an option argument consumes the rest of the bundle
                i += 1
                continue
            operands.append(token)
            i += 1

        if command in self._PATH_COMMANDS:
            for operand in operands:
                self._validate_path(operand)
        elif command == "grep":
            for index, operand in enumerate(operands):
                if seen_regexp_opt or index >= 1:
                    self._validate_path(operand)
        elif command == "date":
            for operand in operands:
                if not operand.startswith("+"):
                    raise ValueError(f"date operand must be a +FORMAT string: {operand!r}")
        elif command == "pwd" and operands:
            raise ValueError("pwd takes no file arguments")
        # echo operands are free-form text.

    def _validate_args(self, command: str, args: list[str]) -> None:
        for token in args:
            if "\x00" in token:
                raise ValueError("command arguments must not contain NUL bytes")
        if command == "find":
            self._validate_find_args(args)
        else:
            self._validate_opt_args(command, args)

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
            if len(out) > _MAX_OUTPUT_CHARS:
                out = out[:_MAX_OUTPUT_CHARS] + "\n[output truncated]"
            if proc.returncode != 0:
                return {"success": False, "output": out, "error": f"Exit {proc.returncode}"}
            return {"success": True, "output": out, "error": None}
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "", "error": "Command timed out"}
        except (OSError, ValueError) as exc:
            return {"success": False, "output": "", "error": f"Execution error: {exc}"}
