#!/usr/bin/env python3
"""Adversarial mutation harness for the evidence and deployment guards.

Evidence rule, same as ``scripts/shell_sandbox_mutations.py`` and
``scripts/pack_trust_mutations.py``: a guard that has never been attacked is not
evidence.  Each mutation below breaks *one* load-bearing thing — the real
``docker-compose.yml``, a real architecture page, a real docstring, or the guard
logic itself — and re-runs the guard.  The guard must turn RED; if it stays green
the invariant is documentation rather than code, and the survivor is reported by
name (the run exits non-zero).

Mutating artifacts rather than only source is the point here: these guards read
files, so the honest question is not "does the code path exist" but "does a
one-line edit to the thing being guarded actually trip it".  Two mutants attack
the guard logic instead (``prose_literals_are_claims``,
``citation_guard_never_reports``): the guards *are* the code under test, and a
disabled comparison is the failure mode the red-proof tests exist to catch.

Usage::

    python scripts/docs_and_deploy_guard_mutations.py            # run every mutation
    python scripts/docs_and_deploy_guard_mutations.py --list

Every touched file is restored afterwards — including on failure — and the run
exits non-zero unless every mutant was killed.
"""

# ruff: noqa: E501 - the MUTATIONS table holds verbatim file snippets; they must
# match the shipped artifacts byte-for-byte, so they cannot be re-wrapped.
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Assembled at run time: this script is inside the citation guard's own scan
#: scope, so a literal absent path written here would make the guard report this
#: harness as a defect (the same reason ``tests/unit/test_docs_integrity.py``
#: builds its red-proof fixture the same way).
ABSENT_TEST = "tests/unit/" + "test_absent_guard_probe.py"

DOCS_INTEGRITY = "tests/unit/test_docs_integrity.py"
DEPLOY_GUARD = "tests/architecture/test_default_deployment_exposure.py"

COMPOSE = ROOT / "docker-compose.yml"
SECURITY = ROOT / "docs" / "architecture" / "SECURITY.md"
OVERVIEW = ROOT / "docs" / "architecture" / "OVERVIEW.md"
API_APP = ROOT / "src" / "nexus_ai_agent" / "api" / "app.py"

#: The shipped T3 evidence cell, kept verbatim so the mutation cannot drift.
T3_EVIDENCE = (
    "`tests/unit/test_dashboard_api.py` (payload keys are asserted), "
    "`tests/architecture/test_default_deployment_exposure.py` (the default publish is loopback-only)"
)


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    old: str
    new: str
    why: str
    tests: tuple[str, ...] = field(default=(DOCS_INTEGRITY,))


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "dashboard_port_published_on_every_interface",
        COMPOSE,
        '      - "127.0.0.1:8000:8000"',
        '      - "8000:8000"',
        "compose short syntax without a host IP binds 0.0.0.0",
        tests=(DEPLOY_GUARD,),
    ),
    Mutation(
        "dashboard_port_published_on_0_0_0_0",
        COMPOSE,
        '      - "127.0.0.1:8000:8000"',
        '      - "0.0.0.0:8000:8000"',
        "an explicit all-interfaces bind is the same exposure spelled out",
        tests=(DEPLOY_GUARD,),
    ),
    Mutation(
        "dashboard_port_long_syntax_without_host_ip",
        COMPOSE,
        '      - "127.0.0.1:8000:8000"',
        "      - target: 8000\n        published: 8000",
        "the long compose spelling must be read, not silently skipped",
        tests=(DEPLOY_GUARD,),
    ),
    Mutation(
        "another_service_publishes_a_port",
        COMPOSE,
        "  bot:\n    build: .",
        '  bot:\n    build: .\n    ports:\n      - "5432:5432"',
        "the rule covers every service, not only the dashboard",
        tests=(DEPLOY_GUARD,),
    ),
    Mutation(
        "quoted_test_module_count_goes_stale",
        OVERVIEW,
        "`tests/architecture/` (27 test modules)",
        "`tests/architecture/` (15 test modules)",
        "a quoted count is a claim and must resolve (docs/README.md rule 4)",
    ),
    Mutation(
        "security_page_cites_an_absent_test_file",
        SECURITY,
        T3_EVIDENCE,
        f"`tests/unit/test_dashboard_api.py`, `{ABSENT_TEST}`",
        "the threat table's evidence cell must not name a file that never existed",
    ),
    Mutation(
        "code_docstring_cites_an_absent_test_file",
        API_APP,
        "(tests/architecture/test_legacy_creative_boundary.py) and",
        f"({ABSENT_TEST}) and",
        "prose in code is a claim too, not only prose in Markdown",
    ),
    Mutation(
        "prose_literals_are_claims",
        ROOT / DOCS_INTEGRITY,
        '    if path.suffix != ".py":',
        "    if True:",
        "reading every line as prose flags three legitimate test fixtures: the split is the guard",
    ),
    Mutation(
        "citation_guard_never_reports",
        ROOT / DOCS_INTEGRITY,
        "            if cited not in existing:",
        "            if cited in set():",
        "a comparison that can never fire turns the guard into decoration",
    ),
)


def run_tests(nodes: tuple[str, ...]) -> bool:
    """True when the guard is GREEN for these nodes."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", *nodes],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        print(result.stdout[-2000:])
        print(result.stderr[-2000:])
    return result.returncode == 0


def apply_mutation(mutation: Mutation) -> str | None:
    """Rewrite the artifact; returns the original text, or None if it drifted."""
    text = mutation.path.read_text(encoding="utf-8")
    if mutation.old not in text:
        return None
    if text.count(mutation.old) != 1:
        return None
    mutation.path.write_text(text.replace(mutation.old, mutation.new, 1), encoding="utf-8")
    return text


def restore(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list mutations and exit")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name:48} {mutation.path.relative_to(ROOT)}")
        return 0

    if not run_tests(tuple(sorted({node for m in MUTATIONS for node in m.tests}))):
        print("BASELINE IS RED — refusing to measure mutants against a failing guard")
        return 2

    killed = 0
    survivors: list[str] = []
    for mutation in MUTATIONS:
        original = apply_mutation(mutation)
        if original is None:
            print(f"MUTATION DOES NOT APPLY (source drifted): {mutation.name}")
            return 1
        try:
            green = run_tests(mutation.tests)
        finally:
            restore(mutation.path, original)
        if green:
            survivors.append(mutation.name)
            print(f"SURVIVED  {mutation.name}  ({mutation.why})")
        else:
            killed += 1
            print(f"killed    {mutation.name}")

    if not run_tests(tuple(sorted({node for m in MUTATIONS for node in m.tests}))):
        print("TREE NOT RESTORED — the guard is still red after the run")
        return 1
    print(f"{killed}/{len(MUTATIONS)} killed")
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
