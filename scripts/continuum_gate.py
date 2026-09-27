#!/usr/bin/env python3
"""Thin CLI over ``nexus_ai_agent.continuum.gate`` (DECISION_LOG D-0023).

Clones the current commit, publishes a Continuum snapshot for it, proves that
``nexus continuum verify`` accepts it (control) and rejects every attack of the
threat model with the expected diagnosis.

```bash
python scripts/continuum_gate.py --out ci-artifacts/continuum-gate.json
```

Exit status: ``0`` only when the control and every scenario pass; ``1``
otherwise (including setup failures such as a shallow or dirty checkout).
The committed ``.nexus/continuum.json`` status is reported in the artifact
(``committed_snapshot``, ``blocking: false``) but never changes the exit code.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # allow running from a source checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))

from nexus_ai_agent.continuum.gate import (  # noqa: E402
    canonical_report,
    report_problems,
    run_gate,
    summary_lines,
)
from nexus_ai_agent.continuum.provenance import atomic_write_bytes  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="continuum_gate.py", description=__doc__)
    parser.add_argument("--out", metavar="PATH", help="write the canonical JSON report to PATH")
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else None
    if out is not None:
        out.unlink(missing_ok=True)  # never leave an older verdict behind

    report = run_gate(REPO_ROOT)
    payload = canonical_report(report)
    if out is not None:
        atomic_write_bytes(out, payload.encode("utf-8"))

    for line in summary_lines(report):
        print(line)
    for problem in report_problems(report):
        print(f"✗ {problem}", file=sys.stderr)
    passed = report["passed"] is True
    print(f"continuum gate @ {report['source_commit']}: {'PASSED' if passed else 'FAILED'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
