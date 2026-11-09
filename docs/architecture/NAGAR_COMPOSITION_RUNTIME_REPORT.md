# NAGAR Creative Capability Runtime — Research & Architecture Report

**Status:** Prototype verified (13/13 tests pass, benchmark green, real FFmpeg/ImageMagick execution).
**Date:** 2026-11-09
**Scope:** `src/nexus_ai_agent/creative/composition/`

This report consolidates the research findings, architecture decisions, and the
verified prototype. Each section is tagged **Verified** (measured in this
session), **Evidence-Based** (from existing code/docs), or **Proposal**
(forward-looking, not yet built).

---

## 1. Reality & Gap Analysis (Verified)

The repository already ships a substantial creative substrate. The gap is
*composition*, not execution.

| Layer | Exists? | What it does | Gap it leaves |
|---|---|---|---|
| Studio command bus | ✅ | In-memory project-state mutations, 9-stage pipeline, `OperationSpec` | Media-type contracts, resource budgets, licence metadata |
| Packs + trust | ✅ | Capability manifests, ed25519 signatures, hardware reqs | No *composition* of packs into new capabilities |
| Creative spine | ✅ | Intent → operations → append-only graph with evidence | Maps to *registered* operations only; no type-safe DAG composition |
| Rendering lane IR | ✅ | Typed video/audio ops → deterministic FFmpeg | Single-domain (video); no cross-domain IR |
| Provenance passport | ✅ | Evidence-derived artifact identity from causal journal | No per-composition proof-carrying artifact |
| **Composition layer** | ❌ (built now) | — | — |

**Gap filled by this prototype:** a type-safe, resource-aware,
licence-aware composition compiler that produces a *new derived capability*
from independent components, executes it through real media workers, and
emits a proof-carrying artifact.

## 2. Build vs Extend vs Integrate Matrix

For each creative capability domain, the decision is Build / Extend / Integrate:

| Domain | Decision | Rationale | License |
|---|---|---|---|
| Image transform (resize, crop, color) | **Integrate** (ImageMagick) | Mature, deterministic, MIT-style | Apache 2.0 (ImageMagick 7) |
| Video encode/trim/mux | **Integrate** (FFmpeg) | Industry standard, deterministic with fixed params | LGPL/GPL (depends on build flags) |
| Audio DSP (loudnorm, duck) | **Integrate** (FFmpeg) | Same as video | LGPL/GPL |
| Non-destructive edit graph | **Extend** (existing Project IR) | The spine already has an append-only graph; extend with media-type edges | MIT (our code) |
| Cross-domain Creative IR | **Build** | No existing typed IR spans image/video/audio/AI; needed for composition | MIT (our code) |
| Capability Composition Compiler | **Build** | Core differentiator; no existing tool validates type+resource+licence+determinism contracts | MIT (our code) |
| Proof-Carrying Artifact | **Build** (extend provenance) | Builds on existing passport concepts; adds per-composition manifest | MIT (our code) |
| Local AI model execution | **Integrate** (ONNX Runtime / llama.cpp) | Avoid reimplementing inference; adapter layer | MIT/BSD |
| Character identity (face/voice) | **Build** (metrics layer) + **Integrate** (models) | Similarity metrics, drift control, and continuity rules are custom; the embedding model is integrated | Mixed (model licences vary) |
| Sandboxed execution | **Integrate** (Linux namespaces/cgroups / bubblewrap) | Kernel-provided isolation; building from scratch is unjustified | GPL (bubblewrap) |

**Principle:** Build only where ownership creates a durable advantage
(composition semantics, IR, proof, identity metrics). Integrate mature media
tooling. Extend the existing spine rather than replace it.

## 3. Technology & License Matrix (Evidence-Based)

| Technology | Purpose | License | Self-host? | Notes |
|---|---|---|---|---|
| Python | Composition layer, AI adapters | MIT (our code) | ✅ | 3.14 in this env; pydantic 2.13 |
| Rust | Future: high-performance runtime, sandbox wrapper | Apache 2.0 / MIT | ✅ | Not yet used in composition layer |
| TypeScript | Studio UI (existing) | MIT | ✅ | Existing `integrations/trigger` |
| FFmpeg | Video/audio | LGPL/GPL | ✅ | 6.1.1; GPL if built with x264/x265 |
| ImageMagick | Image transforms | Apache 2.0 | ✅ | 6.9.12 |
| ONNX Runtime | Local AI inference | MIT | ✅ | CPU/CUDA/DirectML/Vulkan |
| llama.cpp | Local LLM | MIT | ✅ | GGUF models |
| Linux namespaces | Sandbox | GPL (kernel) | ✅ | Native |
| bubblewrap | Sandbox wrapper | LGPL 2.1 | ✅ | Lightweight |

**Honesty note:** FFmpeg's effective licence depends on the build flags. A
build with `--enable-gpl` (x264, x265) makes the binary GPL. The composition
layer records the *tool version string* in the artifact but does not yet
parse the licence from it — this is an open item for the licence ledger.

## 4. Polyglot Architecture Proposal (Proposal)

| Layer | Language | Why |
|---|---|---|
| **Composition core** (spec, compiler, proof) | Python | Rapid iteration, pydantic contracts, existing codebase. Verified. |
| **Media workers** | Python (subprocess) + C (via FFmpeg/IM) | Subprocess isolation; no FFI complexity. Verified. |
| **High-throughput media** (future) | Rust | Zero-copy, SIMD, deterministic pixel ops. *Not yet built.* |
| **AI model adapters** | Python | Ecosystem (ONNX, torch, transformers). |
| **Studio UI** | TypeScript | Existing; rich UI ecosystem. |
| **Sandbox/isolation** | Rust or Go | Minimal, auditable; seccomp/namespace bindings. |

**Complexity control rule (from the brief):** a polyglot boundary is justified
only when it delivers a measurable advantage. The Rust/GPU media path is
deferred until the Python/subprocess path is proven insufficient.

## 5. Creative IR Design (Verified + Proposal)

The v1 IR is the composition graph itself (`CompositionGraph` +
`CapabilitySpec`). It is:

- **Typed:** every port carries a `MediaType` (kind + codec + container + properties).
- **Versioned:** `CapabilitySpec.version` is semver; the compiler checks
  snapshot compatibility.
- **Validatable:** the compiler refuses type/resource/licence/determinism
  violations before execution.
- **Provider-neutral:** the IR names capabilities, not providers; the worker
  binds a capability to a backend.
- **Compilable:** the derived spec is itself a `CapabilitySpec` (composition
  is closed).

**Proposal for v2:** a single canonical `CreativeIR` document format that
serializes the composition graph + the derived spec + the proof, aligned
with the in-toto/SLSA statement shape already used by the provenance
passport.

## 6. Capability Runtime Design (Verified)

The runtime executes a validated composition graph:

```
Intent/Graph → [Compiler: type+resource+licence+determinism] → DerivedSpec
             → [Policy gate: network, commercial, quota]
             → [Topological execution via MediaWorker (FFmpeg/IM)]
             → [ProofCarryingArtifact: hashes, manifest, tool versions, verification]
```

**Authority boundary (unchanged law):** the runtime is synchronous and
stateless; it creates no queue, bus, or commit authority. A later phase can
wrap it in the existing `InProcessJobQueue` authority without changing the
contract.

## 7. Cost & Hardware Analysis (Verified)

Measured on this host (4 vCPU, 7.7 GB RAM, 40 GB disk):

| Metric | Value |
|---|---|
| Compile time (3-op graph) | 0.12 ms |
| End-to-end execution (3-op) | ~965 ms (754 ms = FFmpeg encode) |
| Peak RSS | 31.85 MB |
| Output (2s 1280x720 h264) | 10,874 bytes |
| GPU used | None (CPU-only) |
| Network used | None |

**Zero Paid Dependency by Default:** the verified path uses only FFmpeg and
ImageMagick (both free, self-hosted). No API calls, no cloud, no per-use
fees. GPU, electricity, and storage are real costs but not paid to a third
party.

## 8. Unresolved Risks (Verified gaps)

1. **Licence self-declaration:** capability licences are declared in the
   spec, not cryptographically attested. A signed licence ledger is needed.
2. **Host-level resource limits:** the quota is per-composition; parallel
   compositions need cgroup-level limits.
3. **Clean worker environment:** workers inherit the parent env; secrets
   should be stripped before spawning.
4. **Golden-output determinism tests:** per-capability golden files would
   catch a capability that declares `deterministic=True` but isn't.
5. **More capability backends:** only FFmpeg + ImageMagick ops are
   implemented. AI model adapters (ONNX), GEGL, and GStreamer are deferred.
6. **Cross-domain IR:** v1 IR is media-type-typed but not a single
   serialized document format.

## 9. NOW / NEXT / LATER Roadmap

### NOW (done, verified)
- ✅ `CapabilitySpec` contract with media types, resources, licence, verification.
- ✅ `CompositionCompiler` with type/resource/licence/determinism validation.
- ✅ FFmpeg + ImageMagick workers for 7 deterministic ops.
- ✅ `CompositionRuntime` with policy gate + proof-carrying artifacts.
- ✅ 13 tests (execution, invalid input, policy, contract, reproducibility, tamper, traversal).
- ✅ Benchmark script with real measurements.

### NEXT (next wave)
- Add ONNX Runtime worker for local AI ops (segmentation, depth).
- Implement a signed licence ledger (extend pack trust root).
- Add cgroup-based per-host resource limits.
- Serialize the composition graph + proof into a canonical `CreativeIR` JSON.
- Wrap the runtime in the existing job-queue authority (no new queue).

### LATER (beyond prototype)
- Rust high-throughput media path (zero-copy pixel ops, SIMD).
- Character Capsule with face/voice similarity metrics and drift control.
- Multi-node distributed execution (WASM/WASI workers for untrusted code).
- Real RAG for capability discovery (find the right composition for an intent).

---

## Evidence Bundle

- Source: `src/nexus_ai_agent/creative/composition/{spec,compiler,workers,runtime,proof,bench}.py`
- Tests: `tests/unit/test_composition_runtime.py` (13 tests, all pass)
- Benchmark: `scripts/bench_composition.py`
- ADR: `docs/architecture/adr/0013-creative-capability-composition.md`
- Threat model: `docs/architecture/COMPOSITION_THREAT_MODEL.md`
- Install/run: `docs/ops/COMPOSITION_RUNTIME.md`
