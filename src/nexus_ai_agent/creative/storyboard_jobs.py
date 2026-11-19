"""Queue adapter for the NAGAR One ``storyboard_render`` job.

Wiring, end to end:

* :func:`storyboard_render_job` is the handler registered in
  ``worker.default_job_handlers``. It runs the six-step fail-closed chain in
  :mod:`nexus_ai_agent.creative.storyboard_render` in a worker thread.
* :func:`storyboard_render_verifier` is registered in
  ``default_artifact_verifiers`` (the ratchet test enforces the pairing). The
  queue never trusts the handler's "rendered" on its own: the verifier
  re-reads the MP4 bytes and the passport from disk and re-checks the seal,
  the digest, the asset Merkle root and the independent ffprobe verdict.
  A queue-driven attempt renders into its per-attempt ``.staging``
  quarantine; the verifier re-measures the quarantined bytes there (or the
  final path once the queue has promoted them), and only the queue's own
  ``PASS`` verdict ever promotes quarantined files to the final path.
* Configuration is read at call time, never at import. Missing configuration
  fails the job closed (operator-visible), it never falls back to a default key
  or a temporary workspace.

Approvals are persisted in :class:`DurableApprovalLedger` at the canonical
workspace path, so a restart or a second process cannot replay a consumed
approval.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from nexus_ai_agent.creative.artifact_passport import PassportError, verify_artifact_passport
from nexus_ai_agent.creative.artifact_verifier import verify_mp4
from nexus_ai_agent.creative.storyboard_render import (
    DURATION_MARGIN_S,
    RenderContext,
    render_storyboard_sync,
)
from nexus_ai_agent.jobs.verification import VerificationOutcome
from nexus_ai_agent.nagar.creative.handoff import STORYBOARD_RENDER_JOB_TYPE
from nexus_ai_agent.nagar.ibpc.approval import (
    ApprovalAlreadyConsumedError,
    ApprovalError,
    DurableApprovalLedger,
    durable_approval_ledger_path,
)

ENV_WORKSPACE = "NEXUS_STUDIO_WORKSPACE"
ENV_SIGNING_KEY = "NEXUS_STUDIO_SIGNING_KEY"
MIN_SIGNING_KEY_CHARS = 32
_CHUNK = 65_536


class StoryboardJobConfigError(RuntimeError):
    """Studio configuration is missing or unsafe. The job fails closed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def studio_settings(environ: Mapping[str, str]) -> tuple[Path, str]:
    raw_workspace = environ.get(ENV_WORKSPACE, "").strip()
    if not raw_workspace:
        raise StoryboardJobConfigError(f"{ENV_WORKSPACE} is not set")
    key = environ.get(ENV_SIGNING_KEY, "")
    if len(key) < MIN_SIGNING_KEY_CHARS:
        raise StoryboardJobConfigError(
            f"{ENV_SIGNING_KEY} must be set and at least {MIN_SIGNING_KEY_CHARS} characters"
        )
    return Path(raw_workspace), key


def open_studio(
    environ: Mapping[str, str] | None = None,
) -> tuple[RenderContext, DurableApprovalLedger]:
    """Build the render context from configuration. The caller closes the ledger."""
    workspace, key = studio_settings(os.environ if environ is None else environ)
    ledger = DurableApprovalLedger(durable_approval_ledger_path(workspace), now=_utc_now)
    ctx = RenderContext(workspace_root=workspace, approval_ledger=ledger, signing_key=key)
    return ctx, ledger


async def storyboard_render_job(payload: dict[str, object]) -> dict[str, object]:
    """The registered ``storyboard_render`` queue handler."""
    ctx, ledger = open_studio()
    try:
        result = await asyncio.to_thread(render_storyboard_sync, payload, ctx)
    finally:
        ledger.close()
    return dict(result)


def finalize_storyboard_approval(approval_id: str, *, job_id: str, attempt: int) -> str | None:
    """Close the approval reservation of a durably ``COMPLETED`` job.

    Called by the queue after it has durably recorded ``JobStatus.COMPLETED``
    (and at startup reconciliation). Idempotent: finalizing an already
    finalized reservation is a no-op. Returns ``None`` on success, or a typed
    reason string when the finalization was skipped — a superseded attempt
    (``already_consumed``), a missing studio configuration, or a ledger
    refusal. A skip is never a job failure: the job is already terminal and
    the reservation stays fenced by its ``last_attempt``.
    """
    try:
        workspace, _ = studio_settings(os.environ)
    except StoryboardJobConfigError as exc:
        return str(exc)
    ledger = DurableApprovalLedger(durable_approval_ledger_path(workspace), now=_utc_now)
    try:
        ledger.finalize_consumption(approval_id, job_id=job_id, attempt=attempt)
    except ApprovalAlreadyConsumedError:
        return "already_consumed"
    except ApprovalError as exc:
        return exc.reason
    finally:
        ledger.close()
    return None


def _refuse(reason_code: str, detail: str) -> VerificationOutcome:
    return VerificationOutcome(
        ok=False,
        reason_code=reason_code,
        summary={
            "status": "failed",
            "reason_code": reason_code,
            "detail": detail,
            "job_type": STORYBOARD_RENDER_JOB_TYPE,
        },
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(root: Path, candidate: Path) -> bool:
    return candidate.resolve().is_relative_to(root.resolve())


def storyboard_render_verifier(
    payload: dict[str, object], result: dict[str, object]
) -> VerificationOutcome:
    """Independently re-measure a ``storyboard_render`` result. Runs in a worker thread."""
    if result.get("status") != "rendered" or result.get("verifier_status") != "PASS":
        return _refuse("handler_not_rendered", "result does not claim a PASS render")
    try:
        workspace, key = studio_settings(os.environ)
    except StoryboardJobConfigError as exc:
        return _refuse("studio_not_configured", str(exc))

    rel_mp4 = result.get("output_rel_path")
    rel_passport = result.get("passport_rel_path")
    claimed_sha = result.get("output_sha256")
    shots = result.get("shot_durations_s")
    fps = payload.get("fps")
    if (
        not isinstance(rel_mp4, str)
        or not isinstance(rel_passport, str)
        or not isinstance(claimed_sha, str)
        or not isinstance(shots, list)
        or not shots
        or isinstance(fps, bool)
        or not isinstance(fps, (int, float))
        or fps <= 0
    ):
        return _refuse("malformed_result", "result is missing artifact identity fields")

    # Quarantined results: a queue-driven attempt renders into its per-attempt
    # ``.staging`` quarantine, so the bytes are re-measured THERE. Once the
    # queue has promoted the attempt (atomic rename after PASS), the staged
    # paths are empty and the final paths carry the bytes instead.
    staged_mp4_raw = result.get("staged_output_path")
    staged_passport_raw = result.get("staged_passport_path")
    if isinstance(staged_mp4_raw, str) and isinstance(staged_passport_raw, str):
        staged_mp4 = Path(staged_mp4_raw)
        staged_passport = Path(staged_passport_raw)
        if not (_inside(workspace, staged_mp4) and _inside(workspace, staged_passport)):
            return _refuse("path_outside_workspace", "staged paths escape the studio workspace")
        if staged_mp4.is_file() and staged_passport.is_file():
            mp4 = staged_mp4
            passport = staged_passport
        else:
            final_mp4_raw = result.get("final_output_path")
            final_passport_raw = result.get("final_passport_path")
            if (
                not isinstance(final_mp4_raw, str)
                or not isinstance(final_passport_raw, str)
                or not _inside(workspace, Path(final_mp4_raw))
                or not _inside(workspace, Path(final_passport_raw))
                or not Path(final_mp4_raw).is_file()
                or not Path(final_passport_raw).is_file()
            ):
                return _refuse("artifact_missing", "staged MP4 or passport is not on disk")
            mp4 = Path(final_mp4_raw)
            passport = Path(final_passport_raw)
    else:
        mp4 = workspace / rel_mp4
        passport = workspace / rel_passport
        if not (_inside(workspace, mp4) and _inside(workspace, passport)):
            return _refuse("path_outside_workspace", "artifact paths escape the studio workspace")
        if not mp4.is_file() or not passport.is_file():
            return _refuse("artifact_missing", "MP4 or passport is not on disk")

    try:
        check = verify_artifact_passport(passport, mp4, key)
    except PassportError as exc:
        return _refuse(f"passport_{exc.reason}", exc.detail)

    actual_sha = _sha256_file(mp4)
    if actual_sha != claimed_sha:
        return _refuse("output_digest_mismatch", "MP4 bytes differ from the claimed digest")
    if check.passport.get("asset_merkle_root") != result.get("asset_merkle_root"):
        return _refuse("merkle_root_mismatch", "passport and result disagree on asset root")

    durations = [float(value) for value in shots]
    tolerance = DURATION_MARGIN_S + len(durations) / float(fps)
    verdict = verify_mp4(
        mp4,
        expected_duration_s=sum(durations),
        tolerance_s=tolerance,
    )
    if not verdict.passed:
        return _refuse("media_verdict_not_pass", "; ".join(verdict.reasons) or verdict.status)

    return VerificationOutcome(
        ok=True,
        reason_code=None,
        summary={
            "status": "verified",
            "reason_code": None,
            "job_type": STORYBOARD_RENDER_JOB_TYPE,
            "output_sha256": actual_sha,
            "passport_seal_verified": True,
            "media_verdict": verdict.status,
        },
    )


__all__ = [
    "ENV_SIGNING_KEY",
    "ENV_WORKSPACE",
    "MIN_SIGNING_KEY_CHARS",
    "StoryboardJobConfigError",
    "finalize_storyboard_approval",
    "open_studio",
    "storyboard_render_job",
    "storyboard_render_verifier",
]
