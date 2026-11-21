# NAGAR One — Technical Truth Matrix

Status: **living view**. Every `VERIFIED` row is connected to code **and** a
test or an observable run. Vocabulary: `VERIFIED` · `PARTIAL` · `MISSING` ·
`BLOCKED` · `DEFERRED`. Numbers are reproducible with the commands named in
`docs/architecture/TESTING.md` §6.

**Audit base:** `main` @ `d6876ab378b88b2ccca1187b1f3c51be5bf52657`
(2026-11-19), the merge of PR #350. Since the previous revision
(`main` @ `6838c543` plus the Stage 1 change on
`nagar-one-slice2-artifact-revision-binding`, PR #319) the base also carries
PR #344 (Waves 1–7 closeout: bounded CI parity, median benchmarks, traced
history) and PR #350 (v1 module consolidation: `resource_packs`,
`hyper_compiler`, `personal_cloud_vault`, `storyboard_compiler`, API lifespan).
Rows below whose evidence lives in those modules were re-run at this base.

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
| Personal cloud vault (BYOS drivers) | VERIFIED | `storage/personal_cloud_vault/byos_drivers.py` (Local / S3 SigV4 / portable bundle), `crypto_chunk_envelope.py`, `sync_journal_manager.py` | `tests/storage/personal_cloud_vault/` (3 files, 93 cases) + `tests/unit/test_personal_cloud_vault_lease_fence.py` (final lease fence under a landed lease) | GC/streaming residency ceiling is measured by `tracemalloc` and is environment-sensitive outside the CI image; see `docs/architecture/TESTING.md` §7 |
| Storyboard compiler (typed manifest) | VERIFIED (library) | `creative/storyboard_compiler/{bilingual_script_parser,cinematography_solver,editorial_cut_solver}.py` | `tests/creative/storyboard_compiler/` (199 cases at this base) incl. HMAC seal, whole-script substitution, and the strict-anchor gate | No production consumer yet: nothing under `src/` calls `compile_storyboard`, so delivery does not consult the manifest |
| Resource packs + hyper compiler | VERIFIED (library) | `storage/resource_packs/`; `creative/hyper_compiler/` | `tests/storage/resource_packs/`, `tests/creative/hyper_compiler/` | Pack activation in the live lanes is later work; the trust plane itself is gated by `scripts/pack_trust_mutations.py` |
| Migration schema (single head) | VERIFIED | `migrations/versions/*` (head `20261107bindfence`) | `tests/unit/test_migrations.py` (upgrade=exact schema, no drift, downgrade empties) | — |

## Stage gates (as of 2026-11-19, `main` @ `d6876ab3`)

- Stage 0 (contract + matrix + freeze) — **CLOSED**: this matrix, the product
  contract and the architecture pages are the frozen frame.
- Stage 1 (durable Project ↔ Revision ↔ AssetVersion) — **IMPLEMENTED**;
  `task-282-slice-2` merged as PR #319 (`6838c543` lineage) and the row above
  is verified at the current base.
- Stage 2 (Creative Graph & Passport closure) — **BLOCKED** on PR #294
  (`merged_into_main`, not in this tree) and on slice-2; it is not implemented
  here, so every Stage 2 row stays `PARTIAL`.
- Stage 3 (production-grade Live Editing: durable session/approval/privacy) —
  **NOT STARTED**, no board claim. The in-memory `nagar/ibpc/*` surfaces are
  labelled as such above rather than promoted.

The board's live queue (`.agents/board.json` → `next_work`, updated
2026-11-12) is execution-core hardening, not Stage 2/3 product work:
`task-255` (AttemptStaging in the render publication path), `task-256`
(heartbeat liveness), `task-257` (pending cancel semantics, merged via PR #338),
`task-259`/`task-260`/`task-263` (schema invariant, execution-core mutation CI
job, single-owner startup guard) and `task-269` (cognition quality harness). A
row in this matrix is only promoted when its code *and* its test are in the same
commit — planning language is never evidence.
