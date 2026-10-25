#!/usr/bin/env python3
"""Thin CLI over ``nexus_ai_agent.continuum.mutations`` (DECISION_LOG D-0023).

Replays the Continuum mutation campaign: every catalogued mutation is applied,
its killing tests run in a fresh interpreter, and the file is restored and
checked byte for byte.

```bash
python scripts/continuum_mutations.py --out ci-artifacts/mutations.json
python scripts/continuum_mutations.py --only S9 T2      # replay single records
python scripts/continuum_mutations.py --list
```

Exit status: ``0`` only when the baseline is green and every applicable
mutation is killed (pytest exit 1) and restored; ``1`` otherwise.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # allow running from a source checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))

from nexus_ai_agent.continuum.mutations import CATALOG, canonical_report, run_campaign  # noqa: E402
from nexus_ai_agent.continuum.provenance import atomic_write_bytes  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="continuum_mutations.py", description=__doc__)
    parser.add_argument("--out", metavar="PATH", help="write the canonical JSON record to PATH")
    parser.add_argument("--only", nargs="+", metavar="ID", help="replay only these mutation ids")
    parser.add_argument("--list", action="store_true", help="list the catalogue and exit")
    args = parser.parse_args(argv)

    if args.list:
        for mutation in CATALOG:
            print(f"{mutation.id:4} {mutation.family:12} {mutation.file}  {mutation.rationale}")
        return 0

    out = Path(args.out) if args.out else None
    if out is not None:
        out.unlink(missing_ok=True)  # never leave an older verdict behind
    report = run_campaign(REPO_ROOT, only=args.only)
    if out is not None:
        atomic_write_bytes(out, canonical_report(report).encode("utf-8"))

    records = report.get("mutations")
    for record in records if isinstance(records, list) else []:
        print(
            f"{record['status']:14} {record['id']:4} exit={record['observed_exit']} "
            f"restored={record['restored']} {record['rationale']}"
        )
    problems = report.get("problems")
    for problem in problems if isinstance(problems, list) else []:
        print(f"✗ {problem}", file=sys.stderr)
    passed = report.get("passed") is True
    print(f"mutation campaign @ {report.get('commit')}: {'PASSED' if passed else 'FAILED'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
