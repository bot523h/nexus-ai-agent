"""Domain types for the durable Studio project/revision lifecycle (task-282).

These are the immutable, storage-free contracts the rest of the system reasons
about.  The durable store (:mod:`nexus_ai_agent.creative.project_state.store`)
maps storage rows to these types; nothing here imports the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nexus_ai_agent.creative.studio.models import Project


class DurableProjectStateError(Exception):
    """Base error for the durable project/revision lifecycle."""


class RevisionConflict(DurableProjectStateError):
    """A compare-and-swap on the project head lost to a concurrent writer.

    Raised when the expected parent revision no longer matches the stored head,
    so the caller's write is refused with the head left untouched.
    """


class RevisionNotFound(DurableProjectStateError):
    """The requested revision (or head) does not exist for this project."""


class CrossProjectAccess(DurableProjectStateError):
    """A revision was requested through a project it does not belong to."""


@dataclass(frozen=True)
class RevisionRecord:
    """One durable, immutable project revision as read back from storage."""

    revision_id: str
    project_id: str
    parent_revision_id: str | None
    state_revision: int
    state_hash: str
    state_json: str
    created_at: datetime

    def to_project(self) -> Project:
        """Rebuild the exact Studio project state this revision recorded."""
        from nexus_ai_agent.creative.studio.persistence import deserialize_project

        return deserialize_project(self.state_json)
