"""Durable Studio project/revision lifecycle (task-282).

A Studio ``Project`` state is a derived, content-addressed value.  This package
gives it an authoritative durable record — immutable, content-addressed
revisions plus a single compare-and-swap head pointer — reusing the repository's
one storage schema, the CommandBus write path and the canonical ``AppProject``
identity.  It introduces no parallel store, queue, authority or ledger.
"""

from __future__ import annotations

from nexus_ai_agent.creative.project_state.lifecycle import DurableProject
from nexus_ai_agent.creative.project_state.models import (
    ArtifactBindingAmbiguous,
    ArtifactBindingConflict,
    ArtifactBindingError,
    ArtifactNotFound,
    ArtifactRevisionMismatch,
    CrossProjectAccess,
    DurableProjectStateError,
    RevisionConflict,
    RevisionIntegrityError,
    RevisionNotFound,
    RevisionRecord,
)
from nexus_ai_agent.creative.project_state.store import (
    ArtifactBinding,
    SqlArtifactBindingStore,
    SqlProjectRevisionStore,
    content_address_of_bytes,
    content_address_of_file,
    is_content_address,
)

__all__ = [
    "ArtifactBinding",
    "ArtifactBindingAmbiguous",
    "ArtifactBindingConflict",
    "ArtifactBindingError",
    "ArtifactNotFound",
    "ArtifactRevisionMismatch",
    "CrossProjectAccess",
    "DurableProject",
    "DurableProjectStateError",
    "RevisionConflict",
    "RevisionIntegrityError",
    "RevisionNotFound",
    "RevisionRecord",
    "SqlArtifactBindingStore",
    "SqlProjectRevisionStore",
    "content_address_of_bytes",
    "content_address_of_file",
    "is_content_address",
]
