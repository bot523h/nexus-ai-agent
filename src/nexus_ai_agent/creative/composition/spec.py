"""The unified CapabilitySpec contract.

This is the cross-domain capability contract for the NAGAR composition layer.
It is deliberately richer than the studio :class:`OperationSpec` (which is
scoped to in-memory project-state mutations with pure handlers): a media
capability also declares the *media types* it consumes and produces, the
*resources* it needs, the *licence* it carries, and a *verification contract*
a downstream verifier can check independently.

A capability is the unit of composition: the :class:`CompositionCompiler`
takes a graph of these, validates that the outputs of one satisfy the inputs
of the next (and that the resource/licence/determinism budgets hold), and
produces a derived :class:`CapabilitySpec` for the whole composition.

Every field is fail-closed: an underspecified contract is rejected, never
defaulted to "anything goes".
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Media types
# ---------------------------------------------------------------------------

MediaKind = Literal["image", "video", "audio", "data", "text"]


class MediaType(BaseModel):
    """A typed media contract for one port of a capability.

    ``kind`` is the coarse domain. ``codec`` / ``container`` are optional
    refinement: when present they must match exactly at a binding; when absent
    the port accepts any codec/container of the right kind (the binding is
    *compatible*, not *equal*). ``properties`` carries arbitrary
    dimension/sample-rate constraints the compiler can check exactly.

    The technical media descriptors (``pixel_format``, ``color_space``,
    ``frame_rate``, ``sample_rate``, ``bit_depth``, ``has_alpha``) are
    optional *constraints*: when a port sets one, a produced value must match
    it; when a port leaves it ``None`` the descriptor is unconstrained. This
    keeps compatibility a real predicate over the media contract rather than a
    bare ``kind == kind`` check.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: MediaKind
    codec: str | None = Field(default=None, max_length=64)
    container: str | None = Field(default=None, max_length=64)
    pixel_format: str | None = Field(default=None, max_length=64)
    color_space: str | None = Field(default=None, max_length=64)
    frame_rate: float | None = Field(default=None, gt=0, le=1000)
    sample_rate: int | None = Field(default=None, gt=0, le=768_000)
    bit_depth: int | None = Field(default=None, ge=1, le=64)
    has_alpha: bool | None = None
    lossless: bool | None = None
    properties: dict[str, Any] = Field(default_factory=dict)

    def compatible_with(self, required: "MediaType") -> bool:
        """True if a value of *self* satisfies a port that requires *required*.

        Compatibility rules (fail-closed):

        * ``kind`` must be equal.
        * if ``required.codec`` is set, ``self.codec`` must equal it.
        * if ``required.container`` is set, ``self.container`` must equal it.
        * every key in ``required.properties`` must be present in
          ``self.properties`` with an equal value (range predicates are not
          evaluated here -- they are a runtime check).
        """
        if self.kind != required.kind:
            return False
        if required.codec is not None and self.codec != required.codec:
            return False
        if required.container is not None and self.container != required.container:
            return False
        for field in (
            "pixel_format",
            "color_space",
            "frame_rate",
            "sample_rate",
            "bit_depth",
            "has_alpha",
            "lossless",
        ):
            want = getattr(required, field)
            if want is not None and getattr(self, field) != want:
                return False
        for key, value in required.properties.items():
            if key not in self.properties:
                return False
            if self.properties[key] != value:
                return False
        return True


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


class CapabilityInput(BaseModel):
    """One typed input port of a capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    media_type: MediaType
    description: str = Field(default="", max_length=500)
    optional: bool = False


class CapabilityOutput(BaseModel):
    """One typed output port of a capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    media_type: MediaType
    description: str = Field(default="", max_length=500)


# ---------------------------------------------------------------------------
# Resources / licence / verification
# ---------------------------------------------------------------------------


class ResourceRequirements(BaseModel):
    """Resource budget a capability declares it needs.

    The compiler sums these across a composition and refuses a graph whose
    total exceeds the runtime quota. ``gpu`` is a discrete flag (the runtime
    either has a GPU or it does not); fine-grained GPU fractions are out of
    scope for v1.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_threads: int = Field(default=1, ge=1, le=256)
    ram_mb: int = Field(default=128, ge=1, le=1_000_000)
    gpu: bool = False
    disk_mb: int = Field(default=64, ge=1, le=1_000_000)
    network: bool = False
    timeout_seconds: int = Field(default=60, ge=1, le=86_400)


class LicenseInfo(BaseModel):
    """The licence ledger a capability carries.

    A single ``copyleft``/``allow_commercial`` pair is *not* enough to decide
    compatibility: an adapter's code, the executable it drives, its linked
    libraries, a model, its weights, a dataset and the input asset can each be
    licensed differently, and obligations (attribution, copyleft, network-use,
    distribution) differ per category. Each is recorded separately; a ``None``
    means **unknown**, and unknown NEVER counts as "commercially approved"
    (:attr:`effective_allow_commercial`).

    Categories (Gate 5 of the hardening mission):

    1. ``source_license``    -- this adapter's own code.
    2. ``runtime_license``   -- the executable it drives (e.g. an FFmpeg build).
    3. ``library_licenses``  -- libraries it links.
    4. ``model_license``     -- the model.
    5. ``weights_license``   -- the model weights (may differ from the model).
    6. ``dataset_license``   -- the training/eval dataset.
    7. ``input_asset_license`` -- the user-supplied input asset.
    8. ``distribution_scope``  -- where the output may be distributed.
    9. ``allow_commercial``    -- commercial-use permission (explicit).
    10. ``attribution_required`` / ``notice`` -- attribution and NOTICE text.
    11. ``copyleft``           -- copyleft obligation (propagates).
    12. ``network_use_obligations`` -- e.g. AGPL's network clause.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    spdx_id: str = Field(min_length=1, max_length=64)
    source_license: str = Field(min_length=1, max_length=128)
    model_license: str | None = Field(default=None, max_length=128)
    runtime_license: str | None = Field(default=None, max_length=128)
    library_licenses: tuple[str, ...] = Field(default_factory=tuple)
    weights_license: str | None = Field(default=None, max_length=128)
    dataset_license: str | None = Field(default=None, max_length=128)
    input_asset_license: str | None = Field(default=None, max_length=128)
    distribution_scope: str = Field(default="unspecified", max_length=64)
    network_use_obligations: str = Field(default="none", max_length=64)
    copyleft: bool = False
    allow_commercial: bool = True
    #: True only when commercial compatibility has actually been established.
    #: ``allow_commercial=True`` with ``commercial_use_verified=False`` is
    #: *unproven*, and unproven is treated as not-approved.
    commercial_use_verified: bool = True
    attribution_required: bool = False
    notice: str = Field(default="", max_length=1000)

    @property
    def effective_allow_commercial(self) -> bool:
        if not (self.allow_commercial and self.commercial_use_verified):
            return False
        for label in (self.model_license, self.weights_license):
            if label is not None and ("nc" in label.lower() or "noncommercial" in label.lower()):
                return False
        return True


class VerificationContract(BaseModel):
    """How a downstream verifier can independently check an output.

    ``method`` names the verification strategy. ``expected_properties`` are
    the structural assertions the verifier checks (e.g. ``{"width": 1920,
    "height": 1080, "duration_us": 5000000}``). ``hash_algorithm`` is the
    digest used for the proof-carrying artifact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Literal["hash", "structural", "perceptual", "none"] = "structural"
    expected_properties: dict[str, Any] = Field(default_factory=dict)
    hash_algorithm: Literal["sha256"] = "sha256"
    description: str = Field(default="", max_length=500)


# ---------------------------------------------------------------------------
# The capability spec
# ---------------------------------------------------------------------------


TrustLevel = Literal["builtin", "verified", "experimental", "untrusted"]


class EvidenceContract(BaseModel):
    """What evidence the runtime must record for an execution of this capability.

    The composition layer's proof-carrying artifact always records hashes and a
    manifest; this contract makes the *semantic* obligations explicit so a
    verifier knows what to check (e.g. decode the output, assert dimensions).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    require_input_hash: bool = True
    require_output_hash: bool = True
    require_tool_versions: bool = True
    require_decode: bool = False
    required_properties: tuple[str, ...] = Field(default_factory=tuple)


class CapabilitySpec(BaseModel):
    """A versioned, typed capability contract -- the unit of composition.

    This is *not* an executable operation by itself. It is the *contract*:
    what goes in, what comes out, what it costs, what licence it carries,
    whether it is deterministic, and how to verify its output. The runtime
    binds a spec to a worker backend (FFmpeg, ImageMagick, a model adapter)
    that actually performs the work.

    The canonical-integration fields are fail-closed:

    * ``runtime_adapter_id`` names the worker backend; it must be a member of
      the composition layer's frozen adapter allow-list
      (:data:`nexus_ai_agent.creative.composition.canonical.BUILTIN_ADAPTERS`).
    * ``canonical_operation`` -- when set, the id is cross-checked against the
      **live NEXUS registry** (``build_runtime_registry()``); a capability that
      claims to be a canonical operation the live registry does not provide is
      refused at registration, never silently trusted.
    * ``trust_level`` -- ``untrusted`` capabilities are refused by the runtime;
      ``experimental`` ones require an explicit runtime opt-in.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64, pattern=r"^\d+\.\d+\.\d+$")
    schema_version: int = Field(default=1, ge=1)
    description: str = Field(default="", max_length=1000)
    inputs: tuple[CapabilityInput, ...]
    outputs: tuple[CapabilityOutput, ...]
    resources: ResourceRequirements = Field(default_factory=ResourceRequirements)
    license: LicenseInfo
    runtime_adapter_id: str = Field(default="unbound", max_length=64)
    canonical_operation: str | None = Field(default=None, max_length=128)
    required_permissions: tuple[str, ...] = Field(default_factory=tuple)
    dependencies: tuple[str, ...] = Field(default_factory=tuple)
    trust_level: TrustLevel = "builtin"
    deterministic: bool = True
    cancellable: bool = True
    idempotent: bool = True
    undoable: bool = True
    network_required: bool = False
    verification: VerificationContract = Field(default_factory=VerificationContract)
    evidence: EvidenceContract = Field(default_factory=EvidenceContract)
    tags: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _validate(self) -> "CapabilitySpec":
        input_names = {i.name for i in self.inputs}
        if len(input_names) != len(self.inputs):
            raise ValueError("input names must be unique")
        output_names = {o.name for o in self.outputs}
        if len(output_names) != len(self.outputs):
            raise ValueError("output names must be unique")
        if not self.outputs:
            raise ValueError("a capability must declare at least one output")
        return self

    def input(self, name: str) -> CapabilityInput:
        for i in self.inputs:
            if i.name == name:
                return i
        raise KeyError(f"unknown input: {name!r}")

    def output(self, name: str) -> CapabilityOutput:
        for o in self.outputs:
            if o.name == name:
                return o
        raise KeyError(f"unknown output: {name!r}")


__all__ = [
    "CapabilityInput",
    "CapabilityOutput",
    "CapabilitySpec",
    "EvidenceContract",
    "LicenseInfo",
    "MediaType",
    "MediaKind",
    "ResourceRequirements",
    "VerificationContract",
]
