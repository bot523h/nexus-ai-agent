"""Independent reproducer for the ShellTool workspace-escape defect (task-201).

These tests are written *only* against the tool's documented contract — "Run a
restricted shell command (allowlisted, workspace-only)" — and the public
``ShellTool.execute`` entry point.  They deliberately import no symbol from the
fixed implementation, so the same file produces the RED evidence against the
unfixed validator and the GREEN evidence against the fixed one.

The contract under test is the one the module docstring on ``main`` at
``e5b326b`` states for itself:

    - Only commands on the ALLOWLIST may run.
    - Any argument that is (or resolves to) a path outside the workspace is
      rejected: absolute paths, ``..`` segments, and symlinks pointing out.

Every command used here is on that ALLOWLIST.  The defect: the validator
path-checked *positional* arguments only for ``ls``/``cat``/``find``, and for
``find``/``grep`` only a hand-picked list of exact flag spellings.  An
allowlisted binary whose file-reading flag was absent from that list therefore
reached the host filesystem with no containment check at all.

Measured on ``e5b326b2eaf691a638d030ad57acf1ce60016ef0`` before the fix:

    date -f <outside secret file>
      -> success=False, output="date: invalid date \\u2018SECRET-TOKEN-abc123\\u2019"
    date -f <outside file with three date lines>
      -> success=True, one output line per input line (a complete read)
    date -r <outside file>
      -> success=True, output="Mon Sep 28 17:36:04 UTC 2026" (existence+mtime oracle)
    grep --exclude-from=<outside file> -r .
      -> success=True, ran to completion

``cat`` of the very same path was rejected with "Path is outside the
workspace", which is what makes the asymmetry a boundary defect rather than an
intended capability.

Oracle choice
-------------
Most assertions below do *not* merely require ``success is False``.  Several of
these commands fail on their own once they run (``grep`` exits non-zero when a
pattern file yields no match), so a bare ``success is False`` assertion passes
against the *unfixed* validator for the wrong reason — it was in fact a weak
assertion in the first draft of this file and it is why the oracle is stronger
here.  The security property is that the command is refused *before anything is
executed*, so ``test_nothing_is_executed_*`` installs a tripwire over
``subprocess.run`` and fails if it is ever reached.  That holds for both
implementations and discriminates correctly.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.tools import system_shell
from nexus_ai_agent.tools.system_shell import ShellTool

#: Content placed *outside* the workspace.  Reaching any part of it proves the
#: containment boundary was crossed.
OUTSIDE_SECRET = "SECRET-TOKEN-abc123"

#: A file outside the workspace whose every line GNU ``date`` accepts, so that
#: ``date -f`` emits one output line per input line: proof of a *complete*
#: read, not merely of a diagnostic quoting one line.
OUTSIDE_DATE_FILE = "2026-01-02\n2026-03-04\n2026-05-06\n"


class _Executed(Exception):
    """Raised instead of running a subprocess: proves the vector never ran."""


@pytest.fixture()
def workspace(tmp_path):
    """A workspace plus two sensitive files that live outside of it."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "inside.txt").write_text("inside the workspace\n")

    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text(f"{OUTSIDE_SECRET}\n")
    outside_dates = tmp_path / "outside_dates.txt"
    outside_dates.write_text(OUTSIDE_DATE_FILE)

    return ws, outside_secret, outside_dates


@pytest.fixture()
def tripwire(monkeypatch):
    """Make ``subprocess.run`` explode if the validator lets a vector through."""

    def _boom(*args, **kwargs):
        raise _Executed(f"subprocess.run reached with argv={args[0] if args else kwargs!r}")

    monkeypatch.setattr(system_shell.subprocess, "run", _boom)
    return _boom


async def _run(ws, command: str) -> dict:
    tool = ShellTool(enable_shell=True, workspace_root=ws)
    return await tool.execute({"command": command})


# --------------------------------------------------------------------------- #
# content disclosure — the measured impact of the defect
# --------------------------------------------------------------------------- #
async def test_the_contract_holds_for_cat_so_it_is_the_control(workspace) -> None:
    """Control case: the documented rejection path, unchanged by this fix."""
    ws, outside_secret, _ = workspace
    result = await _run(ws, f"cat {outside_secret}")
    assert result["success"] is False
    assert "outside the workspace" in (result["error"] or "").lower()
    assert OUTSIDE_SECRET not in result["output"]


async def test_date_file_flag_cannot_disclose_an_outside_file(workspace) -> None:
    """DEFECT: ``date -f FILE`` made GNU date read any host file line by line.

    With parseable content it emits one line per input line (a complete read);
    with unparseable content it echoes the offending line verbatim inside its
    own diagnostic.  Either way a file outside the workspace was opened.
    """
    ws, outside_secret, outside_dates = workspace

    disclosed = await _run(ws, f"date -f {outside_secret}")
    assert OUTSIDE_SECRET not in disclosed["output"], (
        f"date -f disclosed the content of a file outside the workspace: {disclosed['output']!r}"
    )
    assert disclosed["success"] is False

    counted = await _run(ws, f"date -f {outside_dates}")
    assert counted["success"] is False, (
        "date -f read every line of a file outside the workspace and produced one "
        f"output line per line: {counted['output']!r}"
    )


async def test_date_long_file_flag_cannot_disclose_an_outside_file(workspace) -> None:
    """Same defect through the long spelling ``--file=``."""
    ws, outside_secret, _ = workspace
    result = await _run(ws, f"date --file={outside_secret}")
    assert OUTSIDE_SECRET not in result["output"]
    assert result["success"] is False


async def test_date_reference_flag_cannot_probe_an_outside_file(workspace) -> None:
    """``date -r FILE`` stats an arbitrary host file (existence + mtime oracle).

    Measured on the unfixed validator this returned the real mtime of the
    outside file, e.g. ``Mon Sep 28 17:36:04 UTC 2026``.
    """
    ws, outside_secret, _ = workspace
    result = await _run(ws, f"date -r {outside_secret}")
    assert result["success"] is False, (
        f"date -r probed a file outside the workspace: {result['output']!r}"
    )


# --------------------------------------------------------------------------- #
# the invariant that was actually broken: nothing unvetted is executed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "command",
    [
        "date -f {secret}",
        "date --file={secret}",
        "date --file {secret}",
        "date -r {secret}",
        "grep --file={secret} pattern",
        "grep -f{secret} .",
        "grep --exclude-from={secret} -r .",
        "cat /etc/passwd",
        "cat {secret}",
        "ls /etc",
        "find / -name '*.txt'",
        "find . -newer {secret}",
        "find . -fprintf {secret} '%p'",
    ],
)
async def test_nothing_unvetted_is_ever_executed(workspace, tripwire, command) -> None:
    """No vector that names an outside path may reach ``subprocess.run``.

    This is the strong oracle: it is unaffected by whether the binary would
    happen to exit non-zero, so it cannot pass for the wrong reason.
    """
    ws, outside_secret, _ = workspace
    argv = command.format(secret=outside_secret)
    tool = ShellTool(enable_shell=True, workspace_root=ws)
    result = await tool.execute({"command": argv})
    assert result["success"] is False, f"{argv!r} was executed: {result['output']!r}"
    assert OUTSIDE_SECRET not in result["output"]


@pytest.mark.parametrize(
    "command",
    [
        # a flag invented for this test stands in for a future coreutils release
        "date --nexus-invented-flag 1",
        "ls --nexus-invented-flag",
        "grep --nexus-invented-flag x inside.txt",
        "find . --nexus-invented-flag",
    ],
)
async def test_an_undeclared_flag_is_refused_not_ignored(workspace, tripwire, command) -> None:
    """An unknown flag must be refused, not silently skipped.

    The old validator stepped over any ``-``-leading token it did not
    recognise, which is precisely how an unheard-of file-reading option got
    through.  Fail-closed means the default answer is "no".

    The tripwire matters here: without it this assertion passes against the
    unfixed validator for the wrong reason, because the *binary* rejects its
    own unknown option and the tool reports that as ``success is False``.  A
    refusal that only happens inside the subprocess is not a boundary.
    """
    ws, _, _ = workspace
    tool = ShellTool(enable_shell=True, workspace_root=ws)
    result = await tool.execute({"command": command})
    assert result["success"] is False, f"undeclared flag was accepted: {result!r}"


# --------------------------------------------------------------------------- #
# no false green: the contained surface must keep working
# --------------------------------------------------------------------------- #
async def test_legitimate_in_workspace_usage_still_works(workspace) -> None:
    """The fix must not buy safety by breaking the documented surface."""
    ws, _, _ = workspace
    (ws / "notes.txt").write_text("hello world\n")

    listed = await _run(ws, "ls -l")
    assert listed["success"] is True, listed["error"]
    assert "notes.txt" in listed["output"]

    read = await _run(ws, "cat notes.txt")
    assert read["success"] is True
    assert "hello world" in read["output"]

    grepped = await _run(ws, "grep hello notes.txt")
    assert grepped["success"] is True
    assert "hello world" in grepped["output"]

    found = await _run(ws, "find . -name '*.txt'")
    assert found["success"] is True
    assert "notes.txt" in found["output"]

    echoed = await _run(ws, "echo hello")
    assert echoed["success"] is True
    assert echoed["output"].strip() == "hello"

    printed = await _run(ws, "date +%Y")
    assert printed["success"] is True
    assert len(printed["output"].strip()) == 4


def test_subprocess_is_never_run_through_a_shell() -> None:
    """Structural guard: the tool must keep handing over an argv list.

    ``shell=True`` would make every argument-grammar question moot in the
    worst way, so the call shape itself is part of the contract.
    """
    import inspect

    source = inspect.getsource(system_shell)
    assert "shell=True" not in source
