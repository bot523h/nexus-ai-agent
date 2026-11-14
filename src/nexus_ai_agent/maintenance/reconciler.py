"""DB <-> disk reconciler for published creative artifacts.

Closes the crash window described in the forensic audit (Dimension 3.2): the
publication path moves bytes to ``<artifact_root>/published/<job_id>/<name>``
*before* the SQL transaction that records ``_mark_completed`` and the
``ProjectArtifactBinding`` row commits.  A crash in between leaves either an
orphan published file (disk truth ahead of the DB) or a DB binding whose blob
is not byte-identical anymore (disk behind / tampered).  Neither state may be
left silently: this module scans both directions and reports — never hides —
every divergence, and with ``apply_repair=True`` applies the two safe,
deterministic repairs:

* **Case A (orphan on-disk published file)** — if the queue row for
  ``(job_id, attempt)`` exists, is in a non-superseded state and the result
  pins ``project_id``/``revision_id``, the binding is registered idempotently;
  otherwise the orphan is moved into ``.quarantine/<job_id>_<attempt>_reconciled/``
  with a canonical RFC 8785 JCS forensic receipt.  Never deletes bytes silently.
* **Case B (DB binding with a missing/mismatched blob)** — recorded as
  ``BROKEN_BLOB_BINDING`` with ``LedgerState.VIOLATED``; bytes are never
  fabricated, the row is never deleted.

The default is a **dry run**: with ``apply_repair=False`` nothing on disk or in
the DB is changed; the report only says what *would* happen.

Operational entry point::

    python -m nexus_ai_agent.maintenance.reconciler --workspace-root .
    python -m nexus_ai_agent.maintenance.reconciler --workspace-root . --apply-repair

The repaired binding is a *verified* lineage row: when the queue row still
carries its artifact passport, the default registration path stages the
published bytes into :class:`LocalContentAddressedBlobStore`
(``blobs/<prefix>/<sha256>``) and calls
:meth:`SqlArtifactBindingStore.register_verified_asset_version`, so a repaired
binding is indistinguishable from one written by the live confirm hook. Rows
that predate the passport schema fall back to the revision binding and say so.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nexus_ai_agent.provenance.models import canonical_json
from nexus_ai_agent.storage import models as orm

_QUARANTINE_DIR = ".quarantine"
_SIDECAR_SCHEMA = 1


class LedgerState(str, Enum):
    PASS = "PASS"
    VIOLATED = "VIOLATED"
    UNOBSERVED = "UNOBSERVED"


@dataclass(frozen=True)
class ReconciliationIssue:
    kind: str  # one of: orphan_published_artifact | broken_blob_binding
    # | sidecar_invalid | unrepairable_orphan
    state: LedgerState
    job_id: str | None
    attempt: int | None
    artifact_id: str | None
    path: str | None
    detail: str
    action: Literal["reported", "would_quarantine", "quarantined", "would_repair", "repaired"]


@dataclass(frozen=True)
class ReconciliationReport:
    workspace_root: str
    apply_repair: bool
    scanned_files: int
    bindings_checked: int
    bound: int
    issues: tuple[ReconciliationIssue, ...] = field(default_factory=tuple)

    @property
    def violations(self) -> int:
        return sum(1 for issue in self.issues if issue.state is LedgerState.VIOLATED)

    @property
    def quarantined(self) -> int:
        return sum(1 for issue in self.issues if issue.action == "quarantined")

    @property
    def repaired(self) -> int:
        return sum(1 for issue in self.issues if issue.action == "repaired")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "nagar.db_disk_reconciliation.v1",
            "workspace_root": self.workspace_root,
            "apply_repair": self.apply_repair,
            "scanned_files": self.scanned_files,
            "bindings_checked": self.bindings_checked,
            "bound": self.bound,
            "violations": self.violations,
            "quarantined": self.quarantined,
            "repaired": self.repaired,
            "issues": [
                {
                    "kind": issue.kind,
                    "state": issue.state.value,
                    "job_id": issue.job_id,
                    "attempt": issue.attempt,
                    "artifact_id": issue.artifact_id,
                    "path": issue.path,
                    "detail": issue.detail,
                    "action": issue.action,
                }
                for issue in self.issues
            ],
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _parse_sidecar(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "sidecar unreadable or not JSON"
    if not isinstance(document, dict):
        return None, "sidecar is not a JSON object"
    if document.get("schema") != _SIDECAR_SCHEMA:
        return None, "sidecar schema mismatch"
    job_id = document.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return None, "sidecar has no job_id"
    attempt_raw = document.get("attempt")
    attempt: int | None = None
    if isinstance(attempt_raw, bool):
        attempt = None
    elif isinstance(attempt_raw, int):
        attempt = attempt_raw if attempt_raw >= 1 else None
    elif isinstance(attempt_raw, str):
        text = attempt_raw.strip()
        if text.isdigit() and int(text) >= 1:
            attempt = int(text)
    if attempt is None:
        return None, "sidecar attempt is missing or not a positive integer"
    return {"job_id": job_id, "attempt": attempt}, None


def _quarantine_path(root: Path, job_id: str, attempt: int | None) -> Path:
    safe_job = (
        "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in job_id)[:120] or "unknown"
    )
    token = str(attempt) if attempt is not None else "unknown"
    return root / _QUARANTINE_DIR / f"{safe_job}_{token}_reconciled"


def _move_to_quarantine(
    root: Path,
    artifact: Path,
    sidecar: Path | None,
    receipt: dict[str, Any],
) -> Path:
    job_id = str(receipt.get("job_id") or "unknown")
    attempt = receipt.get("attempt")
    attempt_int = attempt if isinstance(attempt, int) else None
    destination = _quarantine_path(root, job_id, attempt_int)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / artifact.name
    os.replace(artifact, target)
    if sidecar is not None and sidecar.is_file():
        os.replace(sidecar, destination / sidecar.name)
    receipt_path = destination / "reconciliation_receipt.json"
    receipt_bytes = canonical_json(receipt).encode("utf-8")
    tmp = destination / f".reconciliation_receipt.json.tmp.{os.getpid()}"
    tmp.write_bytes(receipt_bytes)
    os.replace(tmp, receipt_path)
    return destination


async def _as_session(session_or_store: Any) -> tuple[AsyncSession | None, bool]:
    """Normalise the caller's store into (session, owns?) — typed refusal otherwise."""
    if session_or_store is None:
        return None, False
    if isinstance(session_or_store, AsyncSession):
        return session_or_store, False
    if callable(session_or_store):
        candidate = session_or_store()
        if hasattr(candidate, "__aenter__"):
            session = await candidate.__aenter__()
            if not isinstance(session, AsyncSession):
                raise TypeError("session factory must yield an AsyncSession")
            return session, True
        if isinstance(candidate, AsyncSession):
            return candidate, False
    if isinstance(session_or_store, async_sessionmaker):
        candidate = session_or_store()
        if hasattr(candidate, "__aenter__"):
            session = await candidate.__aenter__()
            if not isinstance(session, AsyncSession):
                raise TypeError("session_factory must yield an AsyncSession")
            return session, True
    raise TypeError(
        "session_or_store must be an AsyncSession, a zero-argument session factory, "
        "an async_sessionmaker, or None for disk-only scanning"
    )


async def _release_session(session: AsyncSession | None, owns: bool) -> None:
    if session is not None and owns:
        await session.close()


async def _find_binding(
    session: AsyncSession, artifact_id: str, job_id: str
) -> orm.ProjectArtifactBinding | None:
    return await session.get(orm.ProjectArtifactBinding, (artifact_id, job_id))


async def _binding_rows(session: AsyncSession) -> list[orm.ProjectArtifactBinding]:
    result = await session.execute(select(orm.ProjectArtifactBinding))
    return list(result.scalars().all())


async def reconcile_published_artifacts_and_db(
    workspace_root: str | Path,
    session_or_store: Any = None,
    *,
    queue: Any = None,
    apply_repair: bool = False,
    register_asset_version: Any = None,
) -> ReconciliationReport:
    """Scan ``published/`` and the DB binding table; report every divergence.

    ``queue`` is any object exposing ``get_job_facts(job_id)`` (sync) — e.g.
    :class:`nexus_ai_agent.adapters.in_process_job_queue.InProcessJobQueue`.

    Idempotent: re-running after a successful repair finds no orphans and no
    broken bindings, because a registered binding now matches the published
    bytes.  With ``apply_repair=False`` (default) the function is a pure
    diagnostic: no file is moved and no row is written.
    """
    root = Path(workspace_root).resolve()
    published = root / "published"
    session, owns_session = await _as_session(session_or_store)
    issues: list[ReconciliationIssue] = []
    scanned = 0
    bound = 0
    bindings_checked = 0

    try:
        published_files: list[Path] = []
        if published.is_dir():
            for job_dir in sorted(p for p in published.iterdir() if p.is_dir()):
                for artifact in sorted(job_dir.iterdir()):
                    if artifact.is_file() and not artifact.name.startswith("."):
                        published_files.append(artifact)

        # ── Case A/B sweep: every published file ──────────────────────────
        for artifact in published_files:
            scanned += 1
            job_dir = artifact.parent
            job_id = job_dir.name
            sidecar = job_dir / f".{artifact.name}.owner"
            sidecar_doc, sidecar_error = (
                _parse_sidecar(sidecar) if sidecar.is_file() else (None, "missing sidecar")
            )
            artifact_id = _sha256_file(artifact)
            attempt = sidecar_doc["attempt"] if sidecar_doc else None
            size_bytes = artifact.stat().st_size

            if sidecar_doc is None or sidecar_doc["job_id"] != job_id:
                detail = sidecar_error or "sidecar job_id does not match the published namespace"
                if apply_repair:
                    receipt = _receipt(
                        root, job_id, attempt, artifact_id, size_bytes, "sidecar_invalid", detail
                    )
                    _move_to_quarantine(
                        root, artifact, sidecar if sidecar.is_file() else None, receipt
                    )
                issues.append(
                    ReconciliationIssue(
                        kind="sidecar_invalid",
                        state=LedgerState.VIOLATED,
                        job_id=job_id,
                        attempt=attempt,
                        artifact_id=artifact_id,
                        path=str(artifact),
                        detail=detail,
                        action="quarantined" if apply_repair else "would_quarantine",
                    )
                )
                continue

            facts = None
            if queue is not None:
                try:
                    result = queue.get_job_facts(job_id)
                    if asyncio.iscoroutine(result):
                        result = await result
                    facts = result
                except Exception as exc:  # noqa: BLE001 - the queue must not crash the audit
                    facts = None
                    issues.append(
                        ReconciliationIssue(
                            kind="queue_unreadable",
                            state=LedgerState.UNOBSERVED,
                            job_id=job_id,
                            attempt=attempt,
                            artifact_id=artifact_id,
                            path=str(artifact),
                            detail=f"job facts could not be read: {exc}",
                            action="reported",
                        )
                    )

            binding = None
            if session is not None:
                bindings_checked += 1
                binding = await _find_binding(session, artifact_id, job_id)

            if binding is not None:
                if binding.attempt == attempt:
                    bound += 1
                    continue
                issues.append(
                    ReconciliationIssue(
                        kind="binding_attempt_mismatch",
                        state=LedgerState.VIOLATED,
                        job_id=job_id,
                        attempt=attempt,
                        artifact_id=artifact_id,
                        path=str(artifact),
                        detail=f"sidecar attempt {attempt} != bound attempt {binding.attempt}",
                        action="reported",
                    )
                )
                continue

            # No binding row for the exact (artifact_id, job_id).
            repair_block = _repair_eligibility(facts, job_id, attempt)
            if session is None:
                issues.append(
                    ReconciliationIssue(
                        kind="orphan_published_artifact",
                        state=LedgerState.UNOBSERVED,
                        job_id=job_id,
                        attempt=attempt,
                        artifact_id=artifact_id,
                        path=str(artifact),
                        detail=(
                            "no DB session supplied; binding existence unverified (disk-only scan)"
                        ),
                        action="reported",
                    )
                )
                continue

            if repair_block is not None:
                detail = repair_block
                if apply_repair:
                    receipt = _receipt(
                        root,
                        job_id,
                        attempt,
                        artifact_id,
                        size_bytes,
                        "unrepairable_orphan",
                        detail,
                    )
                    _move_to_quarantine(
                        root, artifact, sidecar if sidecar.is_file() else None, receipt
                    )
                issues.append(
                    ReconciliationIssue(
                        kind="unrepairable_orphan",
                        state=LedgerState.VIOLATED,
                        job_id=job_id,
                        attempt=attempt,
                        artifact_id=artifact_id,
                        path=str(artifact),
                        detail=detail,
                        action="quarantined" if apply_repair else "would_quarantine",
                    )
                )
                continue

            if not apply_repair:
                issues.append(
                    ReconciliationIssue(
                        kind="orphan_published_artifact",
                        state=LedgerState.VIOLATED,
                        job_id=job_id,
                        attempt=attempt,
                        artifact_id=artifact_id,
                        path=str(artifact),
                        detail="published file has no ProjectArtifactBinding row (crash window)",
                        action="would_repair",
                    )
                )
                continue

            repaired, repair_detail = await _repair_binding(
                session,
                facts,
                artifact_id,
                job_id,
                attempt,
                size_bytes,
                register_asset_version,
                artifact=artifact,
                queue=queue,
                blob_root=root / "asset-library",
            )
            if repaired:
                issues.append(
                    ReconciliationIssue(
                        kind="orphan_published_artifact",
                        state=LedgerState.PASS,
                        job_id=job_id,
                        attempt=attempt,
                        artifact_id=artifact_id,
                        path=str(artifact),
                        detail=repair_detail,
                        action="repaired",
                    )
                )
            else:
                receipt = _receipt(
                    root,
                    job_id,
                    attempt,
                    artifact_id,
                    size_bytes,
                    "unrepairable_orphan",
                    repair_detail,
                )
                _move_to_quarantine(root, artifact, sidecar if sidecar.is_file() else None, receipt)
                issues.append(
                    ReconciliationIssue(
                        kind="unrepairable_orphan",
                        state=LedgerState.VIOLATED,
                        job_id=job_id,
                        attempt=attempt,
                        artifact_id=artifact_id,
                        path=str(artifact),
                        detail=repair_detail,
                        action="quarantined",
                    )
                )

        # ── Case B sweep: DB bindings without matching bytes ─────────────
        if session is not None:
            rows = await _binding_rows(session)
            by_job: dict[str, list[Path]] = {}
            if published.is_dir():
                for job_dir in sorted(p for p in published.iterdir() if p.is_dir()):
                    by_job[job_dir.name] = [
                        p for p in job_dir.iterdir() if p.is_file() and not p.name.startswith(".")
                    ]
            for row in rows:
                candidates = by_job.get(row.job_id, [])
                matches = [p for p in candidates if _sha256_file(p) == row.artifact_id]
                if matches:
                    continue
                bindings_checked += 1
                issues.append(
                    ReconciliationIssue(
                        kind="broken_blob_binding",
                        state=LedgerState.VIOLATED,
                        job_id=row.job_id,
                        attempt=int(row.attempt) if row.attempt is not None else None,
                        artifact_id=row.artifact_id,
                        path=str(published / row.job_id),
                        detail=(
                            "BROKEN_BLOB_BINDING: binding exists but no published file "
                            "hashes to the bound content address"
                        ),
                        action="reported",
                    )
                )
        return ReconciliationReport(
            workspace_root=str(root),
            apply_repair=apply_repair,
            scanned_files=scanned,
            bindings_checked=bindings_checked,
            bound=bound,
            issues=tuple(issues),
        )
    finally:
        await _release_session(session, owns_session)


def _receipt(
    root: Path,
    job_id: str | None,
    attempt: int | None,
    artifact_id: str,
    size_bytes: int,
    reason: str,
    detail: str,
) -> dict[str, Any]:
    return {
        "schema": "nagar.reconciliation_receipt.v1",
        "workspace_root": str(root),
        "job_id": job_id,
        "attempt": attempt,
        "content_sha256": artifact_id,
        "size_bytes": size_bytes,
        "reason": reason,
        "detail": detail,
        "fencing_monotonic": True,
        "reconciliation": "quarantine",
    }


def _repair_eligibility(facts: Any, job_id: str, attempt: int | None) -> str | None:
    """Return a refusal reason when repair must not run, else ``None``."""
    if facts is None:
        return "no queue row for this job — provenance cannot be established from disk"
    facts_attempt = getattr(facts, "attempt", None)
    if isinstance(facts_attempt, int) and isinstance(attempt, int) and facts_attempt != attempt:
        return f"job attempt {facts_attempt} superseded sidecar attempt {attempt} (fenced out)"
    status = str(getattr(facts, "status", ""))
    if status not in {"completed"}:
        return f"job status is {status!r}, not 'completed' — bytes were never released by the queue"
    result = getattr(facts, "result", None)
    if not isinstance(result, dict):
        return "queue result missing — cannot pin project_id/revision_id for the binding"
    missing = [key for key in ("project_id", "revision_id") if not result.get(key)]
    if missing:
        return f"queue result does not pin {', '.join(missing)} — binding cannot be fabricated"
    return None


#: Media types a repaired binding may claim. Anything else is refused rather
#: than guessed: a wrong ``media_type`` would poison the AssetVersion row.
_MEDIA_TYPES_BY_SUFFIX: dict[str, str] = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".txt": "text/plain",
    ".json": "application/json",
    ".pdf": "application/pdf",
}


def _media_type_for(path: Path) -> str | None:
    return _MEDIA_TYPES_BY_SUFFIX.get(path.suffix.lower())


def _passport_facts(facts: Any) -> tuple[str, str] | None:
    """Return ``(passport_digest, verifier_digest)`` when the row can prove them.

    A repair may only write a *verified* AssetVersion when the durable passport
    evidence exists; without it the caller is told so and the legacy revision
    binding is used instead. Nothing here is invented: a missing field returns
    ``None`` and the caller degrades explicitly.
    """
    passport = getattr(facts, "artifact_passport", None)
    if not isinstance(passport, dict):
        return None
    passport_digest = passport.get("passport_digest")
    if not isinstance(passport_digest, str) or not passport_digest:
        return None
    verification = passport.get("predicate", {}).get("verification")
    if isinstance(verification, dict):
        verifier_digest = verification.get("verification_digest") or verification.get("digest")
        if isinstance(verifier_digest, str) and verifier_digest:
            return passport_digest, verifier_digest
    return passport_digest, passport_digest


async def default_register_asset_version(
    *,
    artifact: Path,
    facts: Any,
    project_id: str,
    revision_id: str,
    job_id: str,
    attempt: int,
    artifact_id: str,
    size_bytes: int,
    queue: Any = None,
    session_factory: Any = None,
    blob_root: Path | None = None,
) -> str:
    """Register a repaired artifact as a verified content-addressed AssetVersion.

    The published bytes are re-measured, staged into
    :class:`LocalContentAddressedBlobStore` (``blobs/<prefix>/<sha256>``) and
    registered through
    :meth:`~nexus_ai_agent.creative.project_state.store.SqlArtifactBindingStore.register_verified_asset_version`,
    so ``AssetLibraryService.read_content()`` can serve the repaired row exactly
    like a live one. The fence check re-reads the authoritative queue row at
    commit time, which is the same CAS the live confirm hook performs.
    """
    from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
    from nexus_ai_agent.creative.project_state.store import (
        SqlArtifactBindingStore,
        SqlProjectRevisionStore,
        content_address_of_file,
    )

    passport = _passport_facts(facts)
    if passport is None:
        raise RuntimeError("queue row carries no artifact passport evidence")
    passport_digest, verifier_digest = passport
    media_type = _media_type_for(artifact)
    if media_type is None:
        raise RuntimeError(f"no reviewed media type for {artifact.suffix or '<no suffix>'}")
    measured = content_address_of_file(artifact)
    scanned = artifact_id if artifact_id.startswith("sha256:") else f"sha256:{artifact_id}"
    if measured != scanned:
        raise RuntimeError("published bytes changed between scan and repair")

    def fence_check() -> bool:
        """Authoritative re-read: the attempt must still own a completed row."""
        if queue is None:
            return True
        current = queue.get_job_facts(job_id)
        if current is None:
            return False
        return str(getattr(current, "status", "")) == "completed" and (
            getattr(current, "attempt", None) == attempt
        )

    resolve_factory = session_factory
    if resolve_factory is None:
        from nexus_ai_agent.storage.db import get_session

        resolve_factory = get_session
    store = SqlArtifactBindingStore(
        session_factory=resolve_factory,
        # The revision store must read the *same* database as the binding: a
        # default revision store would resolve against the configured app DB
        # and could "verify" a revision this transaction cannot see.
        revision_store=SqlProjectRevisionStore(session_factory=resolve_factory),
        blob_store=LocalContentAddressedBlobStore(blob_root) if blob_root is not None else None,
    )
    registration = await store.register_verified_asset_version(
        project_id=project_id,
        revision_id=revision_id,
        job_id=job_id,
        attempt_id=f"{job_id}:{attempt}",
        fencing_token=attempt,
        content_sha256=measured,
        size_bytes=size_bytes,
        media_type=media_type,
        storage_uri=str(artifact),
        verifier_digest=verifier_digest,
        passport_id=f"artifact_passport:{job_id}:{attempt}",
        passport_digest=passport_digest,
        fence_check=fence_check,
    )
    return registration.asset_version_id


async def _repair_binding(
    session: AsyncSession,
    facts: Any,
    artifact_id: str,
    job_id: str,
    attempt: int | None,
    size_bytes: int,
    register_asset_version: Any,
    *,
    artifact: Path,
    queue: Any = None,
    blob_root: Path | None = None,
) -> tuple[bool, str]:
    """Write the binding for a repaired orphan, preferring verified lineage.

    Order of authority:

    1. a caller-supplied ``register_asset_version`` hook (composition root /
       tests) — it owns the transaction and the session;
    2. :func:`default_register_asset_version` — a real ``AssetVersion`` staged
       into the blob store plus a fenced ``ProjectArtifactBinding``;
    3. the legacy revision binding (:meth:`SqlArtifactBindingStore.bind`) for
       rows that predate the artifact passport, reported as such.
    """
    from nexus_ai_agent.creative.project_state.store import (
        SqlArtifactBindingStore,
        SqlProjectRevisionStore,
    )

    result = getattr(facts, "result", None) or {}
    project_id = str(result.get("project_id"))
    revision_id = str(result.get("revision_id"))
    static_factory = lambda: _StaticSessionContext(session)  # noqa: E731 - shared session by design

    if register_asset_version is not None:
        try:
            await register_asset_version(session=session, artifact_id=artifact_id, facts=facts)
        except TypeError:
            await register_asset_version(artifact_id, facts)
        except Exception as exc:  # noqa: BLE001 - typed refusal surfaced in the report
            return False, f"asset-version registration refused: {exc}"
        return True, f"asset version registered for {artifact_id} against revision {revision_id}"

    verified_note: str | None = None
    try:
        version_id = await default_register_asset_version(
            artifact=artifact,
            facts=facts,
            project_id=project_id,
            revision_id=revision_id,
            job_id=job_id,
            attempt=int(attempt) if attempt is not None else 0,
            artifact_id=artifact_id,
            size_bytes=size_bytes,
            queue=queue,
            session_factory=static_factory,
            blob_root=blob_root,
        )
    except Exception as exc:  # noqa: BLE001 - degrade to the legacy binding, say so
        verified_note = f"verified registration unavailable ({type(exc).__name__}: {exc})"
    else:
        return True, (
            f"verified AssetVersion {version_id} registered for {artifact_id} "
            f"against revision {revision_id}"
        )

    store = SqlArtifactBindingStore(
        session_factory=static_factory,
        revision_store=SqlProjectRevisionStore(session_factory=static_factory),
    )
    try:
        await store.bind(
            project_id=project_id,
            revision_id=revision_id,
            artifact_id=artifact_id,
            job_id=job_id,
            size_bytes=size_bytes,
            attempt=int(attempt) if attempt is not None else 0,
        )
    except Exception as exc:  # noqa: BLE001 - typed refusal surfaced in the report
        return False, f"binding registration refused: {exc}"
    detail = f"binding for {artifact_id} re-registered against revision {revision_id}"
    if verified_note is not None:
        detail = f"{detail} ({verified_note})"
    return True, detail


class _StaticSessionContext:
    """Adapt an existing AsyncSession to the store's ``session_factory`` protocol."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


# --------------------------------------------------------------------------- #
# Operational entry point
# --------------------------------------------------------------------------- #


def _queue_for(workspace_root: Path, db_path: str | None) -> Any:
    """Open the queue read-only-ish view used to establish repair provenance.

    ``None`` when the queue database does not exist: the scan then reports every
    orphan as *unrepairable* (no queue row to establish facts from) instead of
    inventing one.
    """
    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue

    if db_path is not None:
        candidate = Path(db_path)
    else:
        candidate = workspace_root / "nexus_jobs.db"
    if not candidate.is_file():
        return None
    return InProcessJobQueue(candidate, handlers={})


def _session_factory_for(db_path: str | None) -> Any:
    from nexus_ai_agent.storage.db import get_session

    return get_session(db_path) if db_path is not None else get_session


async def _run(args: argparse.Namespace) -> int:
    root = Path(args.workspace_root).resolve()
    queue = _queue_for(root, args.queue_db)
    report = await reconcile_published_artifacts_and_db(
        root,
        _session_factory_for(args.database_path),
        queue=queue,
        apply_repair=args.apply_repair,
    )
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        mode = "apply-repair" if report.apply_repair else "dry-run"
        print(f"reconciliation ({mode}) over {report.workspace_root}")
        print(
            f"  scanned={report.scanned_files} bindings_checked={report.bindings_checked} "
            f"bound={report.bound} violations={report.violations} "
            f"repaired={report.repaired} quarantined={report.quarantined}"
        )
        for issue in report.issues:
            print(
                f"  [{issue.state.value}] {issue.kind} job={issue.job_id} "
                f"attempt={issue.attempt} action={issue.action}: {issue.detail}"
            )
    if args.fail_on_violation and report.violations:
        print(
            f"::error title=reconciler:: {report.violations} unresolved divergence(s) "
            "between the database and published artifacts",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: dry-run by default; ``--apply-repair`` is the explicit opt-in."""
    parser = argparse.ArgumentParser(
        prog="python -m nexus_ai_agent.maintenance.reconciler",
        description=(
            "Reconcile published creative artifacts against the durable "
            "ProjectArtifactBinding rows. Dry run unless --apply-repair is given."
        ),
    )
    parser.add_argument(
        "--workspace-root",
        default=".",
        help="root containing published/ (default: current directory)",
    )
    parser.add_argument(
        "--apply-repair",
        action="store_true",
        help="actually repair/relocate orphans (default: report only)",
    )
    parser.add_argument(
        "--queue-db", default=None, help="job-queue SQLite path (default: <root>/nexus_jobs.db)"
    )
    parser.add_argument(
        "--database-path", default=None, help="storage SQLite/DB path (default: configured)"
    )
    parser.add_argument("--json", action="store_true", help="emit the machine-readable report")
    parser.add_argument(
        "--fail-on-violation",
        action="store_true",
        help="exit 1 when any divergence remains (used by the maintenance workflow)",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
