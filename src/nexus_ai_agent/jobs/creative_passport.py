"""Durable evidence records for the SQLite ``creative_render`` queue lane.

This module deliberately records an execution projection, not a competing
CreativeWork/Project domain model.  A passport binds the queue request and
attempt to the source bytes, the independently verified output bytes, the
render-spec hash, and the capability/policy facts observable in the legacy
worker.  It also states which canonical lineage facts that worker does not
persist (CreativeWork, project revision, CommandBus transaction, and an actor
authorization decision).
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.jobs.verification import VerificationOutcome

CREATIVE_RENDER_JOB_TYPE = "creative_render"
TIMELINE_TRIM_OPERATION = "timeline.trim"
MAX_EVIDENCE_FILE_BYTES = 100 * 1024 * 1024


class CreativePassportError(RuntimeError):
    """A successful render could not be given a complete durable passport."""


class CreativeEvidenceTooLargeError(CreativePassportError):
    """An evidence asset exceeded the bounded store's per-file size limit."""

    def __init__(
        self,
        message: str,
        *,
        code: Literal["invalid_request", "render_failed"],
    ) -> None:
        super().__init__(message)
        self.code = code


class CreativeRequestConflictError(ValueError):
    """An idempotency key was reused for a different creative request."""


class RequestIdentity(BaseModel):
    """Stable queue identities derived from the request, not process-local IDs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    request_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    transaction_id: str = Field(min_length=1)


class StoredAsset(BaseModel):
    """Content identity plus a relocatable key in the queue's artifact store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str = Field(min_length=1)
    logical_asset_id: str = Field(min_length=1)
    media_kind: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    storage_key: str = Field(min_length=1)


class CreativeExecutionPassport(BaseModel):
    """Re-readable, content-addressed evidence for one verified trim attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1)
    passport_id: str = Field(min_length=1)
    request: dict[str, Any]
    transaction_id: str = Field(min_length=1)
    transaction: dict[str, Any]
    job: dict[str, Any]
    attempt: dict[str, Any]
    project_id: str = Field(min_length=1)
    intent: dict[str, Any]
    plan: dict[str, Any]
    revision: dict[str, Any]
    capability: dict[str, Any]
    policy: dict[str, Any]
    authorization: dict[str, Any]
    runtime: dict[str, Any]
    input_asset: StoredAsset
    artifact: StoredAsset
    verification: dict[str, Any]
    lineage: dict[str, Any]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class FileSnapshot:
    """A source file's measured identity before the render handler runs."""

    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ArchivedCreativeEvidence:
    """Queue-ready output and evidence produced before the completion commit."""

    result: dict[str, object]
    verification: dict[str, Any]
    execution_evidence: dict[str, Any]
    passport: CreativeExecutionPassport


def canonical_json(value: object) -> str:
    """Canonical UTF-8 JSON used for fingerprints and evidence IDs."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CreativePassportError(f"evidence is not canonical JSON: {exc}") from exc


def content_id(kind: str, value: object) -> str:
    """Domain-separated stable identifier for a canonical JSON value."""
    if not kind or not kind.replace("_", "").isalnum():
        raise ValueError("content ID kind must be an alphanumeric identifier")
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{kind}_{digest}"


def request_identity(
    job_type: str, idempotency_key: str, payload: Mapping[str, object]
) -> RequestIdentity:
    """Build the durable request/fingerprint/transaction tuple."""
    payload_fingerprint = (
        "sha256:" + hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()
    )
    request_id = content_id("request", {"job_type": job_type, "idempotency_key": idempotency_key})
    transaction_id = content_id(
        "transaction",
        {"request_id": request_id, "request_fingerprint": payload_fingerprint},
    )
    return RequestIdentity(
        request_id=request_id,
        request_fingerprint=payload_fingerprint,
        transaction_id=transaction_id,
    )


def attempt_id(job_id: str, attempt_number: int) -> str:
    """Stable execution ID for one queue fencing-token generation."""
    if attempt_number < 1:
        raise ValueError("attempt_number must be positive")
    return content_id("attempt", {"job_id": job_id, "attempt": attempt_number})


def is_timeline_trim_request(payload: Mapping[str, object]) -> bool:
    """Whether a persisted payload is a well-formed request for the trim lane."""
    from nexus_ai_agent.creative.render_jobs import SURFACE_TO_CANONICAL, CreativeRenderPayload

    try:
        creative_payload = CreativeRenderPayload.model_validate(dict(payload))
    except Exception:  # noqa: BLE001 - malformed legacy/test payload is not a trim request
        return False
    return (
        SURFACE_TO_CANONICAL.get((creative_payload.command, creative_payload.operation))
        == TIMELINE_TRIM_OPERATION
    )


def capture_trim_source(payload: Mapping[str, object]) -> FileSnapshot | None:
    """Measure the guarded input for an actual ``/edit trim`` request.

    Non-trim creative jobs return ``None``. Typed render-input errors from the
    existing guards propagate so the real handler can persist its public
    failure. Evidence-infrastructure errors raise ``CreativePassportError``
    and fail before the handler runs.
    """
    from nexus_ai_agent.creative.render_jobs import (
        SURFACE_TO_CANONICAL,
        CreativeRenderError,
        CreativeRenderPayload,
        _guarded_input,
        _guarded_workspace,
    )

    try:
        creative_payload = CreativeRenderPayload.model_validate(dict(payload))
    except Exception:  # noqa: BLE001 - malformed rows are not trim evidence
        return None
    if (
        SURFACE_TO_CANONICAL.get((creative_payload.command, creative_payload.operation))
        != TIMELINE_TRIM_OPERATION
    ):
        return None
    try:
        workspace = _guarded_workspace(creative_payload.workspace_dir)
        source_path = _guarded_input(creative_payload, workspace).resolve(strict=True)
        sha, size = hash_file(source_path)
    except CreativeRenderError:
        raise
    except CreativePassportError:
        raise
    except Exception as exc:  # noqa: BLE001 - evidence failures fail closed
        raise CreativePassportError(
            f"trim input evidence unavailable: {type(exc).__name__}"
        ) from exc
    return FileSnapshot(path=source_path, sha256=sha, size_bytes=size)


def hash_file(
    path: Path,
    *,
    too_large_code: Literal["invalid_request", "render_failed"] = "invalid_request",
) -> tuple[str, int]:
    """Stream and bound a file while measuring its stable content identity."""
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_EVIDENCE_FILE_BYTES:
                    raise CreativeEvidenceTooLargeError(
                        f"evidence file exceeds {MAX_EVIDENCE_FILE_BYTES} bytes",
                        code=too_large_code,
                    )
                digest.update(chunk)
    except CreativePassportError:
        raise
    except OSError as exc:
        raise CreativePassportError(f"cannot read evidence file: {exc}") from exc
    return "sha256:" + digest.hexdigest(), size


def archive_trim_evidence(
    *,
    artifact_root: Path,
    job_id: str,
    attempt_number: int,
    attempt_identifier: str,
    identity: RequestIdentity,
    payload: Mapping[str, object],
    result: Mapping[str, object],
    verification: Mapping[str, Any],
    source_snapshot: FileSnapshot | None,
    verifier: Callable[[dict[str, object], dict[str, object]], VerificationOutcome],
) -> ArchivedCreativeEvidence:
    """Persist both assets, independently reverify the archived output, and build a passport."""
    if source_snapshot is None:
        raise CreativePassportError("trim source was not measured before execution")
    if str(result.get("operation") or "") != TIMELINE_TRIM_OPERATION:
        raise CreativePassportError("verified result is not timeline.trim")
    if str(verification.get("status") or "") != "verified":
        raise CreativePassportError("original output verification is not successful")

    result_path_raw = result.get("artifact_path")
    if not isinstance(result_path_raw, str) or not result_path_raw:
        raise CreativePassportError("verified output path is missing")
    output_source = Path(result_path_raw).resolve(strict=True)
    if not output_source.is_file():
        raise CreativePassportError("verified output is not a regular file")
    source_after_sha, source_after_size = hash_file(source_snapshot.path)
    if (source_after_sha, source_after_size) != (
        source_snapshot.sha256,
        source_snapshot.size_bytes,
    ):
        raise CreativePassportError("trim input changed while the render was running")
    output_sha, output_size = hash_file(output_source, too_large_code="render_failed")

    attempt_directory_key = f"jobs/{_safe_job_key(job_id)}/{attempt_identifier}"
    attempt_directory = _resolve_store_path(artifact_root, attempt_directory_key)
    _mkdirs_durable(artifact_root, attempt_directory)
    root_resolved = artifact_root.resolve()
    attempt_directory = attempt_directory.resolve()
    try:
        attempt_directory.relative_to(root_resolved)
    except ValueError as exc:
        raise CreativePassportError("artifact directory escaped its configured root") from exc

    input_key = f"{attempt_directory_key}/input.mp4"
    output_key = f"{attempt_directory_key}/output.mp4"
    input_path = _resolve_store_path(artifact_root, input_key)
    output_path = _resolve_store_path(artifact_root, output_key)
    input_sha, input_size = _copy_verified(
        source_snapshot.path, input_path, too_large_code="invalid_request"
    )
    archived_sha, archived_size = _copy_verified(
        output_source, output_path, too_large_code="render_failed"
    )
    if (input_sha, input_size) != (source_snapshot.sha256, source_snapshot.size_bytes):
        raise CreativePassportError("archived trim input differs from the measured source")
    if (archived_sha, archived_size) != (output_sha, output_size):
        raise CreativePassportError("archived trim output differs from the verified render")

    archived_result: dict[str, object] = dict(result)
    archived_result.update(
        {
            "artifact_path": str(output_path),
            "artifact_kind": "video",
            "sha256": archived_sha,
            "size_bytes": archived_size,
        }
    )
    archived_payload = dict(payload)
    archived_payload["workspace_dir"] = str(attempt_directory)
    archived_outcome = verifier(archived_payload, archived_result)
    if not archived_outcome.ok:
        reason = archived_outcome.reason_code or "unknown"
        raise CreativePassportError(f"archived output failed independent verification: {reason}")

    try:
        evidence = runtime_evidence()
    except Exception as exc:  # noqa: BLE001 - missing registry facts invalidate the passport
        raise CreativePassportError("timeline.trim capability/policy evidence unavailable") from exc
    input_asset = StoredAsset(
        asset_id=content_id("asset", {"sha256": input_sha, "media_kind": "video"}),
        logical_asset_id="src",
        media_kind="video",
        sha256=input_sha,
        size_bytes=input_size,
        storage_key=input_key,
    )
    artifact_asset = StoredAsset(
        asset_id=content_id("asset", {"sha256": archived_sha, "media_kind": "video"}),
        logical_asset_id=str(result.get("output_asset_id") or "out"),
        media_kind="video",
        sha256=archived_sha,
        size_bytes=archived_size,
        storage_key=output_key,
    )
    passport = build_passport(
        job_id=job_id,
        attempt_number=attempt_number,
        attempt_identifier=attempt_identifier,
        identity=identity,
        payload=payload,
        result=archived_result,
        verification=archived_outcome.block,
        execution_evidence=evidence,
        input_asset=input_asset,
        artifact_asset=artifact_asset,
    )
    return ArchivedCreativeEvidence(
        result=archived_result,
        verification=archived_outcome.block,
        execution_evidence=evidence,
        passport=passport,
    )


def runtime_evidence() -> dict[str, Any]:
    """Snapshot the capability/policy facts the existing worker can expose."""
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

    registry = build_runtime_registry()
    description = registry.describe(TIMELINE_TRIM_OPERATION)
    decision = registry.check_permission(TIMELINE_TRIM_OPERATION, confirmed=False)
    version: str
    try:
        version = importlib.metadata.version("nexus-ai-agent")
    except importlib.metadata.PackageNotFoundError:
        version = "source-checkout"
    return {
        "capability": description.model_dump(mode="json"),
        "policy": {
            "operation_id": decision.operation_id,
            "permission_level": decision.level.value,
            "allowed": decision.allowed,
            "reason": decision.reason,
            "decision_source": "CapabilityRegistry.check_permission",
        },
        "authorization": {
            "status": "not_recorded_by_legacy_worker",
            "actor_id": None,
            "authorizer_decision": None,
            "note": (
                "The queue payload's user_id is not an authorization decision; "
                "the current CommandBus dispatch does not persist an actor-authorizer result."
            ),
        },
        "runtime": {
            "distribution_version": version,
            "python_version": platform.python_version(),
            "render_operation": TIMELINE_TRIM_OPERATION,
        },
    }


def build_passport(
    *,
    job_id: str,
    attempt_number: int,
    attempt_identifier: str,
    identity: RequestIdentity,
    payload: Mapping[str, object],
    result: Mapping[str, object],
    verification: Mapping[str, Any],
    execution_evidence: Mapping[str, Any],
    input_asset: StoredAsset,
    artifact_asset: StoredAsset,
) -> CreativeExecutionPassport:
    """Build the stable, explicit projection from the existing worker facts."""
    from nexus_ai_agent.creative.render_jobs import SURFACE_TO_CANONICAL, CreativeRenderPayload

    try:
        creative_payload = CreativeRenderPayload.model_validate(dict(payload))
    except Exception as exc:  # noqa: BLE001 - malformed lineage cannot be certified
        raise CreativePassportError("creative trim payload is not valid") from exc
    canonical_operation = SURFACE_TO_CANONICAL.get(
        (creative_payload.command, creative_payload.operation)
    )
    if canonical_operation != TIMELINE_TRIM_OPERATION:
        raise CreativePassportError("passport accepts only the existing timeline.trim lane")
    if str(result.get("operation") or "") != TIMELINE_TRIM_OPERATION:
        raise CreativePassportError("render result operation does not match timeline.trim")
    if verification.get("status") != "verified":
        raise CreativePassportError("passport requires successful independent verification")
    if verification.get("sha256") != artifact_asset.sha256:
        raise CreativePassportError("verification SHA-256 does not match stored output bytes")
    if verification.get("size_bytes") != artifact_asset.size_bytes:
        raise CreativePassportError("verification size does not match stored output bytes")

    logical_identity = verification.get("logical_identity")
    spec_identity = verification.get("spec_identity")
    if not isinstance(logical_identity, dict) or not isinstance(spec_identity, dict):
        raise CreativePassportError("verification omitted logical or spec identity")
    project_id = str(logical_identity.get("project_id") or "")
    if not project_id:
        raise CreativePassportError("verification omitted project identity")
    lane_ir_hash = spec_identity.get("lane_ir_hash")
    if not isinstance(lane_ir_hash, str) or not lane_ir_hash.startswith("sha256:"):
        raise CreativePassportError("render result omitted canonical lane IR hash")

    intent = {
        "intent_kind": "CreativeRenderPayloadProjection",
        "command": creative_payload.command,
        "surface_operation": creative_payload.operation,
        "operation_id": canonical_operation,
        "args": list(creative_payload.args),
        "media_duration_us": creative_payload.media_duration_us,
        "input_asset_id": input_asset.logical_asset_id,
        "input_asset_sha256": input_asset.sha256,
    }
    intent_id = content_id("intent", intent)
    plan = {
        "plan_kind": "render_lane_projection",
        "operation_id": canonical_operation,
        "lane_ir_hash": lane_ir_hash,
        "input_asset_id": input_asset.asset_id,
        "output_asset_id": artifact_asset.asset_id,
    }
    plan_id = content_id("plan", plan)
    revision_basis = {
        "project_id": project_id,
        "parent_revision_id": None,
        "intent_id": intent_id,
        "plan_id": plan_id,
        "output_asset_id": artifact_asset.asset_id,
    }
    revision = {
        "revision_id": content_id("revision_projection", revision_basis),
        "kind": "execution_projection_only",
        "sequence": 1,
        "parent_revision_id": None,
        "basis": revision_basis,
        "canonical_project_state_hash": None,
    }

    normalized_verification = normalize_verification(verification, artifact_asset)
    verification_id = content_id("verification", normalized_verification)
    capability = dict(execution_evidence.get("capability") or {})
    policy = dict(execution_evidence.get("policy") or {})
    authorization = dict(execution_evidence.get("authorization") or {})
    runtime = dict(execution_evidence.get("runtime") or {})
    if not capability or not policy or not authorization:
        raise CreativePassportError("capability/policy/authorization evidence is incomplete")
    if policy.get("allowed") is not True:
        raise CreativePassportError("timeline.trim capability policy did not allow execution")

    body: dict[str, Any] = {
        "schema_version": 1,
        "request": {
            "request_id": identity.request_id,
            "request_fingerprint": identity.request_fingerprint,
        },
        "transaction_id": identity.transaction_id,
        "transaction": {
            "transaction_id": identity.transaction_id,
            "kind": "queue_request_transaction_projection",
            "canonical_commandbus_transaction_id": None,
        },
        "job": {
            "job_id": job_id,
            "job_type": CREATIVE_RENDER_JOB_TYPE,
            "idempotency_key": creative_payload.idempotency_key,
        },
        "attempt": {
            "attempt_id": attempt_identifier,
            "attempt_number": attempt_number,
            "execution_state": "verified",
        },
        "project_id": project_id,
        "intent": {"intent_id": intent_id, **intent},
        "plan": {"plan_id": plan_id, **plan},
        "revision": revision,
        "capability": capability,
        "policy": policy,
        "authorization": authorization,
        "runtime": runtime,
        "input_asset": input_asset.model_dump(mode="json"),
        "artifact": artifact_asset.model_dump(mode="json"),
        "verification": {
            "verification_id": verification_id,
            **normalized_verification,
        },
        "lineage": {
            "request_id": identity.request_id,
            "transaction_id": identity.transaction_id,
            "job_id": job_id,
            "attempt_id": attempt_identifier,
            "project_id": project_id,
            "intent_id": intent_id,
            "plan_id": plan_id,
            "revision_id": revision["revision_id"],
            "input_asset_id": input_asset.asset_id,
            "artifact_id": artifact_asset.asset_id,
            "verification_id": verification_id,
        },
        "limitations": (
            "This is a queue execution projection, not a persisted CreativeWork "
            "or canonical project revision.",
            "The CommandBus transaction is in-memory; its transaction ID, revision, "
            "and state hash are not returned by the legacy worker.",
            "The queue records capability and policy facts but no actor authorization "
            "decision; user_id is not treated as authorization evidence.",
            "Source bytes are sampled before and after execution; the legacy worker "
            "does not bind its CommandBus source-asset digest into the queue result.",
        ),
    }
    passport_id = content_id("passport", body)
    return CreativeExecutionPassport(passport_id=passport_id, **body)


def normalize_verification(
    verification: Mapping[str, Any], artifact: StoredAsset
) -> dict[str, Any]:
    """Keep measured verification facts while replacing host paths with store keys."""
    logical_identity = verification.get("logical_identity")
    spec_identity = verification.get("spec_identity")
    probe = verification.get("probe")
    if not isinstance(logical_identity, dict) or not isinstance(spec_identity, dict):
        raise CreativePassportError("verification identity is malformed")
    physical = {
        "artifact_id": artifact.asset_id,
        "media_kind": artifact.media_kind,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "storage_key": artifact.storage_key,
    }
    return {
        "status": str(verification.get("status") or ""),
        "sha256": str(verification.get("sha256") or ""),
        "size_bytes": int(verification.get("size_bytes") or 0),
        "logical_identity": dict(logical_identity),
        "spec_identity": dict(spec_identity),
        "physical_identity": physical,
        "probe": dict(probe) if isinstance(probe, dict) else None,
    }


def reverify_stored_passport(
    *,
    artifact_root: Path,
    job_record: Mapping[str, Any],
    attempt_record: Mapping[str, Any],
    stored_passport_json: str,
    verifier: Callable[[dict[str, object], dict[str, object]], VerificationOutcome],
) -> CreativeExecutionPassport:
    """Re-read the durable source/output bytes and independently verify a passport."""
    try:
        passport = CreativeExecutionPassport.model_validate_json(stored_passport_json)
        attempt_payload = json.loads(str(job_record["payload_json"]))
        attempt_result = json.loads(str(attempt_record["execution_result_json"]))
        execution_evidence = json.loads(str(attempt_record["execution_evidence_json"]))
    except Exception as exc:  # noqa: BLE001 - malformed persisted evidence is corruption
        raise CreativePassportError("persisted creative passport evidence is malformed") from exc
    if not isinstance(attempt_payload, dict) or not isinstance(attempt_result, dict):
        raise CreativePassportError("persisted creative request/result is not an object")
    if not isinstance(execution_evidence, dict):
        raise CreativePassportError("persisted execution evidence is not an object")
    if str(attempt_record.get("status")) not in {"verified", "completed"}:
        raise CreativePassportError("producing attempt is not durably verified")
    if attempt_result.get("success") is False:
        raise CreativePassportError("persisted execution result is not successful")
    try:
        persisted_verification = json.loads(str(attempt_record["verification_json"]))
    except Exception as exc:  # noqa: BLE001 - missing/malformed attempt proof
        raise CreativePassportError("persisted attempt verification is malformed") from exc
    if not isinstance(persisted_verification, dict):
        raise CreativePassportError("persisted attempt verification is not an object")
    if attempt_result.get("artifact_verification") != persisted_verification:
        raise CreativePassportError("attempt result and verification ledger disagree")
    try:
        computed_identity = request_identity(
            str(job_record["job_type"]),
            str(job_record["idempotency_key"]),
            attempt_payload,
        )
    except Exception as exc:  # noqa: BLE001 - malformed request identity
        raise CreativePassportError("persisted request identity cannot be recomputed") from exc
    if (
        computed_identity.request_id != job_record.get("request_id")
        or computed_identity.request_fingerprint != job_record.get("request_fingerprint")
        or computed_identity.transaction_id != job_record.get("transaction_id")
    ):
        raise CreativePassportError("queue request identity does not match its payload")

    request = passport.request
    job = passport.job
    attempt = passport.attempt
    if (
        request.get("request_id") != job_record.get("request_id")
        or request.get("request_fingerprint") != job_record.get("request_fingerprint")
        or passport.transaction_id != job_record.get("transaction_id")
        or job.get("job_id") != job_record.get("id")
        or job.get("job_type") != CREATIVE_RENDER_JOB_TYPE
        or attempt.get("attempt_id") != attempt_record.get("attempt_id")
        or attempt.get("attempt_id")
        != attempt_id(str(job_record.get("id")), int(attempt_record.get("attempt") or 0))
        or attempt.get("attempt_number") != attempt_record.get("attempt")
    ):
        raise CreativePassportError("passport is detached from its queue/attempt identity")
    if attempt_record.get("passport_json") != stored_passport_json:
        raise CreativePassportError("queue passport differs from the producing attempt record")

    root = artifact_root.resolve()
    input_path = _resolve_existing_store_file(root, passport.input_asset.storage_key)
    output_path = _resolve_existing_store_file(root, passport.artifact.storage_key)
    input_sha, input_size = hash_file(input_path)
    output_sha, output_size = hash_file(output_path, too_large_code="render_failed")
    if (input_sha, input_size) != (passport.input_asset.sha256, passport.input_asset.size_bytes):
        raise CreativePassportError("persisted input asset no longer matches its passport")
    if (output_sha, output_size) != (passport.artifact.sha256, passport.artifact.size_bytes):
        raise CreativePassportError("persisted output asset no longer matches its passport")
    verifier_payload = dict(attempt_payload)
    verifier_payload["workspace_dir"] = str(output_path.parent)
    verification_result: dict[str, object] = dict(attempt_result)
    # The storage key is relocatable; persisted absolute paths can point to the
    # pre-restore root. Reverify the bytes resolved under the current root.
    verification_result["artifact_path"] = str(output_path)
    outcome = verifier(verifier_payload, verification_result)
    if not outcome.ok:
        reason = outcome.reason_code or "unknown"
        raise CreativePassportError(f"stored artifact no longer verifies: {reason}")

    identity = RequestIdentity(
        request_id=str(job_record["request_id"]),
        request_fingerprint=str(job_record["request_fingerprint"]),
        transaction_id=str(job_record["transaction_id"]),
    )
    measured_input = StoredAsset(
        asset_id=content_id("asset", {"sha256": input_sha, "media_kind": "video"}),
        logical_asset_id=passport.input_asset.logical_asset_id,
        media_kind=passport.input_asset.media_kind,
        sha256=input_sha,
        size_bytes=input_size,
        storage_key=passport.input_asset.storage_key,
    )
    measured_output = StoredAsset(
        asset_id=content_id("asset", {"sha256": output_sha, "media_kind": "video"}),
        logical_asset_id=passport.artifact.logical_asset_id,
        media_kind=passport.artifact.media_kind,
        sha256=output_sha,
        size_bytes=output_size,
        storage_key=passport.artifact.storage_key,
    )
    rebuilt = build_passport(
        job_id=str(job_record["id"]),
        attempt_number=int(attempt_record["attempt"]),
        attempt_identifier=str(attempt_record["attempt_id"]),
        identity=identity,
        payload=attempt_payload,
        result=attempt_result,
        verification=outcome.block,
        execution_evidence=execution_evidence,
        input_asset=measured_input,
        artifact_asset=measured_output,
    )
    if rebuilt.model_dump(mode="json") != passport.model_dump(mode="json"):
        raise CreativePassportError("recomputed passport differs from the persisted record")
    expected_passport_id = content_id(
        "passport", passport.model_dump(mode="json", exclude={"passport_id"})
    )
    if passport.passport_id != expected_passport_id:
        raise CreativePassportError("passport content identifier is invalid")
    return passport


def passport_json(passport: CreativeExecutionPassport) -> str:
    """Canonical serialization for a queue row and its producing attempt."""
    return canonical_json(passport.model_dump(mode="json"))


def asset_path(artifact_root: Path, storage_key: str) -> Path:
    """Resolve a portable storage key under the queue-owned artifact root."""
    return _resolve_existing_store_file(artifact_root.resolve(), storage_key)


def _safe_job_key(job_id: str) -> str:
    return hashlib.sha256(job_id.encode("utf-8")).hexdigest()


def _resolve_store_path(root: Path, storage_key: str) -> Path:
    relative = PurePosixPath(storage_key)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise CreativePassportError("invalid artifact-store key")
    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*relative.parts)
    try:
        candidate.resolve(strict=False).relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise CreativePassportError("artifact-store path escapes its root") from exc
    return candidate


def _resolve_existing_store_file(root: Path, storage_key: str) -> Path:
    candidate = _resolve_store_path(root, storage_key)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise CreativePassportError("stored artifact is missing or outside its root") from exc
    if not resolved.is_file():
        raise CreativePassportError("stored artifact is not a regular file")
    return resolved


def _mkdirs_durable(artifact_root: Path, directory: Path) -> None:
    """Create store directories, then sync every component through the store root."""
    root = artifact_root.resolve()
    target = directory.resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise CreativePassportError("artifact directory escaped its configured root") from exc

    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise CreativePassportError("could not find an existing artifact-store ancestor")
        current = parent
    if not current.is_dir():
        raise CreativePassportError("artifact-store path component is not a directory")

    for candidate in reversed(missing):
        try:
            candidate.mkdir()
        except FileExistsError:
            if not candidate.is_dir():
                raise CreativePassportError(
                    "artifact-store path was replaced by a non-directory"
                ) from None

    current = directory
    while current != root.parent:
        if not current.is_dir():
            raise CreativePassportError("artifact-store path component is not a directory")
        _fsync_directory(current)
        current = current.parent
    _fsync_directory(root.parent)


def _copy_verified(
    source: Path,
    destination: Path,
    *,
    too_large_code: Literal["invalid_request", "render_failed"],
) -> tuple[str, int]:
    """Atomically copy one evidence file and return the destination's measured identity."""
    if not destination.parent.is_dir():
        raise CreativePassportError("artifact-store destination directory is missing")
    if destination.exists():
        existing_sha, existing_size = hash_file(destination, too_large_code=too_large_code)
        source_sha, source_size = hash_file(source, too_large_code=too_large_code)
        if (existing_sha, existing_size) != (source_sha, source_size):
            raise CreativePassportError("artifact-store key already contains different bytes")
        return existing_sha, existing_size

    staging = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as reader, staging.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_EVIDENCE_FILE_BYTES:
                    raise CreativeEvidenceTooLargeError(
                        f"evidence file exceeds {MAX_EVIDENCE_FILE_BYTES} bytes",
                        code=too_large_code,
                    )
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        staging.replace(destination)
        _fsync_directory(destination.parent)
    except CreativePassportError:
        staging.unlink(missing_ok=True)
        raise
    except OSError as exc:
        staging.unlink(missing_ok=True)
        raise CreativePassportError(f"could not persist evidence asset: {exc}") from exc
    return "sha256:" + digest.hexdigest(), size


def _fsync_directory(directory: Path) -> None:
    """Persist directory-entry changes or fail the evidence write closed."""
    if os.name != "posix":
        raise CreativePassportError("durable trim evidence requires POSIX directory fsync")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise CreativePassportError(
            f"cannot open directory for durability sync: {directory}"
        ) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise CreativePassportError(f"cannot sync directory entry durability: {directory}") from exc
    finally:
        os.close(descriptor)


__all__ = [
    "ArchivedCreativeEvidence",
    "CreativeExecutionPassport",
    "CreativeEvidenceTooLargeError",
    "CreativePassportError",
    "CreativeRequestConflictError",
    "FileSnapshot",
    "RequestIdentity",
    "StoredAsset",
    "archive_trim_evidence",
    "asset_path",
    "attempt_id",
    "build_passport",
    "canonical_json",
    "capture_trim_source",
    "content_id",
    "hash_file",
    "is_timeline_trim_request",
    "passport_json",
    "reverify_stored_passport",
    "request_identity",
]
