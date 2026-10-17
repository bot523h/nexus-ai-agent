# 04 — Asset Library, durable Creative Graph, Verifier / Artifact Passport (Phase 4)

## What already exists (live source read, not cached summaries)
| Concern | Where | Verdict |
|---|---|---|
| Attempt-scoped staging, atomic rename publish, symlink-free components, quarantine | `execution/staging.py` (`AttemptStaging`), tests `tests/unit/test_execution_staging.py` | present, tested |
| Durable job authority (fenced CAS `_mark_completed`, idempotency, attempts) | `adapters/in_process_job_queue.py` (`nexus_job_queue`) | present — **the** execution authority; the brief's "no second queue/DB" is honoured by building nothing beside it |
| Job lifecycle contract (Command → Job → Execution → Verification → Publication) | `jobs/lifecycle.py` | present |
| Independent artifact verification | `jobs/verification.py`, `jobs/creative_verification.py` (render / slideshow lanes) | present |
| Durable evidence record binding request/attempt/source/output digests | `jobs/creative_passport.py` (`StoredAsset`, `CreativeExecutionPassport`, `archive_trim_evidence`, `reverify_stored_passport`) | present for `timeline.trim` only; assets archived **per attempt** (`jobs/<job>/<attempt>/{input,output}.mp4`), no dedup |
| Hash-chained causal journal (append-only SQLite sidecar, exactly-once transition events) | `provenance/journal.py`, `provenance/models.py` | present, tested (`test_provenance_journal.py`) |
| Read-only Artifact Passport (ledger + job facts + independent re-measurement) | `provenance/passport.py` | present |
| Creative graph (Project→Intent→Capability→Artifact→Evidence, append-only) | `creative/spine/models.py` `CreativeGraph` | present but **in-memory only** (D-0025 first slice) |
| Undo as a new revision (transaction with snapshot, undo records not undo targets) | `creative/studio/bus.py` `system.undo` | present, in-memory |

## Gap analysis against the brief
| Required | Status before | Delivered tonight | Still open |
|---|---|---|---|
| SHA-256 CAS with dedup across attempts | absent (per-attempt copies) | **`execution/asset_library.py`** (commit `7fd4112`) | wiring into `archive_trim_evidence` (replace per-attempt copies by CAS admission + hard link into the attempt directory so existing `storage_key`s stay valid) |
| atomic finalize | staging had it for workspace artifacts | CAS publish by `os.link` (never overwrites) | — |
| symlink / FIFO / race safety | staging: components; passport: `resolve(strict)` | source `O_NOFOLLOW|O_NONBLOCK` + `S_ISREG`; 16-thread identical-bytes race → one blob, one winner | — |
| idempotent retries | queue-level | same bytes/same attempt → dedup, no residue; crash before link leaves only a reapable `.part` | — |
| MIME | absent | conservative sniff + `mime_mismatch` vs declared | verifier policy on mismatch (currently caller's decision) |
| DB↔FS consistency | passport re-verification per job | `fsck(referenced)` → missing / orphans / corrupt / stale parts / unexpected | a queue-side helper that feeds `fsck` the `storage_key`s from all passports (one query over `nexus_job_queue`) |
| retention | absent | `gc(referenced, min_age)` — never deletes referenced blobs, even corrupt ones | retention policy numbers (owner) |
| durable Creative Graph (Project→Asset→Intent→Attempt→CommandRun→Artifact→Verification→Evidence) | journal has Job/Attempt/Artifact/Verification observations; graph in memory | **not built** (building a new table set tonight would be the "second DB/manifest" the brief forbids without a design review) | design: project the graph from the journal + passports (read model), persist only new *edges* the journal lacks (Intent→Attempt, Asset admission) as journal event kinds |
| branching with persistent lineage, Creative Undo as new revision | in-memory transactions | — | same design; undo already is a revision in the bus |
| independent Verifier / Artifact Passport binding attempt id, digest, verifier version, evidence; tamper/missing/wrong-id/stale/malformed/signature/duplicate/partial tests | `provenance/passport.py` + `creative_passport.reverify_stored_passport` cover tamper/missing/stale/malformed; signature only in delivery pack (`packs/delivery/signing.py`, ed25519) | — | add `verifier_version` to passport block; wrong-attempt-id and duplicate-passport negative tests; signature of the passport itself is a design decision (`09_RESEARCH…` item 6) |

## Asset Library — contract and proof
Module docstring lists the properties; `tests/unit/test_asset_library.py` (38 tests) covers each. Hand mutants run against the suite (all killed): `os.link`→`os.rename` (overwrite), drop `O_NOFOLLOW`, drop `O_NONBLOCK` (FIFO blocks), skip re-hash on dedup, `gc` ignoring references. `mypy --strict`: clean (284 files). Status: **IMPLEMENTED · TESTED · NOT WIRED · NOT CI-VERIFIED (head `7fd4112` not yet run)**.

Residual risk: a store that spans two filesystems cannot `os.link` (typed `AssetLibraryError`, nothing published) — the artifact root is one directory today; dedup verification re-reads the existing blob (cost = one extra read per duplicate; bounded by the 100 MiB ceiling).

## Wiring delivered later the same night (`archive_trim_evidence` → CAS)
* `_copy_verified` gained `library`/`attempt_id` parameters; `archive_trim_evidence` constructs
  `AssetLibrary(artifact_root, max_bytes=MAX_EVIDENCE_FILE_BYTES)` and both evidence files are
  admitted into `cas/` then published as hard links at the unchanged attempt keys
  (`_publish_from_library`). The identity returned to the passport is the library's measured
  `sha256:<hex>`/size; the existing "archived bytes must equal measured source/render" checks
  remain in force on top. Status: **IMPLEMENTED · TESTED (local full suite) · NOT CI-VERIFIED
  until the run on its head finishes · NOT MERGED**.
* Fallback: link failure (`EXDEV`/`EPERM`) → previous verified streaming copy (tested with a
  link that works only inside `cas/`); lost publish race → existing "same bytes or fail" rule.
* Consequence (intentional): archived evidence is now `0o444` on disk. Three tests that
  simulate disk corruption by writing to the archived file (`test_job_lifecycle_queue`,
  `test_nagar_durable_handoff_e2e`, `test_provenance_passport_e2e`) now `chmod(0o644)` before
  tampering; their tamper-detection assertions are unchanged. Blast-radius note: tampering a
  shared blob affects every attempt that links it — detection still fires per attempt because
  verification hashes the attempt path.
* Tests: `test_copy_verified_publishes_attempt_paths_as_links_into_the_asset_library` (same
  inode across attempts, `st_nlink ≥ 3`, `PermissionError` on write, idempotent re-publish),
  `test_copy_verified_library_path_keeps_the_passport_error_contract` (too-large → typed error
  with the caller's code, no staging leak, occupied key fails closed, `EXDEV` fallback). Hand
  mutants: swallow too-large → 1 failure; off-by-one size → 2 failures. Full local suite
  `pytest -m "not slow"`: 4181 passed / 37 skipped.
* Still open: queue-side `fsck`/`gc` schedule (blob GC must consult attempt directories, which
  remain the retention authority), an `asset_admitted` journal event, the graph projection.
