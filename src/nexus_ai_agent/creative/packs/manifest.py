"""Capability-pack manifest (Nagar TDD section 2.5): packs are data, never code.

A Nagar pack declares *what it can do* and *which of the runtime's
pre-registered adapters, artifacts, permissions and resource budgets it
needs*.  It cannot ship executable hooks, install scripts or shell commands:

* the strict ``extra="forbid"`` models below reject unknown keys such as
  ``post_install``, ``entrypoint`` or ``shell`` instead of ignoring them;
* ``allow_arbitrary_native_code`` / ``allow_arbitrary_wasm_imports`` are typed
  as ``Literal[False]`` — a manifest that claims otherwise does not validate;
* ``tests/architecture/test_pack_manifest_is_data_only.py`` enforces the same
  rule on the raw JSON bytes and on this package's imports.

Everything here is pure data handling: parse, validate, describe.  No manifest
field is ever executed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

MANIFEST_SCHEMA: Literal["nexus.capability-pack.v1"] = "nexus.capability-pack.v1"
PROTOCOL_VERSION: Literal["nagar.command.v1"] = "nagar.command.v1"
STATE_SCHEMA: Literal["nagar.state.v1"] = "nagar.state.v1"

#: A ``base64:replace-…`` signature is the documented unsigned placeholder.
PLACEHOLDER_SIGNATURE_PREFIX = "base64:replace"

_PACKAGE_ID_RE = re.compile(r"^nexus(\.[a-z][a-z0-9_]*)+$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_RESOLUTION_RE = re.compile(r"^\d+x\d+$")

#: Permission tokens the runtime understands today (TDD section 2.5 plus the
#: Wave 2 additions).  Unknown tokens are reported as warnings, never granted.
KNOWN_PERMISSIONS = frozenset(
    {
        "read_project_audio",
        "read_project_video_frames",
        "read_project_images",
        "write_transcript_assets",
        "write_derived_caption_assets",
        "write_derived_video_assets",
        "write_timeline_layers",
        "egress_media_optin",
    }
)

ARTIFACT_KINDS = ("onnx_model", "wasm_module", "font", "lut", "weights", "data")
RUNTIME_BACKENDS = ("onnx_webgpu", "onnx_wasm_simd", "webcodecs", "none")
PACK_UPDATE_POLICIES = ("forbidden", "optional_signed_download")
SANDBOX_LEVELS = ("process_isolated", "in_process", "none")

AnnotatedStr = Annotated[str, Field(min_length=1)]


class PackManifestError(ValueError):
    """A manifest could not be read or validated."""


def parse_semver(value: str) -> tuple[int, int, int]:
    """Parse a strict ``MAJOR.MINOR.PATCH`` string (no ranges, no prefixes)."""
    match = _SEMVER_RE.match(value.strip())
    if match is None:
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {value!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _validate_semver(value: str) -> str:
    parse_semver(value)
    return value


def _validate_resolution(value: str) -> str:
    if not _RESOLUTION_RE.match(value):
        raise ValueError(f"max_preview_resolution must look like '1280x720', got {value!r}")
    return value


def _validate_package_id(value: str) -> str:
    if not _PACKAGE_ID_RE.match(value):
        raise ValueError(f"package_id must look like 'nexus.<domain>.<name>', got {value!r}")
    return value


def _validate_relative_posix_path(value: str) -> str:
    if not value or value.startswith("/") or "\\" in value or ".." in value.split("/"):
        raise ValueError(f"artifact path must be a relative POSIX path inside the pack: {value!r}")
    return value


# ---------------------------------------------------------------------------
# Manifest sections (1:1 with the TDD JSON example)
# ---------------------------------------------------------------------------


class ArtifactSpec(BaseModel):
    """One hashed file the pack owns (model, font, LUT, data blob)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: AnnotatedStr
    kind: Literal["onnx_model", "wasm_module", "font", "lut", "weights", "data"]
    path: AnnotatedStr
    size_mb: float = Field(ge=0)
    sha256: AnnotatedStr
    license: AnnotatedStr

    _check_path = field_validator("path")(_validate_relative_posix_path)

    @property
    def digest_is_placeholder(self) -> bool:
        return "replace-with" in self.sha256


class NativeRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter_id: AnnotatedStr
    sandbox: Literal["process_isolated", "in_process", "none"]
    network_required: bool = False


class BrowserRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preferred: Literal["onnx_webgpu", "onnx_wasm_simd", "webcodecs", "none"] = "none"
    fallback: Literal["onnx_webgpu", "onnx_wasm_simd", "webcodecs", "none"] = "none"
    worker_required: bool = False


class RuntimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    native: NativeRuntime | None = None
    browser: BrowserRuntime | None = None

    @model_validator(mode="after")
    def _require_a_runtime(self) -> RuntimeSpec:
        if self.native is None and self.browser is None:
            raise ValueError("a pack must declare at least one runtime (native and/or browser)")
        return self


class NetworkPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    upload_media: bool = False
    runtime_network: bool = False
    pack_update: Literal["forbidden", "optional_signed_download"] = "forbidden"


class GpuRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    optional: bool = True
    webgpu: bool = False
    features: tuple[str, ...] = ()


class HardwareRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_ram_mb: int = Field(gt=0)
    recommended_ram_mb: int = Field(gt=0)
    minimum_cpu_threads: int = Field(gt=0)
    gpu: GpuRequirement = Field(default_factory=GpuRequirement)
    disk_free_mb: int = Field(ge=0)

    @model_validator(mode="after")
    def _recommended_at_least_minimum(self) -> HardwareRequirements:
        if self.recommended_ram_mb < self.minimum_ram_mb:
            raise ValueError("recommended_ram_mb must be >= minimum_ram_mb")
        return self


class ResourceBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_resident_model_mb: int = Field(ge=0)
    max_audio_minutes_in_memory: float = Field(gt=0)
    max_preview_resolution: AnnotatedStr

    _check_resolution = field_validator("max_preview_resolution")(_validate_resolution)


class Compatibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_nagar_version: AnnotatedStr
    protocol_version: Literal["nagar.command.v1"] = PROTOCOL_VERSION
    state_schema: Literal["nagar.state.v1"] = STATE_SCHEMA

    _check_min_version = field_validator("min_nagar_version")(_validate_semver)


class SecuritySpec(BaseModel):
    """Fail-closed security posture; the two ``Literal[False]`` fields are hard."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature: AnnotatedStr
    trusted_publisher: AnnotatedStr
    allow_arbitrary_native_code: Literal[False] = False
    allow_arbitrary_wasm_imports: Literal[False] = False

    @property
    def signature_is_placeholder(self) -> bool:
        return self.signature.startswith(PLACEHOLDER_SIGNATURE_PREFIX)


# ---------------------------------------------------------------------------
# The manifest itself
# ---------------------------------------------------------------------------


class CapabilityPackManifest(BaseModel):
    """A validated, immutable capability-pack manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_schema: Literal["nexus.capability-pack.v1"] = MANIFEST_SCHEMA
    package_id: AnnotatedStr
    version: AnnotatedStr
    display_name: AnnotatedStr
    download_size_mb: float = Field(ge=0)
    capabilities: tuple[AnnotatedStr, ...] = Field(min_length=1)
    runtime: RuntimeSpec
    permissions: tuple[str, ...] = ()
    network_policy: NetworkPolicy = Field(default_factory=NetworkPolicy)
    hardware_requirements: HardwareRequirements
    artifacts: tuple[ArtifactSpec, ...] = ()
    resource_budget: ResourceBudget
    compatibility: Compatibility
    security: SecuritySpec
    #: Deliberate additive extension of the TDD schema: packs may depend on host
    #: binaries (e.g. ``ffmpeg>=5``).  Declaring them keeps the dependency
    #: visible to the runtime instead of hiding it inside adapter code.
    external_binaries: tuple[str, ...] = ()

    _check_package_id = field_validator("package_id")(_validate_package_id)
    _check_version = field_validator("version")(_validate_semver)

    @field_validator("capabilities")
    @classmethod
    def _check_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for capability in value:
            if not _CAPABILITY_RE.match(capability):
                raise ValueError(
                    f"invalid capability id {capability!r} (expected 'domain.operation')"
                )
            if capability in seen:
                raise ValueError(f"duplicate capability {capability!r}")
            seen.add(capability)
        return value

    @model_validator(mode="after")
    def _capability_domains_belong_to_the_package(self) -> CapabilityPackManifest:
        segments = set(self.package_id.split("."))
        for capability in self.capabilities:
            domain = capability.split(".")[0]
            if domain not in segments:
                raise ValueError(
                    f"capability {capability!r} is not in the {self.package_id!r} namespace "
                    f"(expected one of {sorted(segments)})"
                )
        return self

    @property
    def signature_is_placeholder(self) -> bool:
        return self.security.signature_is_placeholder


def load_manifest(path: Path) -> CapabilityPackManifest:
    """Read and validate a ``pack.manifest.json`` file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PackManifestError(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PackManifestError(f"manifest is not valid JSON: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PackManifestError(f"manifest root must be a JSON object: {path}")
    try:
        return CapabilityPackManifest.model_validate(raw)
    except ValidationError as exc:
        raise PackManifestError(f"manifest failed validation: {path}\n{exc}") from exc


def dump_manifest(manifest: CapabilityPackManifest) -> str:
    """Canonical JSON rendering (stable key order, used by ``packs list --json``)."""
    return json.dumps(manifest.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
