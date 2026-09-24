"""Artifact verification bound to the ``creative_render`` job contract.

This is the Job layer's check of the canonical one-shot chain
(/edit /caption /grade). Everything it asserts is evidence from the tree:

* the worker writes its artifact into the job workspace, named by the
  operation — ``output.mp4`` for render-lane operations, ``captions.srt``
  for caption transcribe, ``timeline.otio`` for OTIO export
  (``creative/render_jobs.py``);
* the workspace itself is derived from the idempotency key and re-validated
  by the worker (``_guarded_workspace``), so ``payload["workspace_dir"]``
  is the expected root;
* the logical identity is ``project_id = shot-<idempotency_key>`` plus the
  operation's output asset id (``_build_project`` / dispatch output);
* the spec identity is the canonical operation id plus the compiled lane IR
  hash when the handler recorded one (``CompiledLane.ir_hash``);
* media probe evidence comes from the runtime's own allow-listed-binary
  probe (``creative.slideshow.ffmpeg.probe_video``) — the architecture's
  ffprobe-equivalent.

A ``{"success": False, "error_code": ...}`` result is the repository's
typed user-failure dialect: no artifact is claimed, verification is
``not_applicable``, and the job completes so the notifier can translate the
code. Any ``success`` result MUST carry a verifiable artifact claim —
there is no "success without artifact" path through this verifier.
"""

from __future__ import annotations

from pathlib import Path

from nexus_ai_agent.jobs.verification import (
    REASON_NO_CLAIM,
    ArtifactClaim,
    ArtifactClaimError,
    VerificationOutcome,
    default_media_probe,
    verify_artifact,
)

#: Canonical operation id → the artifact file name the worker writes.
#: Evidence: ``creative/render_jobs.py`` (``workspace / "output.mp4"`` etc.).
EXPECTED_ARTIFACT_NAMES: dict[str, str] = {
    "delivery.export_otio": "timeline.otio",
    "caption.transcribe": "captions.srt",
}
DEFAULT_ARTIFACT_NAME = "output.mp4"


def _not_applicable(result: dict[str, object]) -> VerificationOutcome:
    return VerificationOutcome(
        ok=True,
        reason_code=None,
        summary={
            "status": "not_applicable",
            "reason": "typed_user_failure",
            "error_code": str(result.get("error_code") or ""),
            "logical_identity": {"project_id": None, "output_asset_id": ""},
            "spec_identity": {"operation": str(result.get("operation") or "")},
            "physical_identity": {},
            "probe": None,
        },
    )


def creative_render_verifier(
    payload: dict[str, object], result: dict[str, object]
) -> VerificationOutcome:
    """Queue-side verifier for the ``creative_render`` job type.

    Runs in a worker thread; only reads the world. Raises nothing by
    contract — any internal error is the queue's fail-closed path.
    """
    if result.get("success") is False and "error_code" in result:
        return _not_applicable(result)

    try:
        claim = ArtifactClaim.from_handler_result(dict(result), default_kind="binary")
    except ArtifactClaimError as exc:
        # "success" without a usable artifact claim can never complete a
        # job — this is the explicit no-artifact hole, closed.
        return VerificationOutcome(
            ok=False,
            reason_code=REASON_NO_CLAIM,
            summary={
                "status": "failed",
                "reason_code": REASON_NO_CLAIM,
                "detail": str(exc),
                "logical_identity": {"project_id": None, "output_asset_id": ""},
                "spec_identity": {"operation": str(result.get("operation") or "")},
                "physical_identity": {},
                "probe": None,
            },
        )

    workspace_raw = payload.get("workspace_dir")
    expected_root = Path(str(workspace_raw)) if isinstance(workspace_raw, str) else None

    expected_path: Path | None = None
    if expected_root is not None:
        name = EXPECTED_ARTIFACT_NAMES.get(claim.operation, DEFAULT_ARTIFACT_NAME)
        expected_path = expected_root / name

    probe = default_media_probe if claim.kind == "video" else None
    outcome = verify_artifact(
        claim,
        expected_path=expected_path,
        expected_root=expected_root,
        probe=probe,
    )
    if not outcome.ok:
        return outcome
    summary = dict(outcome.summary)
    # Bind the logical identity the chain actually used: the render worker
    # builds ``project_id = shot-<idempotency key>`` (``_build_project``).
    summary["logical_identity"] = {
        "project_id": f"shot-{payload.get('idempotency_key', '')}",
        "output_asset_id": claim.output_asset_id,
    }
    # Bind the spec identity: canonical operation + compiled lane IR hash
    # (``CompiledLane.ir_hash``) when the handler recorded one.
    spec: dict[str, object] = {"operation": claim.operation}
    ir_hash = result.get("spec_ir_hash")
    if isinstance(ir_hash, str) and ir_hash.startswith("sha256:"):
        spec["lane_ir_hash"] = ir_hash
    summary["spec_identity"] = spec
    return VerificationOutcome(ok=True, reason_code=None, summary=summary)


__all__ = ["EXPECTED_ARTIFACT_NAMES", "creative_render_verifier"]
