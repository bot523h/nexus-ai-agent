"""``python -m nexus_ai_agent.nagar`` — regenerate or verify the truth projection.

Commands
--------
(default)      recompute from the sources and write ``OPERATION_TRUTH.json``
``--docs``     also (re)write the generated Markdown projections
``--check``    recompute and exit non-zero when the stored JSON or any
               generated page differs from a fresh recomputation (the CI
               shape of the gate; never writes)

Nothing else can change the projection: there is no edit path, only
regeneration, which is what makes a hand-written number detectable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nexus_ai_agent.nagar import docs_projection, sources, truth


def _check(root: Path) -> int:
    fresh = truth.build_projection(root=root)
    exit_code = 0

    stored = truth.load_projection(root)
    if stored is None:
        print(f"STALE: {truth.PROJECTION_PATH} does not exist — run the generator")
        exit_code = 1
    else:
        findings = truth.compare(stored, fresh)
        for finding in findings:
            print(f"JSON DRIFT: {finding}")
        violations = truth.invariants(stored)
        for violation in violations:
            print(f"JSON INVARIANT: {violation}")
        if findings or violations:
            exit_code = 1

    fresh_violations = truth.invariants(fresh)
    for violation in fresh_violations:
        print(f"FRESH INVARIANT: {violation}")
    if fresh_violations:
        exit_code = 1

    for path, expected in docs_projection.render_all(fresh).items():
        target = root / path
        if not target.is_file():
            print(f"DOCS STALE: {path} does not exist — run with --docs")
            exit_code = 1
            continue
        actual = target.read_text(encoding="utf-8")
        if actual != expected:
            print(f"DOCS DRIFT: {path} differs from a fresh rendering")
            exit_code = 1

    if exit_code == 0:
        print("TRUTH GATE: GREEN — projection and generated docs match the sources")
    else:
        print("TRUTH GATE: RED — regenerate with `python -m nexus_ai_agent.nagar --docs`")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m nexus_ai_agent.nagar",
        description="Recompute, write, or verify the Nagar operation truth projection.",
    )
    parser.add_argument(
        "--docs",
        action="store_true",
        help="also regenerate the generated Markdown projections",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify stored artifacts against a fresh recomputation (no writes)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root to operate on (defaults to the checkout)",
    )
    args = parser.parse_args(argv)
    root = args.root if args.root is not None else sources.REPO_ROOT

    if args.check:
        return _check(root)

    projection = truth.build_projection(root=root)
    violations = truth.invariants(projection)
    if violations:
        for violation in violations:
            print(f"FRESH INVARIANT: {violation}")
        print("refusing to write: the recomputation violates structural truth rules")
        return 1
    path = truth.dump_projection(projection, root)
    print(f"wrote {path}")
    if args.docs:
        for written in docs_projection.write_docs(projection, root):
            print(f"wrote {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
