"""The composition IR: a typed, versioned plan over **canonical** capabilities.

This module is deliberately *not* a registry.  It holds no capability
definitions at all: a plan node names an **operation id** that must exist in
the live NEXUS registry
(:func:`nexus_ai_agent.creative.packs.runtime.build_runtime_registry`), and
the compiler resolves it there.  That is the difference between this layer and
the abandoned prototype on ``trae/agent-CCO4Fr``, which carried a private
``dict[str, CapabilitySpec]`` and could therefore "know" capabilities the
authoritative registry did not.

Three artefacts live here:

* :class:`CompositionPlan` -- the immutable, fingerprinted description of a
  composition: nodes, media bindings, external inputs, expected outputs.
* :class:`LicenseLedger` -- the separated licence facts (adapter code,
  runtime tool, libraries, model, weights, dataset, input asset, distribution
  scope, obligations) that a composition carries.  ``UNKNOWN`` stays unknown:
  an unproven licence is never "commercially approved".
* :class:`ResourceEstimate` -- the *declared* cost of a composition, kept
  separate from the *measured* peak (the runtime measures the peak; the
  estimate is what the compiler can know before a process starts).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_ai_agent.creative.studio.models import MediaContract, MediaPort

#: The plan schema version.  A plan compiled against one version is refused by
#: a worker that speaks another one — the version is part of the fingerprint.
PLAN_VERSION = 1

#: A node id is a **name**, never a path.  It is interpolated into the
#: per-node working directory, so a node id carrying a separator or a parent
#: reference would make the composition write outside its guarded workspace.
#: The pattern admits only word characters and single dashes.
NODE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

#: The twelve licence/dependency categories the ledger keeps apart.
#:
#: Conflating them is how a runtime claims a permission it never had: a
#: tool's licence says nothing about the rights of the media it produces.
CATEGORIES: tuple[str, ...] = (
    "adapter_code",
    "runtime_software",
    "library",
    "model",
    "model_weights",
    "dataset",
    "input_asset",
    "distribution_restrictions",
    "commercial_permission",
    "attribution_notices",
    "copyleft",
    "network_use",
)

#: The media kinds a composition may carry between nodes.
CompositionMediaKind = Literal["image", "video", "audio"]


class PortBinding(BaseModel):
    """How one media input port of a node is fed.

    Exactly one of ``external`` / ``node_output`` is set.  ``external`` names
    an entry of :class:`CompositionPlan.external_inputs`; ``node_output`` names
    an ``(node_id, output_port)`` pair of another node.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_port: str = Field(min_length=1, max_length=64)
    source: Literal["external", "node_output"]
    external: str | None = Field(default=None, max_length=64)
    node_id: str | None = Field(default=None, max_length=64)
    output_port: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _one_source(self) -> PortBinding:
        if self.source == "external":
            if not self.external or self.node_id or self.output_port:
                raise ValueError("an external binding names exactly one external input")
        else:
            if not self.node_id or not self.output_port or self.external:
                raise ValueError("a node_output binding names exactly one (node_id, output_port)")
        return self

    def describe(self) -> str:
        if self.source == "external":
            return f"external:{self.external}"
        return f"node:{self.node_id}.{self.output_port}"


class ExternalInput(BaseModel):
    """A media value the plan consumes from outside the graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    media_kind: CompositionMediaKind
    contract: MediaContract = Field(default_factory=lambda: MediaContract(kind="image"))


class PlanNode(BaseModel):
    """One node: a canonical operation plus the parameters the caller chose.

    The pinned ``capability_version`` / ``operation_schema_version`` are the
    plan's *reproducibility* record: the compiler refuses a plan whose pins no
    longer match the live registry, so a plan can never silently execute
    against a different capability than the one it was compiled for.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1, max_length=64, pattern=NODE_ID_PATTERN)
    operation: str = Field(min_length=1, max_length=128)
    capability_id: str = Field(min_length=1, max_length=128)
    capability_version: str = Field(min_length=1, max_length=64)
    operation_schema_version: int = Field(ge=1)
    params: dict[str, Any] = Field(default_factory=dict)
    bindings: tuple[PortBinding, ...] = ()

    @model_validator(mode="after")
    def _binding_ports_are_unique(self) -> PlanNode:
        names = [binding.input_port for binding in self.bindings]
        if len(set(names)) != len(names):
            raise ValueError("a node may bind each media input port at most once")
        return self


class ExpectedOutput(BaseModel):
    """One exposed output of the composition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    node_id: str = Field(min_length=1, max_length=64)
    output_port: str = Field(min_length=1, max_length=64)


class CompositionPlan(BaseModel):
    """An immutable, fingerprinted composition request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_version: int = Field(default=PLAN_VERSION, ge=1)
    nodes: tuple[PlanNode, ...] = Field(min_length=1)
    external_inputs: tuple[ExternalInput, ...] = ()
    expected_outputs: tuple[ExpectedOutput, ...] = Field(min_length=1)
    #: The fingerprint of the live registry the plan was compiled against.
    registry_fingerprint: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def _identifiers_are_unique(self) -> CompositionPlan:
        node_ids = [node.node_id for node in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("node ids must be unique")
        input_names = [item.name for item in self.external_inputs]
        if len(set(input_names)) != len(input_names):
            raise ValueError("external input names must be unique")
        output_names = [item.name for item in self.expected_outputs]
        if len(set(output_names)) != len(output_names):
            raise ValueError("expected output names must be unique")
        return self

    def node(self, node_id: str) -> PlanNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def external_input(self, name: str) -> ExternalInput:
        for item in self.external_inputs:
            if item.name == name:
                return item
        raise KeyError(name)

    def canonical_json(self) -> str:
        """A deterministic JSON rendering — the fingerprint's input."""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def fingerprint(self) -> str:
        """``sha256:<hex>`` over the canonical JSON of the plan."""
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# licence ledger
# ---------------------------------------------------------------------------


class LicenseRecord(BaseModel):
    """One licence fact, kept separate from every other licence fact.

    The categories are the ones the mission demands be distinguished: the
    adapter's own code, the executable it drives, the libraries it links, the
    model, the model *weights*, the dataset, the input asset, and the
    distribution / commercial / attribution / copyleft / network obligations.

    ``None`` means **unknown** and unknown never counts as "approved": see
    :attr:`effective_allow_commercial`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Which fact this record states.  The twelve categories are kept apart
    #: because conflating them is how a runtime claims a permission it never
    #: had — a tool's licence says nothing about the rights of the media it
    #: produces.
    category: Literal[
        "adapter_code",
        "runtime_software",
        "library",
        "model",
        "model_weights",
        "dataset",
        "input_asset",
        "distribution_restrictions",
        "commercial_permission",
        "attribution_notices",
        "copyleft",
        "network_use",
    ]
    #: The SPDX id or a human-readable licence name.
    spdx_id: str = Field(min_length=1, max_length=128)
    #: True when the licence is a copyleft licence (propagates).
    copyleft: bool = False
    #: True when commercial use is permitted *by the licence text*.
    allow_commercial: bool | None = None
    #: True only when the commercial permission was actually established from
    #: evidence (the build configuration, the licence file, the model card).
    commercial_use_verified: bool = False
    #: The distribution restriction, when the licence imposes one.
    distribution_scope: str = Field(default="unspecified", max_length=128)
    #: The attribution / NOTICE obligation.
    notice: str = Field(default="", max_length=1000)
    attribution_required: bool = False
    #: A network-use obligation (e.g. an AGPL-style network clause).
    network_use_obligations: str = Field(default="none", max_length=128)
    #: Where the evidence for this record came from.
    evidence: str = Field(default="", max_length=1000)

    @property
    def effective_allow_commercial(self) -> bool:
        """Commercially usable **only** when it was actually proven."""
        return bool(self.allow_commercial) and bool(self.commercial_use_verified)


class LicenseLedger(BaseModel):
    """The separated licence facts of one composition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    records: tuple[LicenseRecord, ...] = Field(default_factory=tuple)

    def of(self, category: str) -> tuple[LicenseRecord, ...]:
        return tuple(record for record in self.records if record.category == category)

    @property
    def effective_allow_commercial(self) -> bool:
        """True only when *every* record allows commercial use and proves it."""
        return bool(self.records) and all(
            record.effective_allow_commercial for record in self.records
        )

    @property
    def unknown_categories(self) -> tuple[str, ...]:
        """Categories whose licence is **not established** — reported, never assumed.

        A category is unknown when it has no record at all, and also when its
        only records state ``UNKNOWN``.  A record that admits it does not know
        is a *fact about the runtime*, not a permission, and it must not hide
        the category from this list.
        """
        known = {record.category for record in self.records}
        unresolved = {
            record.category
            for record in self.records
            if record.spdx_id.strip().upper() == "UNKNOWN"
        }
        return tuple(
            category for category in CATEGORIES if category not in known or category in unresolved
        )


# ---------------------------------------------------------------------------
# resource model
# ---------------------------------------------------------------------------


class ResourceRequirements(BaseModel):
    """The *declared* cost of one capability.

    This is an estimate, not an enforcement: a Pydantic model does not limit
    a process.  The runtime enforces what it can (a wall-clock timeout, a
    bounded output size, a bounded concurrency) and measures the rest.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_threads: int = Field(default=1, ge=1, le=256)
    ram_mb: int = Field(default=256, ge=1, le=1_000_000)
    gpu_memory_mb: int = Field(default=0, ge=0, le=1_000_000)
    disk_mb: int = Field(default=64, ge=1, le=1_000_000)
    scratch_mb: int = Field(default=64, ge=0, le=1_000_000)
    timeout_seconds: int = Field(default=120, ge=1, le=86_400)
    network_egress: bool = False


class ResourceEstimate(BaseModel):
    """The summed, *sequential* cost of a composition.

    A composition executes its nodes in topological order, one at a time, so
    the peak resident memory is the **maximum** over the nodes, not their sum.
    The sum is reported separately as ``total_ram_mb`` (what a parallel
    execution would need) so the two are never confused.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    peak_ram_mb: int = Field(ge=0)
    total_ram_mb: int = Field(ge=0)
    peak_concurrent_ram_mb: int = Field(ge=0)
    total_disk_mb: int = Field(ge=0)
    scratch_mb: int = Field(ge=0)
    cpu_threads: int = Field(ge=1)
    gpu_memory_mb: int = Field(ge=0)
    network_egress: bool = False
    max_timeout_seconds: int = Field(ge=1)
    node_count: int = Field(ge=1)


__all__ = [
    "CATEGORIES",
    "CompositionMediaKind",
    "CompositionPlan",
    "ExpectedOutput",
    "ExternalInput",
    "LicenseLedger",
    "LicenseRecord",
    "MediaContract",
    "MediaPort",
    "PLAN_VERSION",
    "PlanNode",
    "PortBinding",
    "ResourceEstimate",
    "ResourceRequirements",
]
