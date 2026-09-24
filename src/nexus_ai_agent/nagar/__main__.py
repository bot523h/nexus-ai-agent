"""``python -m nexus_ai_agent.nagar`` — regenerate, or check, Operation Truth.

The generation command recorded in the projection header is this module.  It is
deliberately tiny: all logic lives in :mod:`nexus_ai_agent.nagar.truth`, because
``tests/architecture/test_scripts_import_boundary.py`` forbids tests from
importing the unpackaged ``scripts/`` namespace — reusable logic must be an
installed module.

Usage::

    python -m nexus_ai_agent.nagar --write     # regenerate OPERATION_TRUTH.json
    python -m nexus_ai_agent.nagar --check     # exit 1 if it drifted (CI mode)
    python -m nexus_ai_agent.nagar            # print the summary
"""

from __future__ import annotations

import sys

from nexus_ai_agent.nagar import truth


def _print_summary() -> None:
    fresh = truth.build_projection()
    reconciliation = fresh["reconciliation"]
    print(f"schema            {fresh['schema']}")
    print(f"catalog           {reconciliation['catalog_count']}")
    print(f"runtime           {reconciliation['runtime_count']}")
    print(f"overlap           {reconciliation['overlap_count']}")
    print(f"missing           {reconciliation['missing_count']}")
    print(f"runtime_only      {reconciliation['runtime_only_count']}")
    print(f"universe          {reconciliation['universe_count']}")
    print(f"surface (derived) {reconciliation['surface_count']}")
    print()
    proven = [node for node in fresh["operations"] if node["status"]["artifact_proven"]]
    print(f"artifact-proven   {len(proven)}  {[node['operation_id'] for node in proven]}")
    surface = fresh["surface"]
    print(f"surface commands  {surface['commands']}")
    print(
        "probes            "
        + ", ".join(f"{name}={data['count']}" for name, data in surface["probes"].items())
    )
    print(
        f"drift findings    {len(surface['disagreements'])} surface, "
        f"{len(fresh['contract_drift'])} recorded"
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if "--write" in args:
        path = truth.write_projection()
        print(f"wrote {path.relative_to(truth.sources.REPO_ROOT)}")
        return 0

    if "--check" in args:
        projection = truth.load_projection()
        if projection is None:
            print(
                f"{truth.PROJECTION_PATH} is missing — run `{truth.GENERATION_COMMAND} --write`",
                file=sys.stderr,
            )
            return 1
        findings = truth.compare(projection, truth.build_projection())
        if findings:
            print(
                f"Operation Truth drifted ({len(findings)} finding(s)) — the sources no "
                f"longer match {truth.PROJECTION_PATH}:",
                file=sys.stderr,
            )
            for finding in findings:
                print(f"  {finding}", file=sys.stderr)
            print(
                f"\nIf the change is intended, regenerate with "
                f"`{truth.GENERATION_COMMAND} --write` and review the diff.",
                file=sys.stderr,
            )
            return 1
        print(f"Operation Truth is consistent with {truth.PROJECTION_PATH}")
        return 0

    _print_summary()
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    raise SystemExit(main())
