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
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import Table, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.project_state.models import (
    ArtifactBindingAmbiguous,
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

# Typed table accessor (SQLAlchemy 2.0 style) so the token-scoped delete predicate
# stays typed without importing SQLModel into this package (the architecture
# ratchet forbids new ``sqlmodel`` imports outside the frozen baseline).
_BINDING: Table = cast(Table, orm.ProjectArtifactBinding.__table__)  # type: ignore[attr-defined]


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
class VerifiedAssetRegistration:
    """Idempotent result of the verified artifact/asset/binding transaction."""

    asset_id: str
    asset_version_id: str
    binding: ArtifactBinding


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
    #: Fencing token of the execution that committed the binding (0 = pre-fence).
    attempt: int = 0
    #: Passport identifier supplied by verified registration (receipt-only on this schema).
    passport_id: str | None = None
    #: Attempt/fence evidence included in registration receipt, not persisted by baseline schema.
    attempt_id: str | None = None
    verifier_digest: str | None = None
    derived_receipt_ref: str | None = None


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
        attempt=row.attempt,
    )


def _derive_receipt(row: orm.ProjectArtifactBinding) -> ArtifactBinding:
    """Derive portable lineage identifiers solely from persisted binding facts."""
    fields = (
        row.project_id,
        row.revision_id,
        row.artifact_id,
        row.job_id,
        str(row.attempt),
        row.artifact_version_id or "",
        str(row.size_bytes),
    )
    encoded = "\\0".join(fields).encode("utf-8")
    receipt_ref = hashlib.sha256(b"nexus:derived-lineage-receipt:v1\\0" + encoded).hexdigest()
    attempt_document = json.dumps(
        {"job_id": row.job_id, "attempt": row.attempt},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    attempt_ref = hashlib.sha256(attempt_document.encode("utf-8")).hexdigest()
    return replace(
        _as_binding(row),
        attempt_id=f"attempt_{attempt_ref}",
        derived_receipt_ref=f"sha256:{receipt_ref}",
    )


async def derive_persisted_lineage_receipt(
    session: AsyncSession, *, artifact_id: str, job_id: str
) -> ArtifactBinding:
    """Read and deterministically re-derive lineage metadata from SQL facts."""
    row = await session.get(orm.ProjectArtifactBinding, (artifact_id, job_id))
    if row is None:
        raise ArtifactBindingError("registered artifact binding does not exist")
    return _derive_receipt(row)


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
        blob_store: LocalContentAddressedBlobStore | None = None,
    ) -> None:
        self._session_factory = session_factory or (lambda: get_session())
        self._revision_store = revision_store or SqlProjectRevisionStore()
        self._blob_store = blob_store

    async def register_verified_asset_version(
        self,
        *,
        project_id: str,
        revision_id: str,
        job_id: str,
        attempt_id: str,
        fencing_token: int,
        content_sha256: str,
        size_bytes: int,
        media_type: str,
        storage_uri: str,
        verifier_digest: str,
        passport_id: str,
        passport_digest: str,
        fence_check: Any,
    ) -> VerifiedAssetRegistration:
        """Atomically register verified bytes, version, project link and binding.

        The queue supplies ``fence_check`` from its authoritative claim. The
        stored path is re-measured in this operation; caller digests are claims
        only. Deterministic identifiers make crash/retry reconciliation a
        no-op when all immutable facts match.
        """
        if not project_id or not revision_id or not job_id or not attempt_id:
            raise ArtifactBindingError("asset registration identity is incomplete")
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token < 1
        ):
            raise ArtifactBindingError("fencing_token must be a positive integer")
        if not callable(fence_check) or not fence_check():
            raise ArtifactBindingError("stale or unverifiable execution fence")
        if (
            not media_type
            or not storage_uri
            or not verifier_digest
            or not passport_id
            or not passport_digest
        ):
            raise ArtifactBindingError("verified asset metadata is incomplete")
        path = Path(storage_uri)
        try:
            digest = content_address_of_file(path)
            size = path.stat().st_size
        except OSError as exc:
            raise ArtifactRevisionMismatch("published asset is unavailable") from exc
        normalized = (
            content_sha256 if content_sha256.startswith("sha256:") else f"sha256:{content_sha256}"
        )
        if digest != normalized or size != size_bytes or size <= 0:
            raise ArtifactRevisionMismatch("published asset digest or size does not match")
        with path.open("rb") as source:
            header = source.read(16)
        signatures = {
            "video/mp4": lambda value: len(value) >= 8 and value[4:8] == b"ftyp",
            "image/png": lambda value: value.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": lambda value: value.startswith(b"\xff\xd8\xff"),
            "audio/wav": lambda value: (
                len(value) >= 12 and value[:4] == b"RIFF" and value[8:12] == b"WAVE"
            ),
            "audio/x-wav": lambda value: (
                len(value) >= 12 and value[:4] == b"RIFF" and value[8:12] == b"WAVE"
            ),
            "video/webm": lambda value: value.startswith(b"\x1a\x45\xdf\xa3"),
        }
        signature = signatures.get(media_type)
        if signature is not None and not signature(header):
            raise ArtifactRevisionMismatch("published bytes do not match the claimed media type")
        blob_store = self._blob_store or LocalContentAddressedBlobStore(
            path.parent / ".asset-library"
        )
        with path.open("rb") as source:
            staging_key, staged_digest, staged_size = blob_store.stage(
                source, max_bytes=max(size_bytes, 1)
            )
        if staged_digest != digest.removeprefix("sha256:") or staged_size != size:
            blob_store.remove_staging(staging_key)
            raise ArtifactRevisionMismatch("blob staging changed the verified asset bytes")
        blob_key = blob_store.blob_key_for(staged_digest)
        try:
            stored_digest, stored_size, _ = blob_store.store(staging_key, blob_key)
            stored_bytes = blob_store.open(blob_key)
        except Exception as exc:  # noqa: BLE001 - blob publication fails closed
            blob_store.remove_staging(staging_key)
            raise ArtifactRevisionMismatch("canonical blob publication failed") from exc
        if (
            stored_digest != staged_digest
            or stored_size != size
            or hashlib.sha256(stored_bytes).hexdigest() != staged_digest
        ):
            raise ArtifactRevisionMismatch("canonical blob readback failed integrity verification")
        await self._load_validated_revision(project_id, revision_id)
        content_identity = f"{project_id}\\0{digest}".encode()
        suffix = hashlib.sha256(content_identity).hexdigest()
        asset_id = f"render_asset_{suffix}"
        version_id = f"render_version_{suffix}"
        link_id = f"render_link_{hashlib.sha256((project_id + version_id).encode()).hexdigest()}"
        artifact_kind = media_type.split("/", 1)[0] or "binary"

        async with self._session_factory() as session:
            await self._claim_content_owner(session, digest, project_id)
            asset = await session.get(orm.Asset, asset_id)
            if asset is None:
                session.add(orm.Asset(asset_id=asset_id, media_kind=artifact_kind))
                await session.flush()
            elif asset.media_kind != artifact_kind:
                raise ArtifactBindingConflict(
                    "deterministic Asset identity has conflicting media kind"
                )
            version = await session.get(orm.AssetVersion, version_id)
            if version is None:
                session.add(
                    orm.AssetVersion(
                        version_id=version_id,
                        asset_id=asset_id,
                        content_sha256=digest.removeprefix("sha256:"),
                        size_bytes=size,
                        media_kind=artifact_kind,
                        media_type=media_type,
                        blob_key=blob_key,
                        original_filename=Path(storage_uri).name,
                    )
                )
                await session.flush()
            elif (
                version.asset_id != asset_id
                or version.content_sha256 != digest.removeprefix("sha256:")
                or version.size_bytes != size
                or version.media_type != media_type
            ):
                raise ArtifactBindingConflict("deterministic AssetVersion identity diverged")
            link = await session.get(orm.ProjectAssetLink, link_id)
            if link is None:
                session.add(
                    orm.ProjectAssetLink(
                        link_id=link_id,
                        project_id=project_id,
                        asset_id=asset_id,
                        version_id=version_id,
                    )
                )
                await session.flush()
            elif (
                link.project_id != project_id
                or link.asset_id != asset_id
                or link.version_id != version_id
            ):
                raise CrossProjectAccess(
                    "AssetVersion is already linked to different project facts"
                )

            existing = await session.get(orm.ProjectArtifactBinding, (digest, job_id))
            if existing is not None:
                if existing.project_id != project_id or existing.revision_id != revision_id:
                    raise ArtifactBindingConflict("existing artifact lineage conflicts with retry")
                if fencing_token < existing.attempt:
                    raise ArtifactBindingError("stale fencing token cannot replace a successor")
                if existing.artifact_version_id != version_id or existing.size_bytes != size:
                    raise ArtifactBindingConflict(
                        "same artifact identity has conflicting immutable facts"
                    )
                if fencing_token == existing.attempt:
                    if not fence_check():
                        raise ArtifactBindingError(
                            "execution fence was lost before idempotent commit"
                        )
                    return VerifiedAssetRegistration(
                        asset_id, version_id, _derive_receipt(existing)
                    )
                existing.attempt = fencing_token
                if not fence_check():
                    raise ArtifactBindingError("execution fence was lost before successor commit")
                await session.commit()
                return VerifiedAssetRegistration(asset_id, version_id, _derive_receipt(existing))
            predecessor = (
                await session.execute(
                    select(orm.ProjectArtifactBinding)
                    .where(_BINDING.c.job_id == job_id)
                    .order_by(_BINDING.c.attempt.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            retired_version_id: str | None = None
            if predecessor is not None:
                if predecessor.project_id != project_id or predecessor.revision_id != revision_id:
                    raise ArtifactBindingConflict("successor attempt changed project or revision")
                if fencing_token <= predecessor.attempt:
                    raise ArtifactBindingError(
                        "stale or conflicting attempt cannot replace current binding"
                    )
                retired_version_id = predecessor.artifact_version_id
                await session.delete(predecessor)
                await session.flush()
            binding = orm.ProjectArtifactBinding(
                artifact_id=digest,
                project_id=project_id,
                revision_id=revision_id,
                job_id=job_id,
                attempt=fencing_token,
                artifact_kind=artifact_kind,
                size_bytes=size,
                artifact_version_id=version_id,
            )
            session.add(binding)
            await session.flush()
            if retired_version_id is not None:
                old_version = await session.get(orm.AssetVersion, retired_version_id)
                if old_version is not None:
                    old_binding_count = (
                        await session.execute(
                            select(func.count())
                            .select_from(_BINDING)
                            .where(_BINDING.c.artifact_version_id == retired_version_id)
                        )
                    ).scalar_one()
                    if old_binding_count == 0:
                        old_link_model: Any = orm.ProjectAssetLink
                        old_link_table: Any = old_link_model.__table__
                        old_link_hash = hashlib.sha256(
                            (project_id + retired_version_id).encode()
                        ).hexdigest()
                        old_link_id = f"render_link_{old_link_hash}"
                        await session.execute(
                            delete(old_link_model).where(old_link_table.c.link_id == old_link_id)
                        )
                        old_link_count = (
                            await session.execute(
                                select(func.count())
                                .select_from(old_link_table)
                                .where(old_link_table.c.version_id == retired_version_id)
                            )
                        ).scalar_one()
                        if old_link_count == 0:
                            old_asset_id = old_version.asset_id
                            await session.delete(old_version)
                            version_model: Any = orm.AssetVersion
                            old_version_table: Any = version_model.__table__
                            remaining = (
                                await session.execute(
                                    select(func.count())
                                    .select_from(old_version_table)
                                    .where(old_version_table.c.asset_id == old_asset_id)
                                )
                            ).scalar_one()
                            if remaining == 0:
                                old_asset = await session.get(orm.Asset, old_asset_id)
                                if old_asset is not None:
                                    await session.delete(old_asset)
            if not fence_check():
                raise ArtifactBindingError("execution fence was lost before asset commit")
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ArtifactBindingConflict("verified AssetVersion transaction raced") from exc
            saved = await session.get(orm.ProjectArtifactBinding, (digest, job_id))
            assert saved is not None
            return VerifiedAssetRegistration(
                asset_id,
                version_id,
                _derive_receipt(saved),
            )

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
        attempt: int = 0,
        require_asset_version: bool = False,
    ) -> ArtifactBinding:
        """Bind one measured artifact to the immutable revision it served.

        The revision must exist for ``project_id`` and its stored bytes must
        still hash to their own identity (``RevisionIntegrityError`` otherwise);
        the project must match (``CrossProjectAccess``).  The binding is
        **per producing job** and idempotent: the same ``(artifact_id, job_id)``
        re-bound to the *same* revision is a no-op success, while a differing
        revision for that job is a hard conflict — a job's provenance can never
        be silently re-pointed.  A *different* job binding the same bytes to the
        same revision gets its **own** row (independent provenance), and the
        content address stays owned by exactly one project (a second project is
        refused, ``CrossProjectAccess``).

        ``artifact_id`` must be a well-formed ``sha256:<64 hex>`` content
        address; a same-length non-hex value is refused rather than stored as an
        identity no file digest could ever match.

        ``artifact_version_id`` is the immutable Asset Library version this
        artifact was registered as (when the caller has one).  It completes the
        artifact→revision→asset-version chain; a binding without it is still
        valid (the artifact may not be in the Asset Library), but a caller that
        has a version should pass it so the chain is resolvable.

        ``attempt`` is the queue fencing token of the committing execution.  It
        is recorded so the queue can retract *exactly* the row this attempt
        wrote if the execution is superseded before its terminal commit; 0 means
        "no token" (a caller outside the queue, or a legacy row).
        """
        if not is_content_address(artifact_id):
            raise ArtifactBindingError("artifact_id must be a sha256:<64-hex> content address")
        if artifact_version_id is not None and not artifact_version_id.strip():
            raise ArtifactBindingError("artifact_version_id must be non-empty when provided")
        if size_bytes < 0:
            raise ArtifactBindingError("size_bytes must be non-negative")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
            raise ArtifactBindingError("attempt must be a non-negative integer fencing token")

        # The revision is the authority: load it (this re-validates its bytes,
        # refusing tampered/re-homed rows) before any binding row is written.
        await self._load_validated_revision(project_id, revision_id)

        async with self._session_factory() as session:
            if artifact_version_id is not None:
                version = await session.get(orm.AssetVersion, artifact_version_id)
                if version is None:
                    if require_asset_version:
                        raise ArtifactBindingError("artifact_version_id references no AssetVersion")
                else:
                    if (
                        version.content_sha256 != artifact_id.removeprefix("sha256:")
                        or version.size_bytes != size_bytes
                    ):
                        raise ArtifactRevisionMismatch(
                            "AssetVersion digest or size does not match the verified artifact"
                        )
                    link_model: Any = orm.ProjectAssetLink
                    link = await session.execute(
                        select(link_model).where(
                            link_model.version_id == artifact_version_id,
                            link_model.project_id == project_id,
                            link_model.asset_id == version.asset_id,
                        )
                    )
                    if link.scalar_one_or_none() is None:
                        raise CrossProjectAccess("AssetVersion is not linked to this project")
            # The content address is claimed FIRST, atomically, in the database:
            # a same-project replay (or a second job of the same project) is a
            # no-op, while a *different* project's claim is refused here — before
            # any per-job row is written (the F1 cross-project ownership fence).
            # The claim is an INSERT against the address's primary key, so it is
            # the database, not a read, that admits exactly one owner.
            await self._claim_content_owner(session, artifact_id, project_id)
            existing = await session.get(orm.ProjectArtifactBinding, (artifact_id, job_id))
            if existing is not None:
                return await self._reconcile_existing(
                    session, existing, project_id, revision_id, job_id, attempt
                )
            # A *new* per-job row for an address this project already owns.
            session.add(
                orm.ProjectArtifactBinding(
                    artifact_id=artifact_id,
                    project_id=project_id,
                    revision_id=revision_id,
                    job_id=job_id,
                    attempt=attempt,
                    artifact_kind=artifact_kind,
                    size_bytes=size_bytes,
                    artifact_version_id=artifact_version_id,
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                # A concurrent binder won the (artifact_id, job_id) primary key;
                # re-read and apply the same idempotency/conflict rule rather
                # than trusting the race.
                await session.rollback()
                existing = await session.get(orm.ProjectArtifactBinding, (artifact_id, job_id))
                if existing is None:  # pragma: no cover - a real PK collision must resolve
                    raise ArtifactBindingConflict(
                        "artifact binding raced and could not be read"
                    ) from exc
                return await self._reconcile_existing(
                    session, existing, project_id, revision_id, job_id, attempt
                )
            row = await session.get(orm.ProjectArtifactBinding, (artifact_id, job_id))
            assert row is not None  # just committed
            return _as_binding(row)

    @staticmethod
    async def _claim_content_owner(
        session: AsyncSession, artifact_id: str, project_id: str
    ) -> None:
        """Claim ``artifact_id`` for ``project_id`` — a DATABASE-enforced invariant.

        A read followed by an insert is a TOCTOU: two projects racing the same
        content address both observe "no owner" and both commit, attributing one
        address to two projects.  The claim is therefore an INSERT of the
        address's primary key (``artifactcontentowner.artifact_id``) inside a
        SAVEPOINT, so the database itself admits exactly one owner.  The nested
        transaction keeps the outer binding transaction usable after a refusal:
        PostgreSQL aborts a transaction on a failed statement, so a bare
        INSERT-and-catch would poison the session, and SQLite is given the same
        path for one code path on both backends.

        A same-project repeat (or a second job of the same project) finds the
        row it already owns and is a no-op; a different project takes the
        primary-key violation and is refused with ``CrossProjectAccess`` — the
        refusal is read back from the committed owner row, never assumed.
        """
        try:
            async with session.begin_nested():
                session.add(
                    orm.ArtifactContentOwner(artifact_id=artifact_id, project_id=project_id)
                )
                await session.flush()
        except IntegrityError:
            # The address is already owned (or a racer claimed it first): fall
            # through to read the authoritative owner and decide from that.
            pass
        owner = await session.get(orm.ArtifactContentOwner, artifact_id)
        if owner is None:  # pragma: no cover - the insert or an existing row must resolve
            raise ArtifactBindingConflict("artifact ownership could not be established")
        if owner.project_id != project_id:
            raise CrossProjectAccess("artifact id already bound to a different project")

    @staticmethod
    async def _reconcile_existing(
        session: AsyncSession,
        row: orm.ProjectArtifactBinding,
        project_id: str,
        revision_id: str,
        job_id: str,
        attempt: int,
    ) -> ArtifactBinding:
        # Provenance is the JOB's: the SAME artifact bound to the SAME revision
        # by the SAME job is an idempotent replay, while a different project or
        # revision for that job is a hard conflict — a job's binding can never be
        # silently re-homed or re-pointed.  A *different* job that produced
        # identical bytes never reaches here: its identity is the distinct
        # ``(artifact_id, job_id)`` primary key, so it owns its own row and this
        # job's retraction can never delete it (F5/R4).
        if row.project_id != project_id:
            raise CrossProjectAccess("artifact id already bound to a different project")
        if row.revision_id != revision_id:
            raise ArtifactBindingConflict("artifact already bound to a different revision")
        # Ownership is the (artifact_id, job_id, attempt) triple, so the fencing
        # token is advanced ONLY by the job that already owns the row — the
        # conditional UPDATE is scoped to the owning job, and the *only* job
        # guard is that predicate (a Python pre-check would be redundant and
        # would hide a dropped predicate from the mutation campaign).  A
        # different job's concurrent bind can never advance this row: its UPDATE
        # matches nothing, so the bind is a pure no-op on this row.
        #
        # Same job: advance the token monotonically so a superseded attempt's
        # retraction can never match a successor attempt's row.  The advance is a
        # single conditional UPDATE, not a read-modify-write: two concurrent
        # re-binds (a successor at a higher token racing a superseded attempt at a
        # lower one) would otherwise both read the old token and the lower one
        # could commit last, regressing the token and reopening the F1 hole.
        await session.execute(
            update(orm.ProjectArtifactBinding)
            .where(
                _BINDING.c.artifact_id == row.artifact_id,
                _BINDING.c.job_id == job_id,
                _BINDING.c.attempt < attempt,
            )
            .values(attempt=attempt)
            .execution_options(synchronize_session="fetch")
        )
        await session.commit()
        # The stored row is the authority for what the binding *is*.  The
        # conditional UPDATE bypasses the ORM identity map, and another process
        # may have advanced the token since it was loaded, so re-read the row
        # from the database rather than trusting any in-session state — the
        # returned binding must be exactly what a fresh reader would observe.
        authoritative = (
            await session.execute(
                select(orm.ProjectArtifactBinding)
                .where(
                    _BINDING.c.artifact_id == row.artifact_id,
                    _BINDING.c.job_id == row.job_id,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if authoritative is None:
            # The authoritative re-read found no row: a concurrent owner
            # retracted the row we just reconciled against (a lost-CAS
            # retraction racing this successor's bind).  Confirming here would
            # let the caller reach COMPLETED with no durable binding — the F1
            # hole — so refuse closed.  A caller that still needs the artifact
            # re-binds (the retractor is superseded, so this cannot ping-pong:
            # only an attempt that already owned the row may retract it).
            raise ArtifactBindingConflict(
                "artifact binding vanished during reconciliation (retracted by its owner)"
            )
        return _as_binding(authoritative)

    async def resolve(
        self, project_id: str, artifact_id: str, *, job_id: str | None = None
    ) -> ArtifactBinding:
        """Return the binding for one artifact, or refuse with a typed error.

        With an explicit ``job_id`` the exact ``(artifact_id, job_id)`` row is
        returned, so a caller resolves *only* the provenance of the job it names
        — even when several jobs share the artifact's bytes (F5/R4).

        Without one, the resolve is deterministic **only while every row for the
        address agrees on the revision it recorded**; then the earliest binding
        is returned, byte-for-byte the old single-row contract.  When two rows
        point the same address at *different* revisions the provenance is
        genuinely ambiguous and the resolve fails closed with
        :class:`ArtifactBindingAmbiguous` rather than silently returning an
        arbitrary "earliest" one.  Multi-job consumers therefore pass ``job_id``.
        """
        async with self._session_factory() as session:
            row = await self._select_binding(session, artifact_id, job_id)
            if row is None:
                raise ArtifactNotFound(f"artifact {artifact_id!r} is not bound")
            if row.project_id != project_id:
                raise CrossProjectAccess("artifact does not belong to the requested project")
            return _as_binding(row)

    @staticmethod
    async def _select_binding(
        session: AsyncSession, artifact_id: str, job_id: str | None
    ) -> orm.ProjectArtifactBinding | None:
        """One binding: the named job's exact row, else the artifact's earliest.

        The unscoped path first proves the address is unambiguous: it must be
        bound to at most one revision.  Distinct revisions mean two different
        states produced the same bytes, so "the earliest" would be an arbitrary
        choice between two real provenances — refused before any row is read.
        """
        if job_id is not None:
            return await session.get(orm.ProjectArtifactBinding, (artifact_id, job_id))
        revisions = (
            (
                await session.execute(
                    select(_BINDING.c.revision_id)
                    .where(_BINDING.c.artifact_id == artifact_id)
                    .distinct()
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        if len(revisions) > 1:
            raise ArtifactBindingAmbiguous(
                f"artifact {artifact_id!r} is bound to more than one revision; "
                "resolve it with an explicit job_id"
            )
        return (
            await session.execute(
                select(orm.ProjectArtifactBinding)
                .where(_BINDING.c.artifact_id == artifact_id)
                .order_by(_BINDING.c.created_at, _BINDING.c.job_id)
                .limit(1)
            )
        ).scalar_one_or_none()

    async def retract(
        self,
        *,
        artifact_id: str,
        job_id: str,
        attempt: int,
    ) -> bool:
        """Delete the binding written by *exactly* this execution, or nothing.

        Used by the queue when the attempt that committed a binding is then
        superseded: the terminal ``COMPLETED`` CAS fails, so the binding must not
        survive — but only the row this attempt wrote may be removed.  The delete
        is scoped on ``artifact_id`` **and** ``job_id`` **and** ``attempt`` (the
        fencing token), so it can never remove a successor's binding, a
        pre-fence row (``attempt = 0``), or a row another job wrote.

        Returns ``True`` iff a row was deleted.  Idempotent: a second call
        deletes nothing and returns ``False``.
        """
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ArtifactBindingError("retract requires a real fencing token (attempt >= 1)")
        async with self._session_factory() as session:
            binding = await session.get(orm.ProjectArtifactBinding, (artifact_id, job_id))
            if binding is None or binding.attempt != attempt:
                await session.rollback()
                return False
            version_id = binding.artifact_version_id
            asset_id: str | None = None
            if version_id is not None:
                version = await session.get(orm.AssetVersion, version_id)
                asset_id = version.asset_id if version is not None else None
            result = await session.execute(
                delete(orm.ProjectArtifactBinding).where(
                    _BINDING.c.artifact_id == artifact_id,
                    _BINDING.c.job_id == job_id,
                    _BINDING.c.attempt == attempt,
                )
            )
            if not getattr(result, "rowcount", 0):
                await session.rollback()
                return False
            if version_id is not None:
                active_binding = (
                    await session.execute(
                        select(func.count())
                        .select_from(_BINDING)
                        .where(_BINDING.c.artifact_version_id == version_id)
                    )
                ).scalar_one()
                if active_binding == 0:
                    links = orm.ProjectAssetLink
                    link_model: Any = links
                    link_table: Any = link_model.__table__
                    link_hash = hashlib.sha256(
                        (binding.project_id + version_id).encode()
                    ).hexdigest()
                    link_id = f"render_link_{link_hash}"
                    await session.execute(delete(links).where(link_table.c.link_id == link_id))
                    remaining_links = (
                        await session.execute(
                            select(func.count())
                            .select_from(link_table)
                            .where(link_table.c.version_id == version_id)
                        )
                    ).scalar_one()
                    if remaining_links == 0:
                        if version is not None:
                            await session.delete(version)
                            await session.flush()
                        if asset_id is not None:
                            versions = orm.AssetVersion
                            version_model: Any = versions
                            version_table: Any = version_model.__table__
                            remaining_versions = (
                                await session.execute(
                                    select(func.count())
                                    .select_from(version_table)
                                    .where(version_table.c.asset_id == asset_id)
                                )
                            ).scalar_one()
                            if remaining_versions == 0:
                                asset = await session.get(orm.Asset, asset_id)
                                if asset is not None:
                                    await session.delete(asset)
            await session.commit()
            return True

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
        self, *, project_id: str, artifact_id: str, artifact_path: Path, job_id: str | None = None
    ) -> ArtifactBinding:
        """Re-measure *artifact_path* against the binding, fail-closed.

        Returns the binding only when (a) the artifact's *current* bytes still
        hash to ``artifact_id`` and (b) the bound revision still validates.  Any
        divergence — changed bytes, missing/unreadable file, missing revision,
        tampered revision, cross project — is a typed refusal; the binding is
        never "assumed valid" and a raw filesystem error never escapes.

        ``job_id`` selects the exact provenance row to verify (F5/R4); omitted,
        the artifact's earliest binding is verified, exactly as before.
        """
        binding = await self.resolve(project_id, artifact_id, job_id=job_id)
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
