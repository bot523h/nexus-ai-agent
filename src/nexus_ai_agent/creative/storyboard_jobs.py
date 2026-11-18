"""Queue adapter and independent verification for the storyboard render lane.

The composition root must configure NEXUS_STUDIO_WORKSPACE,
NEXUS_STUDIO_APPROVAL_DB and NEXUS_STUDIO_SIGNING_KEY on the worker host.
No secrets or trusted paths are accepted from queue payloads.
"""

from __future__ import annotations

import asyncio
import math
import os
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.artifact_passport import PASSPORT_SUFFIX, verify_artifact_passport
from nexus_ai_agent.creative.artifact_verifier import verify_mp4
from nexus_ai_agent.creative.materializer import (
    RenderProfile,
    planned_shot_durations,
    resolve_within,
)
from nexus_ai_agent.creative.storyboard_compiler import compile_storyboard
from nexus_ai_agent.creative.storyboard_render import (
    DURATION_MARGIN_S,
    RenderContext,
    StoryboardRenderError,
    approval_identity_for,
    render_storyboard_sync,
)
from nexus_ai_agent.jobs.verification import ArtifactClaim, VerificationOutcome
from nexus_ai_agent.nagar.creative.handoff import validate_storyboard_render_request
from nexus_ai_agent.nagar.ibpc.approval import ApprovalLedger, DurableApprovalLedger
from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding, ApprovalState


def _config() -> tuple[Path, Path, str]:
    root = os.environ.get("NEXUS_STUDIO_WORKSPACE", "")
    db = os.environ.get("NEXUS_STUDIO_APPROVAL_DB", "")
    key = os.environ.get("NEXUS_STUDIO_SIGNING_KEY", "")
    if not root or not db or len(key.encode("utf-8")) < 32:
        raise StoryboardRenderError("studio_not_configured")
    if not Path(root).is_absolute() or not Path(db).is_absolute():
        raise StoryboardRenderError("studio_not_configured", "paths must be absolute")
    return Path(root), Path(db), key


def _run(payload: Mapping[str, Any]) -> dict[str, Any]:
    root, db, key = _config()
    ledger = DurableApprovalLedger(db, now=lambda: datetime.now(timezone.utc).isoformat())
    try:
        return render_storyboard_sync(dict(payload), RenderContext(root, ledger, key))
    finally:
        ledger.close()


async def storyboard_render_job(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Run the existing render authority off the event loop."""
    return await asyncio.to_thread(_run, payload)


def storyboard_render_verifier(
    payload: Mapping[str, Any], result: Mapping[str, Any]
) -> VerificationOutcome:
    """Recompile, remeasure, check the signed passport and consumed approval."""
    summary: dict[str, Any] = {
        "status": "failed",
        "logical_identity": {},
        "spec_identity": {},
        "physical_identity": {},
        "probe": None,
    }
    try:
        root, db, key = _config()
        req = validate_storyboard_render_request(payload)
        profile = RenderProfile.from_mapping(req.profile)
        manifest = compile_storyboard(req.screenplay_text)
        if req.manifest_sha256 is not None and req.manifest_sha256 != manifest.manifest_sha256:
            raise ValueError("manifest_mismatch")
        identity = approval_identity_for(
            project_id=req.project_id,
            job_id=req.job_id,
            principal_id=req.principal_id,
            source_sha256=req.source_sha256,
            script_sha256=manifest.script_sha256,
            manifest_sha256=manifest.manifest_sha256,
            render_profile_hash=profile.profile_hash(),
        )
        # Open read-only: verification must not create or mutate authorization state.
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT binding FROM approvals WHERE approval_id=?", (req.approval_id,)
            ).fetchone()
        if row is None:
            raise ValueError("approval_missing")
        binding = ApprovalBinding.model_validate_json(row[0])
        ApprovalLedger._check_identity(binding, identity)
        if binding.state is not ApprovalState.CONSUMED:
            raise ValueError("approval_not_consumed")
        rel = Path("projects") / req.project_id / "renders" / req.job_id / "storyboard.mp4"
        path = resolve_within(root, rel)
        passport_path = resolve_within(root, rel.parent / f"storyboard{PASSPORT_SUFFIX}")
        size = result.get("output_size_bytes")
        sha = result.get("output_sha256")
        if isinstance(size, bool) or not isinstance(size, int) or not isinstance(sha, str):
            raise ValueError("invalid_claim")
        if result.get("output_rel_path") != str(rel):
            raise ValueError("path_mismatch")
        claim = ArtifactClaim(
            path=path,
            kind="video",
            sha256=sha,
            size_bytes=size,
            duration_us=None,
            operation="storyboard_render",
            output_asset_id="storyboard",
        )
        durations = planned_shot_durations(manifest, profile.sample_rate)
        verdict = verify_mp4(
            path,
            expected_duration_s=math.fsum(durations),
            tolerance_s=DURATION_MARGIN_S + len(durations) / profile.fps,
        )
        passport = verify_artifact_passport(passport_path, path, key).passport
        expected = {
            "project_id": req.project_id,
            "job_id": req.job_id,
            "source_sha256": req.source_sha256,
            "manifest_sha256": manifest.manifest_sha256,
            "render_profile_hash": profile.profile_hash(),
            "output_rel_path": str(rel),
        }
        if any(passport.get(k) != v for k, v in expected.items()):
            raise ValueError("passport_identity_mismatch")
        if (
            not verdict.passed
            or verdict.size_bytes != claim.size_bytes
            or verdict.output_sha256 != claim.sha256
        ):
            raise ValueError("artifact_mismatch")
        summary = {
            "status": "passed",
            "logical_identity": {"project_id": req.project_id, "output_asset_id": "storyboard"},
            "spec_identity": {
                "operation": "storyboard_render",
                "manifest_sha256": manifest.manifest_sha256,
            },
            "physical_identity": {
                "path": str(path),
                "size_bytes": verdict.size_bytes,
                "sha256": verdict.output_sha256,
            },
            "probe": {"duration_s": verdict.probed_duration_s, "status": verdict.status},
        }
        return VerificationOutcome(ok=True, reason_code=None, summary=summary)
    except Exception as exc:  # noqa: BLE001 - verification must refuse all failures
        summary["reason_code"] = "storyboard_verification_failed"
        summary["detail"] = type(exc).__name__
        return VerificationOutcome(
            ok=False, reason_code="storyboard_verification_failed", summary=summary
        )
