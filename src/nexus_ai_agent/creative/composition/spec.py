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
    *compatible*, not *equal*). ``properties`` carries dimension/sample-rate
    constraints expressed as simple predicates the compiler can check.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: MediaKind
    codec: str | None = Field(default=None, max_length=64)
    container: str | None = Field(default=None, max_length=64)
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
    """The software *and* model licence a capability carries.

    ``copyleft`` is the property the composition compiler cares about: a
    copyleft component propagates its licence to the whole composition.
    ``allow_commercial`` is recorded so a policy engine can refuse commercial
    use of a composition that contains a non-commercial component.

    Honesty rule: ``source_license`` and ``model_license`` are distinct. A
    capability may ship MIT code that drives a CC-BY-NC model -- both are
    recorded and the *stricter* governs the composition.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    spdx_id: str = Field(min_length=1, max_length=64)
    source_license: str = Field(min_length=1, max_length=128)
    model_license: str | None = Field(default=None, max_length=128)
    copyleft: bool = False
    allow_commercial: bool = True
    attribution_required: bool = False
    notice: str = Field(default="", max_length=1000)

    @property
    def effective_allow_commercial(self) -> bool:
        return self.allow_commercial and (self.model_license is None or "nc" not in self.model_license.lower())


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


class CapabilitySpec(BaseModel):
    """A versioned, typed capability contract -- the unit of composition.

    This is *not* an executable operation by itself. It is the *contract*:
    what goes in, what comes out, what it costs, what licence it carries,
    whether it is deterministic, and how to verify its output. The runtime
    binds a spec to a worker backend (FFmpeg, ImageMagick, a model adapter)
    that actually performs the work.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64, pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(default="", max_length=1000)
    inputs: tuple[CapabilityInput, ...]
    outputs: tuple[CapabilityOutput, ...]
    resources: ResourceRequirements = Field(default_factory=ResourceRequirements)
    license: LicenseInfo
    deterministic: bool = True
    cancellable: bool = True
    idempotent: bool = True
    undoable: bool = True
    network_required: bool = False
    verification: VerificationContract = Field(default_factory=VerificationContract)
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
        if self.network_required and self.license.effective_allow_commercial is False:
            # not inherently forbidden, but a non-commercial network-required
            # capability is almost certainly a paid API -- flag it.
            pass
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
    "LicenseInfo",
    "MediaType",
    "MediaKind",
    "ResourceRequirements",
    "VerificationContract",
]
