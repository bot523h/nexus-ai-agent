"""Operation truth: independent recomputation, evidence layers, and the gate.

This module contains no catalogue numbers and no registry counts of its own —
every number it reports is computed from :mod:`nexus_ai_agent.nagar.sources`
at call time, and every status is a *predicate over those sources*.  The
dependency is one-way:

    TDD + Runtime Registry + Executable Surface + Proof Registry
        -> recompute   (:func:`build_projection`)
        -> projection  (``OPERATION_TRUTH.json`` and the generated docs)

The stored projection and the generated Markdown are outputs.  The gate
(:func:`compare` + :func:`invariants`) fails when they disagree with a fresh
recomputation, which is what makes a hand-written number, a fabricated
maturity level, or a silently-renamed operation detectable.

The pipeline (nine stages, four statuses)
-----------------------------------------
Every operation of the universe (``catalog ∪ registry``) carries::

    PRODUCT -> CONTRACT -> REGISTRY -> CAPABILITY -> SURFACE -> COMMAND
            -> EXECUTION -> ARTIFACT -> PROOF

with a status from ``{MISSING, PARTIAL, PRESENT, PROVEN}`` per stage.  PROVEN
means the stage's claim is backed by executable evidence (the suite executes
it or a recorded proof exists); PRESENT is a static fact; PARTIAL is
design-only or wired-but-unproven.

The canonical L0–L4 ladder
--------------------------
The open pull requests defined ``L3``/``L4`` incompatibly; this module owns
the *single* definition (recorded in the projection's ``ladder`` block and
rendered verbatim into ``docs/L0_L4_MATURITY.md``):

* ``L0`` design  — a contract exists (catalogue row or registered spec)
* ``L1`` code    — registered and domain-ready (typed, named, deterministic)
* ``L2`` reachable — a live user-facing entrypoint names the operation
* ``L3`` operational — L2 **and** executable proof
* ``L4`` production-like — L3 **and** recorded artifact proof **and** a
  production-like measurement.  No repository source defines or measures
  production-like today, so the engine refuses to emit ``L4`` at all and
  records the reason instead of guessing.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexus_ai_agent.nagar import sources
from nexus_ai_agent.nagar.sources import (
    Catalog,
    CatalogRow,
    Declared,
    Proof,
    RenderLane,
    Runtime,
    SourceError,
    Surface,
)

#: Schema identifier of the projection this engine writes and checks.
SCHEMA = "nagar.operation_truth.v1"

#: The generation command, recorded verbatim in the projection header.
GENERATION_COMMAND = "python -m nexus_ai_agent.nagar"

#: Where the projection lives (repository root: a machine artifact).
PROJECTION_PATH = Path("OPERATION_TRUTH.json")

#: Status vocabulary for the pipeline stages.
STATUS_TOKENS = ("MISSING", "PARTIAL", "PRESENT", "PROVEN")

#: The nine pipeline stages, in order.
PIPELINE_STAGES = (
    "product",
    "contract",
    "registry",
    "capability",
    "surface",
    "command",
    "execution",
    "artifact",
    "proof",
)

#: The eight recorded evidence tokens (each an independent predicate).
EVIDENCE_TOKENS = (
    "defined",
    "registered",
    "domain_ready",
    "executor_ready",
    "surface_reachable",
    "suite_executed",
    "artifact_proven",
    "production_like",
)

#: The canonical ladder — the only L0–L4 definition this repository accepts.
LADDER: dict[str, str] = {
    "L0": (
        "design: a contract exists — the row appears in the product catalogue "
        "(docs/NAGAR_70_OPERATIONS_TDD.md pack tables) or a registered OperationSpec"
    ),
    "L1": (
        "code: registered in build_runtime_registry() and domain-ready "
        '(typed input model with extra="forbid", a named non-lambda handler, '
        "deterministic=True)"
    ),
    "L2": (
        "reachable: a live user-facing entrypoint (Telegram command → worker map, "
        "or /slideshow service) names the operation"
    ),
    "L3": (
        "operational: L2 and executable proof — the test suite executes the "
        "operation (dispatch/handler/service call sites) or the recorded proof "
        "registry carries an execution proof for it"
    ),
    "L4": (
        "production-like: L3 and a recorded artifact proof and a production-like "
        "measurement; NO repository source defines or measures production-like "
        "today, so this engine never emits L4"
    ),
}

#: The reason the engine emits no L4 (recorded in the projection, not guessed).
L4_UNAVAILABLE_REASON = (
    "no repository source defines or measures a production-like level; "
    "recording L4 would be an unsourced claim"
)

NOT_AVAILABLE = "NOT_AVAILABLE"

#: Wavelengths, computed from evidence (see _wave_for).
WAVES = ("NOW", "NEXT", "AFTER_VERTICAL_SLICE", "POST_MVP", "LATER")


def _count_by_category(operation_ids: tuple[str, ...] | list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for operation_id in operation_ids:
        category = operation_id.split(".", 1)[0]
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items()))


# --------------------------------------------------------------------------- #
# Reconciliation — pure arithmetic over the sources
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Reconciliation:
    """The measured set arithmetic.  Every number is computed, never stored."""

    catalog: tuple[str, ...]
    runtime: tuple[str, ...]
    surface: tuple[str, ...]
    proof_executed: tuple[str, ...]

    @property
    def overlap(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.catalog) & set(self.runtime)))

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.catalog) - set(self.runtime)))

    @property
    def runtime_only(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.runtime) - set(self.catalog)))

    @property
    def universe(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.catalog) | set(self.runtime)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "catalog_count": len(self.catalog),
            "runtime_count": len(self.runtime),
            "overlap_count": len(self.overlap),
            "missing_count": len(self.missing),
            "runtime_only_count": len(self.runtime_only),
            "universe_count": len(self.universe),
            "surface_count": len(self.surface),
            "suite_executed_count": len(self.proof_executed),
            "missing_by_category": _count_by_category(self.missing),
            "runtime_only_by_category": _count_by_category(self.runtime_only),
            "missing": list(self.missing),
            "runtime_only": list(self.runtime_only),
        }


# --------------------------------------------------------------------------- #
# Dependency graph over the catalogue's type signatures
# --------------------------------------------------------------------------- #


def _type_tokens(cell: str) -> set[str]:
    """Type names mentioned in an Input/Output cell (braces stripped)."""
    without_braces = re.sub(r"\{[^}]*\}", " ", cell)
    without_backticks = without_braces.replace("`", " ")
    return {
        token
        for token in re.split(r"[\s+/,&]+", without_backticks)
        if token and (token[0].isupper() or token[:1].isalpha()) and not token.startswith("http")
    }


def build_dependency_edges(catalog: Catalog) -> list[dict[str, str]]:
    """Edge ``a -> b`` when ``b`` consumes a type ``a`` produces.

    Purely mechanical over the catalogue's Input → Output cells: the type a
    family's primitive emits (``FaceTrackSet``, ``BeatGrid``, ``TranscriptRef``,
    ``StyledCaptionAsset``…) is exactly what its dependents read.  Renaming a
    type therefore rewrites the graph; nothing here is hand-curated.
    """
    outputs: dict[str, set[str]] = {}
    inputs: dict[str, set[str]] = {}
    for row in catalog.rows:
        left, _, right = row.io_spec.partition("→")
        inputs[row.operation_id] = _type_tokens(left)
        outputs[row.operation_id] = _type_tokens(right)

    edges: list[dict[str, str]] = []
    for producer in catalog.rows:
        produced = outputs[producer.operation_id]
        if not produced:
            continue
        for consumer in catalog.rows:
            if consumer.operation_id == producer.operation_id:
                continue
            shared = produced & inputs[consumer.operation_id]
            if shared:
                edges.append(
                    {
                        "from": producer.operation_id,
                        "to": consumer.operation_id,
                        "via": ",".join(sorted(shared)),
                    }
                )
    return sorted(edges, key=lambda edge: (edge["from"], edge["to"]))


def _wave_for(
    row: CatalogRow,
    *,
    pack_ships: bool,
    in_degree: int,
    out_degree: int,
) -> str:
    """Assign a missing operation to a wave from measured facts only.

    * ``NOW``            — no model required, the catalogue's pack already
      ships, and no *missing* operation blocks it (in-degree zero)
    * ``NEXT``           — no model required, the pack itself is missing, and
      nothing blocks it
    * ``AFTER_VERTICAL_SLICE`` — blocked: either a model-dependent family
      *primitive* (other missing operations wait on its output types) or an
      operation whose inputs come from such a primitive
    * ``POST_MVP``       — model-dependent and unblocked, permission level < C
    * ``LATER``          — model-dependent *and* permission level C
      (confirmation-heavy identity work)
    """
    if not row.model_dependent:
        if in_degree > 0:
            return "AFTER_VERTICAL_SLICE"
        return "NOW" if pack_ships else "NEXT"
    if out_degree > 0:
        return "AFTER_VERTICAL_SLICE"
    if row.permission_level.upper() == "C":
        return "LATER"
    return "POST_MVP"


def build_missing_set(
    catalog: Catalog,
    missing: tuple[str, ...],
    edges: list[dict[str, str]],
    shipping_packs: frozenset[str],
) -> list[dict[str, Any]]:
    """The canonical missing set — every field computed, none hand-written.

    Per missing operation: identity, capability (the pack the catalogue
    assigns it to), reason, dependency class, expected execution lane, test
    requirement (the repository's pack-test convention), pack requirement,
    locality, security implication, measurable complexity proxy, dependency
    edges, and wave.  A documentation edit cannot remove an operation from
    this list: it is recomputed from the catalogue minus the registry.
    """
    in_degree = {operation_id: 0 for operation_id in missing}
    out_degree = {operation_id: 0 for operation_id in missing}
    for edge in edges:
        if edge["from"] in in_degree and edge["to"] in in_degree:
            out_degree[edge["from"]] += 1
            in_degree[edge["to"]] += 1

    entries: list[dict[str, Any]] = []
    for operation_id in missing:
        row = catalog.by_id[operation_id]
        model = row.model_dependent
        engine = row.engine
        lane = _execution_lane(engine)
        pack_ships = row.pack_id in shipping_packs
        dependency_class = (
            "model_runtime"
            if model
            else ("pack_registration" if not pack_ships else "operation_registration")
        )
        level = row.permission_level.upper()
        security = f"permission level {level} per catalogue"
        if row.category == "portrait":
            security += (
                "; identity-sensitive (face data must stay local, confirm below "
                "confidence threshold)"
            )
        elif level == "C":
            security += "; requires explicit confirmation before dispatch"
        entries.append(
            {
                "operation_id": operation_id,
                "category": row.category,
                "pack_id": row.pack_id,
                "capability": row.pack_id,
                "t_id": row.t_id,
                "reason": (
                    "defined in the product catalogue; absent from the runtime registry "
                    "(no registrar ships it)"
                ),
                "dependency": dependency_class,
                "engine_evidence": engine,
                "io_evidence": row.io_spec,
                "execution_lane": lane,
                "test_requirement": (
                    f"tests/unit/test_{row.category}_pack.py + "
                    f"tests/architecture/test_{row.category}_pack_boundary.py"
                ),
                "pack_requirement": row.pack_id,
                "pack_ships_today": pack_ships,
                "locality": "LOCAL_PREFERRED" if model else "LOCAL_ONLY",
                "security": security,
                "permission_level": level,
                "complexity": (
                    "model-dependent (needs a local model lane: fetch/hash/license/cache)"
                    if model
                    else "reducer or filtergraph-level (no model)"
                ),
                "depends_on": sorted(edge["from"] for edge in edges if edge["to"] == operation_id),
                "needed_by": sorted(edge["to"] for edge in edges if edge["from"] == operation_id),
                "in_degree": in_degree[operation_id],
                "out_degree": out_degree[operation_id],
                "wave": _wave_for(
                    row,
                    pack_ships=pack_ships,
                    in_degree=in_degree[operation_id],
                    out_degree=out_degree[operation_id],
                ),
            }
        )
    return entries


def _execution_lane(engine: str) -> str:
    """Expected execution lane, derived from the catalogue's engine cell."""
    text = engine.lower()
    if "onnx" in text or "webgpu" in text:
        if "onnx" in text:
            return "analysis (ONNX/local model runtime)"
        return "preview (WebGPU/WebCodecs) + master fallback"
    if "ffmpeg" in text or "native" in text:
        return "master (FFmpeg/native)"
    return "pure reducer / typed domain handler"


# --------------------------------------------------------------------------- #
# Evidence layers and the per-operation node
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OperationNode:
    """One node of the evidence graph.

    Identity, source and status are sourced; ``owner`` is ``NOT_AVAILABLE``
    because no repository source names an owner for an operation — the field
    is never invented.
    """

    operation_id: str
    identity: dict[str, Any]
    pipeline: dict[str, str]
    maturity: dict[str, Any]
    evidence: dict[str, Any]
    source: dict[str, Any]
    owner: str = NOT_AVAILABLE
    registrar_pack: str = NOT_AVAILABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "identity": self.identity,
            "pipeline": self.pipeline,
            "maturity": self.maturity,
            "evidence": self.evidence,
            "source": self.source,
            "owner": self.owner,
            "registrar_pack": self.registrar_pack,
        }


def _status(value: bool, present: str = "PRESENT", absent: str = "MISSING") -> str:
    return present if value else absent


def compute_maturity(pipeline: dict[str, str], *, production_like: bool = False) -> int:
    """The ladder rung an operation's pipeline statuses justify.

    Monotone by construction: each rung requires the one below it, so no
    status ordering can promote an operation past a missing stage.
    """
    level = -1
    if pipeline["contract"] in ("PRESENT", "PARTIAL"):
        level = 0
    if level == 0 and pipeline["registry"] == "PRESENT" and pipeline["capability"] == "PRESENT":
        level = 1
    if level == 1 and pipeline["surface"] == "PRESENT":
        level = 2
    if level == 2 and pipeline["proof"] == "PROVEN":
        level = 3
    if level == 3 and pipeline["artifact"] == "PROVEN" and production_like:
        level = 4
    return max(level, 0)


def build_nodes(
    catalog: Catalog,
    runtime: Runtime,
    surface: Surface,
    lane: RenderLane,
    proof: Proof,
    gate4_runtime_passes: frozenset[str] = frozenset(),
) -> tuple[OperationNode, ...]:
    """Assemble one evidence node per operation of the universe."""
    from nexus_ai_agent.creative.render_jobs import SURFACE_TO_CANONICAL

    catalog_by_id = catalog.by_id
    runtime_by_id = runtime.by_id
    surface_ops = set(surface.operation_ids)
    lane_ops = set(lane.operations)
    worker_ops = set(surface.worker_closed_set.operations)
    slideshow_ops = set(surface.slideshow_entrypoint.operations)

    entrypoints: dict[str, str] = {
        canonical: f"/{command} {operation}"
        for (command, operation), canonical in sorted(SURFACE_TO_CANONICAL.items())
    }
    for operation_id in slideshow_ops:
        entrypoints.setdefault(operation_id, "/slideshow")

    runtime_only = set(runtime.operation_ids) - set(catalog.operation_ids)

    universe = sorted(set(catalog.operation_ids) | set(runtime.operation_ids))
    nodes: list[OperationNode] = []
    for operation_id in universe:
        row: CatalogRow | None = catalog_by_id.get(operation_id)
        registered = runtime_by_id.get(operation_id)
        entrypoint = entrypoints.get(operation_id)

        suite = proof.suite.get(operation_id)
        suite_kinds = suite.kinds if suite else ()
        suite_referenced = bool(suite and suite.files)
        recorded_execution = bool(proof.recorded_for(operation_id, "runtime_execution"))
        recorded_artifact = bool(proof.recorded_for(operation_id, "artifact_evidence"))
        recorded_production = bool(proof.recorded_for(operation_id, "production_like"))
        gate4_runtime = operation_id in gate4_runtime_passes

        suite_proven = bool(suite_kinds) or recorded_execution or gate4_runtime
        proof_status = "PROVEN" if suite_proven else ("PARTIAL" if suite_referenced else "MISSING")

        production_path = bool(
            operation_id in lane_ops or operation_id in worker_ops or operation_id in slideshow_ops
        )
        execution_status = (
            ("PROVEN" if suite_proven else "PRESENT") if production_path else "MISSING"
        )

        artifact_kind = row.artifact_kind if row is not None else "unspecified"
        if recorded_artifact:
            artifact_status = "PROVEN"
        elif production_path and artifact_kind == "materialized_file":
            artifact_status = "PARTIAL"  # path exists, artifact not proven
        elif production_path:
            artifact_status = "PRESENT"
        else:
            artifact_status = "MISSING"

        pipeline = {
            "product": _status(row is not None),
            "contract": (
                "PRESENT"
                if registered is not None
                else ("PARTIAL" if row is not None else "MISSING")
            ),
            "registry": _status(registered is not None),
            "capability": (
                "MISSING"
                if registered is None
                else ("PRESENT" if registered.domain_ready else "PARTIAL")
            ),
            "surface": _status(operation_id in surface_ops),
            "command": _status(entrypoint is not None),
            "execution": execution_status,
            "artifact": artifact_status,
            "proof": proof_status,
        }

        production_like = recorded_production
        level = compute_maturity(pipeline, production_like=production_like)

        if operation_id in runtime_only:
            status_flag = "RUNTIME_ONLY"
        elif row is None:
            status_flag = "MISSING"
        elif level >= 3:
            status_flag = "OPERATIONAL"
        elif operation_id in surface_ops:
            status_flag = "REACHABLE"
        else:
            status_flag = "REGISTERED"

        evidence: dict[str, Any] = {
            "suite_execution_kinds": list(suite_kinds),
            "suite_reference_files": list(suite.files) if suite else [],
            "recorded_execution_proof": recorded_execution,
            "recorded_artifact_proof": recorded_artifact,
            "recorded_proofs": [
                {
                    "kind": p.kind,
                    "method": p.method,
                    "recorded_at": p.recorded_at,
                    "source_revision": p.source_revision,
                }
                for p in sorted(
                    proof.recorded_for(operation_id, "runtime_execution")
                    + proof.recorded_for(operation_id, "artifact_evidence")
                    + proof.recorded_for(operation_id, "production_like"),
                    key=lambda p: (p.kind, p.recorded_at),
                )
            ],
            "gate4_runtime_verdict": ("PASS" if gate4_runtime else NOT_AVAILABLE),
            "artifact_kind": artifact_kind,
            "surface_entrypoint": entrypoint or NOT_AVAILABLE,
            "production_like_evidence": NOT_AVAILABLE,
            "production_like_reason": L4_UNAVAILABLE_REASON,
        }
        if registered is not None:
            evidence["input_model"] = registered.input_model
            evidence["handler"] = registered.handler_name
            evidence["permission_level"] = registered.permission_level

        source_map: dict[str, Any] = {
            "defined_in": f"{sources.CATALOG_PATH}:{row.line_number}" if row else None,
            "registered_in": "build_runtime_registry()" if registered else None,
            "surface_probe": (
                surface.worker_closed_set.source
                if operation_id in worker_ops
                else (
                    surface.slideshow_entrypoint.source if operation_id in slideshow_ops else None
                )
            ),
        }

        nodes.append(
            OperationNode(
                operation_id=operation_id,
                identity={
                    "t_id": row.t_id if row else NOT_AVAILABLE,
                    "category": operation_id.split(".", 1)[0],
                    "catalog_line": row.line_number if row else None,
                    "permission_level_catalog": row.permission_level if row else NOT_AVAILABLE,
                    "engine_catalog": row.engine if row else NOT_AVAILABLE,
                    "pack_id": row.pack_id if row else NOT_AVAILABLE,
                    "status_flag": status_flag,
                },
                pipeline=pipeline,
                maturity={
                    "level": level,
                    "token": f"L{level}",
                    "definition": LADDER[f"L{level}"],
                },
                evidence=evidence,
                source=source_map,
                registrar_pack=registered.registrar if registered else NOT_AVAILABLE,
            )
        )
    return tuple(nodes)


def _gate4_runtime_passes(gate4: dict[str, Any] | None) -> frozenset[str]:
    """Operation ids with a recorded ``Runtime = PASS`` in the optional
    cross-gate evidence file (absent file ⇒ the empty set, never a guess)."""
    if not gate4:
        return frozenset()
    slice_map = {str(k): str(v) for k, v in (gate4.get("slice") or {}).items()}
    runtime_verdicts = (gate4.get("matrix") or {}).get("Runtime") or {}
    return frozenset(
        slice_map[t_id]
        for t_id, verdict in runtime_verdicts.items()
        if t_id in slice_map and str(verdict) == "PASS"
    )


# --------------------------------------------------------------------------- #
# Contract drift — mechanical findings, never opinions
# --------------------------------------------------------------------------- #


def _digest(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def ladder_fingerprint() -> dict[str, str]:
    """Fingerprint of every canonical ladder definition line.

    Keyed by level, so *rewriting* a definition changes the digest and
    *adding* a competing ladder elsewhere is caught by the docs equality
    check (the generated page is rendered from :data:`LADDER` only).
    """
    return {level: _digest(text) for level, text in sorted(LADDER.items())}


def detect_contract_drift(
    surface: Surface,
    catalog: Catalog,
    runtime: Runtime,
    declared: Declared,
    proof: Proof,
) -> tuple[dict[str, Any], ...]:
    """Mechanical drift findings.  Each is a measurement, never an opinion."""
    findings: list[dict[str, Any]] = []

    for disagreement in surface.disagreements:
        findings.append({"kind": "surface_contract_drift", "detail": disagreement})

    registered_commands = set(surface.commands)
    mapped_commands = set(surface.mapped_commands)
    if registered_commands != mapped_commands:
        findings.append(
            {
                "kind": "surface_command_drift",
                "detail": (
                    "registered Telegram commands and mapped commands differ: "
                    f"registered={sorted(registered_commands)} mapped={sorted(mapped_commands)}"
                ),
            }
        )

    runtime_ids = set(runtime.operation_ids)
    surface_ids = set(surface.operation_ids)

    declared_ids = set(declared.operation_ids)
    unregistered = sorted(declared_ids - runtime_ids)
    if unregistered:
        findings.append(
            {
                "kind": "implementation_without_registry",
                "detail": f"declared operations never registered at runtime: {unregistered}",
            }
        )
    undeclared = sorted(runtime_ids - declared_ids)
    if undeclared:
        findings.append(
            {
                "kind": "registry_without_declaration",
                "detail": f"registered operations with no source declaration symbol: {undeclared}",
            }
        )

    issues = runtime.composition_issues
    if issues:
        findings.append({"kind": "composition_issues", "detail": "; ".join(issues)})

    surface_without_contract = sorted(surface_ids - runtime_ids)
    if surface_without_contract:
        findings.append(
            {
                "kind": "surface_operation_without_contract",
                "detail": (
                    f"surface operations lacking a registered contract: {surface_without_contract}"
                ),
            }
        )

    unproven_surface = sorted(
        operation_id
        for operation_id in surface_ids
        if operation_id in runtime_ids
        and not proof.suite_kinds(operation_id)
        and not proof.recorded_for(operation_id, "runtime_execution")
    )
    if unproven_surface:
        findings.append(
            {
                "kind": "surface_without_executable_evidence",
                "detail": (
                    f"surface-reachable operations with no executable evidence: {unproven_surface}"
                ),
            }
        )

    findings.append(
        {
            "kind": "ladder_vocabulary_fingerprints",
            "detail": ladder_fingerprint(),
            "note": "canonical L0–L4 definition digests; editing a definition changes these",
        }
    )
    return tuple(findings)


# --------------------------------------------------------------------------- #
# Projection
# --------------------------------------------------------------------------- #


def source_revision(root: Path | None = None) -> str:
    """The git revision the projection was generated from (``UNKNOWN`` off-git)."""
    base = root if root is not None else sources.REPO_ROOT
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=base,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return "UNKNOWN"
    return result.stdout.strip() or "UNKNOWN"


def build_projection(
    *, root: Path | None = None, generated_at: str | None = None
) -> dict[str, Any]:
    """Build the whole projection from the sources (the *only* way it is built)."""
    base = root if root is not None else sources.REPO_ROOT

    catalog = sources.read_product_catalog(base)
    runtime = sources.read_runtime_registry()
    surface = sources.read_executable_surface(base)
    lane = sources.read_render_lane(base)
    declared = sources.read_declared_operations()

    universe_ids = set(catalog.operation_ids) | set(runtime.operation_ids)
    proof = sources.read_proof(base, operation_ids=universe_ids)
    gate4 = sources.read_gate4_evidence(base)
    gate4_passes = _gate4_runtime_passes(gate4)

    edges = build_dependency_edges(catalog)
    reconciliation = Reconciliation(
        catalog=catalog.operation_ids,
        runtime=runtime.operation_ids,
        surface=surface.operation_ids,
        proof_executed=tuple(sorted(op for op, entry in proof.suite.items() if entry.executed)),
    )
    shipping_packs = frozenset(pack for op in runtime.operations for pack in op.required_packs)
    missing_set = build_missing_set(catalog, reconciliation.missing, edges, shipping_packs)
    nodes = build_nodes(catalog, runtime, surface, lane, proof, gate4_runtime_passes=gate4_passes)
    drift = detect_contract_drift(surface, catalog, runtime, declared, proof)

    return {
        "schema": SCHEMA,
        "generated": {
            "generated_from": [
                {
                    "source": "product_catalog",
                    "path": sources.CATALOG_PATH.as_posix(),
                    "rule": "rows of the pack tables, in document order (T01..Tnn)",
                },
                {
                    "source": "runtime_registry",
                    "path": "nexus_ai_agent.creative.packs.runtime.build_runtime_registry()",
                },
                {
                    "source": "executable_surface",
                    "path": (
                        "nexus_ai_agent.bot.creative_surface + "
                        "nexus_ai_agent.creative.render_jobs + "
                        + sources.SLIDESHOW_SERVICE.as_posix()
                    ),
                },
                {
                    "source": "declared_operations",
                    "path": "studio operation_id literals + pack OPERATION_* constants",
                },
                {
                    "source": "proof_registry",
                    "path": "tests/ AST execution probe + "
                    + sources.PROOF_REGISTRY_PATH.as_posix(),
                },
            ],
            "generation_command": GENERATION_COMMAND,
            "source_revision": source_revision(base),
            "generated_at": generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "deterministic_fields": [
                "reconciliation",
                "operations[*].pipeline",
                "operations[*].maturity",
                "operations[*].identity",
                "operations[*].evidence.suite_execution_kinds",
                "surface",
                "proof",
                "declaration_parity",
                "dependency_edges",
                "missing_set",
                "contract_drift",
                "ladder",
            ],
            "non_deterministic_fields": [
                "generated.generated_at",
                "generated.source_revision",
            ],
            "non_deterministic_note": (
                "only the generation timestamp and git revision vary between runs; "
                "every other field is recomputed from the sources"
            ),
        },
        "ladder": dict(LADDER),
        "ladder_fingerprint": ladder_fingerprint(),
        "l4_unavailable_reason": L4_UNAVAILABLE_REASON,
        "reconciliation": reconciliation.as_dict(),
        "declaration_parity": {
            "declared_count": declared.count,
            "registered_count": runtime.count,
            "declared_not_registered": sorted(
                set(declared.operation_ids) - set(runtime.operation_ids)
            ),
            "registered_not_declared": sorted(
                set(runtime.operation_ids) - set(declared.operation_ids)
            ),
        },
        "surface": {
            "commands": list(surface.commands),
            "pairs": [list(pair) for pair in sorted(surface.pairs)],
            "probes": {
                "surface_allow_list": {
                    "count": len(surface.allow_list.pairs),
                    "operations": list(surface.allow_list.operations),
                    "unresolved": [list(pair) for pair in surface.allow_list.unresolved],
                    "source": surface.allow_list.source,
                },
                "worker_closed_set": {
                    "count": len(surface.worker_closed_set.pairs),
                    "operations": list(surface.worker_closed_set.operations),
                    "source": surface.worker_closed_set.source,
                },
                "slideshow_entrypoint": {
                    "count": len(surface.slideshow_entrypoint.operations),
                    "operations": list(surface.slideshow_entrypoint.operations),
                    "source": surface.slideshow_entrypoint.source,
                },
            },
            "derived_operation_count": len(surface.operation_ids),
            "render_lane_operations": list(lane.operations),
            "disagreements": list(surface.disagreements),
        },
        "proof": {
            "suite_probe_rule": (
                "operation id (string literal or imported OPERATION_* constant) in a test "
                "module that also calls .dispatch/._dispatch/.handler or a slideshow "
                "service entrypoint; the truth engine's own test files are excluded "
                "(they observe the engine, they do not evidence operations)"
            ),
            "suite_executed": {
                operation_id: {
                    "kinds": list(entry.kinds),
                    "files": list(entry.files),
                }
                for operation_id, entry in sorted(proof.suite.items())
                if entry.executed
            },
            "suite_referenced_only": sorted(
                operation_id
                for operation_id, entry in sorted(proof.suite.items())
                if entry.files and not entry.executed
            ),
            "recorded_registry": {
                "path": sources.PROOF_REGISTRY_PATH.as_posix(),
                "schema": proof.registry_schema,
                "present": proof.registry_present,
                "count": len(proof.recorded),
                "entries": [
                    {
                        "operation_id": p.operation_id,
                        "kind": p.kind,
                        "method": p.method,
                        "recorded_at": p.recorded_at,
                        "source_revision": p.source_revision,
                    }
                    for p in proof.recorded
                ],
            },
            "gate4_evidence": {
                "path": sources.GATE4_EVIDENCE.as_posix(),
                "present": gate4 is not None,
                "runtime_pass": sorted(gate4_passes),
            },
        },
        "dependency_edges": edges,
        "missing_set": missing_set,
        "operations": [node.as_dict() for node in nodes],
        "contract_drift": [dict(item) for item in drift],
    }


# --------------------------------------------------------------------------- #
# Invariants — structural rules that hold for ANY honest projection
# --------------------------------------------------------------------------- #


def invariants(projection: dict[str, Any]) -> tuple[str, ...]:
    """Violations of the structural truth rules (empty = honest projection).

    These run against both the stored projection and the fresh recomputation,
    so a mutated source *or* a hand-edited artifact turns the gate red even
    when :func:`compare` alone would not see it (fabricated maturity, L4
    without proof, duplicated ids, surface beyond contract, …).
    """
    violations: list[str] = []

    operations = projection.get("operations") or []
    ids = [node.get("operation_id") for node in operations]
    duplicates = sorted({operation_id for operation_id in ids if ids.count(operation_id) > 1})
    if duplicates:
        violations.append(f"duplicated operation ids: {duplicates}")

    recon = projection.get("reconciliation") or {}
    missing_list = recon.get("missing") or []
    runtime_only_list = recon.get("runtime_only") or []
    missing_ids = set(missing_list)
    runtime_only_ids = set(runtime_only_list)

    known_statuses = set(STATUS_TOKENS)
    for node in operations:
        operation_id = node.get("operation_id", "?")
        pipeline = node.get("pipeline") or {}
        if set(pipeline) != set(PIPELINE_STAGES):
            violations.append(
                f"{operation_id}: pipeline stages differ from the canonical nine: "
                f"{sorted(pipeline)}"
            )
            continue
        bad = {stage: status for stage, status in pipeline.items() if status not in known_statuses}
        if bad:
            violations.append(f"{operation_id}: illegal pipeline statuses {bad}")

        level = (node.get("maturity") or {}).get("level")
        if level not in (0, 1, 2, 3, 4):
            violations.append(f"{operation_id}: maturity level {level!r} is not L0..L4")
            continue

        # Ladder monotonicity — the anti-fraud core.
        if level >= 1 and not (
            pipeline["registry"] == "PRESENT" and pipeline["capability"] == "PRESENT"
        ):
            violations.append(
                f"{operation_id}: L{level} without registered, domain-ready code "
                f"(registry={pipeline['registry']}, capability={pipeline['capability']})"
            )
        if level >= 2 and pipeline["surface"] != "PRESENT":
            violations.append(f"{operation_id}: L{level} without a surface-reachable entrypoint")
        if level >= 3 and pipeline["proof"] != "PROVEN":
            violations.append(f"{operation_id}: L{level} without PROVEN executable evidence")
        if level >= 4:
            violations.append(
                f"{operation_id}: L4 emitted although no production-like source exists "
                f"({L4_UNAVAILABLE_REASON})"
            )

        if pipeline.get("surface") == "PRESENT" and pipeline.get("registry") != "PRESENT":
            violations.append(f"{operation_id}: surface-reachable without a registered contract")
        if pipeline.get("command") == "PRESENT" and pipeline.get("surface") != "PRESENT":
            violations.append(f"{operation_id}: command entrypoint without surface reachability")
        if pipeline.get("execution") in ("PRESENT", "PROVEN") and (
            pipeline.get("registry") != "PRESENT"
        ):
            violations.append(f"{operation_id}: execution path without registration")

        if pipeline.get("proof") == "PROVEN":
            evidence = node.get("evidence") or {}
            if not (
                evidence.get("suite_execution_kinds")
                or evidence.get("recorded_execution_proof")
                or evidence.get("gate4_runtime_verdict") == "PASS"
            ):
                violations.append(f"{operation_id}: proof PROVEN without executable evidence")

        if node.get("owner") != NOT_AVAILABLE:
            violations.append(
                f"{operation_id}: owner {node.get('owner')!r} fabricated — no source names owners"
            )

        # Reconciliation membership and node status must tell the same story.
        if operation_id in missing_ids and pipeline["registry"] != "MISSING":
            violations.append(
                f"{operation_id}: listed in reconciliation.missing but pipeline.registry="
                f"{pipeline['registry']} — an operation cannot be missing and implemented"
            )
        if operation_id in runtime_only_ids and pipeline["product"] != "MISSING":
            violations.append(
                f"{operation_id}: listed as runtime-only but pipeline.product={pipeline['product']}"
            )
        if (
            operation_id not in missing_ids
            and operation_id not in runtime_only_ids
            and operation_id in ids
            and (pipeline["registry"] != "PRESENT" or pipeline["product"] != "PRESENT")
        ):
            violations.append(
                f"{operation_id}: in the overlap set but pipeline shows "
                f"product={pipeline['product']} registry={pipeline['registry']}"
            )

    checks = (
        (
            "catalog_count",
            "overlap_count + missing_count",
            (recon.get("overlap_count", 0) + recon.get("missing_count", 0)),
        ),
        (
            "runtime_count",
            "overlap_count + runtime_only_count",
            (recon.get("overlap_count", 0) + recon.get("runtime_only_count", 0)),
        ),
        ("missing_count", "len(missing)", len(missing_list)),
        ("runtime_only_count", "len(runtime_only)", len(runtime_only_list)),
        (
            "universe_count",
            "catalog_count + runtime_only_count",
            (recon.get("catalog_count", 0) + recon.get("runtime_only_count", 0)),
        ),
    )
    for key, expr, value in checks:
        if recon.get(key) != value:
            violations.append(f"reconciliation.{key}={recon.get(key)!r} != {expr}={value!r}")

    parity = projection.get("declaration_parity") or {}
    for key in ("declared_not_registered", "registered_not_declared"):
        if parity.get(key):
            violations.append(f"declaration_parity.{key} not empty: {parity.get(key)}")

    if projection.get("ladder") != LADDER:
        violations.append("projection ladder differs from the canonical LADDER")

    seen_missing = set()
    for entry in projection.get("missing_set") or []:
        operation_id = entry.get("operation_id")
        if operation_id in seen_missing:
            violations.append(f"missing_set duplicates {operation_id}")
        seen_missing.add(operation_id)
        if entry.get("wave") not in WAVES:
            violations.append(f"missing_set {operation_id}: illegal wave {entry.get('wave')!r}")
    if recon.get("missing") is not None and sorted(seen_missing) != sorted(
        recon.get("missing") or []
    ):
        violations.append(
            "missing_set membership differs from reconciliation.missing: "
            f"set-only={sorted(seen_missing - set(recon.get('missing') or []))} "
            f"recon-only={sorted(set(recon.get('missing') or []) - seen_missing)}"
        )

    return tuple(violations)


# --------------------------------------------------------------------------- #
# Compare — stored projection vs fresh recomputation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Finding:
    """One gate finding: what drifted, and where."""

    kind: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"[{self.kind}] {self.detail}"


def compare(projection: dict[str, Any], fresh: dict[str, Any]) -> tuple[Finding, ...]:
    """Compare a stored projection with a fresh recomputation.

    Only deterministic fields are compared (timestamps and the git revision
    are excluded by construction), so a re-run on another machine does not
    produce a false positive.
    """
    findings: list[Finding] = []

    if projection.get("schema") != fresh.get("schema"):
        findings.append(
            Finding(
                "schema_mismatch",
                f"projection schema {projection.get('schema')!r} != {fresh.get('schema')!r}",
            )
        )

    stored_recon = projection.get("reconciliation") or {}
    fresh_recon = fresh["reconciliation"]
    for key in sorted(fresh_recon):
        if key in {"missing", "runtime_only", "missing_by_category", "runtime_only_by_category"}:
            continue
        if stored_recon.get(key) != fresh_recon[key]:
            findings.append(
                Finding(
                    "reconciliation_drift",
                    f"{key}: projection={stored_recon.get(key)!r} computed={fresh_recon[key]!r}",
                )
            )
    for key in ("missing", "runtime_only"):
        stored_set = set(stored_recon.get(key) or [])
        fresh_set = set(fresh_recon[key])
        if stored_set != fresh_set:
            findings.append(
                Finding(
                    "reconciliation_membership_drift",
                    f"{key}: projection-only={sorted(stored_set - fresh_set)} "
                    f"computed-only={sorted(fresh_set - stored_set)}",
                )
            )
    for key in ("missing_by_category", "runtime_only_by_category"):
        if (stored_recon.get(key) or {}) != (fresh_recon.get(key) or {}):
            findings.append(
                Finding(
                    "reconciliation_category_drift",
                    f"{key}: projection={stored_recon.get(key)!r} "
                    f"computed={fresh_recon.get(key)!r}",
                )
            )

    stored_ops = {node["operation_id"]: node for node in projection.get("operations") or []}
    fresh_ops = {node["operation_id"]: node for node in fresh["operations"]}
    for operation_id in sorted(set(stored_ops) | set(fresh_ops)):
        if operation_id not in stored_ops:
            findings.append(Finding("operation_added", f"{operation_id} exists in sources only"))
            continue
        if operation_id not in fresh_ops:
            findings.append(
                Finding("operation_removed", f"{operation_id} exists in projection only")
            )
            continue
        stored_node, fresh_node = stored_ops[operation_id], fresh_ops[operation_id]
        for stage in PIPELINE_STAGES:
            stored_status = (stored_node.get("pipeline") or {}).get(stage)
            fresh_status = (fresh_node.get("pipeline") or {}).get(stage)
            if stored_status != fresh_status:
                findings.append(
                    Finding(
                        "pipeline_drift",
                        f"{operation_id}.{stage}: projection={stored_status!r} "
                        f"computed={fresh_status!r}",
                    )
                )
        if stored_node.get("maturity") != fresh_node.get("maturity"):
            findings.append(
                Finding(
                    "maturity_drift",
                    f"{operation_id}: projection={stored_node.get('maturity')!r} "
                    f"computed={fresh_node.get('maturity')!r}",
                )
            )
        if stored_node.get("identity") != fresh_node.get("identity"):
            findings.append(
                Finding(
                    "identity_drift",
                    f"{operation_id}: projection identity differs from the catalogue measurement",
                )
            )
        if stored_node.get("owner") != fresh_node.get("owner"):
            findings.append(
                Finding("owner_drift", f"{operation_id}.owner differs from NOT_AVAILABLE")
            )
        if stored_node.get("registrar_pack") != fresh_node.get("registrar_pack"):
            findings.append(
                Finding(
                    "registrar_drift",
                    f"{operation_id}: registrar {stored_node.get('registrar_pack')!r} != "
                    f"{fresh_node.get('registrar_pack')!r}",
                )
            )
        stored_evidence = stored_node.get("evidence") or {}
        fresh_evidence = fresh_node.get("evidence") or {}
        for key in ("suite_execution_kinds", "suite_reference_files", "artifact_kind"):
            if stored_evidence.get(key) != fresh_evidence.get(key):
                findings.append(
                    Finding(
                        "evidence_drift",
                        f"{operation_id}.{key}: projection={stored_evidence.get(key)!r} "
                        f"computed={fresh_evidence.get(key)!r}",
                    )
                )

    for key in (
        "commands",
        "pairs",
        "derived_operation_count",
        "render_lane_operations",
        "disagreements",
    ):
        stored_surface = projection.get("surface") or {}
        fresh_surface = fresh["surface"]
        if stored_surface.get(key) != fresh_surface.get(key):
            findings.append(
                Finding(
                    "surface_drift",
                    f"surface.{key}: projection={stored_surface.get(key)!r} "
                    f"computed={fresh_surface.get(key)!r}",
                )
            )
    stored_probes = (projection.get("surface") or {}).get("probes") or {}
    for probe_name, fresh_probe in (fresh["surface"]["probes"]).items():
        stored_probe = stored_probes.get(probe_name) or {}
        for key in ("count", "operations", "unresolved"):
            if stored_probe.get(key) != fresh_probe.get(key):
                findings.append(
                    Finding(
                        "surface_probe_drift",
                        f"surface.probes.{probe_name}.{key}: projection={stored_probe.get(key)!r} "
                        f"computed={fresh_probe.get(key)!r}",
                    )
                )

    stored_parity = projection.get("declaration_parity") or {}
    if stored_parity != (fresh.get("declaration_parity") or {}):
        findings.append(
            Finding(
                "declaration_parity_drift",
                f"projection={stored_parity!r} computed={fresh.get('declaration_parity')!r}",
            )
        )

    stored_proof = projection.get("proof") or {}
    fresh_proof = fresh.get("proof") or {}
    for key in ("suite_executed", "suite_referenced_only", "recorded_registry", "gate4_evidence"):
        if (stored_proof.get(key) or {}) != (fresh_proof.get(key) or {}):
            findings.append(
                Finding(
                    "proof_drift",
                    f"proof.{key} differs between projection and recomputation",
                )
            )

    if (projection.get("dependency_edges") or []) != (fresh.get("dependency_edges") or []):
        findings.append(Finding("dependency_graph_drift", "type-dependency edges changed"))

    stored_missing = {
        entry.get("operation_id"): entry for entry in projection.get("missing_set") or []
    }
    fresh_missing = {entry.get("operation_id"): entry for entry in fresh.get("missing_set") or []}
    for operation_id in sorted(set(stored_missing) | set(fresh_missing)):
        if operation_id not in stored_missing:
            findings.append(
                Finding("missing_set_added", f"{operation_id} newly missing in the sources")
            )
        elif operation_id not in fresh_missing:
            findings.append(
                Finding(
                    "missing_set_removed",
                    f"{operation_id} recorded as missing but the sources now provide it",
                )
            )
        elif stored_missing[operation_id] != fresh_missing[operation_id]:
            changed = sorted(
                key
                for key in set(stored_missing[operation_id]) | set(fresh_missing[operation_id])
                if stored_missing[operation_id].get(key) != fresh_missing[operation_id].get(key)
            )
            findings.append(
                Finding(
                    "missing_set_drift",
                    f"{operation_id}: fields changed: {changed}",
                )
            )

    if (projection.get("ladder") or {}) != (fresh.get("ladder") or {}):
        findings.append(Finding("ladder_drift", "canonical L0–L4 definitions changed"))
    if (projection.get("ladder_fingerprint") or {}) != (fresh.get("ladder_fingerprint") or {}):
        findings.append(
            Finding(
                "ladder_fingerprint_drift",
                "a canonical ladder definition was rewritten — regenerate and re-review",
            )
        )

    stored_drift = {
        item.get("kind"): item.get("detail")
        for item in projection.get("contract_drift") or []
        if item.get("kind") != "ladder_vocabulary_fingerprints"
    }
    fresh_drift = {
        item.get("kind"): item.get("detail")
        for item in fresh.get("contract_drift") or []
        if item.get("kind") != "ladder_vocabulary_fingerprints"
    }
    if stored_drift != fresh_drift:
        findings.append(
            Finding(
                "contract_drift_changed",
                f"projection={stored_drift!r} computed={fresh_drift!r}",
            )
        )
    stored_fp = {
        item.get("kind"): item.get("detail")
        for item in projection.get("contract_drift") or []
        if item.get("kind") == "ladder_vocabulary_fingerprints"
    }
    fresh_fp = {
        item.get("kind"): item.get("detail")
        for item in fresh.get("contract_drift") or []
        if item.get("kind") == "ladder_vocabulary_fingerprints"
    }
    if stored_fp != fresh_fp:
        findings.append(
            Finding(
                "ladder_vocabulary_changed",
                "canonical ladder fingerprint differs between projection and code",
            )
        )

    return tuple(findings)


# --------------------------------------------------------------------------- #
# Load / store
# --------------------------------------------------------------------------- #


def load_projection(root: Path | None = None) -> dict[str, Any] | None:
    """Read the stored projection, or ``None`` when it has not been generated."""
    base = root if root is not None else sources.REPO_ROOT
    path = base / PROJECTION_PATH
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SourceError(f"{PROJECTION_PATH} is not valid JSON: {exc}") from exc


def write_projection(root: Path | None = None) -> Path:
    """Regenerate the projection file.  The only supported way to change it."""
    base = root if root is not None else sources.REPO_ROOT
    return dump_projection(build_projection(root=base), base)


def dump_projection(projection: dict[str, Any], root: Path) -> Path:
    """Serialize an already-computed projection to ``OPERATION_TRUTH.json``."""
    path = root / PROJECTION_PATH
    path.write_text(
        json.dumps(projection, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "EVIDENCE_TOKENS",
    "GENERATION_COMMAND",
    "LADDER",
    "L4_UNAVAILABLE_REASON",
    "NOT_AVAILABLE",
    "PIPELINE_STAGES",
    "PROJECTION_PATH",
    "SCHEMA",
    "STATUS_TOKENS",
    "WAVES",
    "Finding",
    "OperationNode",
    "Reconciliation",
    "build_dependency_edges",
    "build_missing_set",
    "build_nodes",
    "build_projection",
    "compare",
    "compute_maturity",
    "detect_contract_drift",
    "dump_projection",
    "invariants",
    "ladder_fingerprint",
    "load_projection",
    "source_revision",
    "write_projection",
]
