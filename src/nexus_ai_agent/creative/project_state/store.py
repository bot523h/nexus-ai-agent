"""Durable, restartable store for Studio project revisions (task-282).

Uses the repository's single storage engine/session conventions
(:func:`nexus_ai_agent.storage.db.get_session`) and the schema declared once in
:mod:`nexus_ai_agent.storage.models`.  There is no second engine, no parallel
project identity and no second provenance store: ``AppProject`` remains the
canonical project identity, and a revision's bytes are the canonical
serialization of the Studio ``Project`` state produced by the CommandBus.

Consistency model:

* A revision row is IMMUTABLE and content-addressed (``revision_id`` is the
  canonical state hash), so committing identical state is idempotent and history
  can never be rewritten.
* The per-project head row is the single mutable pointer, advanced with an
  optimistic compare-and-swap on ``revision_id``.  Two concurrent writers race
  one conditional UPDATE; exactly one advances the head and the loser gets
  :class:`RevisionConflict` with the head untouched.

The whole commit is one transaction, so a crash before the commit leaves no
revision row and no moved head.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.creative.project_state.models import (
    ArtifactBindingConflict,
    ArtifactBindingError,
    ArtifactNotFound,
    ArtifactRevisionMismatch,
    CrossProjectAccess,
    RevisionConflict,
    RevisionIntegrityError,
    RevisionNotFound,
    RevisionRecord,
)
from nexus_ai_agent.creative.studio.models import Project, compute_state_hash
from nexus_ai_agent.creative.studio.persistence import (
    canonical_state_bytes,
    content_address,
    deserialize_project,
)
from nexus_ai_agent.storage import models as orm
from nexus_ai_agent.storage.db import get_session

#: Sentinel distinguishing "no expected parent given" from an explicit ``None``
#: (which is the genesis token).  A bare ``None`` would conflate the two.
_UNSET: Any = object()

#: The canonical content-address prefix (identical to a revision id).
_ADDRESS_PREFIX = "sha256:"

#: A canonical content address is exactly ``sha256:`` + 64 lowercase hex.
_CONTENT_ADDRESS_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def is_content_address(value: object) -> bool:
    """True only for a well-formed ``sha256:<64 lowercase hex>`` identity.

    A length/prefix check is not enough: ``sha256:`` + 64 non-hex characters is
    the same length but can never match a real file digest, so it would persist
    a permanently unverifiable binding. The digest characters are part of the
    identity contract, not a formatting nicety.
    """
    return isinstance(value, str) and _CONTENT_ADDRESS_RE.match(value) is not None


def content_address_of_bytes(data: bytes) -> str:
    """``sha256:<hex>`` over *data* — the same spelling revisions use."""
    return _ADDRESS_PREFIX + hashlib.sha256(data).hexdigest()


def content_address_of_file(path: Path) -> str:
    """``sha256:<hex>`` over the file's bytes, streamed (never fully loaded)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return _ADDRESS_PREFIX + digest.hexdigest()


@dataclass(frozen=True)
class ArtifactBinding:
    """One durable artifact→revision binding as read back from storage."""

    artifact_id: str
    project_id: str
    revision_id: str
    job_id: str
    artifact_kind: str
    size_bytes: int
    created_at: datetime
    artifact_version_id: str | None = None


def _as_binding(row: orm.ProjectArtifactBinding) -> ArtifactBinding:
    return ArtifactBinding(
        artifact_id=row.artifact_id,
        project_id=row.project_id,
        revision_id=row.revision_id,
        job_id=row.job_id,
        artifact_kind=row.artifact_kind,
        size_bytes=row.size_bytes,
        created_at=row.created_at,
        artifact_version_id=row.artifact_version_id,
    )


def _as_revision(row: orm.ProjectRevision) -> RevisionRecord:
    return RevisionRecord(
        revision_id=row.revision_id,
        project_id=row.project_id,
        parent_revision_id=row.parent_revision_id,
        state_revision=row.state_revision,
        state_hash=row.state_hash,
        state_json=row.state_json,
        created_at=row.created_at,
    )


def _verify_revision_integrity(row: orm.ProjectRevision) -> None:
    """Fail closed unless a stored row still hashes to its own identity.

    The durable bytes are the authority for a revision's identity. A direct
    database edit (or corruption) can leave ``state_json`` no longer hashing to
    ``revision_id``, ``state_hash`` no longer matching the content, or the
    embedded project identity no longer matching the owning row, while the row
    still deserializes. This re-derives all of them from the stored bytes using
    the project's one canonical serialization/hashing contract and refuses any
    mismatch. It never repairs, never falls back to another revision, and never
    echoes the row's content in the error.
    """
    try:
        project = deserialize_project(row.state_json)
        canonical = canonical_state_bytes(project)
    except Exception as exc:  # noqa: BLE001 - malformed bytes are an integrity failure
        raise RevisionIntegrityError(
            f"revision {row.revision_id!r} failed integrity validation"
        ) from exc
    if content_address(canonical) != row.revision_id:
        raise RevisionIntegrityError(f"revision {row.revision_id!r} failed integrity validation")
    if compute_state_hash(project) != row.state_hash:
        raise RevisionIntegrityError(f"revision {row.revision_id!r} failed integrity validation")
    # The embedded project identity must match the owning row: a state written
    # for one project must never be reachable through another project's row
    # (a direct ``project_id`` edit would otherwise re-home the state).
    if project.project_id != row.project_id:
        raise RevisionIntegrityError(f"revision {row.revision_id!r} failed integrity validation")


class SqlProjectRevisionStore:
    """Durable metadata operations over an injected async session factory."""

    def __init__(self, session_factory: Any | None = None) -> None:
        # A zero-arg callable returning an async context manager yielding a
        # session — identical to the Asset Library service's factory contract.
        self._session_factory = session_factory or (lambda: get_session())

    async def commit(
        self,
        project: Project,
        *,
        authorized_project_id: str,
        expected_parent_revision_id: Any = _UNSET,
    ) -> RevisionRecord:
        """Commit one immutable revision and CAS the project head.

        ``authorized_project_id`` is the project the caller was independently
        authorized for; a mismatch with ``project.project_id`` is refused so a
        project's revisions can never be written through another project.

        ``expected_parent_revision_id`` is the CAS token: pass the head you
        read (or ``None`` for a genesis commit).  Omit it only when the caller
        deliberately wants a last-writer-wins commit, which the lifecycle never
        does — the default is to refuse an unguarded write.
        """
        if project.project_id != authorized_project_id:
            raise CrossProjectAccess("project state does not match the authorized project")
        if expected_parent_revision_id is _UNSET:
            raise RevisionConflict(
                "commit requires an explicit expected_parent_revision_id (CAS token)"
            )

        state_bytes = canonical_state_bytes(project)
        revision_id = content_address(state_bytes)
        state_json = state_bytes.decode("utf-8")

        # Preflight (task-282 Finding 2): the state the caller handed us must be
        # internally consistent BEFORE we write anything. ``model_copy(update=...)``
        # and any other mutation that bypasses the Project validator can leave
        # ``state_hash`` stale; storing it would advance the head to a revision
        # the read path then refuses (an unreadable head). Detect the drift here,
        # before the insert or the head advance, and refuse the whole commit.
        if compute_state_hash(project) != project.state_hash:
            raise RevisionIntegrityError("project state_hash does not match its content")

        # Establish the canonical project identity in its own short transaction,
        # so a benign first-use race here can never poison the revision commit.
        await self._ensure_project_identity(project.project_id)

        async with self._session_factory() as session:
            head = await session.get(orm.ProjectRevisionHead, project.project_id)
            if head is not None and head.revision_id == revision_id:
                # Idempotent replay: the exact state is already the accepted
                # head, so this is a no-op success — never a conflict.
                return await self._read_revision(session, project.project_id, revision_id)
            if head is not None and expected_parent_revision_id is None:
                # A second genesis. Detach the loaded head so the insert below is
                # not shadowed by the identity map; the primary key then arbitrates.
                session.expunge(head)
            await self._insert_revision(
                session,
                project_id=project.project_id,
                revision_id=revision_id,
                parent_revision_id=expected_parent_revision_id,
                state_revision=project.state_revision,
                state_hash=project.state_hash,
                state_json=state_json,
            )
            await self._advance_head(
                session,
                project_id=project.project_id,
                revision_id=revision_id,
                state_revision=project.state_revision,
                expected_parent_revision_id=expected_parent_revision_id,
            )
            await session.commit()
            return await self._read_revision(session, project.project_id, revision_id)

    async def load_head(self, project_id: str) -> RevisionRecord | None:
        """Return the accepted (current) revision, or ``None`` for a new project."""
        async with self._session_factory() as session:
            head = await session.get(orm.ProjectRevisionHead, project_id)
            if head is None:
                return None
            return await self._read_revision(session, project_id, head.revision_id)

    async def load_revision(self, project_id: str, revision_id: str) -> RevisionRecord:
        """Load one revision, refusing cross-project access with a typed error."""
        async with self._session_factory() as session:
            return await self._read_revision(session, project_id, revision_id)

    # -- internals ------------------------------------------------------- #

    async def _ensure_project_identity(self, project_id: str) -> None:
        """Create the canonical ``AppProject`` identity row if it is absent.

        Idempotent and race-safe: a concurrent creator's row is simply observed.
        """
        async with self._session_factory() as session:
            if await session.get(orm.AppProject, project_id) is None:
                session.add(orm.AppProject(project_id=project_id))
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()

    async def _insert_revision(
        self,
        session: AsyncSession,
        *,
        project_id: str,
        revision_id: str,
        parent_revision_id: str | None,
        state_revision: int,
        state_hash: str,
        state_json: str,
    ) -> None:
        existing = await session.get(orm.ProjectRevision, revision_id)
        if existing is not None:
            # Content-addressed: the identical state is already stored. Never
            # rewrite it (immutability), but a row under another project is a
            # hard collision that must fail closed.
            if existing.project_id != project_id:
                raise CrossProjectAccess("revision id already belongs to a different project")
            return
        session.add(
            orm.ProjectRevision(
                revision_id=revision_id,
                project_id=project_id,
                parent_revision_id=parent_revision_id,
                state_revision=state_revision,
                state_hash=state_hash,
                state_json=state_json,
            )
        )
        await session.flush()

    async def _advance_head(
        self,
        session: AsyncSession,
        *,
        project_id: str,
        revision_id: str,
        state_revision: int,
        expected_parent_revision_id: str | None,
    ) -> None:
        now = datetime.utcnow()
        if expected_parent_revision_id is None:
            # Genesis: the head must not already exist. The primary key is the
            # arbiter, so a concurrent genesis loses with IntegrityError.
            session.add(
                orm.ProjectRevisionHead(
                    project_id=project_id,
                    revision_id=revision_id,
                    state_revision=state_revision,
                    updated_at=now,
                )
            )
            try:
                await session.flush()
            except IntegrityError as exc:
                raise RevisionConflict("project already has a head; genesis refused") from exc
            return

        # Non-genesis: one conditional UPDATE is the compare-and-swap. It only
        # advances the head when it still points at the revision the caller
        # read; a concurrent writer that moved it makes this a no-op.
        result = await session.execute(
            update(orm.ProjectRevisionHead)
            .where(
                orm.ProjectRevisionHead.project_id == project_id,  # type: ignore[arg-type]
                orm.ProjectRevisionHead.revision_id == expected_parent_revision_id,  # type: ignore[arg-type]
            )
            .values(
                revision_id=revision_id,
                state_revision=state_revision,
                updated_at=now,
            )
            .execution_options(synchronize_session="fetch")
        )
        await session.flush()
        if getattr(result, "rowcount", 0) == 1:
            return
        # The conditional UPDATE matched nothing. That is either a genuine CAS
        # loss (another writer moved the head to a *different* state) or a
        # benign race where a concurrent writer committed the *same* state and
        # already advanced the head to our revision_id. Only the latter is
        # idempotent success; anything else is a conflict with the head left
        # exactly where the winner put it.
        current = await session.get(orm.ProjectRevisionHead, project_id)
        if current is not None and current.revision_id == revision_id:
            return
        raise RevisionConflict(
            "project head moved: expected parent "
            f"{expected_parent_revision_id!r} is no longer current"
        )

    async def _read_revision(
        self, session: AsyncSession, project_id: str, revision_id: str
    ) -> RevisionRecord:
        row = (
            await session.execute(
                select(orm.ProjectRevision)
                .where(orm.ProjectRevision.revision_id == revision_id)  # type: ignore[arg-type]
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            raise RevisionNotFound(f"revision {revision_id!r} not found")
        if row.project_id != project_id:
            raise CrossProjectAccess("revision does not belong to the requested project")
        _verify_revision_integrity(row)
        return _as_revision(row)


class SqlArtifactBindingStore:
    """Durable artifact→revision bindings (task-282 Slice 2).

    When a creative job publishes an artifact, its *measured* content digest is
    bound here to the EXACT immutable revision the job was admitted against —
    never to a project head that can move after admission.  This is the durable
    artifact→revision link, in the same module and over the same single storage
    schema as the revision store: no parallel artifact store, verifier, passport
    or provenance authority is introduced, and the artifact bytes remain the
    authority (a divergence is a hard, fail-closed error, never an update).
    """

    def __init__(
        self,
        session_factory: Any | None = None,
        revision_store: SqlProjectRevisionStore | None = None,
    ) -> None:
        self._session_factory = session_factory or (lambda: get_session())
        self._revision_store = revision_store or SqlProjectRevisionStore()

    async def bind(
        self,
        *,
        project_id: str,
        revision_id: str,
        artifact_id: str,
        job_id: str = "",
        artifact_kind: str = "binary",
        size_bytes: int,
        artifact_version_id: str | None = None,
    ) -> ArtifactBinding:
        """Bind one measured artifact to the immutable revision it served.

        The revision must exist for ``project_id`` and its stored bytes must
        still hash to their own identity (``RevisionIntegrityError`` otherwise);
        the project must match (``CrossProjectAccess``).  The binding is
        content-addressed and idempotent: the same ``(project_id, artifact_id)``
        re-bound to the *same* revision is a no-op success, while a differing
        revision for the same artifact is a hard conflict — an artifact's
        provenance can never be silently re-pointed.

        ``artifact_id`` must be a well-formed ``sha256:<64 hex>`` content
        address; a same-length non-hex value is refused rather than stored as an
        identity no file digest could ever match.

        ``artifact_version_id`` is the immutable Asset Library version this
        artifact was registered as (when the caller has one).  It completes the
        artifact→revision→asset-version chain; a binding without it is still
        valid (the artifact may not be in the Asset Library), but a caller that
        has a version should pass it so the chain is resolvable.
        """
        if not is_content_address(artifact_id):
            raise ArtifactBindingError("artifact_id must be a sha256:<64-hex> content address")
        if artifact_version_id is not None and not artifact_version_id.strip():
            raise ArtifactBindingError("artifact_version_id must be non-empty when provided")
        if size_bytes < 0:
            raise ArtifactBindingError("size_bytes must be non-negative")

        # The revision is the authority: load it (this re-validates its bytes,
        # refusing tampered/re-homed rows) before any binding row is written.
        await self._load_validated_revision(project_id, revision_id)

        async with self._session_factory() as session:
            existing = await session.get(orm.ProjectArtifactBinding, artifact_id)
            if existing is not None:
                return self._reconcile_existing(existing, project_id, revision_id)
            session.add(
                orm.ProjectArtifactBinding(
                    artifact_id=artifact_id,
                    project_id=project_id,
                    revision_id=revision_id,
                    job_id=job_id,
                    artifact_kind=artifact_kind,
                    size_bytes=size_bytes,
                    artifact_version_id=artifact_version_id,
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                # A concurrent binder won the primary key; re-read and apply the
                # same idempotency/conflict rule rather than trusting the race.
                await session.rollback()
                existing = await session.get(orm.ProjectArtifactBinding, artifact_id)
                if existing is None:  # pragma: no cover - a real PK collision must resolve
                    raise ArtifactBindingConflict(
                        "artifact binding raced and could not be read"
                    ) from exc
                return self._reconcile_existing(existing, project_id, revision_id)
            row = await session.get(orm.ProjectArtifactBinding, artifact_id)
            assert row is not None  # just committed
            return _as_binding(row)

    @staticmethod
    def _reconcile_existing(
        row: orm.ProjectArtifactBinding, project_id: str, revision_id: str
    ) -> ArtifactBinding:
        if row.project_id != project_id:
            raise CrossProjectAccess("artifact id already bound to a different project")
        if row.revision_id != revision_id:
            raise ArtifactBindingConflict("artifact already bound to a different revision")
        return _as_binding(row)

    async def resolve(self, project_id: str, artifact_id: str) -> ArtifactBinding:
        """Return the binding for one artifact, or refuse with a typed error."""
        async with self._session_factory() as session:
            row = await session.get(orm.ProjectArtifactBinding, artifact_id)
            if row is None:
                raise ArtifactNotFound(f"artifact {artifact_id!r} is not bound")
            if row.project_id != project_id:
                raise CrossProjectAccess("artifact does not belong to the requested project")
            return _as_binding(row)

    async def _load_validated_revision(self, project_id: str, revision_id: str) -> RevisionRecord:
        """Load the bound revision and re-validate its bytes, with a typed refusal.

        Any storage-layer failure (a missing/tampered revision, a cross-project
        row, or a raw database error) is translated into the durable-project-state
        error hierarchy so callers never see a bare storage exception.
        """
        from nexus_ai_agent.creative.project_state.models import DurableProjectStateError

        try:
            record = await self._revision_store.load_revision(project_id, revision_id)
        except DurableProjectStateError:
            raise
        except Exception as exc:  # noqa: BLE001 - storage failure must be typed
            raise ArtifactBindingError("revision could not be loaded for binding") from exc
        if record.project_id != project_id:  # pragma: no cover - load_revision enforces it
            raise CrossProjectAccess("revision does not belong to the requested project")
        return record

    async def verify_artifact(
        self, *, project_id: str, artifact_id: str, artifact_path: Path
    ) -> ArtifactBinding:
        """Re-measure *artifact_path* against the binding, fail-closed.

        Returns the binding only when (a) the artifact's *current* bytes still
        hash to ``artifact_id`` and (b) the bound revision still validates.  Any
        divergence — changed bytes, missing/unreadable file, missing revision,
        tampered revision, cross project — is a typed refusal; the binding is
        never "assumed valid" and a raw filesystem error never escapes.
        """
        binding = await self.resolve(project_id, artifact_id)
        try:
            measured = content_address_of_file(artifact_path)
        except OSError as exc:
            raise ArtifactRevisionMismatch(
                "artifact bytes could not be read for re-verification"
            ) from exc
        if measured != binding.artifact_id:
            raise ArtifactRevisionMismatch(
                "artifact bytes no longer match the bound content address"
            )
        # Re-validate the revision the artifact was bound to (its own bytes must
        # still hash to their identity); a tampered/vanished revision fails here.
        await self._load_validated_revision(project_id, binding.revision_id)
        return binding

    async def list_for_revision(self, project_id: str, revision_id: str) -> list[ArtifactBinding]:
        """All artifacts durably bound to one revision (for lineage/readback)."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(orm.ProjectArtifactBinding)
                    .where(orm.ProjectArtifactBinding.project_id == project_id)  # type: ignore[arg-type]
                    .where(orm.ProjectArtifactBinding.revision_id == revision_id)  # type: ignore[arg-type]
                    .order_by(orm.ProjectArtifactBinding.created_at)  # type: ignore[arg-type]
                )
            ).scalars()
            return [_as_binding(row) for row in rows]


__all__ = [
    "ArtifactBinding",
    "SqlArtifactBindingStore",
    "SqlProjectRevisionStore",
    "content_address_of_bytes",
    "content_address_of_file",
]
