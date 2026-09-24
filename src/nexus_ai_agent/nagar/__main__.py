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

from nexus_ai_agent.nagar import sources, truth

#: Exit code for "the sources themselves could not be read" — deliberately
#: distinct from 1 ("the sources loaded, and they disagree with the
#: projection").  A CI consumer can then tell a broken tree from a drifted
#: projection without parsing stderr.
EXIT_SOURCE_UNREADABLE = 2


def _load_error(exc: Exception) -> int:
    """Render a source-level load failure as a named, actionable finding.

    A traceback here is technically still a red build, but it names a Python
    frame rather than *the rule the tree broke*: ``duplicate operation: 'x'``
    is the finding, ``ValueError`` is an implementation detail.  A duplicate
    pack registration is exactly this case, so it is reported as a measurement
    outcome instead of crashing the gate.
    """
    print(
        f"Operation Truth could not be measured — a source is not loadable:\n"
        f"  [{type(exc).__name__}] {exc}\n"
        f"\nThe three sources must load before any comparison is meaningful. "
        f"Fix the tree above; do not regenerate {truth.PROJECTION_PATH} to "
        f"silence this.",
        file=sys.stderr,
    )
    return EXIT_SOURCE_UNREADABLE


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
        try:
            path = truth.write_projection()
        except (sources.SourceError, ValueError) as exc:
            return _load_error(exc)
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
        try:
            fresh = truth.build_projection()
        except (sources.SourceError, ValueError) as exc:
            return _load_error(exc)
        findings = truth.compare(projection, fresh)
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
