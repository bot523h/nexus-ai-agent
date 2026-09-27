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
python scripts/pack_coverage.py                          # canonical run, 95% per-pack contract
python scripts/pack_coverage.py --json-out cov.json      # + canonical SHA-bound artifact
python scripts/pack_coverage.py --verify-artifact cov.json  # re-measure; byte-for-byte
python scripts/pack_coverage.py --threshold 85 --json    # diagnostic only: never accepted
python scripts/pack_coverage.py --tests tests/unit/test_opgap_wave5.py --pack core  # diagnostic
python scripts/pack_coverage.py --list-tests
```

Exit status
-----------

* ``0`` — ACCEPTED: the canonical target set ran in full on a clean commit, the
  whole pack surface was measured, and every pack is >= 95%
  (``ACCEPTANCE_THRESHOLD``); for ``--verify-artifact``: the stored artifact was
  reproduced byte for byte by a fresh accepted measurement.
* ``1`` — NOT ACCEPTED: below the bar, incomplete/diagnostic evidence (partial
  targets or packs, custom root, lowered threshold, dirty tree, failed or
  deselected tests), evidence unavailable, or an artifact that does not
  reproduce.
* ``2`` — invalid request (bad CLI usage, NaN/negative threshold, unknown pack,
  missing or duplicate target).

No third-party dependency is required: the harness uses stdlib ``trace``/``dis``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # allow running from a source checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))

from nexus_ai_agent.continuum.pack_coverage import (  # noqa: E402
    ACCEPTANCE_THRESHOLD,
    DEFAULT_TEST_TARGETS,
    DEFAULT_THRESHOLD,
    canonical_json,
    coverage_failures,
    format_table,
    measure,
    verify_artifact,
)
from nexus_ai_agent.continuum.provenance import atomic_write_bytes  # noqa: E402

EXIT_ACCEPTED = 0
EXIT_NOT_ACCEPTED = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pack_coverage.py",
        description="Dependency-free line coverage evidence for the Nagar capability packs.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=(
            f"per-pack bar (default and acceptance contract: {ACCEPTANCE_THRESHOLD}); "
            "a lower value is a diagnostic run that is never accepted"
        ),
    )
    parser.add_argument(
        "--pack",
        action="append",
        dest="packs",
        metavar="NAME",
        help="restrict to one pack directory (repeatable; diagnostic); 'core' = the substrate",
    )
    parser.add_argument(
        "--tests",
        nargs="*",
        default=None,
        help=f"test targets to run (diagnostic; default: the {len(DEFAULT_TEST_TARGETS)} "
        "canonical modules)",
    )
    parser.add_argument(
        "--list-tests",
        action="store_true",
        help="print the canonical test target list and exit",
    )
    parser.add_argument("--json", action="store_true", help="canonical JSON report on stdout")
    parser.add_argument(
        "--json-out",
        metavar="PATH",
        help="also write the canonical JSON artifact to PATH (removed first; written atomically)",
    )
    parser.add_argument(
        "--verify-artifact",
        metavar="PATH",
        help="re-measure canonically and require PATH to be reproduced byte for byte",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress the human table")
    return parser


def _verify(path: Path) -> int:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        print(f"✗ artifact unreadable: {exc}", file=sys.stderr)
        return EXIT_NOT_ACCEPTED
    problems = verify_artifact(raw)
    for problem in problems:
        print(f"✗ {problem}", file=sys.stderr)
    if problems:
        return EXIT_NOT_ACCEPTED
    print(f"✓ {path} reproduced byte-for-byte by an accepted measurement")
    return EXIT_ACCEPTED


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_tests:
        for target in DEFAULT_TEST_TARGETS:
            print(target)
        return EXIT_ACCEPTED

    if args.verify_artifact:
        if args.tests is not None or args.packs or args.json_out:
            parser.error(
                "--verify-artifact re-measures canonically; drop --tests/--pack/--json-out"
            )
        return _verify(Path(args.verify_artifact))

    # A stale artifact from an earlier run must never survive a failed run.
    json_out = Path(args.json_out) if args.json_out else None
    if json_out is not None:
        json_out.unlink(missing_ok=True)

    try:
        report = measure(args.tests, packs=args.packs, threshold=args.threshold)
    except (TypeError, ValueError) as exc:
        print(f"✗ invalid measurement request: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (OSError, RuntimeError) as exc:
        print(f"✗ coverage evidence unavailable: {exc}", file=sys.stderr)
        return EXIT_NOT_ACCEPTED

    payload = canonical_json(report)
    if json_out is not None:
        atomic_write_bytes(json_out, payload.encode("utf-8"))
    if args.json:
        sys.stdout.write(payload)
    if not args.quiet:
        print(format_table(report))

    failures = coverage_failures(report)
    for failure in failures:
        print(f"✗ {failure}", file=sys.stderr)
    return EXIT_NOT_ACCEPTED if failures else EXIT_ACCEPTED


if __name__ == "__main__":
    raise SystemExit(main())
