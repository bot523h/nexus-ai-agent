# ADR 0014: The Composition Compiler Owns DAG Logic Only — Authority Stays Canonical

- Status: accepted
- Date: 2026-11-09
- Deciders: NAGAR Creative Capability Composition Runtime integration (branch `arena/da6837a6-nexus-ai-agent`)
- Consulted: `AGENTS.md` multi-agent contract; `.agents/board.json` zone `nagar-creative-composition`
- Informs: `CANONICAL_INFRASTRUCTURE_AUDIT.md`, `docs/capability_composition_contract.md`, `docs/composition_ir.md`, `docs/canonical_integration_guide.md`, `docs/threat_model_composition.md`

## Context and Problem Statement

NEXUS already owns a **canonical capability runtime**:

* a single `CapabilityRegistry` assembled by `creative/packs/runtime.py::build_runtime_registry()`;
* a single `CommandBus` (`creative/studio/bus.py`) that runs the seven-stage
  validate → authorize → apply pipeline;
* a single authority (`creative/studio/authorization.py::ProjectAccess`) and a single
  permission ladder (`creative/studio/capabilities.py::check_permission`);
* a single pack lifecycle gate (`creative/studio/lifecycle.py::PACK_LIFECYCLE`);
* a single job queue (`adapters/in_process_job_queue.py::InProcessJobQueue`) with a
  single worker registry (`worker.py::default_job_handlers()`) and a single verifier
  registry (`adapters/in_process_job_queue.py::default_artifact_verifiers()`);
* a single media lane (`creative/slideshow/ffmpeg.py`) and a single passport authority
  (`provenance/passport.py::PassportBuilder`).

What it did **not** own was the ability to *compose* capabilities: express "resize this
image, make it grayscale, encode it as a clip" as one typed plan and have that plan run
through the runtime that already exists.

The obvious way to add that is to give the composition layer its own registry, its own
queue and its own process spawner. That is exactly what the abandoned prototype on
`origin/trae/agent-CCO4Fr` (`b7dd454e6f46f199276b710ccd605801f2b1a47a`) did, and it
is precisely the failure mode this ADR exists to prevent: a *parallel* capability
registry that can "know" capabilities the authoritative registry does not, a job type
that is never registered with the worker, `asyncio.to_thread` around a subprocess
instead of the canonical chain, and a private policy model that competes with the
authority.

The runtime registry is at **57** operations at the base SHA `5ed6d4e1`; this ADR adds a
seventh builtin pack (`nexus.media.transform`, 3 operations) and brings the surface to
**60**.

## Decision Drivers

- **One authority.** A composition must not be able to authorize anything the runtime
  would refuse, nor authorize anything the runtime cannot see.
- **Fail closed before dispatch.** Unknown capability, untrusted capability, missing
  permission, untrusted pack, invalid schema, missing required input, incompatible
  licence, exceeded quota, forbidden network and invalid artifact reference must all be
  refused *before* a process is started.
- **Reproducibility.** A plan must be pinned to the capability version, the operation
  schema version and the registry fingerprint it was compiled against, so it can never
  silently execute against a different capability.
- **Honest evidence.** Integrity (re-hash) and semantics (decode, pixels, frame count)
  are different claims and must be different mechanisms. Re-hashing is not semantic
  verification.
- **No second truth.** The registry, the queue, the authority, the command bus, the
  provenance authority and the verification authority all stay where they are.
- **Measurable.** The integration must be provable by a real end-to-end run through the
  canonical chain, not by a script that calls an executor directly.

## Considered Options

1. **Option 1: A parallel composition runtime** (private registry, private queue,
   private process spawner). *Rejected*: it is the prototype's defect; it creates a
   second source of truth and a second authority.
2. **Option 2: A "composition" that is only a convenience wrapper around one existing
   operation.** *Rejected*: it adds no composition semantics; it cannot express a dataflow.
3. **Option 3: The composition compiler owns DAG logic and validation only; everything
   else stays canonical (Chosen).**
4. **Option 4: Move the capability registry itself into the composition layer.**
   *Rejected*: it inverts the architecture; the registry is the authority, not a
   component of the new feature.
5. **Option 5: Express a composition as a synthetic operation in the registry.**
   *Rejected*: the registry describes *capabilities*, not control flow; a synthetic
   operation would need a dynamic dispatch table — the very thing that becomes a
   shadow registry.

## Evaluation Matrix

| Evaluation Criteria | Weight | Option 1 (parallel) | Option 2 (wrapper) | Option 3 (chosen) | Option 4 | Option 5 |
|---|---:|---:|---:|---:|---:|---:|
| Single authority preserved | 30 | 0 | 10 | 30 | 0 | 5 |
| Fail-closed before dispatch | 20 | 3 | 8 | 20 | 5 | 10 |
| Reproducibility / version pinning | 15 | 3 | 5 | 15 | 12 | 8 |
| Honest evidence (integrity ≠ semantics) | 15 | 3 | 6 | 15 | 10 | 8 |
| Provable through the canonical chain | 10 | 0 | 4 | 10 | 6 | 6 |
| Maintenance cost (no new substrate) | 10 | 1 | 6 | 10 | 2 | 3 |
| **Total (100)** | **100** | **10** | **39** | **100** | **35** | **40** |

## Decision Outcome

Chosen option: **Option 3**.

### The authority law

> The Composition Compiler owns the DAG logic and the validation of that DAG.
> It owns **no** authorization, **no** execution authority, **no** job queue,
> **no** command bus, **no** canonical capability registry, **no** pack trust,
> **no** artifact passport, **no** provenance authority and **no** final
> verification authority.

Concretely, `src/nexus_ai_agent/creative/composition/` may:

* read the live registry (`build_runtime_registry()`) and resolve a node's
  `capability_id`, `capability_version` and `operation_schema_version` **from it**;
* validate a plan's topology, types, parameters, bindings and resource estimate;
* compute a stable fingerprint over the plan **and** the registry fingerprint;
* describe *how* a node is realized (which canonical primitive produces the bytes).

and may **not**:

* define, cache or shadow a capability table (`dict[str, CapabilitySpec]`) — there is
  none in the package, and `tests/architecture/test_composition_boundary.py` asserts it;
* authorize an actor, a project or a permission;
* enqueue work on anything but the canonical queue;
* dispatch anything but a canonical `TypedCommand` on the canonical `CommandBus`;
* spawn a binary that is not on the canonical allow-list;
* write a passport or claim provenance authority;
* declare a job "verified" without the canonical verifier.

### The canonical binding of a node

A plan node is **not** a free-form operation call. It carries four pinned facts that
the compiler checks against the **live** registry:

| Field | Resolved from | Failure |
|---|---|---|
| `operation` | `registry.get_spec(operation)` | `unknown_operation` |
| `capability_id` | `registry.describe(operation).capability_id` | `capability_version_drift` |
| `capability_version` | `registry.describe(operation).version` | `capability_version_drift` |
| `operation_schema_version` | `registry.get_spec(operation).schema_version` | `schema_version_drift` |

plus the plan-level `registry_fingerprint`, whose mismatch with the live registry is
`registry_drift`. A plan therefore cannot be replayed against a capability that moved
underneath it.

### The new capability path

`resize`, `grayscale` and `image_to_video` are **not** special-cased in the runtime.
They are a normal builtin pack:

* `src/nexus_ai_agent/creative/packs/media/operations.py` — the three operations, as
  **pure state recorders** (the pack substrate is data-only);
* `src/nexus_ai_agent/creative/packs/media/pack.manifest.json` — the data-only
  manifest (`nexus.capability-pack.v1`), declaring `external_binaries: ["ffmpeg"]`
  and `runtime_network: false`;
* `creative/packs/runtime.py::COMPOSITION` — `PackComposition("media",
  "nexus.media.transform", …)` plus `_register_media`;
* `creative/studio/lifecycle.py::PACK_LIFECYCLE` — `nexus.media.transform` →
  `AVAILABLE` (fail-closed for an unknown pack);
* `tests/architecture/test_pack_activation_completeness.py::BUILDERS` — the pack's
  builder is declared, so the activation-completeness gate covers it.

The **media realization** (the FFmpeg call) lives *outside* the packs tree, in
`creative/composition/engine.py`, because the pack substrate is data-only by
architecture rule. It is the same split the repository already uses for
`slideshow.upscale`: `packs/slideshow/operations.py` records the state,
`creative/slideshow/upscale.py` produces the media.

### The canonical execution path

```
plan → compile (live registry)
     → canonical queue row (job_type = "creative_composition")
     → worker.py::default_job_handlers()["creative_composition"]
     → creative/composition/job.py::composition_render_job
          → trusted_workspace() / guarded_input()      (canonical root + prefix)
          → re-compile against the live registry        (a stale plan is refused)
          → CompositionExecutor
               → CommandBus.dispatch(slideshow.scan_assets)   (canonical registration)
               → MediaEngine.realize(...)                    (canonical FFmpeg)
               → verify_media_semantics(...)                (independent decode)
               → CommandBus.dispatch(node.operation, ...)     (canonical op, per node)
     → queue verification stage
          → default_artifact_verifiers()["creative_composition"]
               → jobs/verification.py::verify_artifact   (integrity)
               → proof.py::verify_*_semantics           (semantics)
     → provenance/passport.py::PassportBuilder.build     (existing passport authority)
```

A composition is therefore **not** a privileged path: it is the ordinary path, with a
compiled plan in front of it.

### Consequential decisions

* **The bus factory.** `creative/render_jobs.py::build_job_bus(project, *,
  operation=None, authorizer=None)` is pinned by
  `tests/architecture/test_lifecycle_gate_boundary.py` and is the only sanctioned bus
  factory for workers. The composition executor builds its bus over the **same live
  registry** and the same `ProjectAccess` shape, and a caller with a real project and a
  real principal injects its own bus instead.
* **The verifier set is exact.** `tests/architecture/test_verification_registry_ratchet.py`
  asserts that `default_artifact_verifiers()` contains *exactly*
  `{creative_render, slideshow_render, story, pdf_extract}`. Adding a job type without
  a verifier would fail that gate, so `creative_composition` was added to both the
  gate and the registry.
* **The generic passport, not the trim passport.** `jobs/creative_passport.py::build_passport`
  is the durable-evidence passport for the `timeline.trim` creative request only
  (`in_process_job_queue.py::is_timeline_trim_request`). A composition row is not a
  trim request, so it uses the **generic** `provenance/passport.py::PassportBuilder`,
  which reads the causal journal and the authoritative row and re-measures the
  artifact. The composition layer writes no passport of its own.
* **Level C in a composition.** `media.image_to_video` is `PermissionLevel.CONFIRMATION`
  (C). The worker dispatches with the **requester's own** `confirmed` flag, a field of
  `CompositionJobPayload` that defaults to `False`; the worker never fabricates it, and
  an unconfirmed level-C node is refused by `CompositionExecutor._authorize_node`
  *before* any media work. `allow_experimental` remains a server-side decision and is
  never read from a payload.
* **The requester is named.** `CompositionJobPayload.actor: ActorClaim` (`kind`,
  `actor_id`) is **required**: the worker binds the canonical grant to that principal
  instead of executing the row under a self-granted service identity.
* **Authorization moves one step earlier, not one layer sideways.** A refused node must
  not leave an encoded file behind, but re-implementing the gates in the composition
  package would be a second authority. The bus therefore exposes
  `CommandBus.check_authorization(command)`, which runs the *same* private helpers the
  dispatch runs (`_spec_locked` for the envelope/schema pin, `_authorize_locked` for the
  actor grant, the capability check, the pack lifecycle gate and the A/B/C/D ladder) and
  stops before reference resolution, reservation and the handler. The composition
  executor calls it before the encoder; the pack gate is still invoked from `bus.py`
  only, which `tests/architecture/test_lifecycle_gate_boundary.py` enforces unchanged.
  The input model is deliberately **not** validated there: the canonical operations take
  the produced artifact's hash as input, and that hash only exists after the encode.

### Negative consequences

* A composition is **not** a capability. It cannot be registered, activated, activated
  on demand or signed. It is a *plan over* capabilities. This is deliberate: the pack
  substrate stays data-only and the trust root stays meaningful.
* A composition's plan is **not** replayable across a registry change. The registry
  fingerprint is part of the plan fingerprint, so a drifted plan is refused
  (`registry_drift`) rather than re-interpreted.
* The composition layer adds ~1,400 lines of code to a repository that already owns
  the runtime. The alternative — a parallel runtime — is smaller but wrong; the cost of
  Option 3 is paid once, in the canonical substrate, instead of twice.

## Confirmation

Compliance is validated by **named, running tests**, not by review prose:

| Claim | Enforced by |
|---|---|
| The composition package holds no capability registry | `tests/architecture/test_composition_boundary.py::test_composition_package_has_no_capability_table` |
| The composition package holds no queue, bus or authority of its own | `tests/architecture/test_composition_boundary.py::test_composition_package_owns_no_parallel_runtime` |
| The composition job type is registered in the canonical worker map | `tests/architecture/test_composition_boundary.py::test_composition_job_type_is_registered_in_the_canonical_worker_map` |
| The composition job type has a registered verifier, and the ratchet knows it | `tests/architecture/test_verification_registry_ratchet.py::test_artifact_verifier_registry_is_exactly_the_expected_set` |
| The new pack is activated through the canonical pack path | `tests/architecture/test_pack_activation_completeness.py` (all six gates) |
| A plan is refused when the registry drifted | `tests/unit/test_composition_compiler.py::test_registry_drift_is_refused` |
| The composition runs end to end through the canonical queue | `tests/integration/test_composition_e2e.py` |

## More Information

* `CANONICAL_INFRASTRUCTURE_AUDIT.md` — the eight-axis audit this decision is based on.
* `docs/capability_composition_contract.md` — the capability contract and the media contract.
* `docs/composition_ir.md` — the composition IR and the fingerprint rules.
* `docs/canonical_integration_guide.md` — how to submit a composition through the canonical chain.
* `docs/threat_model_composition.md` — the threat model and the enforced controls.
* `docs/architecture/adr/0005-canonical-command-capability-contract.md` — the command/capability contract this ADR binds to.
* `docs/architecture/adr/0006-capability-pack-trust-root.md` — the pack trust root the new pack goes through.
