"""Compose the Studio CommandBus write path with durable revision storage.

This is the one real project→revision lifecycle: a command is dispatched through
the existing :class:`~nexus_ai_agent.creative.studio.bus.CommandBus` (which
already owns validation, authorization, idempotency and atomic apply), and the
resulting in-memory project state is then persisted as an immutable revision.
The bus stays the single write authority; durability only records what the bus
already accepted.
"""

from __future__ import annotations

from nexus_ai_agent.creative.project_state.models import RevisionRecord
from nexus_ai_agent.creative.project_state.store import SqlProjectRevisionStore
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import Project


class DurableProject:
    """A project whose accepted revisions survive a process restart."""

    def __init__(self, store: SqlProjectRevisionStore, project_id: str) -> None:
        self._store = store
        self._project_id = project_id

    @property
    def project_id(self) -> str:
        return self._project_id

    async def restore(self) -> Project | None:
        """Rebuild the accepted project state from storage, or ``None`` if new."""
        head = await self._store.load_head(self._project_id)
        return head.to_project() if head is not None else None

    async def head(self) -> RevisionRecord | None:
        return await self._store.load_head(self._project_id)

    async def persist(
        self, bus: CommandBus, *, expected_parent_revision_id: str | None
    ) -> RevisionRecord:
        """Persist the bus's current project state as a new immutable revision.

        ``expected_parent_revision_id`` is the CAS token (``None`` for genesis);
        it is forwarded unchanged so a concurrent writer is refused rather than
        silently overwritten.
        """
        return await self._store.commit(
            bus.project,
            authorized_project_id=self._project_id,
            expected_parent_revision_id=expected_parent_revision_id,
        )


__all__ = ["DurableProject"]
