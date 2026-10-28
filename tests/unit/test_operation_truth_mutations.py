"""Adversarial mutation probes: every guard must be exercised, not assumed.

Each probe follows the same contract — **GREEN → MUTANT → RED → RESTORE →
GREEN**:

1. a recomputation over an untouched sandbox agrees with the stored
   projection (GREEN);
2. a *source* (or the stored artifact) is mutated — a pack dropped, a fake
   operation registered, a catalogue row deleted/renamed/duplicated, a
   surface mapping invented, a test file removed, a doc number edited, a
   maturity/proof claim fabricated (MUTANT);
3. the gate must turn red — either :func:`nexus_ai_agent.nagar.truth.compare`
   reports drift, :func:`nexus_ai_agent.nagar.truth.invariants` reports a
   structural violation, a source refuses to load
   (:class:`nexus_ai_agent.nagar.sources.SourceError`), or a generated
   document no longer matches a fresh rendering (RED);
4. the mutation is undone and the same recomputation agrees again
   (RESTORE → GREEN).

This is the property PR #70's hand-written projection lacked: its expected
values came from the document under test, so deleting a catalogue row or
rewriting every maturity level left the suite green.  Nothing here reads a
number out of the projection under test — expected reds are expressed as
*kinds* of findings, never as stored counts.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.nagar import docs_projection, sources, truth

REPO_ROOT = Path(__file__).parents[2]

#: Files the engine reads through a repository root, mirrored into the sandbox.
_SANDBOX_PATHS = (
    sources.CATALOG_PATH,
    sources.RENDER_JOBS,
    sources.SLIDESHOW_SERVICE,
    sources.BOT_HANDLERS,
    sources.PROOF_REGISTRY_PATH,
    sources.GATE4_EVIDENCE,
    *docs_projection.GENERATED_DOCS,
)


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    """A minimal repository root holding every root-relative source."""
    for relative in _SANDBOX_PATHS:
        source = REPO_ROOT / relative
        if not source.is_file():
            continue
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copytree(
        REPO_ROOT / "tests",
        tmp_path / "tests",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return tmp_path


@pytest.fixture()
def stored() -> dict[str, Any]:
    projection = truth.load_projection(REPO_ROOT)
    assert projection is not None, "OPERATION_TRUTH.json must exist (run the generator)"
    return projection


def _gate(sandbox: Path, stored: dict[str, Any]) -> set[str]:
    """Everything that could turn the gate red for this sandbox state."""
    red: set[str] = set()
    try:
        fresh = truth.build_projection(root=sandbox)
    except sources.SourceError as exc:
        return {f"SourceError: {exc}"}
    red.update(f"[{finding.kind}] {finding.detail}" for finding in truth.compare(stored, fresh))
    red.update(f"fresh-invariant: {violation}" for violation in truth.invariants(fresh))
    red.update(f"stored-invariant: {violation}" for violation in truth.invariants(stored))
    for path, expected in docs_projection.render_all(fresh).items():
        target = sandbox / path
        if not target.is_file():
            red.add(f"doc-missing: {path}")
        elif target.read_text(encoding="utf-8") != expected:
            red.add(f"doc-drift: {path}")
    return red


def _assert_green(sandbox: Path, stored: dict[str, Any]) -> None:
    red = _gate(sandbox, stored)
    assert red == set(), "gate red without a mutation:\n" + "\n".join(sorted(red))


def _assert_red(sandbox: Path, stored: dict[str, Any], expected_kinds: tuple[str, ...]) -> set[str]:
    red = _gate(sandbox, stored)
    assert red, (
        "the gate stayed GREEN under a source mutation — a stored projection or a "
        "generated document would be self-certifying again"
    )
    hit = {marker for marker in expected_kinds if any(marker in finding for finding in red)}
    assert hit, f"none of the expected red markers {expected_kinds} matched; got:\n" + "\n".join(
        sorted(red)
    )
    return red


def _mutate_file(path: Path, old: str, new: str) -> Callable[[], None]:
    original = path.read_text(encoding="utf-8")
    assert old in original, f"mutation target {old!r} not found in {path}"
    path.write_text(original.replace(old, new), encoding="utf-8")

    def restore() -> None:
        path.write_text(original, encoding="utf-8")

    return restore


# --------------------------------------------------------------------------- #
# Registry mutations (live code, monkeypatched)
# --------------------------------------------------------------------------- #


def test_probe_drop_a_pack_is_red(
    monkeypatch: pytest.MonkeyPatch, sandbox: Path, stored: dict[str, Any]
) -> None:
    """MUTANT: a shipped pack leaves the composition — the registry shrinks."""
    from nexus_ai_agent.creative.packs import runtime as runtime_module

    _assert_green(sandbox, stored)

    real_builder = runtime_module.build_runtime_registry
    dropped = "nexus.color.delivery"

    def _mutilated(**kwargs: Any):
        kwargs.pop("compositions", None)
        kept = tuple(c for c in runtime_module.COMPOSITION if c.package_id != dropped)
        return real_builder(compositions=kept, **kwargs)

    monkeypatch.setattr(runtime_module, "build_runtime_registry", _mutilated)
    _assert_red(
        sandbox,
        stored,
        (
            "reconciliation_drift",
            "reconciliation_membership_drift",
            "declared_not_registered",
            "missing_set_added",
        ),
    )

    monkeypatch.undo()  # RESTORE
    _assert_green(sandbox, stored)


def test_probe_fake_registration_is_red(
    monkeypatch: pytest.MonkeyPatch, sandbox: Path, stored: dict[str, Any]
) -> None:
    """MUTANT: an operation nobody declared is injected into the registry."""
    from nexus_ai_agent.creative.packs import runtime as runtime_module
    from nexus_ai_agent.creative.studio.capabilities import OperationSpec

    _assert_green(sandbox, stored)

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
    _assert_red(
        sandbox,
        stored,
        (
            "operation_added",
            "reconciliation_membership_drift",
            "registered_not_declared",
            "declaration_parity_drift",
        ),
    )

    monkeypatch.undo()  # RESTORE
    _assert_green(sandbox, stored)


# --------------------------------------------------------------------------- #
# Surface mutations
# --------------------------------------------------------------------------- #


def test_probe_remove_worker_mapping_is_red(
    monkeypatch: pytest.MonkeyPatch, sandbox: Path, stored: dict[str, Any]
) -> None:
    """MUTANT: the worker stops dispatching an operation the surface advertises."""
    from nexus_ai_agent.creative import render_jobs

    _assert_green(sandbox, stored)

    mutilated = {
        pair: canonical
        for pair, canonical in render_jobs.SURFACE_TO_CANONICAL.items()
        if pair != ("edit", "trim")
    }
    monkeypatch.setattr(render_jobs, "SURFACE_TO_CANONICAL", mutilated)
    _assert_red(
        sandbox,
        stored,
        (
            "surface_contract_drift",
            "surface_probe_drift",
            "surface_drift",
            "contract_drift_changed",
        ),
    )

    monkeypatch.undo()  # RESTORE
    _assert_green(sandbox, stored)


def test_probe_invent_worker_mapping_is_red(
    monkeypatch: pytest.MonkeyPatch, sandbox: Path, stored: dict[str, Any]
) -> None:
    """MUTANT: ``/edit deess`` is claimed reachable although the surface never
    allows it — the fake-mapping attack."""
    from nexus_ai_agent.creative import render_jobs

    _assert_green(sandbox, stored)

    mutilated = dict(render_jobs.SURFACE_TO_CANONICAL)
    mutilated[("edit", "deess")] = "audio.deess"
    monkeypatch.setattr(render_jobs, "SURFACE_TO_CANONICAL", mutilated)
    _assert_red(
        sandbox,
        stored,
        (
            "surface_contract_drift",
            "surface_probe_drift",
            "surface_drift",
            "contract_drift_changed",
        ),
    )

    monkeypatch.undo()  # RESTORE
    _assert_green(sandbox, stored)


def test_probe_surface_accepts_unmappable_operation_is_red(
    monkeypatch: pytest.MonkeyPatch, sandbox: Path, stored: dict[str, Any]
) -> None:
    """MUTANT: the surface allow-list advertises an operation with no worker
    path and no registered contract behind it."""
    from nexus_ai_agent.bot.creative_surface import CreativeSurfaceMapper

    _assert_green(sandbox, stored)

    original = CreativeSurfaceMapper.ALLOWED
    monkeypatch.setattr(
        CreativeSurfaceMapper,
        "ALLOWED",
        {**original, "edit": frozenset({*original["edit"], "ghost"})},
    )
    _assert_red(
        sandbox,
        stored,
        (
            "surface_contract_drift",
            "surface accepts requests nothing can execute",
            "contract_drift_changed",
            "surface_drift",
        ),
    )

    monkeypatch.undo()  # RESTORE
    _assert_green(sandbox, stored)


# --------------------------------------------------------------------------- #
# Catalogue mutations (sandbox files)
# --------------------------------------------------------------------------- #


def test_probe_delete_catalogue_row_is_red(sandbox: Path, stored: dict[str, Any]) -> None:
    """MUTANT: ``timeline.trim``'s catalogue row disappears — the row PR #70's
    guard used to ignore."""
    _assert_green(sandbox, stored)

    catalog_path = sandbox / sources.CATALOG_PATH
    original = catalog_path.read_text(encoding="utf-8")
    row_line = next(
        line for line in original.splitlines() if line.startswith("| `timeline.trim` |")
    )
    catalog_path.write_text(original.replace(row_line + "\n", "", 1), encoding="utf-8")
    _assert_red(
        sandbox,
        stored,
        (
            "reconciliation_drift",
            "reconciliation_membership_drift",
            "identity_drift",
        ),
    )
    catalog_path.write_text(original, encoding="utf-8")  # RESTORE
    _assert_green(sandbox, stored)


def test_probe_rename_operation_with_stale_id_is_red(sandbox: Path, stored: dict[str, Any]) -> None:
    """MUTANT: an operation is renamed in the catalogue while every other
    source still knows the old id (the stale-rename attack)."""
    _assert_green(sandbox, stored)

    catalog_path = sandbox / sources.CATALOG_PATH
    original = catalog_path.read_text(encoding="utf-8")
    mutated = original.replace("`timeline.trim`", "`timeline.trim_renamed`")
    assert mutated != original
    catalog_path.write_text(mutated, encoding="utf-8")
    _assert_red(
        sandbox,
        stored,
        (
            "operation_added",
            "missing_set_added",
            "reconciliation_membership_drift",
            "identity_drift",
        ),
    )
    catalog_path.write_text(original, encoding="utf-8")  # RESTORE
    _assert_green(sandbox, stored)


def test_probe_duplicate_operation_id_is_refused(sandbox: Path, stored: dict[str, Any]) -> None:
    """MUTANT: the same id appears on two catalogue rows — the source must
    refuse the measurement rather than deduplicate it."""
    _assert_green(sandbox, stored)

    catalog_path = sandbox / sources.CATALOG_PATH
    lines = catalog_path.read_text(encoding="utf-8").splitlines()
    row_line = next(line for line in lines if line.startswith("| `timeline.trim` |"))
    lines.insert(lines.index(row_line) + 1, row_line)
    catalog_path.write_text("\n".join(lines), encoding="utf-8")

    red = _gate(sandbox, stored)
    assert any("duplicate operation id" in marker for marker in red), red
    assert any(marker.startswith("SourceError") for marker in red), red

    # RESTORE
    lines.remove(row_line)
    catalog_path.write_text("\n".join(lines), encoding="utf-8")
    _assert_green(sandbox, stored)


def test_probe_empty_catalogue_is_refused_not_zero(sandbox: Path, stored: dict[str, Any]) -> None:
    """MUTANT: the catalogue stops defining anything — refusal, never 0=0."""
    _assert_green(sandbox, stored)

    catalog_path = sandbox / sources.CATALOG_PATH
    original = catalog_path.read_text(encoding="utf-8")
    stripped = "\n".join(
        line for line in original.splitlines() if not sources._OPERATION_ROW.match(line)
    )
    catalog_path.write_text(stripped, encoding="utf-8")

    red = _gate(sandbox, stored)
    assert any("no pack tables" in marker for marker in red), red

    catalog_path.write_text(original, encoding="utf-8")  # RESTORE
    _assert_green(sandbox, stored)


# --------------------------------------------------------------------------- #
# Proof-evidence mutations
# --------------------------------------------------------------------------- #


def test_probe_remove_the_only_evidence_file_is_red(sandbox: Path, stored: dict[str, Any]) -> None:
    """MUTANT: the test module that executes an operation vanishes — its
    proof tier must fall and its maturity must drop with it."""
    _assert_green(sandbox, stored)

    evidence = stored["proof"]["suite_executed"]
    # Find an operation whose execution evidence lives in exactly one file.
    candidates = [
        (operation_id, entry["files"])
        for operation_id, entry in evidence.items()
        if len(entry["files"]) == 1
    ]
    assert candidates, "no single-file evidence operation — probe needs a target"
    operation_id, (only_file,) = candidates[0]

    target = sandbox / only_file
    assert target.is_file(), only_file
    backup = target.read_bytes()
    target.unlink()

    red = _gate(sandbox, stored)
    assert red, "removing the sole evidence file left the gate green"
    assert any(
        marker in finding
        for finding in red
        for marker in ("evidence_drift", "proof_drift", "maturity_drift", "pipeline_drift")
    ), "\n".join(sorted(red))
    assert operation_id  # the probe is anchored to a real operation

    target.write_bytes(backup)  # RESTORE
    _assert_green(sandbox, stored)


def test_probe_fabricated_proof_claim_is_red(stored: dict[str, Any]) -> None:
    """MUTANT: the stored projection claims PROVEN proof while carrying no
    evidence — the “registry without executable evidence” class."""
    mutant = _deepcopy(stored)
    target = next(
        node
        for node in mutant["operations"]
        if node["pipeline"]["proof"] == "PROVEN" and node["evidence"]["suite_execution_kinds"]
    )
    target["evidence"]["suite_execution_kinds"] = []
    target["evidence"]["suite_reference_files"] = []

    red = _gate_for_stored(mutant)
    assert red, "a fabricated proof claim passed the invariants"
    assert any("proof PROVEN without executable evidence" in marker for marker in red), red


# --------------------------------------------------------------------------- #
# Fabrication attacks on the stored artifact
# --------------------------------------------------------------------------- #


def _deepcopy(projection: dict[str, Any]) -> dict[str, Any]:
    import copy

    return copy.deepcopy(projection)


def _gate_for_stored(mutant: dict[str, Any]) -> set[str]:
    """Invariants + fresh comparison against the *real* sources."""
    red: set[str] = set()
    red.update(f"stored-invariant: {v}" for v in truth.invariants(mutant))
    fresh = truth.build_projection(root=REPO_ROOT)
    red.update(f"[{f.kind}] {f.detail}" for f in truth.compare(mutant, fresh))
    return red


def test_probe_missing_marked_implemented_is_red(stored: dict[str, Any]) -> None:
    """MUTANT: a catalogue-only operation is quietly marked implemented."""
    mutant = _deepcopy(stored)
    target = next(
        node for node in mutant["operations"] if node["operation_id"] == "portrait.smooth_skin"
    )
    assert target["pipeline"]["registry"] == "MISSING"
    target["pipeline"]["registry"] = "PRESENT"
    target["pipeline"]["capability"] = "PRESENT"
    target["maturity"] = {"level": 1, "token": "L1", "definition": truth.LADDER["L1"]}

    red = _gate_for_stored(mutant)
    assert red, "a missing operation marked implemented passed the gate"
    assert any("missing and implemented" in marker for marker in red), red
    assert any("pipeline_drift" in marker or "maturity_drift" in marker for marker in red), red


def test_probe_l4_without_artifact_proof_is_red(stored: dict[str, Any]) -> None:
    """MUTANT: an operation is promoted to L4 although nothing measures
    production-like and no artifact proof exists."""
    mutant = _deepcopy(stored)
    target = next(node for node in mutant["operations"] if node["maturity"]["level"] == 1)
    target["maturity"] = {"level": 4, "token": "L4", "definition": "hand-written"}
    target["pipeline"]["proof"] = "PROVEN"
    target["pipeline"]["artifact"] = "PROVEN"
    target["pipeline"]["surface"] = "PRESENT"

    red = _gate_for_stored(mutant)
    assert red, "L4 fabrication passed the gate"
    assert any("L4 emitted" in marker for marker in red), red


def test_probe_l2_for_a_catalogue_only_operation_is_red(stored: dict[str, Any]) -> None:
    """MUTANT: a missing operation is claimed reachable (L2) with no source."""
    mutant = _deepcopy(stored)
    target = next(
        node for node in mutant["operations"] if node["operation_id"] == "scene.replace_sky"
    )
    target["maturity"] = {"level": 2, "token": "L2", "definition": "hand-written"}
    target["pipeline"]["surface"] = "PRESENT"

    red = _gate_for_stored(mutant)
    assert red, "L2 fabrication passed the gate"
    assert any(
        "without a surface-reachable entrypoint" in marker or "pipeline_drift" in marker
        for marker in red
    ), red


def test_probe_hidden_runtime_only_operation_is_red(stored: dict[str, Any]) -> None:
    """MUTANT: a runtime-only operation disappears from the recorded list."""
    mutant = _deepcopy(stored)
    hidden = "system.undo"
    recon = mutant["reconciliation"]
    assert hidden in recon["runtime_only"]
    recon["runtime_only"] = [op for op in recon["runtime_only"] if op != hidden]

    red = _gate_for_stored(mutant)
    assert red, "hiding a runtime-only operation passed the gate"
    assert any("runtime_only" in marker for marker in red), red


def test_probe_duplicated_projection_id_is_red(stored: dict[str, Any]) -> None:
    """MUTANT: the projection itself carries the same id twice."""
    mutant = _deepcopy(stored)
    mutant["operations"].append(dict(mutant["operations"][0]))
    red = _gate_for_stored(mutant)
    assert any("duplicated operation ids" in marker for marker in red), red


def test_probe_ladder_rewrite_is_red(stored: dict[str, Any]) -> None:
    """MUTANT: a ladder definition is rewritten in place (silent redefinition)."""
    mutant = _deepcopy(stored)
    mutant["ladder"]["L3"] = "reachable through a real entry point"
    red = _gate_for_stored(mutant)
    assert any("ladder" in marker for marker in red), red


def test_probe_stale_maturity_vocabulary_is_red(stored: dict[str, Any]) -> None:
    """MUTANT: the recorded ladder fingerprint no longer matches the code."""
    mutant = _deepcopy(stored)
    mutant["ladder_fingerprint"]["L2"] = "0" * 16
    mutant["contract_drift"] = [
        {
            **item,
            "detail": (
                {"L2": "0" * 16}
                if item.get("kind") == "ladder_vocabulary_fingerprints"
                else item.get("detail")
            ),
        }
        for item in mutant["contract_drift"]
    ]
    red = _gate_for_stored(mutant)
    assert any("ladder" in marker or "fingerprint" in marker for marker in red), red


# --------------------------------------------------------------------------- #
# Generated-document mutations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "relative",
    [path.as_posix() for path in docs_projection.GENERATED_DOCS],
)
def test_probe_any_generated_doc_edit_is_red(
    sandbox: Path, stored: dict[str, Any], relative: str
) -> None:
    """MUTANT: a single count inside a generated page is hand-edited."""
    _assert_green(sandbox, stored)

    target = sandbox / relative
    original = target.read_text(encoding="utf-8")
    token = next((t for t in ("70", "57", "47", "23", "80", "12") if t in original), None)
    if token is not None:
        target.write_text(original.replace(token, f"{token}9", 1), encoding="utf-8")
    else:  # a page without those counts: any byte edit must still be caught
        target.write_text(original + "\n<!-- hand-edited -->\n", encoding="utf-8")

    _assert_red(sandbox, stored, ("doc-drift",))

    target.write_text(original, encoding="utf-8")  # RESTORE
    _assert_green(sandbox, stored)


def test_probe_proof_registry_tamper_is_refused(sandbox: Path, stored: dict[str, Any]) -> None:
    """MUTANT: the recorded proof registry is corrupted or given a fake schema."""
    _assert_green(sandbox, stored)

    path = sandbox / sources.PROOF_REGISTRY_PATH
    original = path.read_text(encoding="utf-8")
    path.write_text("{ corrupted", encoding="utf-8")

    red = _gate(sandbox, stored)
    assert any("SourceError" in marker and "JSON" in marker for marker in red), red

    path.write_text(original, encoding="utf-8")  # RESTORE
    _assert_green(sandbox, stored)
