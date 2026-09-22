#!/usr/bin/env python3
"""Thin CLI over ``nexus_ai_agent.continuum.pack_coverage`` (Wave 5, step 8).

The measurement logic lives in the installed package — ``scripts/`` is not an
installed namespace (enforced by
``tests/architecture/test_scripts_import_boundary.py``) — so this file only
parses arguments, prints a table (or JSON) and translates the verdict into an
exit code.

Usage
-----

```bash
python scripts/pack_coverage.py                       # default pack test set, 95% bar
python scripts/pack_coverage.py --threshold 90 --json
python scripts/pack_coverage.py --tests tests/unit/test_opgap_wave5.py --pack core
python scripts/pack_coverage.py --pack-audio          # alias: --pack audio --pack core
```

Exit status: ``0`` every measured unit is at/above the threshold, ``1`` at least
one unit is below it, ``2`` bad CLI usage.  No third-party dependency is
required: the harness uses stdlib ``trace``/``dis`` only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # allow running from a source checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))

from nexus_ai_agent.continuum.pack_coverage import (  # noqa: E402
    DEFAULT_TEST_TARGETS,
    DEFAULT_THRESHOLD,
    coverage_failures,
    format_table,
    measure,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pack_coverage.py",
        description="Dependency-free line coverage for the Nagar capability packs.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"minimum acceptable coverage per pack (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--pack",
        action="append",
        dest="packs",
        metavar="NAME",
        help="restrict to one pack directory (repeatable); 'core' = the substrate files",
    )
    parser.add_argument(
        "--tests",
        nargs="*",
        default=None,
        help=f"test modules to run (default: {len(DEFAULT_TEST_TARGETS)} pack-focused modules)",
    )
    parser.add_argument(
        "--list-tests",
        action="store_true",
        help="print the default test target list and exit",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    parser.add_argument(
        "--json-out",
        metavar="PATH",
        help="also write the JSON report to PATH (useful in CI artefacts)",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress the human table")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_tests:
        for target in DEFAULT_TEST_TARGETS:
            print(target)
        return 0

    if args.threshold < 0:
        parser.error("--threshold must be >= 0")

    report = measure(
        args.tests,
        packs=args.packs,
        threshold=args.threshold,
    )
    payload = json.dumps(report.as_dict(), ensure_ascii=False, indent=2)

    if args.json_out:
        Path(args.json_out).write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
    if not args.quiet:
        print(format_table(report))

    failures = coverage_failures(report)
    for failure in failures:
        print(f"✗ {failure}", file=sys.stderr)
    if report.pytest_exit_code != 0:
        print(f"✗ pytest exited with {report.pytest_exit_code}", file=sys.stderr)
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
