"""Operation truth: independent recomputation, evidence layers, and the gate.

This module is the Gate-2.2 measurement engine.  It contains no catalogue
numbers, no registry counts and no maturity claims of its own — every number it
reports is computed from :mod:`nexus_ai_agent.nagar.sources` at call time, and
every evidence layer has its **own** predicate over those sources.

Why it is built this way
------------------------
Gate 2.2 exists because an earlier Operation-Truth projection (PR #70) was a
hand-written JSON document whose ``maturity_level``/``runtime_proven`` columns
were never recomputed from anything.  Measurement during this gate showed the
consequence: deleting the ``timeline.trim`` row from the product catalogue, or
rewriting all nine ``L4`` rows to ``L0``, left its guard test **green**.

So this module inverts the dependency:

* the sources are read every run (:mod:`sources`);
* the layers are *predicates over those sources*, not stored flags;
* the projection (:func:`build_projection`) is an **output**;
* the gate (:func:`compare`) fails when the projection and a fresh
  recomputation disagree.

The eight evidence layers
-------------------------
They are deliberately not collapsed into a single L0–L4 ladder.  The
repository's open pull requests define ``L0..L4`` incompatibly (PR #68:
``L2 = reachable through a real entry point``; PR #70: ``L4 = surface
reachable``), so a bare ``L4`` token is not portable between documents.  Each
layer below is an independent, machine-checkable fact:

==========================  ==========================================
token                       independent predicate
==========================  ==========================================
``defined``                 the id is a row of the catalogue's pack tables
``registered``              ``build_runtime_registry()`` lists the id
``domain_ready``            its registered spec has a typed, ``extra="forbid"``
                            input model, a named handler, and ``deterministic``
``executor_ready``          the render lane has a ``canonical_id ==`` branch
``surface_reachable``       a live user-facing entrypoint reaches it
``runtime_proven``          Gate 4 recorded ``Runtime = PASS`` for it
``artifact_proven``         Gate 4 recorded ``Artifact = PASS`` for it
``production_like``         **NOT AVAILABLE** — no source defines or measures it
==========================  ==========================================

``runtime_proven`` is not ``registered``, and ``artifact_proven`` is not
``executor_ready``.  A pure state-mutating reducer is none of the latter three,
which is exactly the distinction Gate 4 proved and PR #70 erased.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexus_ai_agent.nagar import sources
from nexus_ai_agent.nagar.sources import (
    Catalog,
    Gate4,
    RenderLane,
    Runtime,
    SourceError,
    Surface,
)

#: Schema identifier of the projection this gate writes and checks.
SCHEMA = "nagar.operation_truth.v1"

#: The generation command, recorded verbatim in the projection header.
GENERATION_COMMAND = "python -m nexus_ai_agent.nagar"

#: Where the projection lives.  Kept at the repository root because it is a
#: machine artifact read by humans and by CI, not a documentation page.
PROJECTION_PATH = Path("OPERATION_TRUTH.json")

#: The eight evidence layers, in promotion order.
EVIDENCE_TOKENS: tuple[str, ...] = (
    "defined",
    "registered",
    "domain_ready",
    "executor_ready",
    "surface_reachable",
    "runtime_proven",
    "artifact_proven",
    "production_like",
)

#: Layers this gate can actually measure.  ``production_like`` is excluded on
#: purpose: nothing in the repository defines it or measures it, so claiming it
#: would be exactly the inflation Gate 2.2 exists to prevent.
MEASURABLE_TOKENS: tuple[str, ...] = EVIDENCE_TOKENS[:-1]

#: Evidence states a claim may carry.  ``NOT_AVAILABLE`` is a first-class
#: answer, not a failure: it means *no source exists yet*, which is the honest
#: record for job identity and artifact identity fields.
VERIFIED = "VERIFIED"
PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
INFERRED = "INFERRED"
NOT_VERIFIED = "NOT_VERIFIED"
MISSING = "MISSING"
NOT_AVAILABLE = "NOT_AVAILABLE"

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")

#: L0–L4 definition lines, frozen by ``file:level`` → normalized digest.
#:
#: The repository currently carries *three* incompatible ladders (see
#: ``docs/audits/GATE22_OPERATION_TRUTH_AUDIT_2026-09-24.md`` §8).  This gate
#: cannot pick a winner — that is an owner decision — but it can refuse to let
#: a fourth ladder, or a silent edit to an existing one, land unnoticed.
MATURITY_VOCABULARY_SOURCES: tuple[str, ...] = (
    "docs/architecture/COMMAND_CAPABILITY_CONTRACT.md",
    "docs/L0_L4_MATURITY.md",
    "docs/architecture/MODULE_MAP.md",
)

#: A line states a maturity definition when a level token is *bound* to something:
#: ``L3 = …`` (prose), ``| **L4** | …`` (this repository's maturity table), or
#: ``| L2 | …`` (its module map).
#:
#: A bare hyphen is deliberately **not** a separator: ``L5 --> L4 --> L3`` is a
#: mermaid dependency-direction diagram, not a maturity ladder, and freezing it
#: would red the gate on an unrelated edit.  Em/en dashes are, because they carry
#: prose definitions (``L4 — production-like``).
_LEVEL_DEFINITION = re.compile(r"L([0-4])\b\s*(?:=|—|–|:|\|)|\|\s*\**L([0-4])\**\s*\|")
_LEVEL_TOKEN = re.compile(r"\bL([0-4])\b")


# --------------------------------------------------------------------------- #
# Reconciliation — pure arithmetic over the sources
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Reconciliation:
    """The measured set arithmetic.  Every number is computed, never stored."""

    catalog: tuple[str, ...]
    runtime: tuple[str, ...]
    surface: tuple[str, ...]

    @property
    def catalog_count(self) -> int:
        return len(self.catalog)

    @property
    def runtime_count(self) -> int:
        return len(self.runtime)

    @property
    def surface_count(self) -> int:
        return len(self.surface)

    @property
    def overlap(self) -> tuple[str, ...]:
        """``|C ∩ R|`` — defined *and* dispatchable."""
        return tuple(sorted(set(self.catalog) & set(self.runtime)))

    @property
    def missing(self) -> tuple[str, ...]:
        """``|C − R|`` — defined in the catalogue, absent from the runtime."""
        return tuple(sorted(set(self.catalog) - set(self.runtime)))

    @property
    def runtime_only(self) -> tuple[str, ...]:
        """``|R − C|`` — dispatchable but never named in the catalogue."""
        return tuple(sorted(set(self.runtime) - set(self.catalog)))

    @property
    def universe(self) -> tuple[str, ...]:
        """``|C ∪ R|``."""
        return tuple(sorted(set(self.catalog) | set(self.runtime)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "catalog_count": self.catalog_count,
            "runtime_count": self.runtime_count,
            "overlap_count": len(self.overlap),
            "missing_count": len(self.missing),
            "runtime_only_count": len(self.runtime_only),
            "universe_count": len(self.universe),
            "surface_count": self.surface_count,
            "missing_by_category": _count_by_category(self.missing),
            "runtime_only_by_category": _count_by_category(self.runtime_only),
            "missing": list(self.missing),
            "runtime_only": list(self.runtime_only),
        }


def _count_by_category(operation_ids: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for operation_id in operation_ids:
        category = operation_id.split(".", 1)[0]
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items()))


# --------------------------------------------------------------------------- #
# Evidence layers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Layers:
    """One operation's evidence layers, each with the source that decided it."""

    defined: bool = False
    registered: bool = False
    domain_ready: bool = False
    executor_ready: bool = False
    surface_reachable: bool = False
    runtime_proven: bool = False
    artifact_proven: bool = False
    production_like: bool | None = None

    def status(self, token: str) -> bool | None:
        return getattr(self, token)


@dataclass(frozen=True)
class OperationNode:
    """One node of the evidence graph.

    §5 of the gate brief asks each node for *identity, source, status,
    evidence, owner*.  Everything here is sourced except ``owner``: no
    repository source names a human/agent owner for an operation, so the field
    is ``NOT_AVAILABLE`` rather than invented.  The pack that registers the
    operation is real and is reported as ``registrar_pack``.
    """

    operation_id: str
    identity: dict[str, Any]
    status: dict[str, Any]
    evidence: dict[str, Any]
    source: dict[str, Any]
    owner: str = NOT_AVAILABLE
    registrar_pack: str = NOT_AVAILABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "identity": self.identity,
            "source": self.source,
            "status": self.status,
            "evidence": self.evidence,
            "owner": self.owner,
            "registrar_pack": self.registrar_pack,
        }


def build_nodes(
    catalog: Catalog,
    runtime: Runtime,
    surface: Surface,
    lane: RenderLane,
    gate4: Gate4,
) -> tuple[OperationNode, ...]:
    """Assemble one evidence node per operation of the universe."""
    from nexus_ai_agent.creative.render_jobs import SURFACE_TO_CANONICAL

    catalog_by_id = catalog.by_id
    runtime_by_id = runtime.by_id
    surface_ops = set(surface.operation_ids)
    lane_ops = set(lane.operations)
    runtime_proven = gate4.proven("Runtime")
    artifact_proven = gate4.proven("Artifact")
    gate4_runtime = gate4.verdicts("Runtime")
    gate4_artifact = gate4.verdicts("Artifact")
    worker_ops = set(surface.worker_closed_set.operations)
    slideshow_ops = set(surface.slideshow_entrypoint.operations)
    entrypoints = {
        canonical: f"/{command} {operation}"
        for (command, operation), canonical in sorted(SURFACE_TO_CANONICAL.items())
    }

    universe = sorted(set(catalog.operation_ids) | set(runtime.operation_ids))
    nodes: list[OperationNode] = []
    for operation_id in universe:
        row = catalog_by_id.get(operation_id)
        registered = runtime_by_id.get(operation_id)

        layers = Layers(
            defined=row is not None,
            registered=registered is not None,
            domain_ready=registered is not None and registered.domain_ready,
            executor_ready=operation_id in lane_ops or operation_id in surface_ops,
            surface_reachable=operation_id in surface_ops,
            runtime_proven=operation_id in runtime_proven,
            artifact_proven=operation_id in artifact_proven,
            production_like=None,
        )

        entrypoint = entrypoints.get(operation_id)
        if entrypoint is None and operation_id in slideshow_ops:
            entrypoint = "/slideshow"

        evidence: dict[str, Any] = {
            "surface_entrypoint": entrypoint or NOT_AVAILABLE,
            "gate4_runtime_verdict": gate4_runtime.get(operation_id, NOT_AVAILABLE),
            "gate4_artifact_verdict": gate4_artifact.get(operation_id, NOT_AVAILABLE),
            "executor_lane": lane.source if operation_id in lane_ops else NOT_AVAILABLE,
            "production_like_evidence": NOT_AVAILABLE,
            "production_like_reason": (
                "no repository source defines or measures a production-like level; "
                "recording it would be an unsourced claim"
            ),
        }
        if registered is not None:
            evidence["input_model"] = registered.input_model
            evidence["handler"] = registered.handler_name
            evidence["permission_level"] = registered.permission_level

        nodes.append(
            OperationNode(
                operation_id=operation_id,
                identity={
                    "t_id": row.t_id if row else NOT_AVAILABLE,
                    "category": operation_id.split(".", 1)[0],
                    "catalog_line": row.line_number if row else None,
                },
                status={token: layers.status(token) for token in EVIDENCE_TOKENS},
                evidence=evidence,
                source={
                    "defined_in": f"{sources.CATALOG_PATH}:{row.line_number}" if row else None,
                    "registered_in": "build_runtime_registry()" if registered else None,
                    "surface_probe": (
                        surface.worker_closed_set.source
                        if operation_id in worker_ops
                        else (
                            surface.slideshow_entrypoint.source
                            if operation_id in slideshow_ops
                            else None
                        )
                    ),
                },
                registrar_pack=registered.registrar if registered else NOT_AVAILABLE,
            )
        )
    return tuple(nodes)


# --------------------------------------------------------------------------- #
# Provenance chain (§15) and artifact evidence (§14)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProvenanceChain:
    """The reconstructible ``operation → command → job → artifact → evidence`` chain."""

    operation_id: str
    links: dict[str, Any] = field(default_factory=dict)
    findings: tuple[str, ...] = ()

    @property
    def intact(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "links": self.links,
            "findings": list(self.findings),
            "intact": self.intact,
        }


def build_provenance_chain(root: Path | None = None, t_id: str = "T02") -> ProvenanceChain:
    """Reconstruct the proven chain from the consumed Gate-4 evidence.

    This does **not** re-run the slice.  It reads the recorded links and checks
    the ones a machine can check: the command id names the operation, the
    revision is a positive integer, the artifact digest is a sha256, the size is
    non-zero, and the reopening interpreter agrees with the recorded digest —
    the in-toto "products of one step are the materials of the next" rule,
    applied to the job/artifact boundary instead of a build.
    """
    base = root if root is not None else sources.REPO_ROOT
    payload = json.loads((base / sources.GATE4_EVIDENCE).read_text(encoding="utf-8"))

    slice_map: dict[str, str] = {str(k): str(v) for k, v in (payload.get("slice") or {}).items()}
    operation_id = slice_map.get(t_id)
    if operation_id is None:
        raise SourceError(f"Gate 4 evidence has no slice entry {t_id!r}")

    per_step = (payload.get("per_step_provenance") or {}).get(t_id) or {}
    provenance = payload.get("provenance") or {}
    reopen = payload.get("reopen") or {}
    artifact = provenance.get("artifact") or per_step.get("artifact") or {}

    findings: list[str] = []

    def _require(condition: bool, message: str) -> None:
        if not condition:
            findings.append(message)

    command_id = str(provenance.get("command_id", ""))
    _require(bool(command_id), "command_id is absent")
    _require(
        operation_id.replace(".", "-") in command_id or operation_id in command_id,
        f"command_id {command_id!r} does not name {operation_id!r}",
    )
    _require(
        str(provenance.get("operation_id", "")) == operation_id,
        "the chain's operation_id disagrees with the slice map",
    )

    revision = per_step.get("revision")
    _require(
        isinstance(revision, int) and revision >= 1, f"revision {revision!r} is not a positive int"
    )

    sha = str(artifact.get("sha256", ""))
    _require(bool(_SHA256.match(sha)), f"artifact digest {sha!r} is not a sha256:… literal")
    size = artifact.get("size_bytes")
    _require(isinstance(size, int) and size > 0, f"artifact size {size!r} is not > 0")

    fresh = reopen.get("fresh_interpreter") or {}
    _require(bool(fresh.get("exists")), "the reopening interpreter found no artifact")
    _require(
        str(fresh.get("sha256", "")) == sha,
        "reopened file digest does not match the recorded artifact digest",
    )
    _require(
        str(fresh.get("file_sha256", "")) == sha,
        "reopened file_sha256 does not match the recorded artifact digest",
    )
    _require(
        str(reopen.get("fresh_instance_status", "")) == "completed",
        "the fresh interpreter did not observe a completed job",
    )

    links: dict[str, Any] = {
        "operation_id": operation_id,
        "t_id": t_id,
        "command_id": command_id or NOT_AVAILABLE,
        "idempotency_key": provenance.get("idempotency_key", NOT_AVAILABLE),
        "job_id": NOT_AVAILABLE,
        "job_id_evidence": payload.get("volatile_ids_note", NOT_AVAILABLE),
        "revision": revision,
        "artifact": {
            "sha256": sha or NOT_AVAILABLE,
            "size_bytes": size,
            "duration_us": artifact.get("duration_us", NOT_AVAILABLE),
            "height": artifact.get("height", NOT_AVAILABLE),
        },
        "verification": {
            "reopen_status": reopen.get("fresh_instance_status", NOT_AVAILABLE),
            "reopen_digest_matches": str(fresh.get("sha256", "")) == sha and bool(sha),
            "resume_pending_reexecuted": reopen.get("resume_pending_reexecuted", NOT_AVAILABLE),
        },
        "bus_state_limitation": reopen.get("bus_state_limitation", NOT_AVAILABLE),
    }
    return ProvenanceChain(operation_id=operation_id, links=links, findings=tuple(findings))


def build_artifact_evidence(root: Path | None = None, t_id: str = "T02") -> dict[str, Any]:
    """§14 — the artifact facts for a proven operation, or ``NOT_AVAILABLE``.

    Gate 2.2 consumes Agent 1's artifact semantics; it never redefines them.
    Fields the schema does not carry (logical / spec / physical identity) are
    recorded as ``NOT_AVAILABLE`` with the reason, never inferred from a path.
    """
    base = root if root is not None else sources.REPO_ROOT
    payload = json.loads((base / sources.GATE4_EVIDENCE).read_text(encoding="utf-8"))
    artifact = (payload.get("provenance") or {}).get("artifact") or {}
    fresh = (payload.get("reopen") or {}).get("fresh_interpreter") or {}

    sha = str(artifact.get("sha256", ""))
    return {
        "operation_id": (payload.get("provenance") or {}).get("operation_id", NOT_AVAILABLE),
        "exists": bool(fresh.get("exists")),
        "size_bytes": artifact.get("size_bytes"),
        "size_positive": isinstance(artifact.get("size_bytes"), int)
        and int(artifact["size_bytes"]) > 0,
        "sha256": sha or NOT_AVAILABLE,
        "sha256_well_formed": bool(_SHA256.match(sha)),
        "probe": {
            "duration_us": artifact.get("duration_us", NOT_AVAILABLE),
            "height": artifact.get("height", NOT_AVAILABLE),
        },
        "verification_status": fresh.get("status", NOT_AVAILABLE),
        "logical_identity": NOT_AVAILABLE,
        "spec_identity": NOT_AVAILABLE,
        "physical_identity": NOT_AVAILABLE,
        "identity_reason": (
            "the JobQueuePort row and the render record carry a path and a digest, "
            "not logical/spec/physical artifact identities; Agent 1 owns that schema"
        ),
    }


def build_job_boundary() -> dict[str, Any]:
    """§13 — what Gate 2.2 consumes from the Job lifecycle, and what is absent.

    The gate does **not** implement job lifecycle.  It declares the fields it
    would consume and records the ones that do not exist yet as
    ``NOT_AVAILABLE``, so a later Job-lifecycle change has an explicit contract
    to satisfy instead of a silent gap.
    """
    from nexus_ai_agent.application.ports.job_queue import JobStatus

    return {
        "implemented_here": False,
        "consumed_fields": {
            "job_exists": {
                "available": True,
                "evidence": "docs/audits/GATE4_TRUTH_MATRIX.json reopen.fresh_instance_status",
            },
            "job_terminal_state": {
                "available": True,
                "evidence": f"JobStatus = [{', '.join(s.value for s in JobStatus)}]",
            },
            "runtime_outcome": {
                "available": True,
                "evidence": "creative/render_jobs.py typed error_code taxonomy",
            },
            "artifact_verification": {
                "available": True,
                "evidence": "reopen digest equality + probe facts",
            },
            "artifact_identity": {
                "available": False,
                "evidence": NOT_AVAILABLE,
                "reason": (
                    "no logical/spec/physical identity field exists in the port or the record"
                ),
            },
            "job_id_in_deterministic_artifact": {
                "available": False,
                "evidence": NOT_AVAILABLE,
                "reason": (
                    "uuid4 per run; Gate 4 excludes it from the deterministic artifact as volatile"
                ),
            },
            "retry_terminal_split": {
                "available": False,
                "evidence": NOT_AVAILABLE,
                "reason": (
                    "JobStatus has no FAILED_RETRYABLE/TERMINAL_FAILED states (a Gate 4 limitation)"
                ),
            },
        },
    }


# --------------------------------------------------------------------------- #
# Contract drift
# --------------------------------------------------------------------------- #


def _digest(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def read_maturity_vocabulary(root: Path | None = None) -> dict[str, str]:
    """Fingerprint every ``L0..L4`` definition line in the declared sources.

    Keyed by ``file:line:level``, so *adding*, *removing* and *rewriting* a
    definition are all visible: the first changes the key set, the second too,
    and the third changes the digest.  The gate therefore cannot let a new or
    silently redefined ladder into the repository — which matters because the
    open pull requests already disagree with each other about what ``L3`` and
    ``L4`` mean.
    """
    base = root if root is not None else sources.REPO_ROOT
    found: dict[str, str] = {}
    for relative in MATURITY_VOCABULARY_SOURCES:
        path = base / relative
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not _LEVEL_DEFINITION.search(line):
                continue
            for level in sorted(set(_LEVEL_TOKEN.findall(line))):
                found[f"{relative}:{number}:L{level}"] = _digest(line)
    return dict(sorted(found.items()))


def detect_contract_drift(
    surface: Surface,
    catalog: Catalog,
    runtime: Runtime,
    root: Path | None = None,
) -> tuple[dict[str, Any], ...]:
    """Mechanical drift findings.  Each is a measurement, never an opinion."""
    findings: list[dict[str, Any]] = []

    for disagreement in surface.disagreements:
        findings.append({"kind": "surface_contract_drift", "detail": disagreement})

    registered_commands = set(surface.commands)
    mapped_commands = {command for command, _ in surface.pairs}
    if mapped_commands != registered_commands:
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
    unsupported = sorted(surface_ids - runtime_ids)
    if unsupported:
        findings.append(
            {
                "kind": "surface_beyond_runtime",
                "detail": (
                    f"surface reaches operations the runtime does not register: {unsupported}"
                ),
            }
        )

    issues = runtime.composition_issues
    if issues:
        findings.append({"kind": "composition_issues", "detail": "; ".join(issues)})

    vocabulary = read_maturity_vocabulary(root)
    ladder_conflict = _ladder_conflict(root)
    if ladder_conflict:
        findings.append({"kind": "maturity_ladder_conflict", "detail": ladder_conflict})
    findings.append(
        {
            "kind": "maturity_vocabulary_fingerprints",
            "detail": vocabulary,
            "note": (
                "declared L0–L4 definition fingerprints; the gate reds when one of "
                "these lines changes or a new ladder appears in a declared source"
            ),
        }
    )
    return tuple(findings)


def _ladder_conflict(root: Path | None = None) -> str:
    """Report that two open changes define ``L0..L4`` differently.

    The conflict is *documented fact*, recorded here as a machine-readable
    string so it cannot quietly disappear: PR #68's contract page and PR #70's
    maturity page both define L0–L4 for Nagar operations, with incompatible
    semantics.  Gate 2.2 does not choose between them; it refuses to emit a
    bare level token and reports both.
    """
    base = root if root is not None else sources.REPO_ROOT
    ladders: dict[str, str] = {}
    for relative in (
        "docs/architecture/COMMAND_CAPABILITY_CONTRACT.md",
        "docs/L0_L4_MATURITY.md",
    ):
        path = base / relative
        if not path.is_file():
            continue
        definitions: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            tokens = sorted(set(_LEVEL_TOKEN.findall(line)))
            if not tokens or not _LEVEL_DEFINITION.search(line):
                continue
            for level in tokens:
                definitions.setdefault(f"L{level}", line.strip())
        if definitions:
            ladders[relative] = " | ".join(
                f"{level}={definition}" for level, definition in sorted(definitions.items())
            )
    if len({_digest(text) for text in ladders.values()}) <= 1:
        return ""
    return "; ".join(f"{name}: {text}" for name, text in sorted(ladders.items()))


# --------------------------------------------------------------------------- #
# Projection + gate
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
    gate4 = sources.read_gate4_evidence(base)

    reconciliation = Reconciliation(
        catalog=catalog.operation_ids,
        runtime=runtime.operation_ids,
        surface=surface.operation_ids,
    )
    nodes = build_nodes(catalog, runtime, surface, lane, gate4)

    return {
        "schema": SCHEMA,
        "generated": {
            "generated_from": [
                {
                    "source": "product_catalog",
                    "path": sources.CATALOG_PATH.as_posix(),
                    "rule": "rows of the seven pack tables, document order (T01..T70)",
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
                    "source": "gate4_evidence",
                    "path": sources.GATE4_EVIDENCE.as_posix(),
                    "consumed": "runtime/artifact/command/job layer verdicts",
                },
            ],
            "generation_command": GENERATION_COMMAND,
            "source_revision": source_revision(base),
            "generated_at": generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "deterministic_fields": [
                "reconciliation",
                "operations[*].status",
                "operations[*].identity.t_id",
                "operations[*].registrar_pack",
                "surface.probes",
                "contract_drift",
                "provenance.links.operation_id",
                "provenance.links.command_id",
                "artifact_evidence.sha256_well_formed",
            ],
            "non_deterministic_fields": [
                "generated.generated_at",
                "generated.source_revision",
                "provenance.links.artifact.sha256",
                "provenance.links.artifact.size_bytes",
                "provenance.links.artifact.duration_us",
            ],
            "non_deterministic_note": (
                "artifact digests and sizes are produced by the local FFmpeg encoder and "
                "vary by build; the gate checks their *shape* and the reopen equality, "
                "never a hard-coded value"
            ),
        },
        "reconciliation": reconciliation.as_dict(),
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
        "operations": [node.as_dict() for node in nodes],
        "provenance": {
            "T02": build_provenance_chain(base).as_dict(),
        },
        "artifact_evidence": {"T02": build_artifact_evidence(base)},
        "job_boundary": build_job_boundary(),
        "contract_drift": list(detect_contract_drift(surface, catalog, runtime, base)),
    }


@dataclass(frozen=True)
class Finding:
    """One gate finding: what drifted, and where."""

    kind: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"[{self.kind}] {self.detail}"


def compare(projection: dict[str, Any], fresh: dict[str, Any]) -> tuple[Finding, ...]:
    """Compare a stored projection with a fresh recomputation.

    Only *deterministic* fields are compared.  Timestamps and encoder-dependent
    artifact digests are excluded by construction (see
    ``generated.non_deterministic_fields``), so a re-run on another machine does
    not produce a false positive.
    """
    findings: list[Finding] = []

    stored_schema = projection.get("schema")
    if stored_schema != fresh["schema"]:
        findings.append(
            Finding(
                "schema_mismatch", f"projection schema {stored_schema!r} != {fresh['schema']!r}"
            )
        )

    stored_recon = projection.get("reconciliation") or {}
    fresh_recon = fresh["reconciliation"]
    for key in sorted(fresh_recon):
        if key in {"missing", "runtime_only"}:
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
        stored_status = stored_ops[operation_id].get("status") or {}
        fresh_status = fresh_ops[operation_id]["status"]
        for token in EVIDENCE_TOKENS:
            if token not in stored_status:
                findings.append(
                    Finding("evidence_layer_missing", f"{operation_id} has no {token!r} layer")
                )
                continue
            if stored_status[token] != fresh_status[token]:
                findings.append(
                    Finding(
                        "evidence_drift",
                        f"{operation_id}.{token}: projection={stored_status[token]!r} "
                        f"computed={fresh_status[token]!r}",
                    )
                )
        if stored_ops[operation_id].get("owner") != fresh_ops[operation_id]["owner"]:
            findings.append(
                Finding(
                    "owner_drift",
                    f"{operation_id}.owner: projection="
                    f"{stored_ops[operation_id].get('owner')!r} "
                    f"computed={fresh_ops[operation_id]['owner']!r}",
                )
            )

    stored_surface = projection.get("surface") or {}
    fresh_surface = fresh["surface"]
    for key in ("commands", "pairs", "derived_operation_count", "render_lane_operations"):
        if stored_surface.get(key) != fresh_surface[key]:
            findings.append(
                Finding(
                    "surface_drift",
                    f"surface.{key}: projection={stored_surface.get(key)!r} "
                    f"computed={fresh_surface[key]!r}",
                )
            )
    stored_probes = stored_surface.get("probes") or {}
    fresh_probes = fresh_surface["probes"]
    for probe_name in sorted(fresh_probes):
        stored_probe = stored_probes.get(probe_name) or {}
        for key in ("count", "operations", "unresolved"):
            if stored_probe.get(key) != fresh_probes[probe_name].get(key):
                findings.append(
                    Finding(
                        "surface_probe_drift",
                        f"surface.probes.{probe_name}.{key}: "
                        f"projection={stored_probe.get(key)!r} "
                        f"computed={fresh_probes[probe_name].get(key)!r}",
                    )
                )

    stored_chain = (projection.get("provenance") or {}).get("T02") or {}
    fresh_chain = fresh["provenance"]["T02"]
    if not fresh_chain["intact"]:
        findings.append(Finding("provenance_chain_broken", "; ".join(fresh_chain["findings"])))
    for key in ("operation_id", "command_id", "revision"):
        stored_links = stored_chain.get("links") or {}
        fresh_links = fresh_chain["links"]
        if stored_links.get(key) != fresh_links[key]:
            findings.append(
                Finding(
                    "provenance_drift",
                    f"provenance.T02.links.{key}: projection={stored_links.get(key)!r} "
                    f"computed={fresh_links[key]!r}",
                )
            )

    if projection.get("job_boundary") != fresh["job_boundary"]:
        findings.append(Finding("job_boundary_drift", "the declared Job-boundary contract changed"))

    # The vocabulary fingerprints are compared separately below: their ``detail``
    # is a whole digest map, and a diff of it is far more useful reported as the
    # changed keys than as two embedded dictionaries.
    stored_drift = {
        item.get("kind"): item.get("detail")
        for item in projection.get("contract_drift") or []
        if item.get("kind") != "maturity_vocabulary_fingerprints"
    }
    fresh_drift = {
        item.get("kind"): item.get("detail")
        for item in fresh["contract_drift"]
        if item.get("kind") != "maturity_vocabulary_fingerprints"
    }
    if stored_drift != fresh_drift:
        findings.append(
            Finding(
                "contract_drift_changed",
                f"projection={stored_drift!r} computed={fresh_drift!r}",
            )
        )

    stored_fingerprints = {
        item.get("kind"): item.get("detail")
        for item in projection.get("contract_drift") or []
        if item.get("kind") == "maturity_vocabulary_fingerprints"
    }
    fresh_fingerprints = {
        item.get("kind"): item.get("detail")
        for item in fresh["contract_drift"]
        if item.get("kind") == "maturity_vocabulary_fingerprints"
    }
    if stored_fingerprints != fresh_fingerprints:
        changed = sorted(
            set(stored_fingerprints.get("maturity_vocabulary_fingerprints") or {})
            ^ set(fresh_fingerprints.get("maturity_vocabulary_fingerprints") or {})
        )
        findings.append(
            Finding(
                "maturity_vocabulary_changed",
                "a declared L0–L4 definition was added, removed, or rewritten: "
                f"{changed or 'definition text changed in place'}",
            )
        )

    return tuple(findings)


def load_projection(root: Path | None = None) -> dict[str, Any] | None:
    """Read the stored projection, or ``None`` when it has not been generated yet."""
    base = root if root is not None else sources.REPO_ROOT
    path = base / PROJECTION_PATH
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_projection(root: Path | None = None) -> Path:
    """Regenerate the projection file.  The only supported way to change it."""
    base = root if root is not None else sources.REPO_ROOT
    projection = build_projection(root=base)
    path = base / PROJECTION_PATH
    path.write_text(
        json.dumps(projection, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "EVIDENCE_TOKENS",
    "GENERATION_COMMAND",
    "INFERRED",
    "MEASURABLE_TOKENS",
    "MISSING",
    "NOT_AVAILABLE",
    "NOT_VERIFIED",
    "PARTIALLY_VERIFIED",
    "PROJECTION_PATH",
    "SCHEMA",
    "VERIFIED",
    "Finding",
    "Layers",
    "OperationNode",
    "ProvenanceChain",
    "Reconciliation",
    "build_artifact_evidence",
    "build_job_boundary",
    "build_nodes",
    "build_projection",
    "build_provenance_chain",
    "compare",
    "detect_contract_drift",
    "load_projection",
    "read_maturity_vocabulary",
    "source_revision",
    "write_projection",
]
