"""Artifact verification for the feature-lane jobs (task-180, GAP-B / GAP-C).

The two feature jobs produce fundamentally different artifacts than the
creative chain, so each gets its own dialect instead of a generalized guess:

``story`` (``worker.generate_story_job``)
    The real artifact is a **PNG image rendered locally by Pillow**
    (``features/story_gen.py::AIStoryGenerator`` — deterministic for the same
    text, no network).  Dialect: exists → size > 0 → exact dispatch-time
    path (``payload["output_path"]``) → contained in the dispatched
    directory → recomputed ``sha256`` equals the handler's claim → the bytes
    decode as a real image (Pillow structural probe).

``pdf_extract`` (``worker.process_pdf_job``)
    The measurable artifact is the **extracted text layer**.  This lane is
    the one with a destination *outside* the job workspace (a sidecar next
    to the source PDF), so it obeys the publication order of
    ``docs/architecture/JOB_LIFECYCLE.md`` §4 (task-181): the handler
    **stages** at ``<stem>.extracted.txt.staged``
    (:func:`pdf_text_staged_path`), the queue verifies the staged bytes,
    publishes atomically (``os.replace`` staged →
    ``<stem>.extracted.txt``), then re-probes the published artifact — only
    then may the job complete.  Publication is a three-verb transaction
    (Gate-5 repair, F1): *publish* backs up the previous artifact to
    ``<stem>.extracted.txt.bak`` and journals the swap (backup path +
    published inode) before replacing; *retract* removes the staged temp and
    — if this attempt's swap is still the live inode — restores the pre-sap
    bytes from the backup (a newer owner's publication is never
    overwritten); *recover* resolves a crashed predecessor's journaled swap
    the same way before a new execution starts.  A refused publication —
    including a post-publish re-probe failure or a refused fenced commit —
    therefore returns the destination to the previous artifact (proven in
    ``tests/integration/test_execution_fencing.py`` T8–T11).  The RAG
    ingestion that follows extraction is
    an external side effect and is *not* claimed as verified; what this
    verifier proves is that the extraction really produced a non-empty,
    UTF-8-decodable text artifact with a stable identity.  Dialect:
    exists → size > 0 → exact expected path (staged or published — both
    derived from the payload) → containment → recomputed ``sha256`` →
    whole-file UTF-8 decode.

Both verifiers follow the task-178 contract: they only read the world, they
raise nothing (any internal error is the queue's fail-closed path), and a
handler claiming success without a usable artifact claim can never complete
the job.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from nexus_ai_agent.jobs.fencing import PUBLICATION_JOURNAL_KEY
from nexus_ai_agent.jobs.verification import (
    REASON_NO_CLAIM,
    ArtifactClaim,
    ArtifactClaimError,
    VerificationOutcome,
    verify_artifact,
)

#: Queue job types (evidence: ``worker.default_job_handlers``).
STORY_JOB_TYPE = "story"
PDF_EXTRACT_JOB_TYPE = "pdf_extract"

#: The pdf_extract artifact file name suffix (one source of truth: the
#: handler publishes through :func:`publish_text_artifact` and the verifier
#: expects exactly this path).
PDF_TEXT_ARTIFACT_SUFFIX = ".extracted.txt"
#: Staging name suffix — never a final name, so a partial/unchecked artifact
#: can never be mistaken for the published one.
PDF_TEXT_STAGED_SUFFIX = ".staged"
#: Backup name suffix — the pre-swap bytes of the published artifact live
#: here from just before a swap until the fenced commit confirms (then the
#: backup is retired).  A crash leaves exactly one of the documented windows
#: behind (see :func:`recover_pdf_text_publication`).
PDF_TEXT_BACKUP_SUFFIX = ".bak"


def pdf_text_backup_path(source_pdf: Path) -> Path:
    """The deterministic backup path for one source PDF's published artifact."""
    final = pdf_text_artifact_path(source_pdf)
    return final.with_name(final.name + PDF_TEXT_BACKUP_SUFFIX)


def pdf_text_artifact_path(source_pdf: Path) -> Path:
    """The deterministic *published* artifact path for one source PDF.

    Derived from the *payload* (``file_path``), never from the handler's
    result — so the expected path is fixed at dispatch time and a handler
    cannot relocate its own artifact.
    """
    return source_pdf.parent / f"{source_pdf.stem}{PDF_TEXT_ARTIFACT_SUFFIX}"


def pdf_text_staged_path(source_pdf: Path) -> Path:
    """The deterministic *staging* path — the handler's claim until publish.

    Exactly one name away from the published path (``.staged`` suffix); both
    names are payload-derived, and the verifier accepts claim paths only
    against these two.
    """
    final = pdf_text_artifact_path(source_pdf)
    return final.with_name(final.name + PDF_TEXT_STAGED_SUFFIX)


def _restore_backup_if_mine(published: Path, backup: Path, published_inode: int | None) -> bool:
    """Reinstate the pre-swap bytes iff the destination is still OUR swap.

    Inode guard = side-effect fencing on the filesystem: when a newer owner
    has published after us (or after the crashed swap we are recovering),
    their bytes own the name and we never overwrite them.  Returns ``True``
    when the destination was restored.
    """
    if not backup.exists():
        return False
    try:
        if published.exists() and published_inode is not None:
            if published.stat().st_ino != int(published_inode):
                return False
        os.replace(backup, published)
        return True
    except BaseException:
        return False


def publish_pdf_text_artifact(
    payload: dict[str, object], result: dict[str, object]
) -> dict[str, object]:
    """Atomically publish the staged extraction (queue-owned, post-verify).

    Three steps, in order (Gate-5 repair, F1):

    1. **back up** the current published bytes to
       :func:`pdf_text_backup_path` (hardlink, copy fallback) — the previous
       artifact becomes recoverable;
    2. **swap** ``os.replace`` staged → published (same-directory rename:
       atomic on POSIX; a crash mid-swap can never truncate the
       destination);
    3. **journal** the swap in the result patch under ``_publication``
       (backup path + the inode now at the destination + whether a previous
       artifact existed) — the queue persists it (fenced) immediately, so a
       later refusal or crash can find and restore the pre-swap bytes.

    Runs only AFTER independent verification succeeded.  A failure before
    the swap raises and leaves the published artifact untouched.
    """
    source_raw = payload.get("file_path")
    if not isinstance(source_raw, str) or not source_raw:
        raise RuntimeError("publish: payload has no source file_path")
    staged = pdf_text_staged_path(Path(source_raw))
    published = pdf_text_artifact_path(Path(source_raw))
    backup = pdf_text_backup_path(Path(source_raw))
    claimed = str(result.get("artifact_path") or "")
    if claimed != str(staged):
        raise RuntimeError(f"publish: claim {claimed!r} is not the staged artifact {staged!r}")
    had_previous = published.exists()
    try:
        if had_previous:
            backup.unlink(missing_ok=True)
            try:
                os.link(published, backup)
            except OSError:  # pragma: no cover - cross-device fallback
                shutil.copy2(published, backup)
        os.replace(staged, published)
    except BaseException:
        staged.unlink(missing_ok=True)
        if had_previous and published.exists() and backup.exists():
            # Swap did not land (or landed and raised after): destination is
            # still the previous artifact; drop the duplicate backup.
            backup.unlink(missing_ok=True)
        raise
    return {
        "artifact_path": str(published),
        PUBLICATION_JOURNAL_KEY: {
            "backup": str(backup),
            "published_inode": published.stat().st_ino,
            "had_previous": had_previous,
        },
    }


def retract_pdf_text_artifact(payload: dict[str, object], result: dict[str, object]) -> None:
    """Undo this attempt's publication namespace after a refusal.

    Removes the staged temp and — when this attempt already swapped and the
    destination is still *this* swap's inode — restores the pre-swap bytes
    from the backup.  A pre-swap refusal never touches the published
    artifact at all (there is no journal to act on), and a newer owner's
    publication is never overwritten (inode guard).  This is what makes
    T9/T10 (previous-artifact preservation) and T8 (stale publication
    cannot overwrite) hold for post-publish failures too.
    """
    source_raw = payload.get("file_path")
    if isinstance(source_raw, str) and source_raw:
        pdf_text_staged_path(Path(source_raw)).unlink(missing_ok=True)
    claimed = str(result.get("artifact_path") or "")
    if claimed.endswith(PDF_TEXT_STAGED_SUFFIX):
        Path(claimed).unlink(missing_ok=True)
    journal = result.get(PUBLICATION_JOURNAL_KEY)
    if isinstance(journal, dict) and isinstance(source_raw, str) and source_raw:
        backup = Path(str(journal.get("backup") or ""))
        published = pdf_text_artifact_path(Path(source_raw))
        inode_raw = journal.get("published_inode")
        inode = int(inode_raw) if isinstance(inode_raw, (int, float)) else None
        restored = _restore_backup_if_mine(published, backup, inode)
        if not restored and journal.get("had_previous") and backup.exists():
            # A newer owner owns the name now; their publication stands.
            # The backup here is the pre-swap bytes of a dead attempt and is
            # safe to drop only when the destination moved on — nothing
            # recoverable is lost (the live artifact is the newer owner's).
            backup.unlink(missing_ok=True)


def recover_pdf_text_publication(
    payload: dict[str, object], prior_result: dict[str, object] | None
) -> None:
    """Resolve a crashed predecessor's publication windows before execution.

    Three deterministic windows around the swap (T11):

    * backup written, no journal → the swap never landed; the backup is a
      byte-duplicate of the live published artifact and is removed;
    * journal present (the swap landed but the fenced commit did not) → the
      pre-swap bytes are restored, inode-guarded: **no uncommitted
      publication survives**, and a newer owner's bytes are never
      overwritten;
    * commit happened and the backup was never retired → handled by the
      journal-less branch only when a later run sees the stray backup; the
      committed bytes at the destination are never touched here.
    """
    source_raw = payload.get("file_path")
    if not isinstance(source_raw, str) or not source_raw:
        return
    published = pdf_text_artifact_path(Path(source_raw))
    backup = pdf_text_backup_path(Path(source_raw))
    journal = (prior_result or {}).get(PUBLICATION_JOURNAL_KEY)
    if isinstance(journal, dict):
        inode_raw = journal.get("published_inode")
        inode = int(inode_raw) if isinstance(inode_raw, (int, float)) else None
        if not _restore_backup_if_mine(published, backup, inode):
            backup.unlink(missing_ok=True)
        return
    if backup.exists():
        backup.unlink(missing_ok=True)


def retire_pdf_text_artifact(payload: dict[str, object], result: dict[str, object]) -> None:
    """Post-commit cleanup: the backup is dead weight once the swap is durable."""
    source_raw = payload.get("file_path")
    if isinstance(source_raw, str) and source_raw:
        pdf_text_backup_path(Path(source_raw)).unlink(missing_ok=True)


def publish_text_artifact(destination: Path, text: str) -> None:
    """Atomically publish a text artifact (temp in-dir → ``os.replace``).

    Same publication rule as the render lane's ``.part`` staging: a crash
    mid-write leaves a stray temp file, never a truncated artifact at the
    expected path. Failures raise — the queue maps them to a durable
    ``failed`` row (fail-closed, never a silent success).
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f"{destination.name}.part-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, destination)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _no_claim_failure(operation: str, detail: str) -> VerificationOutcome:
    return VerificationOutcome(
        ok=False,
        reason_code=REASON_NO_CLAIM,
        summary={
            "status": "failed",
            "reason_code": REASON_NO_CLAIM,
            "detail": detail,
            "logical_identity": {},
            "spec_identity": {"operation": operation},
            "physical_identity": {},
            "probe": None,
        },
    )


def _claim_or_failure(
    result: dict[str, object], *, operation: str, default_kind: str
) -> ArtifactClaim | VerificationOutcome:
    try:
        return ArtifactClaim.from_handler_result(dict(result), default_kind=default_kind)
    except ArtifactClaimError as exc:
        return _no_claim_failure(operation, str(exc))


def story_verifier(payload: dict[str, object], result: dict[str, object]) -> VerificationOutcome:
    """Queue-side verifier for the ``story`` job type (GAP-C).

    The dispatch-time ``payload["output_path"]`` is the contract: the image
    must exist exactly there, inside its dispatched directory, decode as a
    real image, and its recomputed digest must equal the handler's claim.
    """
    claim = _claim_or_failure(result, operation=STORY_JOB_TYPE, default_kind="image")
    if isinstance(claim, VerificationOutcome):
        return claim

    output_raw = payload.get("output_path")
    if not isinstance(output_raw, str) or not output_raw:
        return _no_claim_failure(STORY_JOB_TYPE, "payload has no dispatched output_path")
    expected_path = Path(output_raw)

    outcome = verify_artifact(
        claim, expected_path=expected_path, expected_root=expected_path.parent
    )
    if not outcome.ok:
        return outcome
    summary = dict(outcome.summary)
    summary["logical_identity"] = {
        "user_id": payload.get("user_id"),
        "output_asset_id": expected_path.name,
    }
    summary["spec_identity"] = {"operation": STORY_JOB_TYPE}
    return VerificationOutcome(ok=True, reason_code=None, summary=summary)


def pdf_extract_verifier(
    payload: dict[str, object], result: dict[str, object]
) -> VerificationOutcome:
    """Queue-side verifier for the ``pdf_extract`` job type (GAP-B).

    The expected artifact is derived from the payload's ``file_path`` alone:
    exactly the staged name (:func:`pdf_text_staged_path`) before publication
    or the published name (:func:`pdf_text_artifact_path`) at re-probe — the
    handler's claimed path must equal one of the two, both payload-derived.
    The RAG side effect is deliberately outside this dialect: only the
    measurable, deterministic part of the job (the extracted text layer) is
    verified, and a success claim without the persisted text artifact fails
    closed.
    """
    claim = _claim_or_failure(result, operation=PDF_EXTRACT_JOB_TYPE, default_kind="text")
    if isinstance(claim, VerificationOutcome):
        return claim

    source_raw = payload.get("file_path")
    if not isinstance(source_raw, str) or not source_raw:
        return _no_claim_failure(PDF_EXTRACT_JOB_TYPE, "payload has no source file_path")
    staged_path = pdf_text_staged_path(Path(source_raw))
    published_path = pdf_text_artifact_path(Path(source_raw))
    expected_path = published_path if claim.path == published_path else staged_path

    outcome = verify_artifact(
        claim, expected_path=expected_path, expected_root=expected_path.parent
    )
    if not outcome.ok:
        return outcome
    summary = dict(outcome.summary)
    summary["logical_identity"] = {
        "user_id": payload.get("user_id"),
        "file_id": payload.get("file_id"),
        "output_asset_id": expected_path.name,
    }
    summary["spec_identity"] = {"operation": PDF_EXTRACT_JOB_TYPE}
    return VerificationOutcome(ok=True, reason_code=None, summary=summary)


__all__ = [
    "PDF_EXTRACT_JOB_TYPE",
    "PDF_TEXT_ARTIFACT_SUFFIX",
    "PDF_TEXT_BACKUP_SUFFIX",
    "PDF_TEXT_STAGED_SUFFIX",
    "STORY_JOB_TYPE",
    "pdf_extract_verifier",
    "pdf_text_artifact_path",
    "pdf_text_backup_path",
    "pdf_text_staged_path",
    "publish_pdf_text_artifact",
    "publish_text_artifact",
    "recover_pdf_text_publication",
    "retract_pdf_text_artifact",
    "retire_pdf_text_artifact",
    "story_verifier",
]
