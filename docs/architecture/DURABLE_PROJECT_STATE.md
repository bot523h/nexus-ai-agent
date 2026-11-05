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
- `tests/integration/test_durable_project_concurrency.py` — real interleaving
  (exactly one winner) and atomic rollback on an injected crash.
- `tests/architecture/test_durable_project_state_boundary.py` — no parallel
  authority, single schema declaration site.
