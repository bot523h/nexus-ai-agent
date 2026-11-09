"""NAGAR Creative Capability Composition Layer.

This module is the *composition* half of the creative runtime. It does not
replace the studio command bus (which owns in-memory project-state mutations)
or the durable job queue (which owns execution authority). It adds one thing
the existing layers do not have: the ability to take several independently
declared media capabilities, validate that they *can* be composed (types,
resources, licences, determinism), and produce a *new* derived capability
contract plus a real, proof-carrying execution of it.

Modules:

* :mod:`spec`      -- the unified :class:`CapabilitySpec` contract.
* :mod:`compiler`  -- :class:`CompositionCompiler` (DAG validation + derived spec).
* :mod:`workers`   -- media backends (FFmpeg, ImageMagick) used by the runtime.
* :mod:`runtime`   -- :class:`CompositionRuntime` executes a validated graph.
* :mod:`proof`     -- :class:`ProofCarryingArtifact` (hashes, manifest, verification).
"""

from .compiler import (
    CompositionCompiler,
    CompositionError,
    CompositionGraph,
    CompositionNode,
    InputBinding,
)
from .proof import (
    NodeReceipt,
    ProofCarryingArtifact,
    VerificationReport,
    sha256_file,
    verify_proof,
)
from .runtime import CompositionRuntime, ExecutionResult, RuntimeError, RuntimePolicy
from .spec import (
    CapabilityInput,
    CapabilityOutput,
    CapabilitySpec,
    LicenseInfo,
    MediaType,
    ResourceRequirements,
    VerificationContract,
)
from .workers import (
    BuiltinMediaWorker,
    MediaWorker,
    ToolVersions,
    WorkerError,
    detect_tool_versions,
)

__all__ = [
    "BuiltinMediaWorker",
    "CapabilityInput",
    "CapabilityOutput",
    "CapabilitySpec",
    "CompositionCompiler",
    "CompositionError",
    "CompositionGraph",
    "CompositionNode",
    "CompositionRuntime",
    "ExecutionResult",
    "InputBinding",
    "LicenseInfo",
    "MediaType",
    "MediaWorker",
    "NodeReceipt",
    "ProofCarryingArtifact",
    "ResourceRequirements",
    "RuntimeError",
    "RuntimePolicy",
    "ToolVersions",
    "VerificationContract",
    "VerificationReport",
    "WorkerError",
    "detect_tool_versions",
    "sha256_file",
    "verify_proof",
]
