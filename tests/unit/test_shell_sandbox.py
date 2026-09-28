"""Workspace-sandbox tests for ShellTool.

The tool must never read or write outside the workspace root
(``NEXUS_WORKSPACE_ROOT``), for any allowlisted command.

The first half of this file is the Phase 0 behavioural suite.  The second half
is the adversarial suite added with ADR 0007: it exists because the claim above
was, for a while, *false* while every test stayed green.  Each attack test in
that half fails against the implementation that shipped before ADR 0007 — they
are regression tests for a measured defect, not illustrations.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.tools.system_shell import FlagKind, ShellTool


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """Workspace with a couple of files, plus a secret file outside it.

    ``escape_link`` and ``escape_dir`` point *out* of the workspace.  Any
    command that follows symlinks while walking finds them and can be steered
    outside, which is precisely what the symlink-safety tests exercise.
    """
    secret = tmp_path.parent / "outside_secret.env"
    secret.write_text("TOP_SECRET=1")
    (tmp_path / "notes.txt").write_text("hello world")
    # ``must_match.txt`` contains the outside secret's text.  A command that
    # reads an outside *pattern* file and matches against this file therefore
    # has to succeed — so an attack test asserting failure is leak-sensitive
    # rather than merely asserting "something went wrong".
    (tmp_path / "must_match.txt").write_text("TOP_SECRET=1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.txt").write_text("inner")
    (tmp_path / "escape_link").symlink_to(secret)
    (tmp_path / "inner_link").symlink_to(tmp_path / "notes.txt")
    (tmp_path / "sub" / "escape_dir").symlink_to(tmp_path.parent)
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


# ===========================================================================
# Adversarial suite (ADR 0007)
# ---------------------------------------------------------------------------
# Every test below fails against the pre-ADR-0007 implementation.  They were
# written from *measured* behaviour, not from reading the source: the commands
# in ``test_*_disclosure_*`` were run by hand against main@e6b06e0 and observed
# to print content that lives outside the workspace.
# ===========================================================================


def _leaked(result: dict) -> bool:
    """True when the tool surfaced the outside secret in any form."""
    blob = f"{result.get('output') or ''}{result.get('error') or ''}"
    return "TOP_SECRET" in blob


@pytest.mark.asyncio
async def test_date_file_flag_cannot_disclose_an_outside_file(ws) -> None:
    """Regression: ``date -f`` read an arbitrary file and echoed every line.

    GNU ``date -f FILE`` parses each line as a date and prints the offending
    line back inside ``date: invalid date '…'``.  Against the old validator the
    argument was never inspected (``date`` takes no positionally-checked path),
    so ``date -f /etc/passwd`` printed host file content to the agent.
    """
    tool = ShellTool(enable_shell=True)
    target = str(ws.parent / "outside_secret.env")
    for command in (f"date -f {target}", f"date --file={target}", "date -f escape_link"):
        result = await tool.execute({"command": command})
        assert result["success"] is False, command
        assert not _leaked(result), f"{command} disclosed outside content"


@pytest.mark.asyncio
async def test_date_reference_flag_cannot_probe_an_outside_file(ws) -> None:
    """Regression: ``date -r`` leaked existence and mtime of any host file."""
    tool = ShellTool(enable_shell=True)
    target = str(ws.parent / "outside_secret.env")
    for command in (f"date -r {target}", f"date --reference={target}"):
        result = await tool.execute({"command": command})
        assert result["success"] is False, command


@pytest.mark.asyncio
async def test_grep_pattern_file_is_checked_in_every_spelling(ws) -> None:
    """Regression: only the bare ``-f`` spelling was path-checked.

    ``grep -fFILE`` (attached value) and ``grep --file=FILE`` bypassed the
    validator entirely, because it inspected tokens positionally rather than
    by what the flag consumes.
    """
    tool = ShellTool(enable_shell=True)
    target = str(ws.parent / "outside_secret.env")
    # ``must_match.txt`` holds the outside file's text, so a command that really
    # consumed the outside file as its pattern list would MATCH and succeed.
    # Asserting failure here is therefore evidence of containment, not just of a
    # non-zero exit status.
    for command in (
        f"grep -f {target} must_match.txt",
        f"grep -f{target} must_match.txt",
        f"grep --file={target} must_match.txt",
        f"grep --exclude-from={target} TOP_SECRET notes.txt",
        "grep -f escape_link must_match.txt",
    ):
        result = await tool.execute({"command": command})
        assert result["success"] is False, f"{command} consumed an outside file"
        assert not _leaked(result), command

    # Control: the same query against an in-workspace pattern file must work,
    # proving the fixture would have detected the leak above.
    inside = ws / "patterns.txt"
    inside.write_text("TOP_SECRET\n")
    result = await tool.execute({"command": "grep -f patterns.txt must_match.txt"})
    assert result["success"] is True, result["error"]


@pytest.mark.asyncio
async def test_grep_recursive_follows_symlinks_is_refused(ws) -> None:
    """Measured escape: ``grep -R`` follows symlinks met during the walk.

    With ``ws/escape_link -> outside_secret.env`` present, ``grep -R TOP_SECRET
    .`` printed ``./escape_link:TOP_SECRET=1``.  No path validation can stop
    this — the *flag* is the escape — so the flag must be refused.  Its safe
    sibling ``-r`` (which does not follow in-tree symlinks) stays allowed.
    """
    tool = ShellTool(enable_shell=True)
    for command in (
        "grep -R TOP_SECRET .",
        "grep -R . .",
        "grep --dereference-recursive TOP_SECRET .",
    ):
        result = await tool.execute({"command": command})
        assert result["success"] is False, command
        assert "not allowed" in (result["error"] or "")
        assert not _leaked(result), f"{command} followed a symlink out"

    allowed = await tool.execute({"command": "grep -r TOP_SECRET ."})
    assert "not allowed" not in (allowed["error"] or "")


@pytest.mark.asyncio
async def test_ls_dereference_is_refused(ws) -> None:
    """``ls -L``/``-H``/``--dereference`` follow symlinks out of the workspace."""
    tool = ShellTool(enable_shell=True)
    for command in ("ls -L sub", "ls -H sub", "ls --dereference sub"):
        result = await tool.execute({"command": command})
        assert result["success"] is False, command
        assert "not allowed" in (result["error"] or "")


@pytest.mark.asyncio
async def test_find_follow_links_is_refused(ws) -> None:
    """``find -L`` follows symlinks out; the default ``-P`` does not."""
    tool = ShellTool(enable_shell=True)
    for command in (
        "find -L . -type f",
        "find -H . -type f",
        "find -P . -type f",
        "find . -follow -type f",
    ):
        result = await tool.execute({"command": command})
        assert result["success"] is False, command


@pytest.mark.asyncio
async def test_symlinked_file_inside_the_workspace_is_never_read(ws) -> None:
    """A symlink inside the workspace must not become a read primitive."""
    tool = ShellTool(enable_shell=True)
    for command in ("cat escape_link", "cat -n escape_link", "find . -newer escape_link"):
        result = await tool.execute({"command": command})
        assert result["success"] is False, command
        assert not _leaked(result), f"{command} read through a symlink"


@pytest.mark.asyncio
async def test_symlink_pointing_inside_the_workspace_is_also_refused(ws) -> None:
    """A link is refused even when its target is a legitimate workspace file.

    This is the only case that distinguishes the symlink-component check from
    the "resolved path is still under the root" check: an inside-pointing link
    resolves *inside*, so only the no-symlinks rule rejects it.

    It is load-bearing rather than ceremonial.  Validation and use are separate
    syscalls, so for the shell tool the promise "this argument was checked" is
    only worth something if the checked path cannot be re-pointed between the
    check and the ``execve``.  Allowing links through would make the boundary
    movable; ``WorkspaceFilesystem.read_text`` hides that by also passing
    ``O_NOFOLLOW``, which is why this has to be asserted from the shell side.
    """
    tool = ShellTool(enable_shell=True)
    for command in ("cat inner_link", "cat -n inner_link", "ls inner_link"):
        result = await tool.execute({"command": command})
        assert result["success"] is False, (
            f"{command} resolved through a symlink instead of being refused"
        )


@pytest.mark.asyncio
async def test_absolute_paths_are_refused_in_every_position(ws) -> None:
    """The boundary holds wherever the token sits and however it is spelled."""
    tool = ShellTool(enable_shell=True)
    outside = "/etc/passwd"
    commands = [
        f"ls {outside}",
        f"cat {outside}",
        f"ls -l {outside}",
        f"grep hello {outside}",
        f"grep hello -- {outside}",
        f"find {outside}",
        f"find . -newer {outside}",
        f"find . -print0 -fprint {outside}",
        f"find . -fprintf {outside} '%p'",
        f"echo {outside}",
        f"date {outside}",
        f"cat sub/../../{outside.lstrip('/')}",
    ]
    for command in commands:
        result = await tool.execute({"command": command})
        assert result["success"] is False, command
        assert not _leaked(result), command


@pytest.mark.asyncio
async def test_grep_positional_policy_follows_the_flags_actually_seen(ws) -> None:
    """``-e``/``-f`` hand grep the pattern, so *every* positional is a file.

    A fixed "the first positional is a pattern" rule is wrong whenever the
    pattern came from a flag: ``grep -e hello /etc/passwd`` would then treat
    ``/etc/passwd`` as a regex and never validate it as a path.  The path net
    (M2) happens to catch an absolute or traversing value, so the defect would
    be invisible here — but it would make the net, not the grammar, the thing
    holding the boundary, and the net cannot see a bare relative name.
    """
    tool = ShellTool(enable_shell=True)
    outside = str(ws.parent / "outside_secret.env")
    for command in (
        f"grep -e hello {outside}",
        f"grep --regexp=hello {outside}",
        f"grep -f patterns.txt {outside}",
        "grep -e hello ../outside_secret.env",
        "grep -f patterns.txt escape_link",
    ):
        result = await tool.execute({"command": command})
        assert result["success"] is False, command
        assert "Exit" not in (result["error"] or ""), (
            f"{command} reached grep instead of being refused"
        )

    # The control: the same shapes with an in-workspace file still work.
    inside = ws / "patterns.txt"
    inside.write_text("hello\n")
    for command in ("grep -e hello notes.txt", "grep -f patterns.txt notes.txt"):
        result = await tool.execute({"command": command})
        assert result["success"] is True, (command, result["error"])


@pytest.mark.asyncio
async def test_undeclared_flags_are_refused(ws) -> None:
    """Fail-closed default: an unknown flag is refused, not passed through.

    This is what makes the grammar safe against options that do not exist yet.
    """
    tool = ShellTool(enable_shell=True)
    for command in (
        "ls --nexus-unknown",
        "ls -Z",
        "cat --codec=utf-8",
        "date --resolution",
        "date --debug",
        "grep --nexus-unknown hello notes.txt",
        "find . -nexuspredicate",
        "find . -files0-from /dev/null",
    ):
        result = await tool.execute({"command": command})
        assert result["success"] is False, command
        assert result["error"], command


# ---------------------------------------------------------------------------
# Structural invariants over the grammar itself
# ---------------------------------------------------------------------------


def test_every_allowlisted_command_has_a_declared_grammar() -> None:
    """A command may not be runnable without a table to judge its arguments."""
    for command in ShellTool.ALLOWLIST:
        assert command in ShellTool.FLAGS, command
        assert command in ShellTool.BUNDLED_SHORT_FLAGS or command == "find"


@pytest.mark.asyncio
async def test_every_declared_path_flag_is_validated(ws) -> None:
    """Executable completeness proof for the primary mechanism.

    For *every* flag the tables declare as ``path`` — including the ``tuple``
    ones, whose first token is a path — passing it an outside path must be
    refused.  A flag added to a table without wiring is therefore either
    validated or this test fails; there is no third outcome.
    """
    tool = ShellTool(enable_shell=True)
    outside = str(ws.parent / "outside_secret.env")
    checked = 0
    for command, table in ShellTool.FLAGS.items():
        for flag, kind in table.items():
            if kind is FlagKind.PATH:
                pieces = [flag, outside]
            elif kind is FlagKind.TUPLE:
                pieces = [flag, outside, "%p"]
            else:
                continue
            command_line = " ".join([command, *pieces])
            result = await tool.execute({"command": command_line})
            assert result["success"] is False, command_line
            assert not _leaked(result), command_line
            checked += 1
    # Guards against the sweep silently shrinking to nothing.
    assert checked >= 15, f"only {checked} path flags were exercised"


def test_no_declared_flag_follows_symlinks() -> None:
    """Symlink-following options are refused by construction, not by accident.

    Each of these takes no path argument, so the tables cannot contain them and
    the path validator could never contain the resulting read.
    """
    follows = {
        "ls": {
            "-L",
            "-H",
            "--dereference",
            "--dereference-command-line",
            "--dereference-command-line-symlink-to-dir",
        },
        "grep": {"-R", "--dereference-recursive"},
        "find": {"-L", "-H", "-P", "-follow"},
    }
    for command, flags in follows.items():
        for flag in flags:
            assert flag not in ShellTool.FLAGS[command], (
                f"{command} {flag} must never be declared safe: it follows symlinks"
            )


# ---------------------------------------------------------------------------
# The grammar must not over-refuse: the documented legitimate surface
# ---------------------------------------------------------------------------

#: Commands the tool is expected to support, with the workspace fixture's files.
#: A failure here means the security grammar has started breaking real work,
#: which is the other way a "restricted shell" can fail its purpose.
_LEGITIMATE_COMMANDS: tuple[str, ...] = (
    "ls",
    "ls -la",
    "ls -l sub",
    "ls -1 .",
    "ls --all",
    "ls -R .",
    "ls --color=never .",
    "ls --color .",
    "ls -I '*.tmp' .",
    "pwd",
    "pwd -P",
    "echo hello world",
    "echo -n hi",
    "cat notes.txt",
    "cat -n notes.txt",
    "cat sub/inner.txt",
    "cat -",
    "grep hello notes.txt",
    "grep -n hello notes.txt",
    "grep -rn hello .",
    "grep -r hello .",
    "grep --color=never hello notes.txt",
    "grep -A2 hello notes.txt",
    "grep -A 2 hello notes.txt",
    "grep -e hello notes.txt",
    "grep -m1 hello notes.txt",
    "grep -c hello notes.txt",
    "grep -w hello notes.txt",
    "grep -il hello notes.txt",
    "find . -name notes.txt",
    "find . -type f",
    "find . -type f -print0",
    "find . -maxdepth 1 -type f",
    "find . -name '*.txt' -o -name '*.md'",
    "find . -name notes.txt -a -type f",
    "find . -not -type d",
    "find . -printf '%p\\n'",
    "find . -fprintf out.txt '%p\\n'",
    "find . -type f -size +1c",
    "date",
    "date -u",
    "date -R",
    "date -I",
    "date -Iseconds",
    "date --rfc-3339=seconds",
    "date +%Y-%m-%d",
    "date +%Y/%m/%d",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", _LEGITIMATE_COMMANDS)
async def test_the_documented_surface_still_works(ws, command: str) -> None:
    """Every command here must reach the subprocess (its own exit code is fine)."""
    tool = ShellTool(enable_shell=True)
    result = await tool.execute({"command": command})
    error = result["error"] or ""
    assert "not allowed" not in error, f"{command} was refused: {error}"
    assert "outside the workspace" not in error, f"{command} was refused: {error}"
    assert "requires a value" not in error, f"{command} lost a flag value: {error}"
    assert "Unexpected token" not in error, f"{command} was mis-parsed: {error}"


@pytest.mark.asyncio
async def test_optional_value_does_not_swallow_the_next_token(ws) -> None:
    """``ls --color`` must not eat ``-l``, and ``date -I`` must not eat a format.

    An option with an *optional* value that consumed the following token would
    silently reinterpret a real flag (or path) as that option's argument.
    """
    tool = ShellTool(enable_shell=True)

    result = await tool.execute({"command": "ls --color -l notes.txt"})
    assert result["success"] is True, result["error"]
    assert "notes.txt" in result["output"]

    # ``grep --color -n``: if ``--color`` swallowed ``-n``, grep would report a
    # missing operand instead of matching (and ``-n`` would be reinterpreted).
    result = await tool.execute({"command": "grep --color -n hello notes.txt"})
    assert result["success"] is True, result["error"]
    assert result["output"].startswith("1:"), result["output"]

    # ``date -I -u``: GNU date rejects ``-I`` combined with an explicit format
    # ("multiple output formats specified"), so a zero exit proves ``-I`` did
    # not consume ``-u`` as its optional value.
    result = await tool.execute({"command": "date -I -u"})
    assert result["success"] is True, result["error"]

    # The attached spelling must still be honoured without demanding a value.
    result = await tool.execute({"command": "date -Iseconds"})
    assert result["success"] is True, result["error"]
    assert result["output"].strip().startswith("20"), result["output"]


@pytest.mark.asyncio
async def test_nothing_undeclared_reaches_the_subprocess(ws) -> None:
    """The fail-closed default applies to *token positions*, not just to flags.

    A token the grammar cannot account for must be refused rather than passed
    through: passing it through is how "the tool validates arguments" quietly
    becomes "the tool validates the arguments it happened to think about".

    The discriminator matters.  Several of these commands also exit non-zero on
    their own (``find`` rejects a path after the expression, ``pwd`` rejects an
    unknown option), so asserting only ``success is False`` would pass even if
    the token had been handed to the subprocess.  The tool's refusals never
    contain ``Exit``; only a command that actually ran reports an exit status.
    """
    tool = ShellTool(enable_shell=True)
    for command in (
        "find . -type f notes.txt",
        "find . -name notes.txt stray.txt",
        "pwd -x",
        "pwd extra",
        "ls --all=yes",
        "grep -m",
        "ls -w",
    ):
        result = await tool.execute({"command": command})
        assert result["success"] is False, command
        assert "Exit" not in (result["error"] or ""), (
            f"{command} reached the subprocess instead of being refused"
        )


@pytest.mark.asyncio
async def test_the_grep_positional_policy_can_actually_fail(ws) -> None:
    """Red-proof for the policy switch above.

    Without the flag-aware rule, ``grep -e hello <outside>`` validates the
    outside path as a *pattern* and lets it through; this test pins that the
    refusal is produced by the grammar rather than by luck of the path net.
    """
    tool = ShellTool(enable_shell=True)
    outside = str(ws.parent / "outside_secret.env")
    result = await tool.execute({"command": f"grep -e hello {outside}"})
    assert "positional argument" in (result["error"] or ""), (
        "the outside path must be refused as a positional, not as a pattern"
    )
