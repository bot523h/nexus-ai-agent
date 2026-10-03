"""Content-Addressed Proof-Carrying Artifact Passport & Provenance Model.

Provides canonical identity, cryptographic causal lineage, and verifiable execution proofs.
Binds Request -> Intent -> Plan -> Transaction -> Command -> Execution -> Verification -> Artifact.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    parent_passport_hashes: tuple[str, ...] = ()

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
    causal_chain: ProvenanceCausalChain
    execution_proof: ExecutionProof
    passport_hash: str = Field(default="", pattern=r"^(sha256:[a-f0-9]{64})?$")

    def compute_passport_hash(self) -> str:
        """Deterministically compute content-addressed SHA256 passport hash over logical payload."""
        payload = {
            "passport_version": self.passport_version,
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "content_hash": self.content_hash,
            "causal_chain": self.causal_chain.model_dump(mode="json"),
            "execution_proof": self.execution_proof.model_dump(mode="json"),
        }
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
