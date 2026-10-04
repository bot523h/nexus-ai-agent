"""Content-Addressed Proof-Carrying Artifact Passport & Independent Media Verifier.

Provides canonical identity, cryptographic causal lineage, independent byte/header
media verification, and verifiable execution proofs.
Binds Principal/Actor -> Request/Intent -> Plan -> Command -> Transaction ->
Project Revision -> Job/Attempt/Fencing Token -> Artifact Passport ->
Independent Verification -> Parent Lineage Edges.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IndependentMediaVerificationError(ValueError):
    """Raised fail-closed when an artifact fails independent byte/header verification."""


class ExecutionProof(BaseModel):
    """Machine-verifiable evidence produced during operation execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executor_id: str = Field(min_length=1)
    status: Literal["success", "failure"]
    output_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    duration_ms: float = Field(ge=0.0)
    verification_metrics: dict[str, Any] = Field(default_factory=dict)
    timestamp_utc: str = Field(min_length=1)


class ProvenanceCausalChain(BaseModel):
    """Cryptographic causal lineage binding request identity to execution artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    intent_id: str | None = None
    plan_id: str | None = None
    transaction_id: str = Field(min_length=1)
    command_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    producing_revision: int | None = Field(default=None, ge=0)
    job_id: str | None = None
    attempt_id: str | None = None
    fencing_token: int | None = Field(default=None, ge=1)
    parent_passport_hashes: tuple[str, ...] = ()
    parent_artifact_ids: tuple[str, ...] = ()

    @field_validator("parent_passport_hashes")
    @classmethod
    def _validate_hashes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for v in values:
            if not v.startswith("sha256:") or len(v) != 71:
                raise ValueError(f"invalid parent passport hash format: {v!r}")
        return values


class ArtifactPassport(BaseModel):
    """Canonical content-addressed provenance passport for creative execution artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passport_version: int = Field(default=1, ge=1)
    artifact_id: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    byte_size: int = Field(default=0, ge=0)
    storage_key: str | None = None
    parameter_hash: str | None = None
    verification_status: Literal["verified", "unverified", "failed"] = "verified"
    verification_receipt_id: str | None = None
    causal_chain: ProvenanceCausalChain
    execution_proof: ExecutionProof
    passport_hash: str = Field(default="", pattern=r"^(sha256:[a-f0-9]{64})?$")

    @property
    def project_id(self) -> str:
        return self.causal_chain.project_id

    @property
    def producing_transaction_id(self) -> str:
        return self.causal_chain.transaction_id

    @property
    def producing_command_id(self) -> str:
        return self.causal_chain.command_id

    @property
    def content_sha256(self) -> str:
        return self.content_hash

    @property
    def media_kind(self) -> str:
        return self.artifact_type

    def compute_passport_hash(self) -> str:
        """Deterministically compute content-addressed SHA256 passport hash over logical payload."""
        payload: dict[str, Any] = {
            "passport_version": self.passport_version,
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "content_hash": self.content_hash,
            "causal_chain": self.causal_chain.model_dump(mode="json", exclude_defaults=True),
            "execution_proof": self.execution_proof.model_dump(mode="json"),
        }
        if self.byte_size > 0:
            payload["byte_size"] = self.byte_size
        if self.storage_key is not None:
            payload["storage_key"] = self.storage_key
        if self.parameter_hash is not None:
            payload["parameter_hash"] = self.parameter_hash
        if self.verification_receipt_id is not None:
            payload["verification_receipt_id"] = self.verification_receipt_id
        if self.verification_status != "verified":
            payload["verification_status"] = self.verification_status
        raw_bytes = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"

    def with_computed_hash(self) -> ArtifactPassport:
        """Return a copy of the passport with its deterministic passport_hash populated."""
        computed = self.compute_passport_hash()
        return self.model_copy(update={"passport_hash": computed})

    def verify_integrity(self) -> bool:
        """Verify self-contained passport_hash against deterministic payload hash."""
        if not self.passport_hash:
            return False
        return self.passport_hash == self.compute_passport_hash()


def _verify_magic_signature(media_kind: str, header: bytes) -> None:
    """Inspect container/header signature bytes against declared media_kind."""
    kind = media_kind.lower().strip()
    if kind in ("image", "png", "jpeg", "jpg", "webp"):
        is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
        is_jpeg = header.startswith(b"\xff\xd8\xff")
        is_webp = len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
        if not (is_png or is_jpeg or is_webp):
            raise IndependentMediaVerificationError(
                f"Spoofed or corrupted image header for media_kind={media_kind!r}"
            )
    elif kind in ("video", "mp4", "webm"):
        is_mp4 = len(header) >= 12 and header[4:8] == b"ftyp"
        is_webm = header.startswith(b"\x1a\x45\xdf\xa3")
        if not (is_mp4 or is_webm):
            raise IndependentMediaVerificationError(
                f"Spoofed or corrupted video container header for media_kind={media_kind!r}"
            )
    elif kind in ("audio", "wav", "mp3"):
        is_wav = len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE"
        is_mp3 = header.startswith(b"ID3") or header.startswith(b"\xff\xfb")
        if not (is_wav or is_mp3):
            raise IndependentMediaVerificationError(
                f"Spoofed or corrupted audio header for media_kind={media_kind!r}"
            )
    elif kind == "pdf":
        if not header.startswith(b"%PDF-"):
            raise IndependentMediaVerificationError("Spoofed or corrupted PDF header")
    elif kind in ("document", "otio", "srt", "json", "text"):
        try:
            decoded = header.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IndependentMediaVerificationError(
                f"Document artifact is not valid UTF-8 text: {exc}"
            ) from exc
        if not decoded.strip():
            raise IndependentMediaVerificationError("Document artifact contains only whitespace")
    else:
        raise IndependentMediaVerificationError(
            f"Unsupported media_kind for verification: {kind!r}"
        )


def verify_media_artifact_independently(
    file_path: str | Path,
    *,
    allowed_root: str | Path,
    declared_sha256: str,
    declared_media_kind: str,
    declared_byte_size: int | None = None,
) -> dict[str, Any]:
    """Independently inspect actual artifact bytes, container headers, and SHA-256 digest.

    Never trusts producer-supplied metadata alone. Zero-byte, truncated, corrupted,
    wrong-digest, root-escaping, or spoofed-extension artifacts fail closed.
    """
    raw_path = Path(file_path)
    root_path = Path(allowed_root).resolve(strict=False)
    if raw_path.is_symlink():
        raise IndependentMediaVerificationError("Artifact path must not be a symbolic link")
    resolved = raw_path.resolve(strict=False)
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise IndependentMediaVerificationError(
            f"Artifact path {resolved} escapes allowed_root {root_path}"
        ) from exc

    if not resolved.exists() or not resolved.is_file():
        raise IndependentMediaVerificationError(f"Artifact file does not exist: {resolved}")

    data = resolved.read_bytes()
    actual_size = len(data)
    if actual_size <= 0:
        raise IndependentMediaVerificationError("Zero-byte artifact rejected fail-closed")

    if declared_byte_size is not None and actual_size != declared_byte_size:
        raise IndependentMediaVerificationError(
            f"Truncated or size-mismatched artifact: expected {declared_byte_size} bytes, "
            f"measured {actual_size} bytes"
        )

    actual_sha256 = f"sha256:{hashlib.sha256(data).hexdigest()}"
    if actual_sha256 != declared_sha256:
        raise IndependentMediaVerificationError(
            f"Artifact SHA-256 mismatch: declared {declared_sha256}, measured {actual_sha256}"
        )

    _verify_magic_signature(declared_media_kind, data[:64])

    return {
        "verified": True,
        "artifact_path": str(resolved),
        "byte_size": actual_size,
        "content_sha256": actual_sha256,
        "media_kind": declared_media_kind,
    }


def verify_causal_provenance_chain(passports: Sequence[ArtifactPassport]) -> bool:
    """Verify integrity and parent-child linkage across a causal sequence of ArtifactPassports."""
    if not passports:
        return False
    seen_hashes: set[str] = set()
    for passport in passports:
        if not passport.verify_integrity():
            return False
        if passport.verification_status != "verified":
            return False
        for parent_hash in passport.causal_chain.parent_passport_hashes:
            if parent_hash not in seen_hashes:
                return False
        seen_hashes.add(passport.passport_hash)
    return True


__all__ = [
    "ArtifactPassport",
    "ExecutionProof",
    "IndependentMediaVerificationError",
    "ProvenanceCausalChain",
    "verify_causal_provenance_chain",
    "verify_media_artifact_independently",
]
