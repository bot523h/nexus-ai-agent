"""NAGAR Creative Capability Composition Runtime.

The composition layer is the *composition* half of the creative runtime.  It
adds exactly one thing to NEXUS: the ability to **compose** capabilities that
already exist into one typed, executable plan.

What it deliberately does **not** own (the authority law, ADR 0014):

| Concern | Owner |
|---|---|
| Capability registry | ``creative/packs/runtime.py::build_runtime_registry`` |
| Pack trust / activation | ``creative/packs/registry.py`` + ``creative/packs/trust.py`` |
| Operation allow-list, permission ladder | ``creative/studio/capabilities.py`` |
| Project authorization | ``creative/studio/authorization.py`` |
| Capability lifecycle gate | ``creative/studio/lifecycle.py`` |
| Command dispatch, idempotency | ``creative/studio/bus.py`` |
| Job queue, leases, fencing | ``adapters/in_process_job_queue.py`` |
| Worker registry | ``worker.py::default_job_handlers`` |
| Media execution (FFmpeg allow-list) | ``creative/slideshow/ffmpeg.py`` |
| Artifact verification | ``jobs/verification.py`` + the registered verifier |
| Provenance / passport | ``provenance/passport.py`` |

This package owns the composition logic and nothing else:

* :mod:`contracts` -- the typed, fingerprinted composition IR and the licence
  ledger;
* :mod:`compiler` -- the DAG compiler, which resolves every node against the
  **live** registry and refuses an unknown, mistyped, unbound or cyclic plan;
* :mod:`engine` -- the media realizations (one per canonical operation), run
  through the canonical FFmpeg allow-list;
* :mod:`executor` -- the execution of a compiled plan through the canonical
  command bus;
* :mod:`proof` -- the proof-carrying artifact and the **independent**
  semantic verification;
* :mod:`job` -- the canonical queue adapter (``composition_render_job``).

There is no capability dictionary in this package: a plan node names an
operation id, and the compiler resolves it in the registry the command bus
authorizes against.  A composition therefore cannot authorize anything.
"""

from __future__ import annotations

from .compiler import (
    CompiledPlan,
    CompositionCompiler,
    CompositionError,
    registry_fingerprint,
)
from .contracts import (
    CompositionPlan,
    ExpectedOutput,
    ExternalInput,
    LicenseLedger,
    LicenseRecord,
    PlanNode,
    PortBinding,
    ResourceEstimate,
    ResourceRequirements,
)
from .engine import MediaEngine, MediaEngineError, MediaOutcome
from .executor import CompositionExecutionError, CompositionExecutor, ExecutionResult
from .job import (
    COMPOSITION_JOB_TYPE,
    CompositionJobError,
    CompositionJobPayload,
    composition_render_job,
    plan_from_dict,
    plan_to_dict,
)
from .license import (
    CATEGORIES,
    ffmpeg_build_facts,
    ffmpeg_ledger,
    ledger_report,
)
from .proof import (
    IntegrityReport,
    NodeReceipt,
    ProofCarryingArtifact,
    SemanticReport,
    verify_image_semantics,
    verify_integrity,
    verify_media_semantics,
    verify_video_semantics,
)
from .verifier import composition_verifier

__all__ = [
    "CATEGORIES",
    "COMPOSITION_JOB_TYPE",
    "CompositionCompiler",
    "CompositionError",
    "CompositionExecutionError",
    "CompositionExecutor",
    "CompositionJobError",
    "CompositionJobPayload",
    "CompositionPlan",
    "CompiledPlan",
    "ExecutionResult",
    "ExpectedOutput",
    "ExternalInput",
    "IntegrityReport",
    "LicenseLedger",
    "LicenseRecord",
    "MediaEngine",
    "MediaEngineError",
    "MediaOutcome",
    "NodeReceipt",
    "PlanNode",
    "PortBinding",
    "ProofCarryingArtifact",
    "ResourceEstimate",
    "ResourceRequirements",
    "SemanticReport",
    "composition_render_job",
    "composition_verifier",
    "ffmpeg_build_facts",
    "ffmpeg_ledger",
    "ledger_report",
    "plan_from_dict",
    "plan_to_dict",
    "registry_fingerprint",
    "verify_image_semantics",
    "verify_integrity",
    "verify_media_semantics",
    "verify_video_semantics",
]
