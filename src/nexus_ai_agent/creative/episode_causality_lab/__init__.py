"""NAGAR ATLAS — an offline, deterministic episodic-causality lab.

ATLAS is a *proposal and analysis layer* for long-form and serialized
productions. It consumes an explicit, versioned story document and reports:

* three-valued causal-continuity findings (possession, knowledge, ordering);
* exact-integer temporal consistency with a proven negative-cycle witness;
* a bounded, deterministic edit-plan proposal or an honest refusal;
* incremental re-analysis that is provably equal to a full recomputation;
* advisory (never authoritative) repair suggestions;
* a real-disk report plus an inert integration handoff.

It never infers biometric identity, invents characters, proves real actors look
identical, or certifies media quality. It owns no project state, mints no
passport, and dispatches no command. Anything unverified is ``UNKNOWN`` or
``NEEDS_REVIEW``.

The package is intentionally dependency-light: standard library + pydantic, plus
the repository's existing ``filesystem_policy`` and ``execution.staging``
boundaries for real-disk output.
"""

from __future__ import annotations

from .analysis import AnalysisReport, analyze_story, report_to_dict
from .causal import (
    CausalEngine,
    CausalFindingCode,
    CausalReport,
    Finding,
    WorldState,
    declared_fact_to_rows,
    encode_fact_token,
    knows_fact_key,
)
from .errors import (
    AtlasError,
    DegenerateGeometryError,
    DuplicateIdentifierError,
    LimitExceededError,
    StoryValidationError,
    UnknownReferenceError,
    UnsupportedSchemaError,
)
from .handoff import ExportedArtifact, build_handoff, export_report
from .incremental import IncrementalEngine, IncrementalResult, full_recompute
from .models import (
    SCHEMA_VERSION,
    Character,
    DeclaredFact,
    Episode,
    Event,
    EventKind,
    FactKey,
    FactRelation,
    Location,
    Prop,
    Scene,
    ShotCandidate,
    SourceRef,
    StoryBeat,
    StoryDocument,
    TemporalConstraint,
    TimeOfDay,
)
from .plan import PlanRequest, PlanResult, PlanStep, plan_edit
from .statuses import SolverStatus, Status, TriValue
from .story import loads_story, parse_story, validate_references
from .suggest import Suggestion, suggest_repairs
from .temporal import NegativeCycleWitness, TemporalResult, check_consistency

__all__ = [
    "SCHEMA_VERSION",
    "AnalysisReport",
    "AtlasError",
    "CausalEngine",
    "CausalFindingCode",
    "CausalReport",
    "Character",
    "DeclaredFact",
    "DegenerateGeometryError",
    "DuplicateIdentifierError",
    "Episode",
    "Event",
    "EventKind",
    "ExportedArtifact",
    "FactKey",
    "FactRelation",
    "Finding",
    "IncrementalEngine",
    "IncrementalResult",
    "LimitExceededError",
    "Location",
    "NegativeCycleWitness",
    "PlanRequest",
    "PlanResult",
    "PlanStep",
    "Prop",
    "Scene",
    "ShotCandidate",
    "SolverStatus",
    "SourceRef",
    "Status",
    "StoryBeat",
    "StoryDocument",
    "StoryValidationError",
    "Suggestion",
    "TemporalConstraint",
    "TemporalResult",
    "TimeOfDay",
    "TriValue",
    "UnknownReferenceError",
    "UnsupportedSchemaError",
    "WorldState",
    "analyze_story",
    "build_handoff",
    "check_consistency",
    "declared_fact_to_rows",
    "encode_fact_token",
    "export_report",
    "full_recompute",
    "knows_fact_key",
    "loads_story",
    "parse_story",
    "plan_edit",
    "report_to_dict",
    "suggest_repairs",
    "validate_references",
]
