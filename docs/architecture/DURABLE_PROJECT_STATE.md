# Durable Project State and Revisions (task-282)

The Studio `Project` state (`creative/studio/models.py`) is a **derived,
content-addressed value**: `state_hash` is recomputed from the content on every
construction and `state_revision` is a monotonic counter owned by the
`CommandBus`. Wave 1 kept that state **in memory only** — the CommandBus applied
commands atomically, but a process restart lost the project and its revision
history. This page documents the durable record that closes that gap without
introducing a second project authority.

## Source of truth

| Concept | Authoritative owner | Durable record | Identity / hash rule | Forbidden duplicate |
|---|---|---|---|---|
| Project identity | `AppProject` (`storage/models.py`) | `appproject` row | opaque `project_id` | a second project table |
| Project state | `CommandBus` (`creative/studio/bus.py`) | in-memory `Project` | `state_hash` derived from content | a second state store |
| Revision | `SqlProjectRevisionStore` | `projectrevision` row | `revision_id` = `sha256:` of canonical state bytes | a second revision log |
| Head pointer | `SqlProjectRevisionStore` | `projectrevisionhead` row | CAS on `revision_id` | a second head pointer |
| Lineage | `projectrevision.parent_revision_id` | same row | previous `revision_id` | a second lineage ledger |

The durable store reuses the repository's **single** storage engine
(`storage.db.get_session`) and the schema declared **once** in
`storage/models.py`. It builds no second engine, queue, renderer, verifier or
provenance/passport ledger — those owners remain authoritative.

## Identity and concurrency

A revision is immutable and content-addressed:

- the **bytes** are the canonical JSON of the `Project`
  (`creative/studio/persistence.py::canonical_state_bytes` — sorted keys, no
  insignificant whitespace);
- the **revision id** is `sha256:<hex>` over those bytes
  (`content_address`), so identical state can never be stored twice and history
  can never be rewritten;
- `state_revision` (the bus counter) is serialized too, so a fresh process
  rebuilds the **exact** revision, counter included.

The per-project head row is the **single mutable pointer**. `commit()` is a
compare-and-swap: it passes an `expected_parent_revision_id` token and advances
the head with one conditional `UPDATE ... WHERE revision_id = <expected>`. Two
concurrent writers race that statement; exactly one advances the head, and the
loser gets a typed `RevisionConflict` with the head left untouched. A benign
race where a concurrent writer committed the **same** state is idempotent
success, never a conflict.

## Failure and recovery contract

The whole commit is **one transaction** (revision insert + head advance +
commit). A crash before the commit leaves no revision row and no moved head.
The lifecycle composes the existing `CommandBus` write path with the store
(`creative/project_state/lifecycle.py::DurableProject`): the bus stays the single
write authority, and durability only records what the bus already accepted.

## Revision pinning contract (admission → execution)

A durable intent is admitted against **one** revision, chosen at `commit()`
time, and every downstream step carries that exact revision — the head is never
re-read:

```
commit(project) → RevisionRecord(R1) → handoff(R1) → queue(payload pin R1) → worker(load R1)
```

- the composition root (`cli.py`) captures the `RevisionRecord` `commit()`
  returned and passes it to the handoff as `admitted_revision`;
- the handoff re-validates **that** revision through
  `SqlProjectRevisionStore.load_revision` (never `load_head`) and refuses with a
  typed `revision_unavailable` before any enqueue if it is missing, tampered, or
  re-homed;
- the worker loads the pinned `revision_id` and refuses if it is gone, even
  though the project head still exists.

There is **no fallback to "latest"**: a canonical admission with no admitted
revision is a refusal, not a head read. This closes a TOCTOU where a concurrent
commit between admission and handoff would otherwise silently re-target the job
at a newer revision.

## Artifact → revision binding (Slice 2)

A pinned job executes one immutable revision; when it publishes an artifact, the
artifact's **measured** content digest must be durably bound to that exact
revision — never to the project head, which may have moved after admission.

```
worker(load R1) → publish(output.mp4, sha256=D) → record INTENT {artifact=D, revision=R1} → result
                                                                                        │
queue: verify(result) ─ re-measure bytes == D ──▶ publish (atomic rename) ──▶ confirm: bind(project, R1, artifact=D) ──▶ COMPLETED
                                                                                        │
        re-verification: verify_artifact(project, D, path) ─ re-measure bytes == D ─────┘   and R1 still validates
```

The worker does **not** commit the binding: it runs before the queue's
independent verification and atomic publication, so a result the queue later
rejects would otherwise leave a durable binding for an artifact that was never
published. The worker records only the *measured* binding **intent**; the
queue's post-publication `confirm` hook (`ArtifactPublication.confirm`, wired to
`creative_render_binding_confirm`) commits the durable row, and only then may the
job become `COMPLETED`. The hook is fail-closed: a refusal (or crash) fails the
job and retracts this attempt, so a job is never `COMPLETED` without its binding
and a rejected artifact never leaves one behind. The digest is re-measured from
the published bytes in the hook as well.

The queue **re-reads the fence** (`_owns_execution`) immediately before the
confirmation, because the binding is a durable side effect: ownership was last
read before the (irreversible) publication, and the re-probe window between that
check and the confirmation is the classic plain-filesystem fencing limit. An
attempt superseded in that window commits no provenance and reaches no
`COMPLETED` — it owns nothing.

**Ownership fence (F1).** That re-check cannot cover a takeover landing *inside*
the confirm commit window itself (after the re-check, at/around the binding
write): the attempt commits its binding and *then* loses the terminal
`COMPLETED` CAS, which would leave an orphan durable binding from a superseded
attempt that never reached `COMPLETED`. The binding therefore carries the
**queue-minted fencing token** (`attempt`) in its own column (migration
`20261107bindfence`), and the lost-CAS path retracts **exactly** the row this
attempt wrote — the delete is scoped on `(artifact_id, job_id, attempt)`, never a
successor's row, a pre-fence row (`attempt = 0`), or another job's row. A re-bind
by a newer attempt advances the token **monotonically** and **atomically** (a
single conditional `UPDATE ... WHERE attempt < :new`, never a read-modify-write,
so two racing re-binds can never let the lower token commit last and reopen the
hole; provenance is never rewritten), so a stale retraction can never match a
successor's live binding; a
retraction without a real token (`attempt >= 1`) is a typed refusal. The
confirm hook is the **last** durable side effect before the terminal CAS, so a
lost terminal CAS is the only non-crash exit that can leave a committed binding,
and that exit retracts it.

**Crash-consistency model (honest, not claimed atomic).** If the process dies
after the confirmation commits the binding but before the durable `COMPLETED`,
the row stays `verifying` with a queue-owned heartbeat; startup recovery
(`resume_pending`) supersedes its fencing token and re-runs the job. The binding
is append-only history of bytes that *were* published and is **not** deleted by
the later failure, and the job is never falsely `COMPLETED`. There is no
cross-database atomic transaction between the artifact-binding store and the
queue, so the guarantee is "no `COMPLETED` without a binding", **not** "the
binding exists iff the job completed".

The binding lives in the **same** `project_state` package and the **same** single
storage schema (`store.py`, one module maps `storage.models`), as one
content-addressed row:

| Concept | Authoritative owner | Durable record | Identity / hash rule | Forbidden duplicate |
|---|---|---|---|---|
| Artifact | the worker's measured bytes | `projectartifactbinding` row | `artifact_id` = `sha256:<hex>` of the published bytes | a second artifact store |
| Binding | `SqlArtifactBindingStore` | same row | append-only, idempotent per `(project_id, artifact_id)` | a second provenance ledger |

- `bind()` records the digest against `(project_id, revision_id)`; the revision
  must exist and its bytes must still hash to their own identity, so an artifact
  can never bind to a tampered, vanished or re-homed revision;
- the binding is **append-only**: re-binding the same artifact to a *different*
  revision is a hard `ArtifactBindingConflict`, so an artifact's provenance can
  never be silently re-pointed (idempotent for the same revision);
- `verify_artifact()` **re-measures** the artifact's current bytes and confirms
  the bound revision still validates — any divergence is a typed refusal
  (`ArtifactRevisionMismatch` / `RevisionIntegrityError` / `CrossProjectAccess`);
- the confirm hook wires this fail-closed: a refused binding fails the job and
  retracts the attempt — never a `COMPLETED` job without provenance. Unpinned
  (legacy) jobs publish no binding.

## Boundaries

`tests/architecture/test_durable_project_state_boundary.py` enforces that the
package imports no web framework, Telegram, provider SDK, heavy ML library or
competing authority (`jobs`, `provenance`, `worker`, `llm`, the job queue),
declares no database table, maps the storage schema from exactly one module, and
exposes no combined `execute`-style method that would bypass the bus.

## Evidence

- `tests/unit/test_durable_project_state.py` — content addressing, immutability,
  CAS conflict, idempotent replay, cross-project refusal.
- `tests/integration/test_durable_project_restart.py` — a real OS process commits
  a revision; a brand-new store over the same file rebuilds the identical id,
  hash and state.
- `tests/integration/test_durable_project_revision_pinning.py` — the worker loads
  the exact pinned revision (never a moved head) and refuses a vanished or
  tampered pin; the handoff pins the **admitted** revision even after a
  concurrent head move (deterministic ordering, no sleeps); and (Slice 2) the
  artifact→revision binding: bind/re-verify, idempotent replay, repoint refusal,
  artifact-byte re-measurement, tampered-revision refusal, cross-project refusal,
  the unpinned no-op, the intent/confirm split, and the queue-level end-to-end
  proofs (a verified+published job commits its binding then `COMPLETED`; a
  refused confirmation fails the job closed with no binding row and its published
  bytes retracted; a superseded attempt commits no binding at the commit boundary
  — fault-injected takeover in the re-probe window; a takeover landing *inside*
  the confirm commit window retracts exactly its own row under the fencing token
  (F1); token-scoped retraction that never deletes a successor's binding;
  a token-less retraction is refused; the fencing token advances monotonically on
  re-bind; and a crash after the confirmation but before `COMPLETED` recovers
  honestly).
- `tests/integration/test_durable_project_concurrency.py` — real interleaving
  (exactly one winner) and atomic rollback on an injected crash.
- `tests/architecture/test_durable_project_state_boundary.py` — no parallel
  authority, single schema declaration site.
- `tests/unit/test_migrations.py` — `alembic upgrade head` creates the exact
  schema (including `projectartifactbinding`), no drift, downgrade empties it.
- `scripts/durable_project_state_mutations.py` — 28/28 mutants killed (15 Slice 1
  + 13 Slice 2: repoint-ignored, revision-validation-skipped, re-measure-removed,
  digest-remeasure-removed, content-address-validation-removed,
  verify-oserror-fail-open, binding-fail-open, the three queue confirm-seam
  mutants — confirm-gate disabled, confirmation-refusal-ignored, and
  confirmation-fence-recheck-removed — and the three F1 ownership-fence mutants:
  lost-CAS retraction removed, retraction not token-scoped, and re-bind fencing
  token no longer monotonic).
