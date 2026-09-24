"""Artifact verification bound to the creative job contracts.

This is the Job layer's check of the canonical one-shot chain
(/edit /caption /grade) and — since task-180 (GAP-A) — of the Wave 2.5
``slideshow_render`` lane. Everything it asserts is evidence from the tree:

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


#: The queue job type of the Wave 2.5 slideshow lane. Evidence:
#: ``creative/slideshow/worker_adapter.py::SLIDESHOW_JOB_TYPE`` (not imported
#: to keep this module import-light; the adapter's module import pulls the
#: pydantic envelope and the whole render lane).
SLIDESHOW_RENDER_JOB_TYPE = "slideshow_render"


def slideshow_render_verifier(
    payload: dict[str, object], result: dict[str, object]
) -> VerificationOutcome:
    """Queue-side verifier for the ``slideshow_render`` job type (task-180, GAP-A).

    The runtime dialect is already documented in
    :meth:`ArtifactClaim.from_handler_result`: the worker returns
    ``output_path`` + ``content_sha256`` + measured ``duration_us`` /
    ``size_bytes`` (``worker_adapter._render_sync`` reads them from the
    render outcome — never invented). The dispatch-time payload is the
    independent anchor:

    * expected path — ``payload["output_path"]`` as the worker normalizes it
      (``_inside`` resolves to an absolute, workspace-contained path, so the
      expected path is compared resolved, exactly like the claim);
    * expected root — ``payload["workspace_dir"]`` (the bot creates one fresh
      workspace per job);
    * media probe — the runtime's own allow-listed-binary probe via
      :func:`verify_artifact` (``kind == "video"`` for the ``.mp4`` master).

    A ``{"success": False, "error_code": ...}`` result is the same typed
    user-failure dialect as ``creative_render``: nothing was claimed, so
    verification is ``not_applicable`` and the notifier maps the code.
    """
    if result.get("success") is False and "error_code" in result:
        return _not_applicable(result)

    try:
        claim = ArtifactClaim.from_handler_result(dict(result), default_kind="video")
    except ArtifactClaimError as exc:
        # Attack E closed: a "success" without a usable artifact claim can
        # never complete a slideshow job.
        return VerificationOutcome(
            ok=False,
            reason_code=REASON_NO_CLAIM,
            summary={
                "status": "failed",
                "reason_code": REASON_NO_CLAIM,
                "detail": str(exc),
                "logical_identity": {"project_name": "", "output_asset_id": ""},
                "spec_identity": {"operation": SLIDESHOW_RENDER_JOB_TYPE},
                "physical_identity": {},
                "probe": None,
            },
        )

    workspace_raw = payload.get("workspace_dir")
    expected_root = Path(str(workspace_raw)) if isinstance(workspace_raw, str) else None

    expected_path: Path | None = None
    output_raw = payload.get("output_path")
    if isinstance(output_raw, str) and output_raw:
        # The worker's ``_inside`` resolves the dispatch-time path, so the
        # expected path is compared in the same normalization as the claim.
        expected_path = Path(output_raw).resolve()

    outcome = verify_artifact(claim, expected_path=expected_path, expected_root=expected_root)
    if not outcome.ok:
        return outcome
    summary = dict(outcome.summary)
    project_name = str(payload.get("project_name") or "")
    asset_name = Path(expected_path).name if expected_path is not None else ""
    summary["logical_identity"] = {
        "project_name": project_name,
        "output_asset_id": claim.output_asset_id or asset_name,
    }
    spec: dict[str, object] = {"operation": SLIDESHOW_RENDER_JOB_TYPE}
    template_id = result.get("template_id")
    if isinstance(template_id, str) and template_id:
        spec["template_id"] = template_id
    shot_count = result.get("shot_count")
    if isinstance(shot_count, int):
        spec["shot_count"] = shot_count
    summary["spec_identity"] = spec
    return VerificationOutcome(ok=True, reason_code=None, summary=summary)


__all__ = [
    "EXPECTED_ARTIFACT_NAMES",
    "SLIDESHOW_RENDER_JOB_TYPE",
    "creative_render_verifier",
    "slideshow_render_verifier",
]
