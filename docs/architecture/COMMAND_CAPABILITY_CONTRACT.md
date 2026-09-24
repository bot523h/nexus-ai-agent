# Nagar Command and Capability Contract

**Scope:** Gate 2, the existing `creative/studio/` command bus and registry, not a new renderer or a UI. **Baseline:** `035a896dd2ed1293de6accf2ef4309da2fd64c89` (2026-09-24). This page describes the security contract; the separate Creative Runtime work in PR#67 is **open, not on this baseline**. Its `studio/bus.py` lifecycle check is a prospective integration, not evidence on this HEAD.

## Truth freeze and reason for the pipeline change

| Concern | Evidence on baseline | Baseline level | Gap / decision |
|---|---|---|---|
| Bus | `creative/studio/bus.py::CommandBus.dispatch` and `test_nagar_wave1_green_cockpit.py` | L2 (CLI/worker calls it; pure handlers apply) | Previously replayed idempotency **before** authorization, schema, references and payload comparison. Move replay after security gates, reserve before apply. |
| Envelope | `studio/models.py::TypedCommand`, `protocol_version=nagar.command.v1`, `target.project_id` optional | L2 | Keep the existing target, preconditions and protocol name; require actor, project target, provenance; add an explicit envelope schema version and typed policy/refs/context/trace/snapshot. No DB migration. |
| Registry | `studio/capabilities.py::CapabilityRegistry`, `OperationSpec`, `packs/runtime.py::build_runtime_registry`, `packs/manifest.py` | L2 (code allow-list); runtime availability L0 on baseline | Keep the one registry. Expose version/schema/provider/permissions/modes/availability, check against **installed** records, never the caller's snapshot. PR#67 adds a separate pack-lifecycle gate only if integrated. |
| Authorization | `bot/access_guard.py` is at the Telegram edge; `studio/bus.py` has only the A/B/C/D ladder | L0 at bus | Inject a trusted, project-scoped authorizer into the bus; no authorization from envelope claims or implicit anonymous principal. Worker/CLI must bind a trusted service actor; a future HTTP adapter must bind its authenticated principal and project access. |
| Semantic references | `studio/references.py::ReferenceResolver.resolve` pins time expressions; no asset reference check | time L2, assets L0 | Extend **that resolver** to validate typed input_refs against the project registry/timeline, not a second resolver. No URL, path or external scheme in an input_ref. |
| Idempotency | per-bus `dict[str, CommandResult]`; `adapters/in_process_job_queue.py` unique key returns old id without comparing payload | bus L1, queue L3 but conflicting payload is silently ignored | Scope bus keys by `(project_id, operation, idempotency_key)` and compare a canonical logical payload. Queue conflict check uses its existing payload column; no new table/migration. In-memory bus reservations do **not** claim cross-process durability. |
| Execution policy | `PermissionLevel` A/B/C/D, `confirmed`, revision/hash preconditions | L2 | Keep these checks, add declared execution mode (local only by default). Validate policy before references, reservation or handler. |
| API/worker | `creative/render_jobs.py::_dispatch` constructs a fresh Bus per render job; `bot/creative_surface.py` enqueues via `JobQueuePort` | L2; rendered one-shot L3 separately | Gate 2 changes the typed dispatch, not a new endpoint or rendering implementation. Queue admission is a distinct durable boundary. |
| Architecture | `test_nagar_studio_isolation.py`, `test_slideshow_adapter_boundary.py` | L1 | Pin the **actual** import/dispatch seams. Existing generic agent shell tool is outside Nagar and is not evidence that a Nagar command can run a shell. |

L0 = design only; L1 = code + unit/contract evidence; L2 = reachable through a real entry point; L3 = operational with real execution; L4 = production-like. Baseline levels are bounded to the evidence listed, not inferred from file names.

**Why change ordering?** The old bus returned a cached result for *any* matching key, even an unauthorized actor, another operation, or a different payload. It also ran the permission gate before operation schema validation. A retry with stale optimistic preconditions should replay only **after** actor/project/capability/policy/ref gates and fingerprint comparison; it must not run a second handler. Parsing and schema validation do no I/O or side effects. This reordering may intentionally turn previously silent duplicate/conflicting requests into typed refusals. The handler stays the existing pure `OperationSpec.handler`; all writes still commit atomically in the Bus.

## Pipeline and boundaries

`Parse → envelope + operation schema validation → actor/project authorization → capability/version/operation-permission check → execution policy (including A/B/C/D confirmation and allowed mode) → input reference validation/pinning → idempotency reservation → optimistic preconditions → execution request to the registered pure handler → atomic state/result commit.`

A duplicate never submits a second execution request. Revalidation on retries may reject a caller who has since lost authorization/capability; this is intentional revocation, not a replay. No client-supplied snapshot grants a capability or project access. Temporal expressions are pinned through `ReferenceResolver` just before reservation. The Bus revalidates its initial `Project` snapshot to recompute its derived state hash (Pydantic `model_copy(update=...)` can bypass validators), so forged/stale hashes do not pass preconditions. Project identity is the existing `target.project_id`; the trusted authorizer independently binds the actor and project and **must** be supplied by a composition root.

## Contract, versioning, and limits

The existing `protocol_version=nagar.command.v1` remains the pack-compatibility family. `schema_version=2` denotes the hardened envelope; the prior incomplete form is not accepted by the bus without its required fields. Operation input models keep their independently declared version (currently 1); an unknown envelope or operation schema version is refused, not guessed. No database/schema migration is involved. Serialization is JSON-only (`model_dump_json` / `model_validate_json`); operation inputs are validated by the Pydantic model registered for the operation. Extra envelope or input fields are forbidden, including any request for raw shell execution. The executor still receives an `OperationContext` only from the Bus; no new operation, FFmpeg, UI or filesystem primitive is introduced.

`actor` is a **claimed** identity, not proof of authentication. The bus compares it with a trusted project-scoped authorizer injected by the caller's composition root; absent authorizer, missing actor/project, mismatched identity/project, or missing required permissions fail closed. The registry advertises each capability's id, semantic version, operation(s), JSON input schema and its version, availability, allowed execution modes, effective permissions, and provider/required packs. A supplied `capability_snapshot` is an advisory compatibility hint and must match the authoritative registry; it can never declare a new operation or override an unavailable capability.

`input_refs` support asset, clip and timeline **identifiers only**, scoped to the same project (at most 512 refs, covering the slideshow's 500-image limit and its soundtrack). The existing resolver checks *every* declared ref's type, shape/length/metadata, project match, existence in `Project.assets` or `Project.timeline`, and optional media/hash assertions. Worker and local slideshow adapters declare existing source assets in the command; scan/enrollment probes files before they become project assets and cannot cite them as already-owned refs. Absolute paths, traversal, encoded traversal, URLs and external schemes are not references. The project's in-memory asset registry proves *logical membership*, not that a media file exists on disk: staging/path containment and bytes are adapter responsibilities (`creative/render_jobs.py`, `creative/slideshow/`). `request_context`, `provenance` and `trace_id` are bounded metadata for audit, not authorization inputs.

`(project_id, operation, idempotency_key)` uniquely identifies a logical bus request. A matching fingerprint returns a copy of the original `CommandResult` (transaction id, revision, hash and output), including when the live project has advanced. A changed logical payload under the same key raises a conflict; `command_id` and `trace_id` alone may change on retry, but do not alter the logical payload. With no explicit key, `command_id` is the fallback key. Reservation and apply are serialized within the bus. This store is **in memory and per bus**: it does not claim durability across a restart or guarantee exactly-once external rendering. The existing SQLite queue provides a durable job id for a redelivered surface request; its own key is global. It compares canonical JSON payload and job type on collisions (including JSON boolean-vs-number types) and schedules a job only when its row was first inserted: a duplicate in another live adapter gets the original id without claiming the in-flight row. Explicit `resume_pending` remains crash recovery, not proof of exactly-once execution after a process crash. Queue jobs and FFmpeg are not run by the pure Bus. A real API endpoint, persistent project ownership source, and cross-worker reservation adapter are **not** introduced in this gate.

**Enforcers:** `tests/unit/test_command_capability_contract.py`, `tests/unit/test_nagar_wave1_green_cockpit.py`, `tests/integration/test_in_process_job_queue.py`, `tests/architecture/test_command_capability_boundary.py`, `tests/architecture/test_nagar_studio_isolation.py`, `tests/unit/test_docs_integrity.py`. See the boundary register in [`MODULE_MAP.md`](MODULE_MAP.md) §3. Check the actual branch and CI before claiming a level above L2 for the new contract.

## Gate 2 Truth Matrix (2026-09-24, this branch before CI)

These levels describe **the new contract**, not the whole product or an
unrelated render feature. A level is earned only by the evidence in its row.

| Level | Gate 2 slice and proof | Status / limit |
|---|---|---|
| L0 — design only | AI-originated Nagar command submission from a live agent; persistent multi-user project ownership adapter; cross-worker Bus reservation | **NOT VERIFIED**: no such Nagar AI adapter, ownership database or persistent reservation was added. Existing generic agent tools are a different path. |
| L1 — implemented, tested | Versioned/JSON envelope, installed capability descriptor/version, strict schema and permission gate, reject-before-handler negative cases (including forged project/actor, missing/wrong refs, URL/traversal and changed replay), architecture ratchet R13 | **CLOSED within in-memory Bus boundary**: `tests/unit/test_command_capability_contract.py`, `tests/architecture/test_command_capability_boundary.py`; source review of `studio/{bus,models,capabilities,references,authorization}.py`. This is not an authenticated network API. |
| L2 — wired to existing entry points | Local slideshow planning/upscale and the Telegram creative worker construct the new envelope with an explicit service grant, pass source refs and dispatch via Bus; SQLite queue rejects changed payloads after adapter restart/across instances and does not schedule a second live owner on a same-payload replay | **CLOSED for those entry points**: `tests/unit/test_slideshow_{engine,render,upscale}.py`, `tests/unit/test_creative_render_jobs.py`, `tests/integration/test_in_process_job_queue.py`; AST R13 checks the constructor/dispatch seams. Service actors own ephemeral per-job projects, not human/project ACLs. |
| L3 — real execution | Existing slideshow/creative worker integration tests encode real artifacts with imageio-ffmpeg; the new command gate is exercised during that flow | **LIMITED**: this proves the already-existing FFmpeg lane works from the worker/local adapters, not exactly-once rendering, a new FFmpeg feature, or a pre-encode `slideshow.render` authorization gate. Some adapters encode *before* the pure recording command. |
| L4 — production-like | Real authenticated multi-user authorization, durable cross-process effect deduplication, deployment verification, post-merge reconciliation with PR#67 | **NOT VERIFIED**; no L4 claim. |

**Reproduction on this branch (local Python 3.11, installed existing `[dev]`
dependencies):** `ruff check .` → pass; `ruff format --check .` → 461 files
formatted; `mypy src --no-incremental` → no issues in 233 source files;
`pytest -q --tb=short` → **1946 passed, 20 skipped, 3 existing SQLModel
deprecation warnings**; `pytest -q tests/architecture
tests/unit/test_docs_integrity.py` → **144 passed**;
`pytest -q tests/integration/test_in_process_job_queue.py` → **18 passed**.
The initially timed-out story-queue test was a local environment issue:
`arabic-reshaper`/`python-bidi` (already declared in `pyproject.toml`) were
not installed in the partial virtualenv; it passed after the normal
`pip install -e '.[dev]'`. PostgreSQL-backed proof is a separate CI job.

**Concurrent work:** PR#67 (Creative Runtime session 3) was **OPEN** at
`9c3a34f107b4b5f3c878a0ec2a7d82e8c24614d3` when reviewed; its checks
were green, but it is **not** on this branch's `035a896...` base. It changes
`studio/bus.py` to add an optional pack lifecycle gate. On integration, place
its lifecycle/`required_packs` check in stage 4, *after trusted grant and
registry lookup and before reservation/execution*, and retain its deliberate
`allow_experimental` opt-in without letting it bypass Gate 2 authorization,
input schema or references. A conflict-free merge has **NOT VERIFIED** status.
CI for this branch is also **NOT VERIFIED** in this pre-push matrix; report its
actual run/check links separately after push, not as local test evidence.
