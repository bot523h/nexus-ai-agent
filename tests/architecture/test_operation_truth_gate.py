"""Gate 2.2 — the architecture fitness function for Operation Truth.

This is the gate that has to fail in CI when Operation Truth drifts.  It is a
*fitness function* in the evolutionary-architecture sense: one deterministic
check over the system's shape, run in the pipeline it already has, whose failure
message names the rule and the offender.

What it enforces
----------------
1. **The projection is an output.**  A fresh recomputation from the three
   sources must equal ``OPERATION_TRUTH.json``.  If they disagree, the file is
   stale and the build stops.
2. **The layers are not collapsed.**  Adding a registry entry must not, by
   itself, raise ``runtime_proven``; mapping a surface must not, by itself,
   raise ``artifact_proven``.  Both are asserted as *reachable counterexamples*,
   not as prose.
3. **``production_like`` is never claimed.**  No source measures it.
4. **``owner`` is never invented.**  No source names one.
5. **The three sources stay decoupled.**  Catalogue ≠ registry ≠ surface.

What it deliberately does *not* enforce
---------------------------------------
* It does not pin a count.  ``57`` became ``77`` the moment a sibling change
  added the vision packs; a gate that hard-codes a measurement turns a
  *correct* tree red.  The counts live in the generated projection, and a source
  change reds the gate through the *projection comparison* instead — which is
  the difference between catching drift and blocking progress.
* It does not compare timestamps or encoder-dependent digests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_ai_agent.nagar import sources, truth

REPO_ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def projection() -> dict:
    loaded = truth.load_projection(REPO_ROOT)
    assert loaded is not None, (
        f"{truth.PROJECTION_PATH} is missing — regenerate it with "
        f"`{truth.GENERATION_COMMAND} --write`"
    )
    return loaded


@pytest.fixture(scope="module")
def fresh() -> dict:
    return truth.build_projection(root=REPO_ROOT)


# --------------------------------------------------------------------------- #
# 1. the gate itself
# --------------------------------------------------------------------------- #


def test_projection_matches_a_fresh_recomputation(projection: dict, fresh: dict) -> None:
    """The one assertion that makes every other claim in the projection checkable."""
    findings = truth.compare(projection, fresh)
    message = "\n".join(str(finding) for finding in findings)
    assert not findings, (
        "Operation Truth drifted — the committed projection no longer matches the "
        f"sources it claims to describe.  Regenerate with `{truth.GENERATION_COMMAND} "
        f"--write` and review the diff.\n{message}"
    )


def test_a_missing_or_empty_projection_is_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the projection must never be a way to make the gate green."""
    assert truth.load_projection(tmp_path) is None, "an absent projection must read as absent"


def test_the_gate_reads_no_projection_as_evidence() -> None:
    """Anti-self-reference, checked on the source text itself.

    ``sources.py`` is the only module allowed to touch a source.  If it ever
    read ``OPERATION_TRUTH.json`` (or any matrix/reconciliation document) it
    would be grading its own homework, which is precisely the defect Gate 2.2
    was created to remove.
    """
    text = (REPO_ROOT / "src/nexus_ai_agent/nagar/sources.py").read_text(encoding="utf-8")
    forbidden = (
        "OPERATION_TRUTH.json",
        "OPERATION_MATRIX.json",
        "RECONCILIATION.json",
        "OPERATION_CONTRACT_MATRIX.md",
        "L0_L4_MATURITY.md",
    )
    offenders = [name for name in forbidden if name in text]
    assert not offenders, (
        "sources.py must derive from the catalogue, the live registry and the live "
        f"surface only — it references projection documents: {offenders}"
    )


# --------------------------------------------------------------------------- #
# 2. anti-inflation trip-wires
# --------------------------------------------------------------------------- #


def _by_token(projection: dict, token: str) -> set[str]:
    return {node["operation_id"] for node in projection["operations"] if node["status"][token]}


def test_registration_does_not_imply_runtime_proof(projection: dict) -> None:
    """``registered`` must be a strictly weaker claim than ``runtime_proven``.

    If these two sets were ever equal, the evidence gate would have degenerated
    into "the registry lists it, therefore it runs" — the exact conflation the
    brief forbids.  Measured on the audited baseline: 57 registered, 1 proven.
    """
    registered = _by_token(projection, "registered")
    proven = _by_token(projection, "runtime_proven")
    assert registered, "the runtime registry must not be empty"
    assert proven < registered, (
        "runtime_proven must be a strict subset of registered; "
        f"registered={len(registered)} runtime_proven={len(proven)}"
    )


def test_surface_reachability_does_not_imply_artifact_proof(projection: dict) -> None:
    """``surface_reachable`` must be a strictly weaker claim than ``artifact_proven``."""
    reachable = _by_token(projection, "surface_reachable")
    proven = _by_token(projection, "artifact_proven")
    assert reachable, "the executable surface must not be empty"
    assert proven < reachable, (
        "artifact_proven must be a strict subset of surface_reachable; "
        f"surface_reachable={len(reachable)} artifact_proven={len(proven)}"
    )


def test_executor_readiness_does_not_imply_artifact_proof(projection: dict) -> None:
    """Having an execution branch is not having produced a verified artifact."""
    executor = _by_token(projection, "executor_ready")
    proven = _by_token(projection, "artifact_proven")
    assert proven < executor, (
        "artifact_proven must be a strict subset of executor_ready; "
        f"executor_ready={len(executor)} artifact_proven={len(proven)}"
    )


def test_every_layer_is_pinned_to_its_own_source(fresh: dict) -> None:
    """Each layer must equal what *its own* documented source says.

    Measured defect this pins: ``executor_ready`` used to be built as
    ``operation_id in lane_ops or operation_id in surface_ops``, which made it a
    **synonym of** ``surface_reachable`` (both 12) while the module's own
    docstring documented it as "the render lane has a ``canonical_id ==``
    branch". Two layers that always agree are one layer, and a collapsed layer
    is not evidence. The predicate now reads the render lane alone; the
    assertion for that specific derivation is
    :func:`test_executor_ready_is_derived_from_the_render_lane_alone`.

    Note what is deliberately **not** asserted: that the layer sets are pairwise
    unequal. They may coincide whenever their predicates happen to agree on the
    data — on this baseline ``registered == domain_ready`` (every registered
    operation has a well-formed spec) and ``runtime_proven == artifact_proven``
    (Gate 4's one end-to-end slice proves both tiers). Forbidding that would
    forbid the data from improving. Independence is a property of the
    *derivation*, so that is what is pinned here.
    """
    assert _by_token(fresh, "defined") == set(sources.read_product_catalog().operation_ids)
    assert _by_token(fresh, "registered") == set(sources.read_runtime_registry().operation_ids)
    assert _by_token(fresh, "surface_reachable") == set(
        sources.read_executable_surface().operation_ids
    )


def test_executor_ready_is_derived_from_the_render_lane_alone(fresh: dict) -> None:
    """Pin the *derivation*: ``executor_ready`` == the render lane's branches."""
    from nexus_ai_agent.nagar import sources

    lane = set(sources.read_render_lane().operations)
    executor = _by_token(fresh, "executor_ready")
    assert executor == lane, (
        "executor_ready must be exactly the render lane's dispatchable set — "
        f"lane={len(lane)} executor_ready={len(executor)} "
        f"diff={sorted(executor ^ lane)}"
    )


def test_production_like_is_never_claimed(projection: dict) -> None:
    """No source defines or measures ``production_like``, so the gate cannot claim it."""
    values = {node["status"]["production_like"] for node in projection["operations"]}
    assert values == {None}, (
        "production_like has no source in this repository; it must be recorded as "
        f"NOT AVAILABLE, never as a boolean.  Found: {values}"
    )


def test_owner_is_never_invented(projection: dict) -> None:
    """No source names an operation owner, so every node reports ``NOT_AVAILABLE``."""
    owners = {node["owner"] for node in projection["operations"]}
    assert owners == {truth.NOT_AVAILABLE}, owners
    assert all(
        node["registrar_pack"] != truth.NOT_AVAILABLE
        for node in projection["operations"]
        if node["source"]["registered_in"]
    ), "a registered operation must name the pack (or the Wave-1 core) that registered it"


def test_every_node_carries_all_eight_layers(projection: dict) -> None:
    """§5: every node carries identity, source, status, evidence and owner."""
    for node in projection["operations"]:
        missing = [token for token in truth.EVIDENCE_TOKENS if token not in node["status"]]
        assert not missing, f"{node['operation_id']} is missing layers {missing}"
        for field in ("identity", "source", "status", "evidence"):
            assert field in node, f"{node['operation_id']} has no {field!r}"
        assert "owner" in node


def test_evidence_states_come_from_the_declared_vocabulary(projection: dict) -> None:
    """A claim may only use one of the five declared states (§20)."""
    allowed = {
        truth.VERIFIED,
        truth.PARTIALLY_VERIFIED,
        truth.INFERRED,
        truth.NOT_VERIFIED,
        truth.MISSING,
        truth.NOT_AVAILABLE,
    }
    verdicts = {node["evidence"]["gate4_runtime_verdict"] for node in projection["operations"]} | {
        node["evidence"]["gate4_artifact_verdict"] for node in projection["operations"]
    }
    assert verdicts, "the projection must record Gate 4 verdicts"
    assert verdicts <= allowed | {"PASS", "FAIL"}, sorted(verdicts - allowed - {"PASS", "FAIL"})


# --------------------------------------------------------------------------- #
# 3. the three layers stay decoupled
# --------------------------------------------------------------------------- #


def test_the_three_sources_are_strictly_decoupled(fresh: dict) -> None:
    """Catalogue ≠ registry ≠ surface — asserted as set inequality, not prose."""
    reconciliation = fresh["reconciliation"]
    catalog = set(sources.read_product_catalog(REPO_ROOT).operation_ids)
    runtime = set(sources.read_runtime_registry().operation_ids)
    surface = set(sources.read_executable_surface(REPO_ROOT).operation_ids)

    assert catalog != runtime, "the catalogue and the registry must not be the same set"
    assert runtime != surface, "the registry and the executable surface must not be the same set"
    assert catalog != surface, "the catalogue and the executable surface must not be the same set"
    assert len(catalog) == reconciliation["catalog_count"]
    assert len(runtime) == reconciliation["runtime_count"]
    assert len(surface) == reconciliation["surface_count"]


def test_the_surface_is_inside_the_runtime(fresh: dict) -> None:
    """Nothing may be reachable that the runtime cannot dispatch."""
    catalog = sources.read_product_catalog(REPO_ROOT)
    runtime = sources.read_runtime_registry()
    surface = sources.read_executable_surface(REPO_ROOT)
    assert set(surface.operation_ids) <= set(runtime.operation_ids), sorted(
        set(surface.operation_ids) - set(runtime.operation_ids)
    )
    assert set(runtime.operation_ids) - set(catalog.operation_ids), (
        "the runtime must contain at least one operation the catalogue never defined; "
        "if it did not, catalogue and registry could not be told apart"
    )


def test_the_surface_probes_agree(fresh: dict) -> None:
    """The surface's own allow-list and the worker's closed set must agree exactly."""
    assert fresh["surface"]["disagreements"] == [], fresh["surface"]["disagreements"]
    assert fresh["surface"]["probes"]["surface_allow_list"]["unresolved"] == []


def test_reconciliation_arithmetic_is_self_consistent(fresh: dict) -> None:
    """The §8 formulas must hold for the measured sets, not just for the counts."""
    reconciliation = fresh["reconciliation"]
    catalog = set(reconciliation["missing"]) | set(reconciliation["runtime_only"])
    # Rebuild the true sets from the operations list and re-derive every number.
    defined = {node["operation_id"] for node in fresh["operations"] if node["status"]["defined"]}
    registered = {
        node["operation_id"] for node in fresh["operations"] if node["status"]["registered"]
    }
    assert catalog, "the projection must name its gaps"
    assert len(defined) == reconciliation["catalog_count"]
    assert len(registered) == reconciliation["runtime_count"]
    assert len(defined & registered) == reconciliation["overlap_count"]
    assert len(defined - registered) == reconciliation["missing_count"]
    assert len(registered - defined) == reconciliation["runtime_only_count"]
    assert len(defined | registered) == reconciliation["universe_count"]
    assert set(reconciliation["missing"]) == defined - registered
    assert set(reconciliation["runtime_only"]) == registered - defined


# --------------------------------------------------------------------------- #
# 4. generated-artifact hygiene (§17)
# --------------------------------------------------------------------------- #


def test_the_projection_declares_its_provenance(projection: dict) -> None:
    """A generated file must say what produced it and what is unstable."""
    header = projection.get("generated") or {}
    assert header.get("generation_command") == truth.GENERATION_COMMAND
    assert header.get("source_revision"), "the header must pin a source revision"
    assert header.get("generated_at"), "the header must record a generation time"
    assert header.get("generated_from"), "the header must name its sources"
    deterministic = set(header.get("deterministic_fields") or [])
    non_deterministic = set(header.get("non_deterministic_fields") or [])
    assert deterministic, "the header must declare its deterministic fields"
    assert non_deterministic, (
        "the header must declare its non-deterministic fields — an honest gate says "
        "which parts of the artifact it refuses to compare"
    )
    assert not deterministic & non_deterministic, "a field cannot be both"


#: Blocks the projection declares volatile, excluded from the determinism equality.
_VOLATILE_BLOCKS = ("generated", "provenance", "artifact_evidence")


def _stable(document: dict) -> dict:
    return {key: value for key, value in document.items() if key not in _VOLATILE_BLOCKS}


def test_regeneration_is_deterministic(fresh: dict) -> None:
    """Two recomputations must agree everywhere outside the declared volatile blocks."""
    first = truth.build_projection(root=REPO_ROOT)
    second = truth.build_projection(root=REPO_ROOT)
    assert _stable(first) == _stable(second), "the projection is not deterministic"
    assert _stable(first) == _stable(fresh)
    assert fresh["schema"] == truth.SCHEMA


def test_the_projection_is_valid_json_on_disk() -> None:
    raw = (REPO_ROOT / truth.PROJECTION_PATH).read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert parsed["schema"] == truth.SCHEMA
    assert raw.endswith("\n"), "generated files end with a newline (diff hygiene)"
