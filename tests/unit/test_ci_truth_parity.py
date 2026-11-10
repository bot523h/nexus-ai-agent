"""CI rail parity — the local truth gate must mirror the CI gate (task-282/PR #330).

PR #330 added ``tests/unit/test_ibpc_delta.py``, which names a creative operation
id.  ``nagar.sources.read_suite_evidence`` scans ``tests/**/*.py`` by ``rglob``, so a
fresh recomputation immediately listed that file under
``timeline.trim.suite_reference_files`` and ``proof.suite_executed`` — but the
committed ``OPERATION_TRUTH.json`` projection was never regenerated.  The result:
``test`` and ``python-parity`` (3.10/3.11/3.12) went red on an **already-pushed**
SHA.  Nothing local caught it, because no local rail ran the gate.

This module makes the rail mechanical, in the same spirit as
``tests/unit/test_ci_lint_parity.py``:

1. ``make truth-check`` exists and runs exactly ``python -m nexus_ai_agent.nagar
   --check`` — the same, never-writing, exit-non-zero-on-drift shape CI uses.  A
   future edit that repoints or deletes the target is a red build here, not a
   silent hole that only surfaces after a push.
2. The ``--check`` command really is fail-closed: a drifted projection exits
   non-zero, and a clean one exits zero.  Proven on the live tree, not a mock.

The Makefile is scanned as text on purpose: no YAML/Make parser is imported, so
this module keeps running in the core-only install.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
MAKEFILE = REPO_ROOT / "Makefile"

#: The exact command the CI gate uses; the Makefile target must match it verbatim.
GATE_COMMAND = "python -m nexus_ai_agent.nagar --check"

_TARGET = re.compile(r"^truth-check:[ \t]*$", re.MULTILINE)


def _truth_check_recipe(makefile_text: str) -> str:
    """The recipe lines (tab-indented) directly under the ``truth-check:`` target."""
    lines = makefile_text.splitlines()
    recipe: list[str] = []
    inside = False
    for line in lines:
        if _TARGET.match(line):
            inside = True
            continue
        if inside:
            if line.startswith("\t"):
                recipe.append(line[1:].strip())
            elif line.strip() == "" or line.startswith("#"):
                continue
            else:
                break
    return "\n".join(recipe)


def test_makefile_declares_truth_check_target() -> None:
    recipe = _truth_check_recipe(MAKEFILE.read_text(encoding="utf-8"))
    assert recipe, "Makefile has no `truth-check` target with a recipe"
    assert GATE_COMMAND in recipe, f"`make truth-check` must run {GATE_COMMAND!r}; found {recipe!r}"


def test_truth_check_target_is_phony() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    phony = next(
        (ln for ln in text.splitlines() if ln.startswith(".PHONY:")),
        "",
    )
    assert "truth-check" in phony.split(), (
        "`truth-check` must be declared .PHONY so a file named `truth-check` "
        "cannot silently no-op the rail"
    )


def test_check_command_is_fail_closed_on_a_clean_tree() -> None:
    """The gate exits 0 when the projection matches the sources (live tree)."""
    proc = subprocess.run(
        [sys.executable, "-m", "nexus_ai_agent.nagar", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        "the stored projection must match a fresh recomputation; "
        f"stdout/stderr:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "GREEN" in proc.stdout
