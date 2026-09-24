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
    then may the job complete.  A verification refusal retracts the staged
    temp and leaves any previous valid ``<stem>.extracted.txt`` untouched
    (the pre-task-181 lane replaced the previous artifact *before*
    verification — reproduced destruction of a valid artifact by a refused
    publication, fixed here).  The RAG ingestion that follows extraction is
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
from pathlib import Path

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


def publish_pdf_text_artifact(
    payload: dict[str, object], result: dict[str, object]
) -> dict[str, object]:
    """Atomically publish the staged extraction (queue-owned, post-verify).

    ``os.replace`` staged → published (same-directory rename: atomic on
    POSIX).  Runs only AFTER independent verification succeeded; then the
    queue re-probes the published bytes.  A failure raises — the staged temp
    is removed and any previous published artifact is untouched.
    """
    source_raw = payload.get("file_path")
    if not isinstance(source_raw, str) or not source_raw:
        raise RuntimeError("publish: payload has no source file_path")
    staged = pdf_text_staged_path(Path(source_raw))
    published = pdf_text_artifact_path(Path(source_raw))
    claimed = str(result.get("artifact_path") or "")
    if claimed != str(staged):
        raise RuntimeError(f"publish: claim {claimed!r} is not the staged artifact {staged!r}")
    try:
        os.replace(staged, published)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return {"artifact_path": str(published)}


def retract_pdf_text_artifact(payload: dict[str, object], result: dict[str, object]) -> None:
    """Retract this attempt's staged temp after a refusal (never the final).

    The published destination is untouched: a previous valid artifact
    survives a refused publication (``docs/architecture/JOB_LIFECYCLE.md``
    §4/§6 — atomic publication, task-181).
    """
    source_raw = payload.get("file_path")
    if isinstance(source_raw, str) and source_raw:
        pdf_text_staged_path(Path(source_raw)).unlink(missing_ok=True)
    claimed = str(result.get("artifact_path") or "")
    if claimed.endswith(PDF_TEXT_STAGED_SUFFIX):
        Path(claimed).unlink(missing_ok=True)


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
    "PDF_TEXT_STAGED_SUFFIX",
    "STORY_JOB_TYPE",
    "pdf_extract_verifier",
    "pdf_text_artifact_path",
    "pdf_text_staged_path",
    "publish_pdf_text_artifact",
    "publish_text_artifact",
    "retract_pdf_text_artifact",
    "story_verifier",
]
