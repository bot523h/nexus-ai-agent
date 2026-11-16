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
  ``(job_id, attempt)`` exists, is in a non-superseded state and pins
  ``project_id``/``revision_id`` — in its ``result`` **or**, inside the crash
  window, in the immutable ``payload`` it was admitted with — the binding is
  registered idempotently; otherwise the orphan is moved into
  ``.quarantine/<job_id>_<attempt>_reconciled/`` with a canonical RFC 8785 JCS
  forensic receipt.  Never deletes bytes silently, and never overwrites an
  earlier quarantine witness for the same attempt.
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
import errno
import hashlib
import json
import os
import secrets
import shutil
import sys
from collections.abc import Iterator, Sequence
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

#: Job states in which *published* bytes can still belong to an attempt whose
#: completion never landed.  This is the audit's Case A crash window: the bytes
#: are already moved into ``published/<job_id>/`` while the row is still
#: ``processing``/``verifying``, because the publication, the confirm hook and
#: ``_mark_completed`` are three separate steps and a crash between any two of
#: them leaves exactly this state.
#:
#: ``pending`` is **not** in this set.  A pending row means the queue has not
#: (or no longer) granted this attempt the work: either it was never leased —
#: and no publication can legitimately exist — or the lease was released back
#: and a successor will own the same destination.  Repairing from a pending row
#: would bind bytes to an attempt the queue never confirmed.  The failure states
#: are excluded for the same reason: the queue refused those bytes.
_CRASH_WINDOW_STATUSES = frozenset({"processing", "verifying"})

#: Keys under which a job row may carry the project/revision pin.  ``result`` is
#: the post-completion record; ``payload`` is the immutable admission record,
#: which is the only evidence that exists *inside* the crash window.
_PIN_CONTAINERS = ("artifact_binding", "intent", "request", "data", "result", "payload")

#: Keys under which a job row may record the published artifact's content address.
_DIGEST_KEYS = ("artifact_id", "sha256", "content_sha256", "digest")


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
    document_out: dict[str, Any] = {"job_id": job_id, "attempt": attempt}
    content_digest = document.get("content_digest")
    if isinstance(content_digest, str) and content_digest.strip():
        document_out["content_digest"] = content_digest.strip()
    return document_out, None


def _quarantine_path(root: Path, job_id: str, attempt: int | None) -> Path:
    safe_job = (
        "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in job_id)[:120] or "unknown"
    )
    token = str(attempt) if attempt is not None else "unknown"
    return root / _QUARANTINE_DIR / f"{safe_job}_{token}_reconciled"


#: ``os.link`` fails with these when source and destination are not on the same
#: filesystem (or the filesystem refuses hard links).  They are the only cases in
#: which the copy fallback runs; every other error is a real failure.
_LINK_UNSUPPORTED = frozenset(
    {
        errno.EXDEV,
        errno.EPERM,
        errno.EMLINK,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EPERM),
        getattr(errno, "EOPNOTSUPP", errno.EPERM),
    }
)


def _quarantine_candidates(
    directory: Path, name: str, content_digest: str | None
) -> Iterator[Path]:
    """Every witness name this file may take, in preference order.

    Quarantine is **evidence**, and the destination for a given
    ``(job_id, attempt)`` is a fixed directory — so a second crash for the same
    attempt must never ``os.replace`` the first witness away.  The first
    collision is disambiguated with the content address (two different payloads
    keep distinguishable names), later ones with a counter.  Nothing is ever
    deleted to make room.

    This function *only names* candidates.  Existence is decided by
    :func:`_place_witness`, which creates the destination atomically, because
    ``if not path.exists(): os.replace(...)`` is a time-of-check/time-of-use
    race: two reconcilers scanning the same workspace can both see the name
    free, and the second ``os.replace`` silently destroys the first witness.
    """
    yield directory / name
    stem, suffix = Path(name).stem, Path(name).suffix
    marker = (content_digest or "").split(":")[-1][:12] or "repeat"
    yield directory / f"{stem}.{marker}{suffix}"
    counter = 2
    while True:
        yield directory / f"{stem}.{marker}-{counter}{suffix}"
        counter += 1


def _place_witness(source: Path, candidate: Path) -> bool:
    """Move *source* to *candidate* **without ever overwriting** an existing name.

    Two mechanisms, one invariant (the destination name was free at the moment
    it was taken):

    1. ``os.link(source, candidate)`` — creating a hard link fails atomically
       with :class:`FileExistsError` if the name is taken, and needs no copy, so
       the witness is a rename in all but name.  The source is unlinked only
       after the link exists.
    2. on a filesystem that cannot link across devices, the destination is
       *reserved* with ``O_CREAT|O_EXCL`` (again atomic, again
       :class:`FileExistsError`), the bytes are copied to a temp name in the
       same directory, and the temp replaces the reservation the caller already
       owns.  A crash mid-copy leaves the reserved name holding no evidence,
       never a half-written file pretending to be one.

    Returns ``False`` when the name was already taken, so the caller can move on
    to the next candidate.
    """
    try:
        os.link(source, candidate)
    except FileExistsError:
        return False
    except OSError as error:
        if error.errno not in _LINK_UNSUPPORTED:
            raise
        return _copy_into_reserved_name(source, candidate)
    os.unlink(source)
    return True


def _copy_into_reserved_name(source: Path, candidate: Path) -> bool:
    try:
        os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    # The name is now ours, exclusively.  The bytes still arrive atomically: the
    # copy goes to a temp name in the same directory and replaces *our own*
    # reservation, so no reader ever sees a half-written witness.
    temp = candidate.with_name(f".{candidate.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        with temp.open("wb") as destination, source.open("rb") as origin:
            shutil.copyfileobj(origin, destination, 1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temp, candidate)
    except BaseException:
        temp.unlink(missing_ok=True)
        candidate.unlink(missing_ok=True)  # an empty name proves nothing; free it
        raise
    os.unlink(source)
    return True


def _quarantine_witness(source: Path, directory: Path, name: str, digest: str | None) -> Path:
    """Give *source* the first witness name in *directory* that is really free."""
    for candidate in _quarantine_candidates(directory, name, digest):
        if _place_witness(source, candidate):
            return candidate
    raise RuntimeError("quarantine candidates exhausted")  # pragma: no cover - unbounded


def _move_to_quarantine(
    root: Path,
    artifact: Path,
    sidecar: Path | None,
    receipt: dict[str, Any],
) -> Path:
    job_id = str(receipt.get("job_id") or "unknown")
    attempt = receipt.get("attempt")
    attempt_int = attempt if isinstance(attempt, int) else None
    digest = receipt.get("content_sha256")
    destination = _quarantine_path(root, job_id, attempt_int)
    destination.mkdir(parents=True, exist_ok=True)
    # Artifact first: if the process dies here the receipt's absence is the
    # signal that this quarantine is incomplete, and the name is still unique.
    _quarantine_witness(artifact, destination, artifact.name, digest)
    if sidecar is not None and sidecar.is_file():
        _quarantine_witness(sidecar, destination, sidecar.name, digest)
    receipt_bytes = canonical_json(receipt).encode("utf-8")
    tmp = destination / f".{secrets.token_hex(6)}.receipt.tmp"
    tmp.write_bytes(receipt_bytes)
    _quarantine_witness(tmp, destination, "reconciliation_receipt.json", digest)
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
            repair_block = _repair_eligibility(
                facts,
                job_id,
                attempt,
                artifact_id=artifact_id,
                sidecar=sidecar_doc,
                published_digest=artifact_id,
            )
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


def _is_pin_value(value: Any) -> bool:
    """Whether *value* can be one half of a project/revision pin (never a guess)."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value > 0
    return isinstance(value, str) and bool(value.strip())


def _search_pin(node: Any, depth: int = 0) -> tuple[str, str] | None:
    """Find a complete ``(project_id, revision_id)`` pin inside *node*, or ``None``."""
    if not isinstance(node, dict) or depth > 3:
        return None
    project, revision = node.get("project_id"), node.get("revision_id")
    if _is_pin_value(project) and _is_pin_value(revision):
        return str(project), str(revision)
    for container in _PIN_CONTAINERS:
        nested = _search_pin(node.get(container), depth + 1)
        if nested is not None:
            return nested
    return None


def _pinned_project_ref(facts: Any) -> tuple[str, str] | None:
    """The ``(project_id, revision_id)`` a job row itself pins, or ``None``.

    Order of authority is the row's own evidence and nothing else: the
    post-completion ``result`` first, then the immutable admission ``payload`` —
    which is the *only* place the pin exists inside the crash window, because
    ``result`` is written with ``_mark_completed``, after publication. A file
    name, a workspace path or the sidecar's ``job_id`` is never turned into a
    pin: that would be inventing lineage.
    """
    for attribute in ("result", "payload", "request_payload", "request"):
        pin = _search_pin(getattr(facts, attribute, None))
        if pin is not None:
            return pin
    return None


def _search_digest(node: Any, depth: int = 0) -> str | None:
    if not isinstance(node, dict) or depth > 3:
        return None
    for key in _DIGEST_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for container in _PIN_CONTAINERS:
        nested = _search_digest(node.get(container), depth + 1)
        if nested is not None:
            return nested
    return None


def _recorded_artifact_digest(facts: Any) -> str | None:
    """The content address the job row recorded for its artifact, if any."""
    for attribute in ("result", "payload"):
        digest = _search_digest(getattr(facts, attribute, None))
        if digest is not None:
            return digest
    return None


def _normalise_digest(value: str) -> str:
    text = value.strip().lower()
    return text if text.startswith("sha256:") else f"sha256:{text}"


def _pins_per_source(facts: Any) -> dict[str, tuple[str, str]]:
    """Every distinct pin the row states, keyed by the attribute it came from.

    Two authorities exist and they are independent: the post-completion
    ``result`` and the immutable admission ``payload``.  When both state a pin
    they must agree — a row that pins two different revisions cannot be used to
    write a binding, because there is no honest way to choose.
    """
    pins: dict[str, tuple[str, str]] = {}
    for attribute in ("result", "payload", "request_payload", "request"):
        pin = _search_pin(getattr(facts, attribute, None))
        if pin is not None and pin not in pins.values():
            pins[attribute] = pin
    return pins


def _repair_eligibility(
    facts: Any,
    job_id: str,
    attempt: int | None,
    *,
    artifact_id: str | None = None,
    sidecar: dict[str, Any] | None = None,
    published_digest: str | None = None,
) -> str | None:
    """Return a refusal reason when repair must not run, else ``None``.

    Two admissible shapes, one rule: the row must still own this attempt and
    must itself pin the revision.

    * ``completed`` — the ordinary case; the ``result`` carries the pin.
    * ``processing`` / ``verifying`` — the crash window. The publication landed
      and ``_mark_completed`` did not, so the pin comes from the immutable
      payload the job was admitted with.  ``pending`` is deliberately **not**
      admissible: the queue has not granted (or has released) this attempt, so
      there is no confirmed owner to bind the bytes to.

    Inside the crash window the disk must also *prove* it is this job's
    publication, because the queue row cannot: the ownership sidecar has to
    carry the ``job_id``, the ``attempt`` and the ``content_digest`` of the
    bytes actually sitting there.  A sidecar without a digest, a digest that
    does not match the file, or two pins that disagree all mean the orphan stays
    ``unrepairable_orphan``.  Guessing here would write lineage the queue never
    granted, so every ambiguity fails closed.
    """
    if facts is None:
        return "no queue row for this job — provenance cannot be established from disk"
    facts_attempt = getattr(facts, "attempt", None)
    if isinstance(facts_attempt, int) and isinstance(attempt, int) and facts_attempt != attempt:
        return f"job attempt {facts_attempt} superseded sidecar attempt {attempt} (fenced out)"
    status = str(getattr(facts, "status", ""))
    pins = _pins_per_source(facts)
    distinct = set(pins.values())
    if len(distinct) > 1:
        rendered = ", ".join(f"{source}={'/'.join(pin)}" for source, pin in sorted(pins.items()))
        return f"the job row pins conflicting revisions ({rendered}) — ambiguous lineage"
    pin = next(iter(distinct), None)
    if status == "completed":
        if pin is None:
            return "queue result missing — cannot pin project_id/revision_id for the binding"
        return None
    if status in _CRASH_WINDOW_STATUSES:
        problem = _crash_window_problem(
            sidecar, job_id=job_id, attempt=attempt, digest=artifact_id or published_digest
        )
        if problem is not None:
            return problem
        if pin is None:
            return (
                f"job status is {status!r} and the row pins no project_id/revision_id — "
                "a crashed publication cannot be attributed to a revision"
            )
        recorded = _recorded_artifact_digest(facts)
        if recorded is not None and artifact_id is not None:
            if _normalise_digest(recorded) != _normalise_digest(artifact_id):
                return (
                    "published bytes do not match the content address recorded on the job row "
                    f"({recorded} != {artifact_id})"
                )
        return None
    return (
        f"job status is {status!r}: neither completed nor an in-flight attempt — "
        "the queue never released these bytes"
    )


def _crash_window_problem(
    sidecar: dict[str, Any] | None,
    *,
    job_id: str,
    attempt: int | None,
    digest: str | None,
) -> str | None:
    """What the on-disk sidecar fails to prove, or ``None`` when it proves the job.

    The sidecar is written *before* the destination is touched and refreshed with
    the published content digest immediately after the atomic publish, so inside
    the crash window it is the only artifact that can attribute the bytes.  Each
    requirement below is one the writer can always satisfy; failing any of them
    means this file cannot be shown to be this job's output.
    """
    if sidecar is None:
        return (
            "the crash window requires the on-disk owner sidecar to attribute the "
            "publication, and none is readable"
        )
    if sidecar.get("job_id") != job_id:
        return "owner sidecar job_id does not match the published namespace"
    if attempt is not None and sidecar.get("attempt") != attempt:
        return f"owner sidecar attempt {sidecar.get('attempt')!r} does not match {attempt}"
    recorded = sidecar.get("content_digest")
    if not isinstance(recorded, str) or not recorded.strip():
        return (
            "owner sidecar carries no content_digest — the published bytes cannot be "
            "tied to this attempt"
        )
    if digest is None:
        return "the published bytes could not be measured — refusing to guess"
    if _normalise_digest(recorded) != _normalise_digest(digest):
        return (
            "published bytes do not match the content_digest in the owner sidecar "
            f"({recorded} != {digest})"
        )
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
        """Authoritative re-read: the attempt must still own the row.

        The live confirm hook runs when the row is ``completed``; a repair made
        inside the crash window re-reads a row that is still ``processing`` or
        ``verifying``. Both mean the *same* thing here — this attempt still owns
        the row — so the fence accepts the unfinished states too and refuses
        every terminal failure. It is not a relaxation: a successor attempt
        changes ``attempt`` and still fails the fence.
        """
        if queue is None:
            return True
        current = queue.get_job_facts(job_id)
        if current is None:
            return False
        status = str(getattr(current, "status", ""))
        if status != "completed" and status not in _CRASH_WINDOW_STATUSES:
            return False
        return getattr(current, "attempt", None) == attempt

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

    pin = _pinned_project_ref(facts)
    if pin is None:  # pragma: no cover - guarded by _repair_eligibility
        return False, "the job row pins no project_id/revision_id — binding cannot be fabricated"
    project_id, revision_id = pin
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
    """A zero-argument factory of async session context managers.

    ``get_session`` is an ``@asynccontextmanager``: it must be *called with no
    arguments* by the store to produce the context manager, so binding the path
    has to produce a callable — ``get_session(db_path)`` would call it once here
    and hand the store a context manager instead of a factory (which only shows
    up on ``--database-path``, the flag that was never exercised end to end).
    """
    from functools import partial

    from nexus_ai_agent.storage.db import get_session

    return partial(get_session, db_path) if db_path is not None else get_session


async def _run(args: argparse.Namespace) -> int:
    from nexus_ai_agent.storage.db import dispose_all_engines

    root = Path(args.workspace_root).resolve()
    queue = _queue_for(root, args.queue_db)
    try:
        report = await reconcile_published_artifacts_and_db(
            root,
            _session_factory_for(args.database_path),
            queue=queue,
            apply_repair=args.apply_repair,
        )
    finally:
        # This is a one-shot CLI: close the pools it opened before the loop ends,
        # so an aiosqlite connection never outlives the loop that owns it
        # (`ResourceWarning: unclosed database`, and the worker thread with it).
        await dispose_all_engines()
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
