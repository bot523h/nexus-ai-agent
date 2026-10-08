"""Restricted shell tool: allowlisted commands sandboxed to the workspace.

Security model
--------------
The tool hands an argument vector to a real subprocess, so this module's
validation of that vector is the only thing standing between an agent and the
host filesystem.  Two independent, fail-closed mechanisms enforce it.

**M1 — declared flag grammar (primary).**  Every allowlisted command has an
explicit table of the flags it may receive.  A flag absent from the table is
*refused*, so the tool is safe against options it has never heard of — including
options added by a future coreutils/findutils/grep release.  Each entry declares
how many argument tokens the flag consumes and what they are:

``bool``
    consumes nothing (``ls -l``, ``grep -i``).
``value``
    consumes one token that is not a filesystem path (a number, word, or regex:
    ``grep -m 3``, ``find -name '*.py'``).
``path``
    consumes one token that must be a workspace-contained path
    (``grep -f PATTERNFILE``, ``find -newer FILE``).
``optional_value``
    consumes only a value attached with ``=`` (``ls --color=auto``); it never
    swallows the following token, which may be a real path.
``tuple``
    consumes several tokens, each with its own kind
    (``find -fprintf FILE FORMAT``).

The table is what makes the boundary *complete*: every token a command can open
as a file is either declared ``path`` (and therefore validated) or the flag is
refused outright.

**M2 — path-shaped argument net (defence in depth).**  Independently of the
tables, any token that lexically denotes an absolute or traversing path
(``/etc/passwd``, ``../x``, ``--file=/etc/x``, ``~/.ssh``) is validated against
the workspace boundary wherever it appears in the vector, in any position and in
any spelling.  M2 alone would not be sufficient — a bare relative name
(``-f secret``) is only recognised as a path if a table says the flag takes one
— which is exactly why M1 carries the weight and M2 catches the shapes a table
might mis-declare.

Both mechanisms route through
:class:`~nexus_ai_agent.tools.filesystem_policy.WorkspaceFilesystem`, which
rejects absolute paths, ``..`` segments, NUL bytes, non-POSIX spellings, and any
path carrying a symlink component.  Commands run with ``cwd`` set to the
workspace root and ``shell=False``, so no shell metacharacter is ever
interpreted.

Symlink-following options are refused outright
----------------------------------------------
``-L``/``-H``/``--dereference`` (``ls``), ``-R``/``--dereference-recursive``
(``grep``) and ``-L``/``-H``/``-follow`` (``find``) take no path argument, so no
amount of path validation would contain them: they make the *command* follow a
symlink it meets while walking, and one symlink inside the workspace then
redirects the read outside it.  Measured, not assumed — with a symlink
``ws/linkfile -> /tmp/secret.env`` present, ``grep -R TOP_SECRET .`` prints
``./linkfile:TOP_SECRET=42``.  The safe siblings (``ls -R``, ``grep -r``,
``find`` with its default ``-P``) were measured not to follow and are allowed.

Why not simply blocklist the known-dangerous options?
-----------------------------------------------------
That was the previous design, and it was defeated in practice:

* ``date`` is allowlisted, and ``date -f FILE`` (``--file=FILE``) makes GNU
  ``date`` read an arbitrary file line by line and echo each line back inside
  its own error text — a direct file-disclosure primitive;
* ``date -r FILE`` (``--reference=FILE``) confirms existence and mtime of any
  file on the host;
* ``grep --file=FILE`` and the attached spelling ``grep -fFILE`` were never
  inspected at all, because the old validator only looked at tokens
  *positionally* and only recognised the exact spellings ``-f``/``-newer``.

An enumeration of things to *forbid* can only ever cover the options someone
thought of; a declared set of things to *allow* covers the rest by default.
See ``docs/architecture/adr/0011-restricted-shell-flag-grammar.md``.
"""

from __future__ import annotations

import asyncio
import ntpath
import shlex
import subprocess
from enum import Enum
from pathlib import Path
from typing import ClassVar

from nexus_ai_agent.tools.base import BaseTool, RiskLevel
from nexus_ai_agent.tools.files import _workspace_root
from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError, WorkspaceFilesystem


class FlagKind(str, Enum):
    """How an allowlisted flag consumes the tokens that follow it."""

    BOOL = "bool"
    VALUE = "value"
    PATH = "path"
    OPTIONAL_VALUE = "optional_value"
    #: Consumes several tokens, each with the kind named in a companion tuple.
    TUPLE = "tuple"


# ---------------------------------------------------------------------------
# The declared flag grammar — one table per allowlisted command
# ---------------------------------------------------------------------------
#
# Rule: only flags listed here are accepted.  Anything absent is refused.
#
# Deliberately absent, in addition to the symlink-following options described in
# the module docstring: anything that opens a file we do not intend to open
# (``date -f/-r/--file/--reference``, ``grep -f/--file/--exclude-from``,
# ``find -files0-from``), anything that executes or deletes
# (``find -exec/-execdir/-ok/-okdir/-delete``), and anything that mutates host
# state (``date -s/--set``).

_LS_FLAGS: dict[str, FlagKind] = {
    # -- booleans ---------------------------------------------------------
    **{
        f: FlagKind.BOOL
        for f in (
            "-1",
            "-a",
            "--all",
            "-A",
            "--almost-all",
            "-b",
            "--escape",
            "-B",
            "--ignore-backups",
            "-c",
            "-C",
            "-d",
            "--directory",
            "-D",
            "--dired",
            "-f",
            "-F",
            "--classify",
            "--file-type",
            "-g",
            "-G",
            "-h",
            "--human-readable",
            "-i",
            "--inode",
            "-k",
            "-l",
            "-m",
            "-n",
            "--numeric-uid-gid",
            "-N",
            "--literal",
            "-o",
            "-p",
            "-q",
            "--quote-name",
            "-Q",
            "-r",
            "--reverse",
            "-R",
            "--recursive",
            "-s",
            "--size",
            "-S",
            "-t",
            "-u",
            "-U",
            "-v",
            "--si",
            "--zero",
            "--author",
            "--group-directories-first",
            "--full-time",
            "--color",
            "--hyperlink",
            "--help",
            "--version",
        )
    },
    # -- one non-path value ------------------------------------------------
    **{
        f: FlagKind.VALUE
        for f in (
            "-w",
            "--width",
            "-T",
            "--tabsize",
            "--block-size",
            "--format",
            "--time",
            "--time-style",
            "--sort",
            "--quoting-style",
            "--indicator-style",
            "-I",
            "--ignore",
            "--hide",
        )
    },
    # -- optional attached value (never swallows the next token) -----------
    "--color": FlagKind.OPTIONAL_VALUE,
    "--hyperlink": FlagKind.OPTIONAL_VALUE,
}

_CAT_FLAGS: dict[str, FlagKind] = {
    f: FlagKind.BOOL
    for f in (
        "-A",
        "--show-all",
        "-b",
        "--number-nonblank",
        "-e",
        "-E",
        "--show-ends",
        "-n",
        "--number",
        "-s",
        "--squeeze-blank",
        "-t",
        "-T",
        "--show-tabs",
        "-u",
        "-v",
        "--show-nonprinting",
        "--help",
        "--version",
    )
}

_PWD_FLAGS: dict[str, FlagKind] = {
    f: FlagKind.BOOL for f in ("-L", "--logical", "-P", "--physical", "--help", "--version")
}

_ECHO_FLAGS: dict[str, FlagKind] = {
    f: FlagKind.BOOL for f in ("-n", "-e", "-E", "--help", "--version")
}

#: ``date`` keeps only the read-only display flags.  ``-f``/``--file``,
#: ``-r``/``--reference``, ``-d``/``--date``, ``-s``/``--set``, ``--debug`` and
#: ``--resolution`` are absent on purpose: two of them open arbitrary files, one
#: would set the system clock, and none is needed to read the clock.
_DATE_FLAGS: dict[str, FlagKind] = {
    **{
        f: FlagKind.BOOL
        for f in (
            "-u",
            "--utc",
            "--universal",
            "-R",
            "--rfc-email",
            "--rfc-2822",
            "--rfc-822",
            "--help",
            "--version",
        )
    },
    # ``-I[timespec]`` / ``--iso-8601[=timespec]`` take an optional attached value.
    "-I": FlagKind.OPTIONAL_VALUE,
    "--iso-8601": FlagKind.OPTIONAL_VALUE,
    # ``--rfc-3339=timespec`` requires its value.
    "--rfc-3339": FlagKind.VALUE,
}

_GREP_FLAGS: dict[str, FlagKind] = {
    **{
        f: FlagKind.BOOL
        for f in (
            "-E",
            "--extended-regexp",
            "-F",
            "--fixed-strings",
            "-G",
            "--basic-regexp",
            "-P",
            "--perl-regexp",
            "-a",
            "--text",
            "-b",
            "--byte-offset",
            "-c",
            "--count",
            "-h",
            "--no-filename",
            "-H",
            "--with-filename",
            "-i",
            "--ignore-case",
            "-y",
            "-I",
            "-l",
            "--files-with-matches",
            "-L",
            "--files-without-match",
            "-n",
            "--line-number",
            "-o",
            "--only-matching",
            "-q",
            "--quiet",
            "--silent",
            "-r",
            "--recursive",
            "-s",
            "--no-messages",
            "-U",
            "--binary",
            "-v",
            "--invert-match",
            "-w",
            "--word-regexp",
            "-x",
            "--line-regexp",
            "-z",
            "--null-data",
            "-Z",
            "--null",
            "--line-buffered",
            "-T",
            "--initial-tab",
            "--no-group-separator",
            "--help",
            "--version",
        )
    },
    **{
        f: FlagKind.VALUE
        for f in (
            "-e",
            "--regexp",
            "-m",
            "--max-count",
            "-A",
            "--after-context",
            "-B",
            "--before-context",
            "-C",
            "--context",
            "-d",
            "--directories",
            "-D",
            "--devices",
            "--binary-files",
            "--include",
            "--exclude",
            "--exclude-dir",
            "--label",
            "--group-separator",
        )
    },
    # ``--color[=WHEN]`` never swallows the next token.
    "--color": FlagKind.OPTIONAL_VALUE,
    "--colour": FlagKind.OPTIONAL_VALUE,
    # -- pattern *files*: the value must be a workspace path ---------------
    "-f": FlagKind.PATH,
    "--file": FlagKind.PATH,
    "--exclude-from": FlagKind.PATH,
}

#: ``find`` predates the getopt convention: every option is spelled as its own
#: whole word, so its tokens must never be split into short-flag clusters.
_FIND_FLAGS: dict[str, FlagKind] = {
    **{
        f: FlagKind.BOOL
        for f in (
            "-print",
            "-print0",
            "-ls",
            "-empty",
            "-readable",
            "-writable",
            "-executable",
            "-true",
            "-false",
            "-not",
            "-a",
            "-o",
            "-and",
            "-or",
            "-prune",
            "-quit",
            "-depth",
            "-noleaf",
            "-mount",
            "-xdev",
            "-daystart",
            "-ignore_readdir_race",
            "-noignore_readdir_race",
            "--help",
            "--version",
        )
    },
    **{
        f: FlagKind.VALUE
        for f in (
            "-name",
            "-iname",
            "-path",
            "-ipath",
            "-wholename",
            "-iwholename",
            "-regex",
            "-iregex",
            "-type",
            "-xtype",
            "-maxdepth",
            "-mindepth",
            "-size",
            "-mtime",
            "-atime",
            "-ctime",
            "-mmin",
            "-amin",
            "-cmin",
            "-user",
            "-group",
            "-uid",
            "-gid",
            "-perm",
            "-links",
            "-inum",
            "-printf",
            "-fstype",
            "-regextype",
            "-used",
            "-lname",
            "-ilname",
            "-context",
        )
    },
    # -- predicates whose value is a file ----------------------------------
    "-newer": FlagKind.PATH,
    "-anewer": FlagKind.PATH,
    "-cnewer": FlagKind.PATH,
    "-samefile": FlagKind.PATH,
    "-fprint": FlagKind.PATH,
    "-fprint0": FlagKind.PATH,
    "-fls": FlagKind.PATH,
    # ``-newerXY REFERENCE`` — the reference is a file.
    **{f"-newer{axis}": FlagKind.PATH for axis in "aBcCmMtT"},
    # ``-fprintf FILE FORMAT`` consumes a path and then a format string.
    "-fprintf": FlagKind.TUPLE,
}

#: Token kinds consumed by each ``TUPLE`` flag, in order.
_FLAG_TUPLES: dict[str, tuple[str, ...]] = {
    "-fprintf": ("path", "value"),
}

#: Flags that are refused by *absence* from the tables, but get a named reason
#: because an operator will plausibly try them and deserves to know why.
_NAMED_REFUSALS: dict[str, dict[str, str]] = {
    "find": {
        "-exec": "runs an arbitrary program",
        "-execdir": "runs an arbitrary program",
        "-ok": "runs an arbitrary program",
        "-okdir": "runs an arbitrary program",
        "-delete": "deletes files",
        "-files0-from": "reads an arbitrary file list",
        "-L": "follows symlinks, which can leave the workspace",
        "-H": "follows symlinks, which can leave the workspace",
        "-P": "changes symlink traversal semantics",
        "-follow": "follows symlinks, which can leave the workspace",
    },
    "date": {
        "-f": "reads an arbitrary file and echoes its lines back",
        "--file": "reads an arbitrary file and echoes its lines back",
        "-r": "reads an arbitrary file's metadata",
        "--reference": "reads an arbitrary file's metadata",
        "-d": "parses an arbitrary date string",
        "--date": "parses an arbitrary date string",
        "-s": "would set the system clock",
        "--set": "would set the system clock",
    },
    "grep": {
        "--exclude-from": "reads an arbitrary pattern file",
        "-R": "follows symlinks, which can leave the workspace",
        "--dereference-recursive": "follows symlinks, which can leave the workspace",
    },
    "ls": {
        "-L": "follows symlinks, which can leave the workspace",
        "--dereference": "follows symlinks, which can leave the workspace",
        "-H": "follows symlinks, which can leave the workspace",
        "--dereference-command-line": "follows symlinks, which can leave the workspace",
    },
}


class ShellCommandError(ValueError):
    """An argument vector violated the sandbox contract (fail closed)."""


#: How positional (non-flag) arguments are validated, per command.
#: ``paths``  — every positional must be a workspace path.
#: ``leading`` — positionals before the expression are paths (``find``).
#: ``paths_after_first`` — the first positional is a pattern (``grep``).
#: ``text``   — net-checked only; bare names are free text (``echo``, ``date``).
#: ``none``   — the command takes no positional arguments (``pwd``).
_POSITIONAL_POLICY: dict[str, str] = {
    "ls": "paths",
    "cat": "paths",
    "find": "leading",
    "grep": "paths_after_first",
    "echo": "text",
    "date": "text",
    "pwd": "none",
}


class ShellTool(BaseTool):
    """Run an allowlisted command whose file arguments stay inside the workspace."""

    name = "shell"
    description = "Run a restricted shell command (allowlisted, workspace-only)."
    risk_level = RiskLevel.GUARDED

    ALLOWLIST: ClassVar[list[str]] = ["ls", "pwd", "echo", "cat", "grep", "find", "date"]

    #: Per-command declared grammar.  A command with no entry cannot run.
    FLAGS: ClassVar[dict[str, dict[str, FlagKind]]] = {
        "ls": _LS_FLAGS,
        "cat": _CAT_FLAGS,
        "pwd": _PWD_FLAGS,
        "echo": _ECHO_FLAGS,
        "grep": _GREP_FLAGS,
        "find": _FIND_FLAGS,
        "date": _DATE_FLAGS,
    }

    #: Commands whose short flags may be clustered (``-la`` == ``-l -a``).
    #: ``find`` is excluded: ``-name`` is one option, not ``-n -a -m -e``.
    BUNDLED_SHORT_FLAGS: ClassVar[frozenset[str]] = frozenset(
        {"ls", "cat", "pwd", "echo", "grep", "date"}
    )

    #: ``find`` expression tokens that name no file and take no argument.
    _FIND_EXPRESSION_TOKENS: ClassVar[frozenset[str]] = frozenset({"(", ")", "!", ","})

    #: ``grep`` flags that supply the pattern themselves.  Once one of these is
    #: seen, every remaining positional is a *file*, not the pattern.
    _GREP_PATTERN_SOURCES: ClassVar[frozenset[str]] = frozenset({"-e", "--regexp", "-f", "--file"})

    def __init__(self, enable_shell: bool, workspace_root: str | Path | None = None):
        self._workspace = WorkspaceFilesystem(workspace_root or _workspace_root())
        if not enable_shell:
            self.risk_level = RiskLevel.BLOCKED

    def bind_workspace(self, workspace_root: str | Path) -> None:
        self._workspace = WorkspaceFilesystem(workspace_root)

    # ── validation ──────────────────────────────────────────────────

    def _validate_path(self, arg: str, *, what: str = "argument") -> None:
        """Reject traversal and symlink escapes before subprocess execution."""
        try:
            self._workspace.resolve(arg, allow_root=arg in ("", "."))
        except (OSError, ValueError) as exc:
            raise ShellCommandError(f"Path is outside the workspace ({what}): {arg}") from exc

    @staticmethod
    def _looks_like_a_path(token: str) -> bool:
        """True for tokens that can only be a path, whatever the flag grammar says.

        Deliberately syntactic, so it stays independent of the tables.  A bare
        relative name (``notes.txt``) is *not* path-shaped: it can only name a
        workspace entry anyway, because the subprocess runs with ``cwd`` set to
        the workspace root.
        """
        if not token:
            return False
        if token.startswith(("/", "\\", "~")):
            return True
        if ntpath.splitdrive(token)[0]:
            return True
        return "/" in token or "\\" in token

    def _net_check(self, token: str, *, what: str) -> None:
        """M2: a path-shaped token must be contained, in any position or spelling."""
        candidate = token
        if token.startswith("--") and "=" in token:
            candidate = token.partition("=")[2]
        if self._looks_like_a_path(candidate):
            self._validate_path(candidate, what=what)

    @staticmethod
    def _refusal(command: str, flag: str) -> ShellCommandError:
        reason = _NAMED_REFUSALS.get(command, {}).get(flag)
        detail = f" — it {reason}" if reason else ""
        return ShellCommandError(f"Flag not allowed for {command}: {flag}{detail}")

    def _flag_kinds(self, command: str, flag: str) -> tuple[str, ...]:
        """Resolve a flag token to the kinds of value-tokens it consumes."""
        kind = self.FLAGS[command].get(flag)
        if kind is None:
            raise self._refusal(command, flag)
        if kind is FlagKind.TUPLE:
            return _FLAG_TUPLES[flag]
        return (kind.value,)

    def _validate_args(self, command: str, args: list[str]) -> None:
        """Walk the vector once, accepting declared tokens and refusing the rest.

        Raises :class:`ShellCommandError` for an undeclared flag, a flag used
        with the wrong shape (``--flag=value`` on a boolean, a missing value),
        or any argument naming a path outside the workspace.
        """
        if command not in self.FLAGS:  # pragma: no cover - the allowlist gate runs first
            raise ShellCommandError(f"Command not allowed: {command}")

        bundles = command in self.BUNDLED_SHORT_FLAGS
        positionals: list[str] = []
        find_paths: list[str] = []
        expression_started = False
        end_of_options = False
        #: ``grep`` only treats its *first* positional as a pattern when no
        #: pattern was supplied by a flag.  With ``-e``/``-f`` present, every
        #: positional is a file — so the positional policy depends on the flags
        #: actually seen, not on a fixed position.
        grep_pattern_supplied = False

        i = 0
        while i < len(args):
            token = args[i]

            if not end_of_options and token == "--":
                end_of_options = True
                for extra in args[i + 1 :]:
                    self._net_check(extra, what="argument after --")
                positionals.extend(args[i + 1 :])
                if command == "find":
                    find_paths.extend(args[i + 1 :])
                break

            if not end_of_options and token.startswith("-") and token != "-":
                i, consumed = self._consume_flag(command, args, i, bundles)
                if command == "find":
                    expression_started = True
                elif consumed in self._GREP_PATTERN_SOURCES:
                    grep_pattern_supplied = True
                continue

            # -- a positional token -----------------------------------------
            self._net_check(token, what="positional argument")
            if command == "find":
                if not expression_started:
                    if token in self._FIND_EXPRESSION_TOKENS:
                        expression_started = True
                    else:
                        find_paths.append(token)
                elif token not in self._FIND_EXPRESSION_TOKENS:
                    raise ShellCommandError(f"Unexpected token in a find expression: {token!r}")
            else:
                positionals.append(token)
            i += 1

        if command == "find":
            for path in find_paths:
                self._validate_path(path, what="find path")
        else:
            self._validate_positionals(command, positionals, pattern_supplied=grep_pattern_supplied)

    def _consume_flag(
        self, command: str, args: list[str], index: int, bundles: bool
    ) -> tuple[int, str]:
        """Validate the flag token at ``index``.

        Returns ``(next_index, flag_name)``; the flag name is the spelling that
        actually consumed the value (the short option that terminated a cluster,
        not the cluster token), so the caller can reason about *which* option it
        saw rather than guessing from the surface token.
        """
        token = args[index]

        if token.startswith("--"):
            name, sep, attached = token.partition("=")
            kinds = self._flag_kinds(command, name)
            if kinds == ("bool",):
                if sep:
                    raise ShellCommandError(f"Flag does not take a value for {command}: {name}")
                self._net_check(token, what=f"value of {name}")
                return index + 1, name
            if kinds == ("optional_value",):
                # Never swallows the following token: it may be a real path.
                if sep:
                    self._check_piece(attached, "optional_value", command, name)
                self._net_check(token, what=f"value of {name}")
                return index + 1, name
            return (
                self._consume_kinds(command, name, kinds, args, index, attached if sep else None),
                name,
            )

        if not bundles:
            kinds = self._flag_kinds(command, token)
            if kinds == ("optional_value",):
                return index + 1, token
            return self._consume_kinds(command, token, kinds, args, index, None), token

        # Short-flag cluster: ``-la`` is ``-l -a``.  A value-taking flag must be
        # last in the cluster; its value may be attached (``-A3``) or the next
        # token (``-A 3``).
        cluster = token[1:]
        if not cluster:
            raise self._refusal(command, token)
        for position, char in enumerate(cluster):
            flag = f"-{char}"
            kinds = self._flag_kinds(command, flag)
            if kinds == ("bool",):
                continue
            remainder = cluster[position + 1 :] or None
            return self._consume_kinds(command, flag, kinds, args, index, remainder), flag
        self._net_check(token, what="flag")
        return index + 1, token

    def _consume_kinds(
        self,
        command: str,
        flag: str,
        kinds: tuple[str, ...],
        args: list[str],
        index: int,
        attached: str | None,
    ) -> int:
        """Consume and validate the value tokens a flag declares."""
        cursor = index + 1
        pending = attached
        for position, kind in enumerate(kinds):
            if kind == "bool":
                # Consumes nothing.  Reached for whole-word flags (``find -o``,
                # ``find -print0``); a ``bool`` never swallows the next token.
                continue
            if kind == "optional_value":
                if pending is not None:
                    self._check_piece(pending, kind, command, flag)
                    pending = None
                continue
            if pending is not None and position == 0:
                value = pending
                pending = None
            else:
                if cursor >= len(args):
                    raise ShellCommandError(f"Flag requires a value for {command}: {flag}")
                value = args[cursor]
                cursor += 1
            self._check_piece(value, kind, command, flag)
        return cursor

    def _check_piece(self, value: str, kind: str, command: str, flag: str) -> None:
        """Validate one value token against both mechanisms."""
        if kind == "path":
            # M1: the table says this token is a file the command will open.
            self._validate_path(value, what=f"value of {flag}")
        elif self._looks_like_a_path(value):
            # M2: path-shaped values are contained whatever the table said.
            self._validate_path(value, what=f"value of {flag}")

    def _validate_positionals(
        self, command: str, positionals: list[str], *, pattern_supplied: bool = False
    ) -> None:
        policy = _POSITIONAL_POLICY.get(command)
        if policy == "none":
            if positionals:
                raise ShellCommandError(f"Command takes no arguments: {command} {positionals[0]!r}")
            return
        if policy == "text":
            return  # already net-checked
        if policy == "paths_after_first":
            # grep: the first positional is the pattern and may be any regex —
            # but only when no pattern came from ``-e``/``-f``.  With a flag
            # pattern present grep reads *every* positional as a file, so
            # treating the first as a pattern would leave a real file argument
            # unvalidated and make the second mechanism carry the boundary.
            targets = positionals if pattern_supplied else positionals[1:]
        elif policy == "paths":
            targets = positionals
        else:  # pragma: no cover - defensive
            raise ShellCommandError(f"unknown positional policy {policy!r}")
        for target in targets:
            if command == "cat" and target == "-":
                continue  # stdin marker: opens nothing on the filesystem
            self._validate_path(target, what="positional argument")

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
        except (ShellCommandError, FilesystemBoundaryError) as exc:
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
