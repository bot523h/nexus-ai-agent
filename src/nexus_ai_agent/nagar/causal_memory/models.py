from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.studio.models import ActorIdentity


class MemoryStatus(str, Enum):
    """Evidence status for a read-only causal-memory projection."""

    VERIFIED = "VERIFIED"
    VERIFIED_WITH_LIMITATIONS = "VERIFIED_WITH_LIMITATIONS"
    INCOMPLETE = "INCOMPLETE"
    COMPROMISED = "COMPROMISED"


class MemoryNodeKind(str, Enum):
    JOB = "job"
    INTENT = "intent"
    OPERATION = "operation"
    ATTEMPT = "attempt"
    JOURNAL_EVENT = "journal_event"
    ARTIFACT = "artifact"
    VERIFICATION = "verification"


class MemoryScope(BaseModel):
    """The authenticated actor and exact project boundary for one query.

    The canonical main-branch contracts expose project membership but no
    tenant identity or project-to-tenant mapping. A tenant-scoped query is
    therefore rejected rather than guessed from a project or principal ID.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor: ActorIdentity
    project_id: str = Field(min_length=1, max_length=128)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=128)


class MemoryEvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1)
    source: Literal["journal", "queue_payload", "queue_result", "artifact_passport"]
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sequence: int | None = Field(default=None, ge=1)


class MemoryNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1)
    kind: MemoryNodeKind
    # Deliberately bounded to identifiers, digests, and non-sensitive state.
    attributes: dict[str, str | int | bool | None]


class MemoryEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    edge_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    relation: Literal[
        "intent_for_job",
        "job_requests_operation",
        "job_has_attempt",
        "job_has_event",
        "attempt_has_event",
        "job_records_subject",
        "subject_has_verification",
    ]
    evidence_ids: tuple[str, ...] = ()


class MemorySnapshot(BaseModel):
    """Deterministic, single-job projection; never an authority or execution plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    status: MemoryStatus
    passport_status: str = Field(min_length=1)
    tenant_scope: Literal["unavailable"] = "unavailable"
    completeness: Literal["single_job"] = "single_job"
    nodes: tuple[MemoryNode, ...]
    edges: tuple[MemoryEdge, ...]
    evidence: tuple[MemoryEvidenceRef, ...]
    finding_codes: tuple[str, ...]
    snapshot_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class MemoryScopeError(RuntimeError):
    """The requested job cannot be proven to belong to the authorized project."""


class TenantScopeUnavailable(MemoryScopeError):
    """The canonical source tree has no tenant identity/membership contract."""
