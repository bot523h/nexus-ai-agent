"""The Operation Truth gate: stored artifacts must equal a fresh recomputation.

This suite is the machine-verifiable boundary the mission asks for: after it,
no agent can claim an operation is "implemented" by pointing at a Markdown
page or a registry snapshot.  The claim must survive:

* a full recomputation from the live sources (``compare`` finds nothing);
* the structural truth rules (``invariants`` finds nothing);
* a byte-exact re-render of every generated document;
* a static scan proving the engine itself carries no hard-coded counts.

The adversarial counterpart lives in
``tests/unit/test_operation_truth_mutations.py`` (each guard exercised
GREEN → MUTANT → RED → RESTORE → GREEN).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from nexus_ai_agent.nagar import docs_projection, sources, truth

REPO_ROOT = Path(__file__).parents[2]

#: Integer literals in the engine that are *not* counts (documented exemption:
#: git timeout seconds, digest hex width).  Everything else ≥ 10 that equals a
#: freshly computed count is a hard-coded-number smell the scan refuses.
NON_COUNT_LITERALS = {10, 16}


@pytest.fixture(scope="module")
def fresh() -> dict:
    return truth.build_projection(root=REPO_ROOT)


@pytest.fixture(scope="module")
def stored() -> dict:
    projection = truth.load_projection(REPO_ROOT)
    assert projection is not None, (
        "OPERATION_TRUTH.json is missing — run `python -m nexus_ai_agent.nagar --docs`"
    )
    return projection


# --------------------------------------------------------------------------- #
# Freshness: stored projection == recomputation
# --------------------------------------------------------------------------- #


def test_stored_projection_matches_a_fresh_recomputation(stored: dict, fresh: dict) -> None:
    findings = truth.compare(stored, fresh)
    assert findings == (), "projection drifted from the sources:\n" + "\n".join(map(str, findings))


def test_fresh_recomputation_satisfies_the_structural_rules(fresh: dict) -> None:
    violations = truth.invariants(fresh)
    assert violations == (), "fresh projection violates truth rules:\n" + "\n".join(violations)


def test_stored_projection_satisfies_the_structural_rules(stored: dict) -> None:
    violations = truth.invariants(stored)
    assert violations == (), "stored projection violates truth rules:\n" + "\n".join(violations)


def test_projection_declares_its_sources_and_commands(stored: dict) -> None:
    assert stored["schema"] == truth.SCHEMA
    assert stored["generated"]["generation_command"] == truth.GENERATION_COMMAND
    source_names = {entry["source"] for entry in stored["generated"]["generated_from"]}
    assert {
        "product_catalog",
        "runtime_registry",
        "executable_surface",
        "declared_operations",
        "proof_registry",
    } <= source_names


def test_only_timestamp_and_revision_are_nondeterministic(stored: dict) -> None:
    assert stored["generated"]["non_deterministic_fields"] == [
        "generated.generated_at",
        "generated.source_revision",
    ]


# --------------------------------------------------------------------------- #
# Maturity honesty: L3 needs executable evidence, L4 is impossible today
# --------------------------------------------------------------------------- #


def test_no_operation_is_l3_or_l4_without_proven_evidence(stored: dict) -> None:
    offenders = [
        node["operation_id"]
        for node in stored["operations"]
        if node["maturity"]["level"] >= 3 and node["pipeline"]["proof"] != "PROVEN"
    ]
    assert offenders == [], f"L3/L4 without executable evidence: {offenders}"


def test_l3_requires_a_surface_reachable_entrypoint(stored: dict) -> None:
    offenders = [
        node["operation_id"]
        for node in stored["operations"]
        if node["maturity"]["level"] >= 2 and node["pipeline"]["surface"] != "PRESENT"
    ]
    assert offenders == [], f"L2/L3 without a live entrypoint: {offenders}"


def test_l4_is_unavailable_and_says_why(stored: dict) -> None:
    l4 = [node["operation_id"] for node in stored["operations"] if node["maturity"]["level"] == 4]
    assert l4 == [], f"L4 emitted without a production-like source: {l4}"
    assert stored["l4_unavailable_reason"] == truth.L4_UNAVAILABLE_REASON
    assert stored["ladder"]["L4"] == truth.LADDER["L4"]


def test_owner_is_never_fabricated(stored: dict) -> None:
    assert {node["owner"] for node in stored["operations"]} == {truth.NOT_AVAILABLE}


# --------------------------------------------------------------------------- #
# Reconciliation is arithmetic, not a stored claim
# --------------------------------------------------------------------------- #


def test_reconciliation_matches_direct_set_arithmetic(stored: dict) -> None:
    catalog = set(sources.read_product_catalog(REPO_ROOT).operation_ids)
    runtime = set(sources.read_runtime_registry().operation_ids)
    recon = stored["reconciliation"]
    assert recon["catalog_count"] == len(catalog)
    assert recon["runtime_count"] == len(runtime)
    assert set(recon["missing"]) == catalog - runtime
    assert set(recon["runtime_only"]) == runtime - catalog
    assert recon["universe_count"] == len(catalog | runtime)
    assert recon["overlap_count"] == len(catalog & runtime)


def test_missing_set_is_exactly_the_unshipped_catalogue(stored: dict) -> None:
    catalog = set(sources.read_product_catalog(REPO_ROOT).operation_ids)
    runtime = set(sources.read_runtime_registry().operation_ids)
    computed_missing = catalog - runtime
    recorded = {entry["operation_id"] for entry in stored["missing_set"]}
    assert recorded == computed_missing
    for entry in stored["missing_set"]:
        for field in (
            "capability",
            "reason",
            "dependency",
            "execution_lane",
            "test_requirement",
            "pack_requirement",
            "locality",
            "security",
            "wave",
        ):
            assert entry.get(field), f"{entry['operation_id']} lacks {field}"


def test_every_operation_carries_the_full_nine_stage_pipeline(stored: dict) -> None:
    for node in stored["operations"]:
        assert tuple(node["pipeline"]) == truth.PIPELINE_STAGES, node["operation_id"]
        assert node["maturity"]["token"] == f"L{node['maturity']['level']}"


# --------------------------------------------------------------------------- #
# Generated documents are projections, byte-exact
# --------------------------------------------------------------------------- #


def test_every_generated_document_matches_a_fresh_rendering(fresh: dict) -> None:
    for path, expected in docs_projection.render_all(fresh).items():
        target = REPO_ROOT / path
        assert target.is_file(), f"{path} is missing — run --docs"
        actual = target.read_text(encoding="utf-8")
        assert actual == expected, (
            f"{path} differs from a fresh rendering — regenerate with "
            "`python -m nexus_ai_agent.nagar --docs` (hand edits are detected here)"
        )


def test_generated_documents_are_marked_generated(fresh: dict) -> None:
    for path in docs_projection.GENERATED_DOCS:
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        assert text.startswith(docs_projection.HEADER), f"{path} lost its generated header"


def test_wave_plan_covers_the_missing_set_exactly(fresh: dict) -> None:
    text = (REPO_ROOT / docs_projection.WAVE_PLAN_PATH).read_text(encoding="utf-8")
    for entry in fresh["missing_set"]:
        assert f"`{entry['operation_id']}`" in text, (
            f"wave plan omits missing operation {entry['operation_id']}"
        )


def test_ladder_definitions_appear_verbatim_in_the_maturity_page(fresh: dict) -> None:
    text = (REPO_ROOT / docs_projection.MATURITY_PATH).read_text(encoding="utf-8")
    for level, definition in truth.LADDER.items():
        assert f"**{level}** — {definition}" in text, f"{level} definition drifted"


# --------------------------------------------------------------------------- #
# Anti-fraud: no hard-coded counts in the engine
# --------------------------------------------------------------------------- #


def _fresh_count_values(fresh: dict) -> set[int]:
    recon = fresh["reconciliation"]
    values: set[int] = set()
    for value in recon.values():
        if isinstance(value, int) and value >= 10:
            values.add(value)
        if isinstance(value, dict):
            values.update(v for v in value.values() if isinstance(v, int) and v >= 10)
    parity = fresh["declaration_parity"]
    values.update(
        v
        for v in (parity["declared_count"], parity["registered_count"])
        if isinstance(v, int) and v >= 10
    )
    values.update(
        v for v in (fresh["surface"]["derived_operation_count"],) if isinstance(v, int) and v >= 10
    )
    return values


def test_engine_source_carries_no_hardcoded_counts(fresh: dict) -> None:
    """Every number the engine reports must be recomputable; an integer
    literal in the engine that equals a live count is the shape of a frozen
    claim (the mutation probes prove the counts actually move)."""
    counts = _fresh_count_values(fresh)
    assert counts, "no counts computed — the scan would be vacuous"
    package = REPO_ROOT / "src" / "nexus_ai_agent" / "nagar"
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, int):
                if node.value in counts and node.value not in NON_COUNT_LITERALS:
                    offenders.append(f"{path.name}:{node.lineno} literal {node.value}")
    assert offenders == [], (
        "hard-coded count literals in the truth engine (recompute instead): "
        f"{offenders} (live counts: {sorted(counts)})"
    )


def test_projection_json_is_loadable_and_sorted_headers_hold() -> None:
    payload = json.loads((REPO_ROOT / truth.PROJECTION_PATH).read_text(encoding="utf-8"))
    assert payload["schema"] == truth.SCHEMA
    ids = [node["operation_id"] for node in payload["operations"]]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids)), "duplicated operation ids in the projection"
