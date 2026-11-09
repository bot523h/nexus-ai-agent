"""The queue-side verifier for the ``creative_composition`` job type.

The verifier is the *independent* half of the proof: it re-measures the
artifact the worker produced, using its own tools, and refuses a job whose
artifact is missing, empty, tampered with, outside the workspace or — the
part a hash cannot cover — **not what it claims to be**.

The semantic pass is deliberately a different measurement path than the
producer's:

* a **video** is fully decoded with ``ffmpeg -f null -`` (which also counts
  the decoded frames) and its stream table is re-parsed;
* an **image** is re-opened with Pillow, and a grayscale artifact is checked
  *pixel by pixel* (every colour channel compared), not by its metadata.

A file whose bytes hash correctly but whose content is wrong therefore fails
here, which is exactly the hole a re-hash leaves open.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nexus_ai_agent.jobs.verification import (
    REASON_NO_CLAIM,
    ArtifactClaim,
    ArtifactClaimError,
    VerificationOutcome,
    default_media_probe,
    verify_artifact,
)

from .proof import SemanticReport, verify_image_semantics, verify_video_semantics

#: The typed reason code of a failed semantic pass.
REASON_SEMANTIC_MISMATCH = "semantic_mismatch"


def composition_verifier(
    payload: dict[str, object], result: dict[str, object]
) -> VerificationOutcome:
    """Queue-side verifier for the composition job type.

    Runs in a worker thread; only reads the world.  Raises nothing by
    contract — any internal error is the queue's fail-closed path.
    """
    if result.get("success") is False and "error_code" in result:
        return VerificationOutcome(
            ok=False,
            reason_code="typed_user_failure",
            summary={
                "status": "failed",
                "reason_code": "typed_user_failure",
                "detail": str(result.get("error_detail") or ""),
                "error_code": str(result.get("error_code") or ""),
            },
        )

    try:
        claim = ArtifactClaim.from_handler_result(dict(result), default_kind="binary")
    except ArtifactClaimError as exc:
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

    probe = default_media_probe if claim.kind == "video" else None
    outcome = verify_artifact(
        claim,
        expected_path=None,
        expected_root=expected_root,
        probe=probe,
    )
    if not outcome.ok:
        return outcome

    summary = dict(outcome.summary)
    summary["logical_identity"] = {
        "project_id": str(result.get("project_id") or ""),
        "output_asset_id": claim.output_asset_id,
    }
    spec: dict[str, Any] = {"operation": claim.operation}
    plan_fingerprint = result.get("plan_fingerprint")
    if isinstance(plan_fingerprint, str):
        spec["plan_fingerprint"] = plan_fingerprint
    registry_fingerprint = result.get("registry_fingerprint")
    if isinstance(registry_fingerprint, str):
        spec["registry_fingerprint"] = registry_fingerprint
    summary["spec_identity"] = spec

    # --- the semantic pass: what the file actually is ------------------------
    semantic = _semantic_pass(claim, result)
    summary["semantic"] = semantic.as_dict()
    if not semantic.verified:
        summary["status"] = "failed"
        summary["reason_code"] = REASON_SEMANTIC_MISMATCH
        return VerificationOutcome(ok=False, reason_code=REASON_SEMANTIC_MISMATCH, summary=summary)
    return VerificationOutcome(ok=True, reason_code=None, summary=summary)


def _semantic_pass(claim: ArtifactClaim, result: dict[str, object]) -> Any:
    """Re-measure the artifact's content, independently of the producer.

    The **contract** comes from the durable row (what the plan promised); the
    **measurement** is taken here, with this verifier's own tools.  The
    producer's own report is never used as the measurement.
    """
    from nexus_ai_agent.creative.studio.models import MediaContract

    contract_raw = result.get("media_contract")
    contract: MediaContract | None = None
    if isinstance(contract_raw, dict):
        try:
            contract = MediaContract.model_validate(contract_raw)
        except Exception:  # noqa: BLE001 - reported below, never ignored
            contract = None

    # A media claim with no usable contract cannot be verified: "no contract"
    # means "nothing was promised", and a verifier that accepted an
    # unconstrained claim would pass any decodable file at all.
    if claim.kind in {"video", "image", "audio"} and contract is None:
        return SemanticReport(
            verified=False,
            method="contract_missing",
            findings=("the row carries no usable media contract for a media claim",),
            measurements={},
        )

    if claim.kind == "video":
        return verify_video_semantics(
            claim.path,
            expected_width=_int(contract, "width"),
            expected_height=_int(contract, "height"),
            expected_duration_us_min=_int(contract, "duration_us_min"),
            expected_duration_us_max=_int(contract, "duration_us_max"),
            expected_fps=_float(contract, "frame_rate"),
            expected_codec=_str(contract, "codec"),
            expected_pixel_format=_str(contract, "pixel_format"),
        )
    if claim.kind == "image":
        return verify_image_semantics(
            claim.path,
            expect_grayscale=bool(contract is not None and contract.color_space == "gray"),
        )
    # A non-media artifact has no semantic contract; the canonical integrity
    # pass above is the whole verification for it.
    return SemanticReport(
        verified=True,
        method="not_applicable",
        findings=(),
        measurements={"kind": claim.kind},
    )


def _str(contract: object, field: str) -> str | None:
    value = getattr(contract, field, None) if contract is not None else None
    return str(value).lower() if isinstance(value, str) and value else None


def _float(contract: object, field: str) -> float | None:
    value = getattr(contract, field, None) if contract is not None else None
    return float(value) if isinstance(value, (int, float)) else None


def _int(contract: object, field: str) -> int | None:
    value = getattr(contract, field, None) if contract is not None else None
    return int(value) if isinstance(value, int) else None


__all__ = ["REASON_SEMANTIC_MISMATCH", "composition_verifier"]
