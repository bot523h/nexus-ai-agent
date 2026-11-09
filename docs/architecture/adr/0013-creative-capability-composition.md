---
status: proposed
date: 2026-11-09
deciders: NAGAR Creative Runtime research session
consulted: docs/architecture/COMMAND_CAPABILITY_CONTRACT.md, creative/spine/, creative/studio/capabilities.py, creative/packs/registry.py
informed: creative/composition/
---

# 0013. NAGAR Creative Capability Composition Layer

## Context and Problem Statement

The repository already ships a rich creative execution substrate:

* the **studio command bus** (`creative/studio/`) owns in-memory project-state
  mutations through a 9-stage pipeline and a typed `OperationSpec` contract;
* the **packs** (`creative/packs/`) declare capabilities with hardware
  requirements, resource budgets, network policy, and ed25519 trust
  signatures;
* the **creative spine** (`creative/spine/`) maps an `Intent` to registry
  operations and records artifacts + evidence in an append-only graph;
* the **rendering lane** (`creative/rendering/ir.py`) compiles typed video/audio
  ops to a deterministic FFmpeg invocation;
* the **provenance passport** (`provenance/passport.py`) projects
  evidence-derived artifact identity from the causal journal.

What is *missing* is the ability to take several independently-declared media
capabilities and compose them into a **new derived capability** -- not a
pre-registered workflow, but a graph whose type compatibility, resource
budget, licence compatibility, and determinism chain are validated at compile
time, then executed for real with a proof-carrying output.

The NAGAR thesis is that the value is in *building capabilities from
components*, not in gluing tools. This ADR adds the composition layer that
proves that thesis.

## Decision Drivers

- Composition must be **closed**: the output of the compiler is itself a
  `CapabilitySpec`, so compositions can be composed again.
- Composition must be **fail-closed**: type mismatch, cycle, missing binding,
  quota overrun, or licence conflict all refuse before any worker runs.
- Execution must be **real and deterministic**: FFmpeg + ImageMagick, not
  mocks; same inputs + params yield the same output hash.
- No **parallel authority**: no new command bus, queue, or commit authority.
  The layer executes synchronously over a scratch directory and integrates
  with the existing provenance concepts.
- **Proof-carrying**: every output carries input hashes, operation manifest,
  tool versions, output hash, and an independently-reverified verification
  report.

## Considered Options

1. Extend the studio `OperationSpec` to carry media types and compose through
   the command bus.
2. Build a separate composition layer that reuses the spine's graph +
   provenance ideas but executes media workers directly.
3. Depend on an external workflow engine (Temporal, Prefect, Argo) for
   composition semantics.

## Decision Outcome

Chosen option: **2. A separate composition layer under
`creative/composition/`**, because:

* The studio bus is scoped to in-memory project state with pure handlers;
  media processing (FFmpeg subprocesses) is a different domain and forcing it
  through the bus would conflate state mutation with media execution.
* Reusing the spine's typed-graph + evidence vocabulary keeps the system
  coherent without duplicating the bus or the queue.
* An external workflow engine would add a network/dependency surface and a
  second authority, violating the "no parallel authority" law.

### Consequences

- (+) New capabilities are derived from components, not registered by hand.
- (+) Type/resource/licence/determinism contracts are validated before
  execution.
- (+) Every output is independently re-verifiable (tamper-evident).
- (−) The builtin worker covers a small set of ops; more capability backends
  (model adapters, GEGL, GStreamer) must be added to scale.
- (~) Composition is synchronous in v1; a later phase can wrap it in the
  existing job queue authority without changing the contract.

## Confirmation

* `tests/unit/test_composition_runtime.py` (13 tests) covers: 3-op real
  execution, type-mismatch/cycle/missing-binding/quota refusal, network and
  non-commercial policy refusal, contract conformance, byte-for-byte
  reproducibility, tamper detection, and path-traversal guard.
* `scripts/bench_composition.py` reports real compile/execution/per-node
  timing, peak RSS, output size, and verification status.
* `python -m unittest tests.unit.test_composition_runtime -v` must pass.

## Pros and Cons of the Options

### Option 1 (extend studio bus)

- (+) Reuses the existing 9-stage pipeline and registry.
- (−) Conflates in-memory state mutation with media subprocess execution; the
  bus's pure-handler guarantee would break.

### Option 2 (separate composition layer) [chosen]

- (+) Clean domain separation; composition is closed and fail-closed.
- (+) Real deterministic execution + proof-carrying artifacts.
- (−) A new module surface to maintain.

### Option 3 (external workflow engine)

- (+) Mature scheduling/retry semantics.
- (−) Adds network/dependency surface and a second execution authority.

## More Information

* `creative/composition/spec.py` -- `CapabilitySpec` contract.
* `creative/composition/compiler.py` -- `CompositionCompiler` (DAG validation).
* `creative/composition/runtime.py` -- `CompositionRuntime` (execution + proof).
* `creative/composition/workers.py` -- FFmpeg + ImageMagick backends.
* `creative/composition/proof.py` -- `ProofCarryingArtifact`.
* ADR 0005 (canonical command-capability contract) -- the studio contract this
  layer complements, not replaces.
