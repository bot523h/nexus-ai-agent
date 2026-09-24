"""Gate 2.2 — the three sources, and the derivations that read them.

These tests pin the *derivations*, not their results: that the catalogue parser
follows the same T-id rule Gate 4 engineered, that the runtime facts come from
the live registry, and that the surface probes measure code rather than repeat a
constant.  A test in this file may assert an exact number only where the number
is a property of the derivation itself (for example "the seven pack tables") —
never where it is a property of the product, which belongs in the generated
projection and is checked by the mutation probes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.nagar import sources, truth

REPO_ROOT = Path(__file__).parents[2]

#: Gate 4's authoritative T-id rule, restated here as the cross-check.
#: ``docs/audits/gate4_slice.py`` documents it as "the rows of the seven pack
#: tables in docs/NAGAR_70_OPERATIONS_TDD.md, in document order, zero-padded".
GATE4_SLICE = {
    "T01": "timeline.split_at_playhead",
    "T02": "timeline.trim",
    "T22": "scene.remove_object",
    "T31": "motion.add_transition",
    "T64": "color.match_shot",
}


@pytest.fixture(scope="module")
def catalog() -> sources.Catalog:
    return sources.read_product_catalog(REPO_ROOT)


@pytest.fixture(scope="module")
def runtime() -> sources.Runtime:
    return sources.read_runtime_registry()


@pytest.fixture(scope="module")
def surface() -> sources.Surface:
    return sources.read_executable_surface(REPO_ROOT)


# --------------------------------------------------------------------------- #
# Source 1 — the product catalogue
# --------------------------------------------------------------------------- #


def test_the_catalogue_has_seven_pack_tables(catalog: sources.Catalog) -> None:
    """The T-id rule names seven tables; the parser must find exactly seven."""
    assert len(catalog.table_lines) == 7, catalog.table_lines


def test_the_catalogue_parser_agrees_with_gate_4(catalog: sources.Catalog) -> None:
    """The same T-id rule Gate 4 engineered must hold here, or downstream joins lie."""
    by_t_id = catalog.by_t_id
    for t_id, operation_id in GATE4_SLICE.items():
        assert by_t_id.get(t_id) == operation_id, (
            f"the catalogue parser and Gate 4's slice disagree about {t_id}: "
            f"{by_t_id.get(t_id)!r} != {operation_id!r}"
        )


def test_the_catalogue_parser_ignores_prose_mentions(catalog: sources.Catalog) -> None:
    """Only *table rows* are catalogue entries.

    A naive ``grep`` of backticked ``a.b`` tokens over the same document returns
    71 because it also picks up a prose example (``media.play``).  The parser
    must not: the count it produces is the catalogue.
    """
    text = (REPO_ROOT / sources.CATALOG_PATH).read_text(encoding="utf-8")
    backticked = {
        token
        for line in text.splitlines()
        for token in [line.split("`")[1] if line.count("`") >= 2 else ""]
        if "." in token and " " not in token
    }
    parsed = set(catalog.operation_ids)
    assert parsed <= backticked
    assert len(catalog.rows) == 70, (
        f"the seven pack tables must yield 70 rows (the document's own title); got "
        f"{len(catalog.rows)}"
    )


def test_t_ids_are_dense_and_ordered(catalog: sources.Catalog) -> None:
    """``T01..T70`` with no gap: a deleted row shifts every later id, loudly."""
    assert [row.t_id for row in catalog.rows] == [
        f"T{index:02d}" for index in range(1, len(catalog.rows) + 1)
    ]


def test_row_order_is_document_order(catalog: sources.Catalog) -> None:
    lines = [row.line_number for row in catalog.rows]
    assert lines == sorted(lines), "catalogue rows must be read in document order"


# --------------------------------------------------------------------------- #
# Source 2 — the live runtime registry
# --------------------------------------------------------------------------- #


def test_runtime_facts_come_from_the_live_registry(runtime: sources.Runtime) -> None:
    """Every recorded fact must match a second, independent read of the registry."""
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

    registry = build_runtime_registry()
    assert set(runtime.operation_ids) == set(registry.list_operations())
    for operation in runtime.operations:
        spec = registry.get_spec(operation.operation_id)
        assert operation.permission_level == spec.permission_level.value
        assert operation.handler_name == getattr(spec.handler, "__name__", "")
        assert operation.deterministic is bool(spec.deterministic)


def test_composition_is_clean(runtime: sources.Runtime) -> None:
    """A pack on disk with no builder, or a builder with no pack, is a finding."""
    assert runtime.composition_issues == ()


def test_wave1_core_operations_are_exactly_the_pack_less_ones(runtime: sources.Runtime) -> None:
    """``registrar`` is derived, not guessed: the two definitions must coincide.

    ``RuntimeOperation.registrar`` reports the Wave-1 core registry for any
    operation with no ``required_packs``.  That is only honest if those are
    precisely the operations ``build_wave1_registry()`` registers — which is
    what this test measures.
    """
    from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry

    pack_less = {op.operation_id for op in runtime.operations if not op.required_packs}
    wave1 = set(build_wave1_registry().list_operations())
    assert pack_less == wave1, (
        "the operations without required_packs must be exactly the Wave-1 core "
        f"registry: pack-less-only={sorted(pack_less - wave1)} "
        f"wave1-only={sorted(wave1 - pack_less)}"
    )


# --------------------------------------------------------------------------- #
# Source 3 — the live executable surface
# --------------------------------------------------------------------------- #


def test_surface_probes_measure_code_not_constants(surface: sources.Surface) -> None:
    """Each probe must name the live code object it read."""
    for probe in (surface.allow_list, surface.worker_closed_set, surface.slideshow_entrypoint):
        assert probe.source, f"probe {probe.name} does not name its source"
        assert probe.operations, f"probe {probe.name} measured an empty surface"


def test_the_surface_and_the_worker_agree_exactly(surface: sources.Surface) -> None:
    """The surface's allow-list and the worker's closed set must be the same set.

    This is the cross-check that makes a "fake surface mapping" impossible: a
    pair accepted by one and not the other is a drift, and the gate reds.
    """
    assert surface.disagreements == ()
    assert surface.pairs == frozenset(surface.worker_closed_set.pairs)
    assert surface.allow_list.unresolved == ()


def test_registered_commands_match_the_mapped_commands(surface: sources.Surface) -> None:
    """Every Telegram command the surface registers must appear in its own map."""
    assert set(surface.commands) == {command for command, _ in surface.pairs}


def test_the_surface_does_not_change_without_the_source_changing() -> None:
    """Triple-read stability: three separate reads must agree byte for byte."""
    reads = [sources.read_executable_surface(REPO_ROOT) for _ in range(3)]
    assert {read.operation_ids for read in reads} == {reads[0].operation_ids}
    assert {read.pairs for read in reads} == {reads[0].pairs}


def test_the_slideshow_entrypoint_is_parsed_from_the_service() -> None:
    """The slideshow probe must come from the dispatch call sites, not a list."""
    probe = sources.probe_slideshow_entrypoint(REPO_ROOT)
    assert probe.source.endswith("service.py bus.dispatch(_command(OPERATION_*, …))")
    assert probe.operations, "the /slideshow surface must reach at least one operation"


# --------------------------------------------------------------------------- #
# Consumed evidence — Gate 4
# --------------------------------------------------------------------------- #


def test_gate4_evidence_is_consumed_with_out_of_slice_verdicts_intact() -> None:
    """Gate 2.2 consumes Gate 4's matrix and must not soften a MISSING to a PASS."""
    gate4 = sources.read_gate4_evidence(REPO_ROOT)
    runtime_verdicts = gate4.verdicts("Runtime")
    assert set(runtime_verdicts) == set(GATE4_SLICE.values())
    assert runtime_verdicts[GATE4_SLICE["T02"]] == "PASS"
    for t_id in ("T01", "T22", "T31", "T64"):
        assert runtime_verdicts[GATE4_SLICE[t_id]] == "MISSING", (
            f"Gate 4 recorded {t_id} Runtime as MISSING; Gate 2.2 must preserve that"
        )


def test_gate4_evidence_covers_every_layer() -> None:
    gate4 = sources.read_gate4_evidence(REPO_ROOT)
    layers = {cell.layer for cell in gate4.cells}
    assert layers == {
        "Artifact",
        "Capability",
        "Command",
        "E2E",
        "Job",
        "Product",
        "Reopen",
        "Runtime",
    }


# --------------------------------------------------------------------------- #
# Provenance chain (§15) and artifact evidence (§14)
# --------------------------------------------------------------------------- #


def test_the_t02_provenance_chain_is_intact() -> None:
    """The one proven chain must reconstruct without a broken link."""
    chain = truth.build_provenance_chain(REPO_ROOT)
    assert chain.intact, chain.findings
    assert chain.operation_id == GATE4_SLICE["T02"]
    assert chain.links["command_id"].endswith("timeline.trim")
    assert chain.links["verification"]["reopen_digest_matches"] is True


def test_the_chain_reports_the_job_id_it_does_not_have() -> None:
    """§13: a field the schema does not carry is recorded, never fabricated."""
    chain = truth.build_provenance_chain(REPO_ROOT)
    assert chain.links["job_id"] == truth.NOT_AVAILABLE
    assert chain.links["job_id_evidence"] != truth.NOT_AVAILABLE, (
        "the chain must say *why* the job id is absent and where it does exist"
    )


def test_artifact_evidence_records_what_the_schema_lacks() -> None:
    """§14: identity fields absent from the Job/Artifact schema stay NOT_AVAILABLE."""
    evidence = truth.build_artifact_evidence(REPO_ROOT)
    assert evidence["sha256_well_formed"] is True
    assert evidence["size_positive"] is True
    assert evidence["exists"] is True
    assert evidence["verification_status"] == "completed"
    for field in ("logical_identity", "spec_identity", "physical_identity"):
        assert evidence[field] == truth.NOT_AVAILABLE, field
    assert evidence["identity_reason"] != truth.NOT_AVAILABLE


def test_the_job_boundary_declares_its_absences() -> None:
    """§13: Gate 2.2 does not implement job lifecycle; it declares what it consumes."""
    boundary = truth.build_job_boundary()
    assert boundary["implemented_here"] is False
    unavailable = {
        name: entry for name, entry in boundary["consumed_fields"].items() if not entry["available"]
    }
    assert unavailable, "the boundary must record at least one absent field"
    for name, entry in unavailable.items():
        assert entry.get("reason"), f"{name} is unavailable without a stated reason"


def test_production_like_has_no_source() -> None:
    """The ninth layer is unmeasurable here, and the module says so."""
    assert "production_like" in truth.EVIDENCE_TOKENS
    assert "production_like" not in truth.MEASURABLE_TOKENS
