"""Source-level tests: each truth source must be read strictly and independently.

These pin the *predicates* the projection is built from — catalogue parsing,
declaration parity, surface probe agreement, render-lane derivation, the
suite-execution proof probe, and the recorded-proof registry contract — so a
weakening of any probe shows up here before it can weaken the gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.nagar import sources, truth

REPO_ROOT = Path(__file__).parents[2]


# --------------------------------------------------------------------------- #
# Source 1 — product catalogue
# --------------------------------------------------------------------------- #


def test_catalog_is_parsed_from_the_pack_tables_only() -> None:
    catalog = sources.read_product_catalog(REPO_ROOT)
    assert catalog.count > 0
    # Rows are exactly the backticked first cells inside `| Operation` tables.
    lines = (REPO_ROOT / sources.CATALOG_PATH).read_text(encoding="utf-8").splitlines()
    table_ids: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("| Operation"):
            inside = True
            continue
        if inside and not line.startswith("|"):
            inside = False
            continue
        if inside:
            match = sources._OPERATION_ROW.match(line)
            if match:
                table_ids.append(match.group(1))
    assert list(catalog.operation_ids) == table_ids


def test_catalog_ordinals_are_sequential_and_t_ids_unique() -> None:
    catalog = sources.read_product_catalog(REPO_ROOT)
    assert [row.ordinal for row in catalog.rows] == list(range(1, catalog.count + 1))
    assert len({row.t_id for row in catalog.rows}) == catalog.count


def test_every_catalog_row_carries_a_contract_cell_set() -> None:
    catalog = sources.read_product_catalog(REPO_ROOT)
    for row in catalog.rows:
        assert row.io_spec, row.operation_id
        assert row.engine, row.operation_id
        assert row.permission_level.upper() in {"A", "B", "C"}, (
            row.operation_id,
            row.permission_level,
        )
        assert row.pack_id.startswith("nexus."), (row.operation_id, row.pack_id)


def test_duplicate_catalogue_ids_are_refused_not_deduplicated(tmp_path: Path) -> None:
    source = REPO_ROOT / sources.CATALOG_PATH
    sandbox = tmp_path / sources.CATALOG_PATH
    sandbox.parent.mkdir(parents=True, exist_ok=True)
    lines = source.read_text(encoding="utf-8").splitlines()
    # Duplicate the first catalogue row immediately after itself.
    for index, line in enumerate(lines):
        if line.startswith("| Operation"):
            row_line = next(
                candidate
                for candidate in lines[index + 1 :]
                if sources._OPERATION_ROW.match(candidate)
            )
            lines.insert(lines.index(row_line) + 1, row_line)
            break
    sandbox.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(sources.SourceError, match="duplicate operation id"):
        sources.read_product_catalog(tmp_path)


def test_missing_catalogue_file_is_an_error_not_zero_rows(tmp_path: Path) -> None:
    with pytest.raises(sources.SourceError, match="product catalogue missing"):
        sources.read_product_catalog(tmp_path)


# --------------------------------------------------------------------------- #
# Source 2 — runtime registry
# --------------------------------------------------------------------------- #


def test_runtime_registry_is_a_measurement_with_domain_facts() -> None:
    runtime = sources.read_runtime_registry()
    assert runtime.count > 0
    for operation in runtime.operations:
        assert operation.input_model
        # The domain-ready flag is exactly its three mechanical facts — the
        # projection's capability stage must not drift from them.
        assert operation.domain_ready is (
            operation.input_forbids_extra and operation.handler_is_named and operation.deterministic
        )
        assert operation.input_forbids_extra, operation.operation_id
        assert operation.handler_is_named, operation.operation_id
        assert operation.deterministic, operation.operation_id


def test_pack_composition_is_clean() -> None:
    assert sources.read_runtime_registry().composition_issues == ()


# --------------------------------------------------------------------------- #
# Source 3 — executable surface
# --------------------------------------------------------------------------- #


def test_surface_probes_agree_on_main() -> None:
    surface = sources.read_executable_surface(REPO_ROOT)
    assert surface.disagreements == ()
    assert set(surface.commands) == set(surface.mapped_commands)
    assert surface.allow_list.unresolved == ()
    assert surface.worker_closed_set.operations
    assert surface.slideshow_entrypoint.operations
    assert surface.slideshow_command_registered is True


def test_every_surface_operation_is_registered() -> None:
    surface = sources.read_executable_surface(REPO_ROOT)
    runtime = set(sources.read_runtime_registry().operation_ids)
    stray = sorted(set(surface.operation_ids) - runtime)
    assert stray == [], f"surface reaches unregistered operations: {stray}"


# --------------------------------------------------------------------------- #
# Source 4 — render lane & declared symbols
# --------------------------------------------------------------------------- #


def test_render_lane_branches_exist_and_are_registered() -> None:
    lane = sources.read_render_lane(REPO_ROOT)
    assert lane.operations
    runtime = set(sources.read_runtime_registry().operation_ids)
    stray = sorted(set(lane.operations) - runtime)
    assert stray == [], f"lane executes unregistered operations: {stray}"


def test_declared_symbols_match_the_registry_both_ways() -> None:
    declared = sources.read_declared_operations()
    runtime = set(sources.read_runtime_registry().operation_ids)
    declared_ids = set(declared.operation_ids)
    assert declared_ids - runtime == set(), "implementation without registry"
    assert runtime - declared_ids == set(), "registry without a declaration symbol"
    assert declared.count == len(runtime)


# --------------------------------------------------------------------------- #
# Source 5 — proof evidence
# --------------------------------------------------------------------------- #


def test_suite_evidence_requires_an_execution_call_site() -> None:
    universe = set(sources.read_product_catalog(REPO_ROOT).operation_ids) | set(
        sources.read_runtime_registry().operation_ids
    )
    evidence = sources.read_suite_evidence(universe, root=REPO_ROOT)
    executed = [op for op, entry in evidence.items() if entry.executed]
    assert executed, "the suite executes nothing — the proof probe is vacuous"
    # A made-up id can never accumulate evidence.
    assert evidence.get("nonexistent.op") is None


def test_recorded_proof_registry_is_schema_valid_and_honest_when_empty() -> None:
    schema, proofs = sources.read_recorded_proofs(root=REPO_ROOT)
    assert schema.startswith("nagar.operation_proof_registry.")
    assert isinstance(proofs, tuple)
    universe = set(sources.read_product_catalog(REPO_ROOT).operation_ids) | set(
        sources.read_runtime_registry().operation_ids
    )
    for proof in proofs:
        assert proof.operation_id in universe, proof.operation_id
        assert proof.kind in {"runtime_execution", "artifact_evidence", "production_like"}
        assert proof.method and proof.recorded_at


def test_corrupt_proof_registry_is_refused(tmp_path: Path) -> None:
    path = tmp_path / sources.PROOF_REGISTRY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(sources.SourceError, match="not valid JSON"):
        sources.read_recorded_proofs(root=tmp_path)
    path.write_text('{"schema": "evil.v1", "proofs": []}', encoding="utf-8")
    with pytest.raises(sources.SourceError, match="unknown schema"):
        sources.read_recorded_proofs(root=tmp_path)


def test_gate4_evidence_is_optional_and_never_invented() -> None:
    payload = sources.read_gate4_evidence(REPO_ROOT)
    assert payload is None or isinstance(payload, dict)


# --------------------------------------------------------------------------- #
# Ladder ownership
# --------------------------------------------------------------------------- #


def test_ladder_levels_cover_the_whole_range_once() -> None:
    assert set(truth.LADDER) == {"L0", "L1", "L2", "L3", "L4"}
    for definition in truth.LADDER.values():
        assert definition.strip()


def test_maturity_is_monotone_by_construction() -> None:
    def pipeline(**overrides: str) -> dict[str, str]:
        base = {stage: "MISSING" for stage in truth.PIPELINE_STAGES}
        base.update(overrides)
        return base

    # A bare pipeline floors at L0 (universe membership implies a contract
    # row or a registered spec by construction); it can never climb higher.
    assert truth.compute_maturity(pipeline()) == 0
    # Registered+ready but no surface: L1 only.
    only_code = pipeline(contract="PRESENT", registry="PRESENT", capability="PRESENT")
    assert truth.compute_maturity(only_code) == 1
    # Surface without proof stops at L2; proof promotes to L3.
    reachable = dict(only_code, surface="PRESENT", command="PRESENT")
    assert truth.compute_maturity(reachable) == 2
    proven = dict(reachable, proof="PROVEN")
    assert truth.compute_maturity(proven) == 3
    # L4 additionally requires artifact proof and production-like evidence.
    l4_ready = dict(proven, artifact="PROVEN")
    assert truth.compute_maturity(l4_ready) == 3
    assert truth.compute_maturity(l4_ready, production_like=True) == 4
    # Skipping a rung is impossible: drop capability and the climb stops at L0.
    broken = dict(proven, capability="MISSING")
    assert truth.compute_maturity(broken) == 0
    # Proof without surface cannot reach L3 either.
    unreachably_proven = dict(only_code, proof="PROVEN")
    assert truth.compute_maturity(unreachably_proven) == 1
