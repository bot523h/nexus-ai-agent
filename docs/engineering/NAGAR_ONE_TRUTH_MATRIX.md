# NAGAR One — Technical Truth Matrix

Status: **living view**. Every `VERIFIED` row is connected to code **and** a
test or an observable run. Vocabulary: `VERIFIED` · `PARTIAL` · `MISSING` ·
`BLOCKED` · `DEFERRED`. Numbers are reproducible with the commands named in
`docs/architecture/TESTING.md` §6. Audited against `main` @ `6838c543` plus the
Stage 1 change on `nagar-one-slice2-artifact-revision-binding` (PR #319).

| Capability | Status | Implementation site | Test / evidence | Remaining gap |
|---|---|---|---|---|
| Project identity | VERIFIED | `storage/models.py::AppProject`; `creative/studio/models.py::Project` | `tests/unit/test_durable_project_state.py` | — |
| ProjectRevision (durable) | VERIFIED | `creative/project_state/store.py::SqlProjectRevisionStore`; `storage/models.py::ProjectRevision` | `tests/integration/test_durable_project_restart.py` (new process rebuilds identical id/hash) | — |
| WorkingRevision / AcceptedRevision | VERIFIED | `ProjectRevisionHead` (single CAS head) | `tests/integration/test_durable_project_concurrency.py` (one winner) | — |
| Revision CAS + durable persistence | VERIFIED | `store.py::commit` (conditional UPDATE) | same + `durable_project_state_mutations.py` (29/29) | — |
| Asset Library (ingest/blob/identity) | VERIFIED | `creative/assets/*` | `tests/…/test_asset_library_*`; `scripts/asset_library_mutations.py` | — |
| AssetVersion (immutable, measured) | VERIFIED | `assets/models.py::AssetVersion`; `storage/models.py::AssetVersion` | asset-library suite | — |
| Revision ↔ AssetVersion binding (published artifact) | VERIFIED (this mission) | `project_state/store.py::SqlArtifactBindingStore`; `storage/models.py::ProjectArtifactBinding` | `tests/integration/test_durable_project_revision_pinning.py` (bind/re-verify/repoint/cross-project/fail-closed + the F1 inside-window takeover retraction + token-scoped/monotonic/concurrent fencing + the adversarial cross-job non-seizure) and `tests/integration/test_artifact_binding_postgres_concurrency.py` (real OS processes, PostgreSQL: cross-job non-seizure, owner-only retract, racing advances); harness 29/29 (14 Slice-2 mutants) | AssetVersion *row* binding is by measured `sha256`; a later slice may pin the `version_id` explicitly |
| Creative Intent (typed) | VERIFIED (narrow) | `application/creative_intent.py::CanonicalCreativeIntent` | `tests/…/test_canonical_creative_execution.py` | Trim-only; broader op coverage is later |
| Typed Creative IR / AST | VERIFIED (studio-local) | `creative/spine/models.py`; `creative/rendering/ir.py` | spine + render-lane tests | — |
| Compiler / Command DAG | VERIFIED | `creative/spine/compiler.py`; `creative/rendering/compiler.py` | compiler tests | — |
| Policy / Authority boundary | VERIFIED | `creative/studio/bus.py::CommandBus.dispatch` (9-stage fail-closed) | `tests/…/test_command_bus_*` | — |
| CommandBus (single write path) | VERIFIED | `creative/studio/bus.py` | bus suite | — |
| Job queue + attempt fencing | VERIFIED | `adapters/in_process_job_queue.py` | queue suite; `scripts/queue_ownership_mutations.py` | — |
| Worker revision pinning (admission → execution) | VERIFIED | `creative/render_jobs.py::_resolve_pinned_project` | `test_durable_project_revision_pinning.py` (moved head, vanished/tampered pin) | — |
| Render + Attempt staging | VERIFIED | `creative/render_jobs.py`; `rendering/executor.py` (`.part` → atomic rename) | render-lane tests; `scripts/task255_mutation_probes.py` | — |
| Independent verification (artifact-level) | VERIFIED | `jobs/verification.py`; `jobs/creative_verification.py` | verifier suite | Footprint-level verifier lives in `nagar/ibpc/footprint.py` (video scope) |
| Artifact Passport | VERIFIED (durable) | `provenance/passport.py`; `jobs/creative_passport.py` | `tests/integration/test_provenance_passport_e2e.py` | Passport→pinned-revision/AssetVersion link is **Stage 2** (slice-3, gated on PR #294) |
| Provenance journal (hash-chained) | VERIFIED | `provenance/journal.py`, `models.py` | provenance suite | — |
| Creative Graph / lineage | PARTIAL (in-memory) | `creative/spine/models.py::CreativeGraph` | spine tests | Durable, rebuildable graph is **Stage 2** (PR #294) |
| Session / Turn persistence | MISSING (in-process) | `nagar/ibpc/session.py` (in-memory) | IBPC suite (in-process) | Durable store is **Stage 3** |
| Approval persistence + replay prevention | PARTIAL (in-memory, single-process) | `nagar/ibpc/approval.py` (single-use CAS, in-memory) | `scripts/ibpc_security_mutations.py` (13/13) | Durable, cross-process single-use is **Stage 3** |
| Privacy consent + cloud egress | PARTIAL | `nagar/ibpc/privacy.py` (LOCAL_VAULT default; typed refusal for non-local) | IBPC privacy tests | Durable consent store is **Stage 3** |
| Studio API / Telegram adapter | PARTIAL | `bot/creative_surface.py`; `api/app.py` | surface tests | No approval/preview session API yet |
| Recovery / restart behavior | VERIFIED (queue+revision) | queue recovery; `durable_project_restart.py` | restart suites | IBPC restart durability is **Stage 3** |
| Migration schema (single head) | VERIFIED | `migrations/versions/*` (head `20261107bindfence`) | `tests/unit/test_migrations.py` (upgrade=exact schema, no drift, downgrade empties) | — |

## Stage gates (from the board)

- `task-282-slice-2` (Stage 1) — **claimed & implemented** (PR #319).
- `task-282-slice-3` (Stage 2) — **BLOCKED** on PR #294 (`merged_into_main`);
  also depends on slice-2. See `docs/architecture/MODULE_MAP.md` law numbering.
- Stage 3 — no board claim yet; scoped in this mission.
