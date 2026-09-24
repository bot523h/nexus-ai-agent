"""Gate 2.2 — adversarial mutation probes (A–J).

Every probe here mutates **a source** and asserts the gate turns red.  This is
the property PR #70's guard test did not have: measurement showed that deleting
the ``timeline.trim`` row from the product catalogue, rewriting all nine ``L4``
rows to ``L0``, inventing a surface mapping, and renaming every owner all left
that suite **green** (14 passed each time).  Its ``expected`` values came from
the document under test, so the document could not contradict them.

Two shapes of "red" are accepted, and each probe must produce one of them:

* the recomputation disagrees with the stored projection
  (:func:`nexus_ai_agent.nagar.truth.compare` returns a finding);
* the measurement itself refuses to proceed
  (:class:`nexus_ai_agent.nagar.sources.SourceError`).

A probe may only pass without either if it documents a **precise exemption** —
an input the gate deliberately does not claim to measure, stated in the probe
body.  ``production_like`` and the human ``owner`` field are the two such
exemptions; both are recorded as ``NOT_AVAILABLE`` by the projection rather than
guessed, so no mutation can fabricate them.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.nagar import sources, truth

REPO_ROOT = Path(__file__).parents[2]

#: Files the gate reads through a repository root, mirrored into the sandbox.
_SANDBOX_FILES = (
    sources.CATALOG_PATH,
    sources.GATE4_EVIDENCE,
    sources.SLIDESHOW_SERVICE,
    sources.RENDER_JOBS,
    Path("docs/L0_L4_MATURITY.md"),
    Path("docs/architecture/COMMAND_CAPABILITY_CONTRACT.md"),
    Path("docs/architecture/MODULE_MAP.md"),
)


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    """A minimal root holding the source files the gate reads, plus one extra."""
    for relative in _SANDBOX_FILES:
        source = REPO_ROOT / relative
        if not source.is_file():
            continue
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return tmp_path


@pytest.fixture()
def stored() -> dict[str, Any]:
    projection = truth.load_projection(REPO_ROOT)
    assert projection is not None, "OPERATION_TRUTH.json must exist (run the generator)"
    return projection


def _findings(sandbox: Path, stored: dict[str, Any]) -> tuple[truth.Finding, ...]:
    """Compare the stored projection with a recomputation over ``sandbox``."""
    fresh = truth.build_projection(root=sandbox)
    return truth.compare(stored, fresh)


def _kinds(findings: tuple[truth.Finding, ...]) -> set[str]:
    return {finding.kind for finding in findings}


def _gate_red(sandbox: Path, stored: dict[str, Any]) -> set[str]:
    """Return the finding kinds; raise the measurement error if the gate refused to run."""
    try:
        findings = _findings(sandbox, stored)
    except sources.SourceError as exc:
        return {f"SourceError: {exc}"}
    assert findings, (
        "the gate stayed GREEN under a source mutation — this is the self-referential "
        "failure Gate 2.2 exists to remove"
    )
    return _kinds(findings)


# --------------------------------------------------------------------------- #
# A / B — runtime source
# --------------------------------------------------------------------------- #


def test_probe_a_removing_a_runtime_operation_is_red(
    monkeypatch: pytest.MonkeyPatch, sandbox, stored
):
    """A: drop a pack from the live composition — the runtime registry shrinks."""
    from nexus_ai_agent.creative.packs import runtime as runtime_module

    # Capture the real builder FIRST: the mutation must call the original, or
    # the patched name would recurse into itself forever.
    real_builder = runtime_module.build_runtime_registry
    dropped = "nexus.color.delivery"

    def _mutilated(**kwargs: Any):
        kwargs.pop("compositions", None)
        kept = tuple(c for c in runtime_module.COMPOSITION if c.package_id != dropped)
        return real_builder(compositions=kept, **kwargs)

    monkeypatch.setattr(runtime_module, "build_runtime_registry", _mutilated)
    kinds = _gate_red(sandbox, stored)
    assert kinds & {"reconciliation_drift", "reconciliation_membership_drift"}, kinds


def test_probe_b_adding_a_fake_runtime_operation_is_red(
    monkeypatch: pytest.MonkeyPatch, sandbox, stored
):
    """B: register an operation the catalogue never defined and no pack ships."""
    from nexus_ai_agent.creative.packs import runtime as runtime_module
    from nexus_ai_agent.creative.studio.capabilities import OperationSpec

    real_builder = runtime_module.build_runtime_registry

    def _inflated(**kwargs: Any):
        registry = real_builder(**kwargs)
        real = registry.get_spec("timeline.trim")
        registry.register_operation(
            domain="timeline",
            capability="editor",
            spec=OperationSpec(
                operation_id="timeline.fake_inflation",
                description="invented by a mutation probe",
                permission_level=real.permission_level,
                input_model=real.input_model,
                handler=real.handler,
            ),
        )
        return registry

    monkeypatch.setattr(runtime_module, "build_runtime_registry", _inflated)
    kinds = _gate_red(sandbox, stored)
    assert kinds & {
        "operation_added",
        "reconciliation_drift",
        "reconciliation_membership_drift",
    }, kinds


# --------------------------------------------------------------------------- #
# C / D — surface source
# --------------------------------------------------------------------------- #


def test_probe_c_removing_a_surface_mapping_is_red(
    monkeypatch: pytest.MonkeyPatch, sandbox, stored
):
    """C: the worker stops dispatching an operation the surface still advertises."""
    from nexus_ai_agent.creative import render_jobs

    mutilated = {
        pair: canonical
        for pair, canonical in render_jobs.SURFACE_TO_CANONICAL.items()
        if pair != ("edit", "trim")
    }
    monkeypatch.setattr(render_jobs, "SURFACE_TO_CANONICAL", mutilated)
    kinds = _gate_red(sandbox, stored)
    assert kinds & {"surface_drift", "surface_probe_drift", "surface_contract_drift"}, kinds


def test_probe_d_adding_a_fake_surface_mapping_is_red(
    monkeypatch: pytest.MonkeyPatch, sandbox, stored
):
    """D: claim ``/edit deess`` is reachable — the fake-surface-mapping attack."""
    from nexus_ai_agent.creative import render_jobs

    mutilated = dict(render_jobs.SURFACE_TO_CANONICAL)
    mutilated[("edit", "deess")] = "audio.deess"
    monkeypatch.setattr(render_jobs, "SURFACE_TO_CANONICAL", mutilated)
    kinds = _gate_red(sandbox, stored)
    # ``audio.deess`` is NOT in the surface allow-list, so this is exactly the
    # "surface accepts what nothing executes" drift the probe must catch.
    assert kinds & {
        "surface_drift",
        "surface_probe_drift",
        "surface_contract_drift",
        "surface_command_drift",
    }, kinds


# --------------------------------------------------------------------------- #
# E — product catalogue source
# --------------------------------------------------------------------------- #


def test_probe_e_changing_the_catalog_count_is_red(sandbox, stored):
    """E: delete the ``timeline.trim`` row — the row PR #70's gate ignored."""
    catalog = sandbox / sources.CATALOG_PATH
    lines = catalog.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith("| `timeline.trim`")]
    assert len(kept) == len(lines) - 1, "the probe must actually remove one row"
    catalog.write_text("".join(kept), encoding="utf-8")

    kinds = _gate_red(sandbox, stored)
    assert kinds & {"reconciliation_drift", "reconciliation_membership_drift"}, kinds


def test_probe_e2_an_empty_catalogue_is_refused_not_ignored(sandbox, stored):
    """E2: a catalogue with no pack table must fail loudly, never measure zero."""
    catalog = sandbox / sources.CATALOG_PATH
    catalog.write_text("# emptied by a mutation probe\n", encoding="utf-8")
    kinds = _gate_red(sandbox, stored)
    assert any(kind.startswith("SourceError") for kind in kinds), kinds


# --------------------------------------------------------------------------- #
# F — maturity vocabulary
# --------------------------------------------------------------------------- #


def test_probe_f_mutating_the_maturity_ladder_is_red(sandbox, stored):
    """F: silently redefine ``L4`` in the maturity document.

    The mutation is deliberately wording-independent: it locates whatever line
    currently defines ``L4`` and appends to it.  A probe that matched one exact
    sentence would pass trivially the first time that sentence was edited — and
    silently stop testing anything.
    """
    maturity = sandbox / "docs/L0_L4_MATURITY.md"
    lines = maturity.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, line in enumerate(lines):
        if "L4" in line and any(sep in line for sep in ("|", "=", "—")):
            lines[index] = line.rstrip("\n") + " **PRODUCTION_PROVEN**\n"
            break
    else:  # pragma: no cover - the document must define L4 somewhere
        pytest.fail("the maturity document no longer defines L4 — the probe is stale")
    maturity.write_text("".join(lines), encoding="utf-8")

    kinds = _gate_red(sandbox, stored)
    assert kinds & {"maturity_vocabulary_changed", "contract_drift_changed"}, kinds


def test_probe_f2_a_new_competing_ladder_is_red(sandbox, stored):
    """F2: add a fourth, incompatible ``L3`` definition to a declared source."""
    contract = sandbox / "docs/architecture/COMMAND_CAPABILITY_CONTRACT.md"
    contract.parent.mkdir(parents=True, exist_ok=True)
    existing = contract.read_text(encoding="utf-8") if contract.is_file() else ""
    contract.write_text(existing + "\n\nL3 = whatever the author felt like.\n", encoding="utf-8")
    kinds = _gate_red(sandbox, stored)
    assert kinds & {"maturity_vocabulary_changed", "contract_drift_changed"}, kinds


# --------------------------------------------------------------------------- #
# G — evidence state (the consumed Gate 4 matrix)
# --------------------------------------------------------------------------- #


def test_probe_g_mutating_a_gate4_verdict_is_red(sandbox, stored):
    """G: flip T02's Artifact cell from PASS to MISSING."""
    evidence = sandbox / sources.GATE4_EVIDENCE
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["matrix"]["Artifact"]["T02"] == "PASS"
    payload["matrix"]["Artifact"]["T02"] = "MISSING"
    evidence.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    kinds = _gate_red(sandbox, stored)
    assert kinds & {"evidence_drift", "artifact_evidence_drift"}, kinds


def test_probe_g2_promoting_a_slice_operation_is_red(sandbox, stored):
    """G2: claim T01's Runtime cell is PASS when Gate 4 measured MISSING."""
    evidence = sandbox / sources.GATE4_EVIDENCE
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["matrix"]["Runtime"]["T01"] == "MISSING"
    payload["matrix"]["Runtime"]["T01"] = "PASS"
    evidence.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    kinds = _gate_red(sandbox, stored)
    assert kinds & {"evidence_drift"}, kinds


# --------------------------------------------------------------------------- #
# I / J — provenance chain
# --------------------------------------------------------------------------- #


def test_probe_i_altering_the_t02_artifact_is_red(sandbox, stored):
    """I: swap the recorded artifact digest so the reopen link no longer matches."""
    evidence = sandbox / sources.GATE4_EVIDENCE
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["provenance"]["artifact"]["sha256"] = "sha256:" + "de" * 32
    evidence.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Re-grade the verdicts so the probe isolates the *chain*, not the matrix.
    kinds = _gate_red(sandbox, stored)
    assert kinds & {"provenance_chain_broken", "provenance_drift"}, kinds


def test_probe_j_altering_the_command_reference_is_red(sandbox, stored):
    """J: point the chain's command at a different operation."""
    evidence = sandbox / sources.GATE4_EVIDENCE
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["provenance"]["command_id"] = "cmd-gate4-T99-scene.remove_object"
    evidence.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    kinds = _gate_red(sandbox, stored)
    assert kinds & {"provenance_chain_broken", "provenance_drift"}, kinds


def test_probe_j2_a_missing_command_reference_is_red(sandbox, stored):
    """J2: delete the command id entirely — a broken link is not a pass."""
    evidence = sandbox / sources.GATE4_EVIDENCE
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["provenance"].pop("command_id", None)
    evidence.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    kinds = _gate_red(sandbox, stored)
    assert kinds & {"provenance_chain_broken", "provenance_drift"}, kinds


# --------------------------------------------------------------------------- #
# H — owner, with the exemption stated precisely
# --------------------------------------------------------------------------- #


def test_probe_h_owner_cannot_be_fabricated(stored):
    """H: ``owner`` has no source, so the projection records ``NOT_AVAILABLE``.

    Precise exemption: no repository source names an operation owner.  The
    mutation this probe can run is therefore not "change the owner" but "make
    the gate invent one" — and the gate must refuse.  A hand-written matrix can
    assert any owner it likes (PR #70 asserted ``Agent B (Creative)`` for 57
    rows and ``Agent 1 (Runtime)`` for 23, from no source); this gate cannot.
    """
    owners = {node["owner"] for node in stored["operations"]}
    assert owners == {truth.NOT_AVAILABLE}, owners

    # What the gate reports instead IS sourced: the pack that declares the
    # operation, or the Wave-1 core registry for the five pack-less operations.
    fresh = truth.build_projection()
    registrar_sources = {
        node["registrar_pack"]
        for node in fresh["operations"]
        if node["source"]["registered_in"] is not None
    }
    assert truth.NOT_AVAILABLE not in registrar_sources, registrar_sources
    assert any("build_wave1_registry" in source for source in registrar_sources), registrar_sources


def test_probe_h2_injected_owner_drifts(sandbox, stored):
    """H2: a projection whose ``owner`` disagrees with the recomputation is red."""
    tampered = json.loads(json.dumps(stored))
    tampered["operations"][0]["owner"] = "Agent 1 (Runtime)"
    findings = truth.compare(tampered, truth.build_projection(root=sandbox))
    assert "owner_drift" in _kinds(findings), _kinds(findings)


# --------------------------------------------------------------------------- #
# the exclusions are deliberate, not accidental
# --------------------------------------------------------------------------- #


def test_volatile_fields_do_not_produce_false_positives(sandbox, stored):
    """A new timestamp or revision must never red the gate on its own.

    Gate 2.2 is a drift gate, not a clock.  ``generated_at`` and
    ``source_revision`` change on every run and on every commit; if they were
    compared, the gate would be permanently red and quickly ignored.
    """
    tampered = json.loads(json.dumps(stored))
    tampered["generated"]["generated_at"] = "1999-01-01T00:00:00Z"
    tampered["generated"]["source_revision"] = "0" * 40
    tampered["provenance"]["T02"]["links"]["artifact"]["sha256"] = "sha256:" + "ab" * 32
    findings = truth.compare(tampered, truth.build_projection(root=sandbox))
    assert not _kinds(findings) & {"provenance_drift", "reconciliation_drift"}, _kinds(findings)


def test_probe_a2_an_unloadable_source_is_named_not_a_traceback():
    """A2: a tree whose sources cannot load must fail *as a finding*, not a crash.

    Measured against a real mutation: swapping one pack's registrar for another
    in ``creative/packs/runtime.py`` raises ``ValueError: duplicate operation:
    'audio.detect_beats'`` while the registry is built.  That is still a red
    build, but a traceback names a Python frame rather than the rule the tree
    broke.  The CLI contract is therefore: a source-level load failure exits
    ``EXIT_SOURCE_UNREADABLE`` (2) — distinct from ``1`` ("loaded, and it
    disagrees") — and prints the finding.

    The real tree is edited, so the restore is **byte-exact and file-scoped**.
    An earlier version of this test restored with ``git checkout -- src``; it
    passed while silently reverting an unrelated uncommitted change, because
    the constant it asserted on had already been imported.  Never widen this
    to a directory-level checkout.
    """
    import subprocess  # noqa: PLC0415 - one-shot CLI probe, not an import cycle
    import sys  # noqa: PLC0415

    from nexus_ai_agent.nagar.__main__ import EXIT_SOURCE_UNREADABLE  # noqa: PLC0415

    target = REPO_ROOT / "src/nexus_ai_agent/creative/packs/runtime.py"
    original = target.read_bytes()
    mutated = original.replace(
        b'        "nexus.color.delivery",\n        _register_delivery,',
        b'        "nexus.color.delivery",\n        _register_audio,',
        1,
    )
    assert mutated != original, "the composition table changed shape - probe A2 is stale"

    try:
        target.write_bytes(mutated)
        result = subprocess.run(
            [sys.executable, "-m", "nexus_ai_agent.nagar", "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
    finally:
        target.write_bytes(original)

    assert target.read_bytes() == original, "probe A2 failed to restore the tree byte-exactly"
    assert result.returncode == EXIT_SOURCE_UNREADABLE, (
        f"expected the named-load-failure contract, got exit {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "Traceback" not in result.stderr, (
        f"the gate crashed instead of reporting:\n{result.stderr}"
    )
    assert "duplicate operation" in result.stderr, result.stderr
