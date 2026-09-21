"""No test may import the unpackaged ``scripts`` namespace.

CI executes the console-script ``pytest`` entrypoint, where the repository
root is NOT on ``sys.path``; ``scripts/`` has no ``__init__.py`` and is not
an installed package (src-layout installs only ``nexus_ai_agent``).  A test
importing the scripts namespace therefore passes under ``python -m pytest``
(CWD gets injected into ``sys.path[0]``) and fails in CI with
``ModuleNotFoundError`` — an environment-dependent red gate.

That asymmetry cost a full red CI job on PR#40 (2026-09-21); the full
causal chain and the A/B reproduction live in
``docs/audits/FORENSIC_PR40_CI_2026-09-21.md``.

Contract: reusable logic lives inside the installed ``nexus_ai_agent``
package; files under ``scripts/`` are thin CLIs over it.  This guard keeps
that boundary mechanical: any ``from scripts...`` / ``import scripts``
statement inside ``tests/`` turns this suite red with the offending paths.
"""

from __future__ import annotations

import re
from pathlib import Path

_TESTS_DIR = Path(__file__).parents[1]

#: Anchored statement-level match only (leading whitespace allowed), so
#: prose in docstrings/comments can never trigger a false positive.
_SCRIPTS_IMPORT = re.compile(r"^\s*(?:from|import)\s+scripts\b", re.MULTILINE)


def test_no_test_imports_scripts_namespace() -> None:
    offenders: list[str] = []
    for path in sorted(_TESTS_DIR.rglob("*.py")):
        if path.resolve() == Path(__file__).resolve():
            continue
        hits = _SCRIPTS_IMPORT.findall(path.read_text(encoding="utf-8"))
        if hits:
            offenders.append(f"{path.relative_to(_TESTS_DIR)}: {len(hits)} import statement(s)")
    assert offenders == [], (
        "tests/ must not import the unpackaged scripts/ namespace — "
        f"use the installed nexus_ai_agent package instead: {offenders}"
    )
