"""CLI for the strict, machine-readable Agent assurance gate."""

from __future__ import annotations

import argparse
import sys

from nexus_ai_agent.evaluation.corpus import (
    ADVERSARIAL_CASES,
    AGENT_FOUNDATION_SUITE,
    FAILURE_CASES,
    GOLDEN_CASES,
)
from nexus_ai_agent.evaluation.domain import canonical_json, is_subject_sha
from nexus_ai_agent.evaluation.runner import run_suite


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nexus_ai_agent.evaluation",
        description=(
            "Run the independent Agent assurance gate. Without an explicitly "
            "injected subject adapter, the gate fails closed as BLOCKED."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gate = subparsers.add_parser("gate", help="evaluate a subject or report the missing adapter")
    gate.add_argument(
        "--subject-sha", required=True, help="40- or 64-character lowercase commit SHA"
    )

    subparsers.add_parser("corpus", help="print deterministic suite and case identities")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "corpus":
        print(
            canonical_json(
                {
                    "suite": AGENT_FOUNDATION_SUITE.suite_id,
                    "version": AGENT_FOUNDATION_SUITE.version,
                    "suite_digest": AGENT_FOUNDATION_SUITE.suite_digest,
                    "golden_count": len(GOLDEN_CASES),
                    "adversarial_count": len(ADVERSARIAL_CASES),
                    "failure_count": len(FAILURE_CASES),
                    "case_ids": [case.case_id for case in AGENT_FOUNDATION_SUITE.cases],
                }
            )
        )
        return 0

    if args.command == "gate":
        if not is_subject_sha(args.subject_sha):
            print(
                "AGENT_ASSURANCE_GATE = FAIL\ninvalid --subject-sha",
                file=sys.stderr,
            )
            return 2
        run = run_suite(AGENT_FOUNDATION_SUITE, subject_sha=args.subject_sha, adapter=None)
        report = run.report()
        print(f"AGENT_ASSURANCE_GATE = {report.verdict.value}", file=sys.stderr)
        print(report.to_json())
        return 0 if report.verdict.value == "PASS" else 1

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
