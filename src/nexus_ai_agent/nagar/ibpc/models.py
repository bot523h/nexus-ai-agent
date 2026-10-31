"""Typed, versioned, immutable contracts of the Intent-Bound Proof Capsule layer.

Every model here is frozen pydantic with ``extra="forbid"``: the IBPC contract
is data, and a digest is computed over its canonical JSON spelling (the same
``canonical_json`` the provenance ledger uses — see ``canonical.py``). Changing
any effective field changes the digest; reordering fields in a serialized blob
does not (canonical JSON sorts keys), which is exactly the property the capsule
needs: *digest binds semantic content, not storage whitespace*.

Authority note (mission §9): none of these models is an authority. An
:class:`ApprovalBinding` records a human decision; it never grants a
permission. Execution authority stays with Policy/CommandBus/queue, evidence
stays with the provenance journal/passport.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nexus_ai_agent.nagar.ibpc.canonical import digest_of

CAPSULE_VERSION: Literal["1"] = "1"
AST_SCHEMA_ID: Literal["nexus.ibpc.ast.v1"] = "nexus.ibpc.ast.v1"
PLAN_SCHEMA_ID: Literal["nexus.ibpc.plan.v1"] = "nexus.ibpc.plan.v1"
PREVIEW_SCHEMA_ID: Literal["nexus.ibpc.preview.v1"] = "nexus.ibpc.preview.v1"
APPROVAL_SCHEMA_ID: Literal["nexus.ibpc.approval.v1"] = "nexus.ibpc.approval.v1"
REPORT_SCHEMA_ID: Literal["nexus.ibpc.verifier_report.v1"] = "nexus.ibpc.verifier_report.v1"

_HEX64 = r"^sha256:[0-9a-f]{64}$"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _reject_non_finite(value: Any) -> None:
    """Reject NaN/Inf anywhere in a params payload (digests must be finite)."""
    import math

    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite float in params")
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_non_finite(item)


class CheckVerdict(str, Enum):
    """The only three verdicts a footprint verifier may issue.

    ``PASS`` requires *proof* (bit-exact preservation + proven intended
    change). ``INCONCLUSIVE`` is the honest answer when encoding/compression
    makes exact proof impossible — the system must never upgrade it to PASS.
    ``FAIL`` means a provable violation (out-of-scope change, or the intended
    change did not happen).
    """

    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"

    def worse_than(self, other: CheckVerdict) -> bool:
        order = {CheckVerdict.PASS: 0, CheckVerdict.INCONCLUSIVE: 1, CheckVerdict.FAIL: 2}
        return order[self] > order[other]


def worst_verdict(verdicts: tuple[CheckVerdict, ...]) -> CheckVerdict:
    """FAIL dominates INCONCLUSIVE dominates PASS."""
    worst = CheckVerdict.PASS
    for verdict in verdicts:
        if verdict.worse_than(worst):
            worst = verdict
    return worst


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class VideoTimeRange(BaseModel):
    """Half-open ``[start_us, end_us)`` microsecond range on one asset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> VideoTimeRange:
        if self.start_us >= self.end_us:
            raise ValueError(f"empty range: {self.start_us}..{self.end_us}")
        return self


class VideoTimeScope(BaseModel):
    """The testable change scope for a video edit.

    ``effect="keep"`` (the canonical ``timeline.trim`` semantics — in/out
    points are a KEEP window, output duration is ``end - start``): the range
    ``[start_us, end_us)`` is **preserved**; everything outside it is removed.
    ``effect="modify"`` (e.g. a graded segment): the range may change in
    place; everything outside it must be preserved bit-exactly for a PASS.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["video_time_range"] = "video_time_range"
    asset_id: str = Field(min_length=1, max_length=128)
    effect: Literal["keep", "modify"]
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> VideoTimeScope:
        if self.start_us >= self.end_us:
            raise ValueError(f"empty scope: {self.start_us}..{self.end_us}")
        return self

    @property
    def scope_digest(self) -> str:
        return digest_of(self.model_dump(mode="json"))

    def _complement(self, duration_us: int) -> tuple[VideoTimeRange, ...]:
        if duration_us <= 0:
            return ()
        out: list[VideoTimeRange] = []
        if self.start_us > 0:
            out.append(VideoTimeRange(start_us=0, end_us=min(self.start_us, duration_us)))
        if self.end_us < duration_us:
            out.append(VideoTimeRange(start_us=self.end_us, end_us=duration_us))
        return tuple(r for r in out if r.start_us < r.end_us)

    def forbidden_ranges(self, duration_us: int) -> tuple[VideoTimeRange, ...]:
        """The regions that must be **preserved** (never changed).

        ``keep``: the kept window itself. ``modify``: the complement.
        """
        if duration_us <= 0:
            return ()
        if self.effect == "keep":
            clamped_end = min(self.end_us, duration_us)
            if self.start_us < clamped_end:
                return (VideoTimeRange(start_us=self.start_us, end_us=clamped_end),)
            return ()
        return self._complement(duration_us)

    def removed_ranges(self, duration_us: int) -> tuple[VideoTimeRange, ...]:
        """The regions the operation is allowed to delete (``keep`` only)."""
        if self.effect == "keep":
            return self._complement(duration_us)
        return ()


# ---------------------------------------------------------------------------
# Typed AST / plan / preview / readback
# ---------------------------------------------------------------------------


class TypedCreativeAST(BaseModel):
    """The typed AST one user intent compiles to.

    Built by the deterministic parser (:mod:`nagar.ibpc.intent`) and validated
    against the live registry by the compiler (:mod:`nagar.ibpc.compiler`).
    ``ambiguities`` non-empty means the AST is *not* executable — the caller
    must ask, never guess.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["nexus.ibpc.ast.v1"] = AST_SCHEMA_ID
    ast_id: str = Field(default_factory=lambda: _new_id("ast"))
    project_id: str = Field(min_length=1, max_length=128)
    asset_id: str = Field(min_length=1, max_length=128)
    source_revision: int = Field(ge=0)
    operation: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    scope: VideoTimeScope
    forbidden_scopes: tuple[VideoTimeScope, ...] = ()
    ambiguities: tuple[str, ...] = ()

    @field_validator("params")
    @classmethod
    def _bounded_finite(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_non_finite(value)
        digest_of(value)  # raises on non-JSON-able content
        return value

    @property
    def ast_digest(self) -> str:
        return digest_of(self.model_dump(mode="json"))

    @property
    def executable(self) -> bool:
        return not self.ambiguities


class CapabilityIdentity(BaseModel):
    """Which registry capability (and schema) the plan compiled against.

    Derived from the authoritative registry at compile time — never from user
    text. ``schema_digest`` binds the operation's input JSON schema, so a
    registry-side schema change moves the capsule digest.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    schema_version: int = Field(ge=1)
    permission_level: str
    execution_modes: tuple[str, ...]
    required_packs: tuple[str, ...]
    required_permissions: tuple[str, ...]
    schema_digest: str = Field(pattern=_HEX64)

    @property
    def identity_digest(self) -> str:
        return digest_of(self.model_dump(mode="json"))


class PlannedStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)


class CompiledPlan(BaseModel):
    """The compiled, registry-validated plan. ``plan_digest`` binds it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["nexus.ibpc.plan.v1"] = PLAN_SCHEMA_ID
    plan_id: str = Field(default_factory=lambda: _new_id("plan"))
    ast_digest: str = Field(pattern=_HEX64)
    capability: CapabilityIdentity
    steps: tuple[PlannedStep, ...]
    execution_mode: Literal["local"] = "local"

    @property
    def plan_digest(self) -> str:
        return digest_of(self.model_dump(mode="json"))


class PreviewManifest(BaseModel):
    """A preview derived from — and bound to — the *same* compiled plan.

    Kind ``plan_derived`` is a valid preview per the mission contract: it is
    computed deterministically from the plan (frame map, output duration),
    never from free text, and its digest is what the approval binds. It is not
    master evidence and never executes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["nexus.ibpc.preview.v1"] = PREVIEW_SCHEMA_ID
    preview_id: str = Field(default_factory=lambda: _new_id("preview"))
    plan_digest: str = Field(pattern=_HEX64)
    scope_digest: str = Field(pattern=_HEX64)
    kind: Literal["plan_derived"] = "plan_derived"
    source_duration_us: int = Field(ge=0)
    output_duration_us: int = Field(ge=0)
    frame_map: dict[str, Any] = Field(default_factory=dict)

    @property
    def preview_digest(self) -> str:
        return digest_of(self.model_dump(mode="json"))


class Readback(BaseModel):
    """The structured readback shown to the user before approval.

    Rendered **from the typed AST/plan/preview** — every field is a typed
    value, the prose is generated from them, so the readback can never say
    something the plan does not contain.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    source_revision: int
    operation: str
    params: dict[str, Any]
    scope: VideoTimeScope
    forbidden_scopes: tuple[VideoTimeScope, ...]
    ambiguities: tuple[str, ...]
    preview: PreviewManifest
    text: str = Field(max_length=4000)


# ---------------------------------------------------------------------------
# Approval binding
# ---------------------------------------------------------------------------


class ApprovalState(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CONSUMED = "CONSUMED"
    REVOKED = "REVOKED"


class ApprovalBinding(BaseModel):
    """One human approval bound to exact digests, one revision, one turn.

    Single-use by construction: ``consume`` moves APPROVED → CONSUMED and any
    later use (replay) is refused. Binding to ``source_revision`` means a stale
    approval can never execute against a newer revision. The binding is
    evidence of a decision — it grants no permission; the canonical
    Policy/CommandBus/queue still authorize the execution.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["nexus.ibpc.approval.v1"] = APPROVAL_SCHEMA_ID
    approval_id: str = Field(default_factory=lambda: _new_id("appr"))
    session_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    sequence_number: int = Field(ge=1)
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    principal_id: str = Field(min_length=1, max_length=128)
    source_revision: int = Field(ge=0)
    source_asset_digest: str = Field(pattern=_HEX64)
    ast_digest: str = Field(pattern=_HEX64)
    plan_digest: str = Field(pattern=_HEX64)
    preview_digest: str = Field(pattern=_HEX64)
    state: ApprovalState = ApprovalState.PENDING
    nonce: str = Field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = ""
    decided_at: str = ""

    @property
    def identity_digest(self) -> str:
        """Digest over the immutable binding fields (excludes state/timestamps)."""
        return digest_of(
            self.model_dump(
                mode="json",
                include={
                    "schema_id",
                    "approval_id",
                    "session_id",
                    "turn_id",
                    "sequence_number",
                    "tenant_id",
                    "project_id",
                    "principal_id",
                    "source_revision",
                    "source_asset_digest",
                    "ast_digest",
                    "plan_digest",
                    "preview_digest",
                    "nonce",
                },
            )
        )

    @property
    def binding_digest(self) -> str:
        return digest_of(self.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Footprint verifier report
# ---------------------------------------------------------------------------


class FootprintCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    verdict: CheckVerdict
    evidence: dict[str, Any] = Field(default_factory=dict)


class VerifierReport(BaseModel):
    """Independent, content-based verification of one real artifact.

    Produced by :mod:`nagar.ibpc.footprint` from the **decoded pixels** of the
    source and the artifact — never from renderer metadata. ``verdict`` is
    the worst of the individual checks: PASS only when the intended change is
    proven *and* preservation outside the scope is bit-exact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["nexus.ibpc.verifier_report.v1"] = REPORT_SCHEMA_ID
    report_id: str = Field(default_factory=lambda: _new_id("vfy"))
    scope_digest: str = Field(pattern=_HEX64)
    source_digest: str = Field(pattern=_HEX64)
    artifact_digest: str = Field(pattern=_HEX64)
    source_frames: int = Field(ge=0)
    artifact_frames: int = Field(ge=0)
    checks: tuple[FootprintCheck, ...]
    verdict: CheckVerdict

    @property
    def report_digest(self) -> str:
        return digest_of(self.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# IBPC capsule
# ---------------------------------------------------------------------------


class IBPCCapsule(BaseModel):
    """Intent-Bound Proof Capsule — the mission's central contract.

    One immutable, versioned record that binds: what was understood
    (``intent_digest``, ``typed_ast_digest``), the exact asset+revision
    (``asset_id``, ``source_revision``, ``source_asset_digest``), the allowed
    scope (``scope_digest``), the capability (``capability_identity`` +
    ``capability_schema_digest``), the plan (``compiled_plan_digest``), the
    preview the user saw (``preview_digest``), the approval
    (``approval_id`` + ``approval_binding`` digest), the principal, the
    execution attempt (``execution_attempt_identity``), the real artifact
    (``output_artifact_digest``), the independent verifier report
    (``verifier_report_digest`` + verdict), evidence references and the
    existing provenance chain (``provenance_reference``).

    This is a **contract in the existing chain**, not a new authority: it is
    built after the canonical execution completed, it references the existing
    causal journal/passport, and it can be re-validated by anyone from the
    digests alone.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    capsule_version: Literal["1"] = CAPSULE_VERSION
    capsule_id: str = Field(default_factory=lambda: _new_id("capsule"))
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    asset_id: str = Field(min_length=1, max_length=128)
    source_revision: int = Field(ge=0)
    source_asset_digest: str = Field(pattern=_HEX64)
    intent_digest: str = Field(pattern=_HEX64)
    typed_ast_digest: str = Field(pattern=_HEX64)
    scope_digest: str = Field(pattern=_HEX64)
    capability_identity: str = Field(min_length=1, max_length=256)
    capability_schema_digest: str = Field(pattern=_HEX64)
    compiled_plan_digest: str = Field(pattern=_HEX64)
    preview_digest: str = Field(pattern=_HEX64)
    approval_id: str = Field(min_length=1)
    approval_binding: str = Field(pattern=_HEX64)
    principal_identity: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    execution_attempt_identity: str = Field(min_length=1, max_length=256)
    output_artifact_digest: str = Field(pattern=_HEX64)
    verifier_report_digest: str = Field(pattern=_HEX64)
    verifier_verdict: CheckVerdict
    evidence_references: tuple[str, ...] = ()
    provenance_reference: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""

    @property
    def capsule_digest(self) -> str:
        return digest_of(self.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


class PrivacyMode(str, Enum):
    LOCAL_VAULT = "LOCAL_VAULT"
    TRUSTED_CLOUD = "TRUSTED_CLOUD"
    EXPERIMENTAL_CLOUD = "EXPERIMENTAL_CLOUD"


class ConsentRecord(BaseModel):
    """An explicit, recorded consent for one privacy mode. Digest-only: the
    record never contains transcript or audio content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: PrivacyMode
    principal_id: str = Field(min_length=1, max_length=128)
    scope: str = Field(min_length=1, max_length=256)
    granted_at: str = Field(min_length=1)
    consent_id: str = Field(default_factory=lambda: _new_id("consent"))

    @property
    def consent_digest(self) -> str:
        return digest_of(self.model_dump(mode="json"))


__all__ = [
    "APPROVAL_SCHEMA_ID",
    "AST_SCHEMA_ID",
    "CAPSULE_VERSION",
    "PLAN_SCHEMA_ID",
    "PREVIEW_SCHEMA_ID",
    "REPORT_SCHEMA_ID",
    "ApprovalBinding",
    "ApprovalState",
    "CapabilityIdentity",
    "CheckVerdict",
    "CompiledPlan",
    "ConsentRecord",
    "FootprintCheck",
    "IBPCCapsule",
    "PlannedStep",
    "PrivacyMode",
    "PreviewManifest",
    "Readback",
    "TypedCreativeAST",
    "VerifierReport",
    "VideoTimeRange",
    "VideoTimeScope",
    "worst_verdict",
]
