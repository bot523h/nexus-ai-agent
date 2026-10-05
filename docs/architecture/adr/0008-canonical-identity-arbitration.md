---
status: accepted
date: 2026-10-05
deciders: session arena/01a10d52-nexus-ai-agent (canonical identity arbitration; no task-240 claim taken)
consulted: live GitHub state re-read at 2026-10-05T18:30Z (origin/main 5a228ea9a114363f6b77a4becd0681eeab3527a2; PRs #126/#127/#128/#129/#131/#134/#148/#149/#150/#151/#152/#156/#159 at their exact heads), merged main code (`creative/studio`, `jobs/creative_passport`, `provenance`), D-0013, D-0017, D-0020, D-0024
informed: owners of the open PRs above, holders of the live leases `task-232` / `task-230` / `task-231`, gates owners, and whoever resumes `task-240`
---

# 0008. Canonical identity arbitration: one authority per identity, one durable lineage

## Context and Problem Statement

Several open branches invented **overlapping identity systems** for the same facts.
`task-240` (durable creative project/revision lineage) cannot be implemented until one
authority per identity is decided, because three open PRs each ship their own
`creative/spine/` or `creative/intelligence/` package, a fourth ships a second durable
ledger, and two more edit the same `studio/bus.py` / `studio/models.py` surfaces:

| Surface | Open-PR diffs (exact-file overlap, re-read 2026-10-05) |
|---|---|
| `creative/spine/` | #126, #127, #128, #150 — four competing implementations of the same package |
| `creative/intelligence/` | #131, #134, #150 |
| `creative/studio/bus.py` | #126, #127, #128, #129, #148, #149, #151 |
| `creative/studio/models.py` | #128, #129, #148, #149, #151 |
| `creative/render_jobs.py` | #148, #149, #150, #151 |
| `adapters/in_process_job_queue.py` | #152 only |
| `provenance/passport.py` | none — the merged provenance lane is uncontested |

**Live truth (re-read 2026-10-05T18:30Z, not carried over from any earlier report).**
`origin/main = 5a228ea9a114363f6b77a4becd0681eeab3527a2` (merge of #156; second parent
`eb18091777330b1c95a966b92548ec72dd79cbe6`); main CI run
[37337104871](https://github.com/bot523h/nexus-ai-agent/actions/runs/37337104871) passed
16/16 jobs. Exact PR heads and their last CI run: #126 `1fc4938356` cancelled;
#127 `62b754c223` success; #128 `830edbc13e` success; #129 `75f646a52e` success;
#131 `e61b909fca` failure; #134 `9ea3654c48` failure; #148 `5dbe96e111` failure;
#149 `e865c14650` success; #150 `5f84be4dd2` failure (lint, python-parity 3.10/3.11/3.12,
pytest); #151 `779202d491` success; #152 `55514b630d` failure (python-parity 3.10);
#159 `db0c72e356` success, still a draft with no review.

**Live leases fence the `task-240` zone right now.** `main` still carries
`task-232-durable-creative-queue-evidence` as `active`, `gates_owner: true`, claimed
`2026-10-05T07:39:41Z`, TTL 24 h (expires `2026-10-06T07:39:41Z`), on branch
`arena/01a10af5-nexus-ai-agent` — a branch whose PR #156 is already **merged** — fencing
`docs/architecture/JOB_LIFECYCLE.md`, `adapters/in_process_job_queue.py`,
`jobs/creative_passport.py`, `provenance/models.py`, `provenance/passport.py`, and five
tests. PR #150's pushed board holds `task-230-agent-intelligence-execution`
(`active_in_review`) until `2026-10-07T17:19:00Z`, fencing `creative/spine/`,
`creative/intelligence/`, `orchestration/`, `render_jobs.py`, `render_contracts.py`,
`jobs/lifecycle.py`, `bot/app.py`, `bot/agent_intelligence_surface.py`, `cli.py`,
`i18n/locales/en.json`, `docs/DECISION_LOG.md` and its tests. PR #152's pushed board
holds a **contradictory** `task-231-causal-evidence-ledger` claim until
`2026-10-05T20:25:57Z` while `task-231` is already merged as the provenance ledger
(PR #153); it fences `src/nexus_ai_agent/causal/` and the queue adapter. Everything else
(#126, #127, #128, #129, #131, #134, #148, #149, #151) has an **expired** lease but a
real, conflicting diff.

Merged main already states the honest version of the problem in code: the creative
passport projects `transaction.kind = "queue_request_transaction_projection"` with
`canonical_commandbus_transaction_id: null`, and its limitations say "The CommandBus
transaction is in-memory; its transaction ID, revision, and state hash are not returned
by the legacy worker" and "This is a queue execution projection, not a persisted
CreativeWork or canonical project revision" (`jobs/creative_passport.py`). D-0024 adds
that the parallel proposal branches "each grew their own in-memory graph instead — exactly
the 'required durable fact exists only in memory' failure the contract forbids".

## Decision Drivers

- **One authority per identity** — a second definition, table, or store for the same
  identity is a defect, not a variant (the failure mode of #126–#134 and #152).
- **Honest evidence** — a queue transaction is not a CommandBus transaction; a projection
  is not an authority; unknown stays unknown and nullable, never success-shaped.
- **Durability where the fact is authoritative** — queue row for job state, bus for
  in-memory project revision, artifact verifier for bytes, provenance journal for the
  causal account; nothing durable lives only in memory, nothing in memory pretends to be
  durable.
- **Immutability of history** — revisions, parents, attempts, and lineage are append-only;
  retry/recovery may add attempts, never rewrite identity.
- **Fail-closed contradiction** — the same identity with different content is refused and
  quarantined, never silently absorbed or healed.
- **Testability** — every rule needs an executable confirmation, green on merged main.

## Considered Options

1. **(A) One contract over merged main plus one future pure identity layer** — the merged
   queue/passport/provenance/CommandBus authorities stay canonical; a single pure
   `creative/intelligence` (CreativeWork + deterministic content identity + semantic
   revision, from #134) and a single thin `creative/spine` bridge (intent → work → plan →
   the existing queue, adapted from #150) may later be added, with no store and no
   executor of their own.
2. **(B) #126/#127/#128 spine stack as the canonical backbone** — its own `Intent`,
   `CompiledIntent`, `ArtifactRecord`, `EvidenceRecord`, `CreativeGraph`, and plan
   rollback become the identities; the merged queue-based lane is retrofitted to it.
3. **(C) #152's `causal/` ledger as the provenance authority** — a second append-only
   ledger and passport implementation beside the merged `provenance/`.
4. **(D) A new identity service or domain store** — a dedicated registry/persistence
   component owning Project/CreativeWork/Intent/revision identities.
5. **(E) #129's staged plan transactions first** — multi-step atomic commit in the bus
   before any lineage slice.

## Decision Outcome

Chosen option: **(A)**. Options B, C, D each create the second authority this decision
exists to prevent (B forks Intent/plan/graph identity, C forks the durable account, D
forks identity ownership); E solves a problem the single vertical slice does not have
(one CommandBus command is already atomic).

### The canonical identity contract (normative)

| Identity | Source of truth | How it is built | Authority (who writes it) | Where it is durable | Never stored or recreated |
|---|---|---|---|---|---|
| **Project** | `studio.models.Project.project_id` | opaque local id supplied by the caller/composition root, validated by `_safe_project_id` | `CommandBus` — the only writer of project state | queue row (`nexus_job_queue` payload `project_id`) and passport projection | never derived from a state hash; never re-minted per attempt |
| **CreativeWork** | `creative/intelligence/ir.py::CreativeWork.work_id` (ADOPT #134) | domain-separated content id over canonical IR (prefix `wrk`), from the sealed document only | the composition root that seals the work; a pure function with no store | not durable in V1 — carried as an evidence digest inside the request lineage payload | no second work type; no work store; never authored from model output |
| **Typed Intent** | `creative/spine/models.py::Intent.intent_id` (ADAPT #150) | `int_` + sha256 over the canonical model excluding `intent_id` itself | the trusted application boundary; an LLM may only propose a wire-schema candidate | not durable in V1 beyond its evidence snapshot in the queue payload | never accept an id from a model; never re-derive a persisted intent id |
| **Revision (sequence)** | `studio.models.Project.state_revision` | `parent + 1` at the bus commit step | `CommandBus` commit only | in-memory in V1; durable only as bus evidence (see below) | no second counter anywhere; revision never enters `state_hash` |
| **Revision (content)** | `studio.models.Project.state_hash` | `sha256` over canonical project JSON (`project_id`, `name`, `timeline`, `assets`), revision excluded | `CommandBus` commit only (`compute_state_hash`) | in-memory in V1; durable as `canonical_project_state_hash` evidence once the worker returns it | never recompute over a mutated snapshot; never let two revisions share a hash silently as "the same state" |
| **Parent revision** | `EditTransaction.parent_revision` | the previous `state_revision` copied at commit | `CommandBus` commit | in-memory history in V1; durable as evidence (nullable) | never recomputed after the fact; unknown stays `NULL` |
| **CommandBus transaction** | `EditTransaction.transaction_id` (`tx_` + `uuid4().hex`) with `command_id` and the `(project, operation, idempotency_key)` reservation | opaque unique id minted per applied commit; `command_id` is request-stable | `CommandBus` only | in-memory in V1; durable only as *evidence* referenced by the queue passport after the implementing slice | never used as the queue `transaction_id`; never reused; never fabricated when a worker returns none |
| **Queue request/job** | the `nexus_job_queue` row; `RequestIdentity = (request_id, request_fingerprint, transaction_id)` | `content_id("request", {job_type, idempotency_key})`; `content_id("transaction", {request_id, fingerprint})` | `InProcessJobQueue` at enqueue; `UNIQUE` index on `transaction_id` | SQLite row (WAL + durable sync) | never re-minted on retry, recovery, restart, or backfill; `request_id/fingerprint/transaction_id` stay `NULL` for legacy rows |
| **Queue attempt** | `nexus_job_queue` attempt history | `content_id("attempt", {job_id, attempt_number})` | queue fencing token (CAS) | SQLite attempt history | never rewrite a historical attempt; a retry adds an attempt, it does not edit one |
| **Artifact** | the stored bytes; `StoredAsset.sha256` (content) + `storage_key` (locator) | digest measured from the stored bytes and re-verified independently | the registered artifact verifier — never the handler | artifact store + queue row + passport | never trust a handler-declared digest; never create a second artifact registry |
| **Lineage binding** | `provenance.CausalJournal` (`nexus_causal_journal`), with the queue row as state authority | exactly-once `dedupe_key(kind, job, attempt)` + hash chain over canonical record bytes | the journal append path (observer/backfill) only; the passport is a **read-only** projection | SQLite sidecar | no second ledger or passport store; no journal mutation; no invented reservation across a witnessed gap |
| **Legacy unknown** | the authority row's `NULL` / absent fields | nothing — absence *is* the representation | none | persisted `NULL`, with `*_known = False` flags | never backfilled by invention; never shaped as verified; never used to authorize |

### Cross-cutting rules

- **R1 — One authority per identity.** A second class, table, or store for one of the
  identities above is a defect. Additions must extend the owning module.
- **R2 — Projection discipline.** Passports, graphs, snapshots, and caches are evidence:
  they may not authorize, mutate state, or become the state authority (D-0024).
- **R3 — Queue ≠ CommandBus transaction.** `transaction_id` in `RequestIdentity` is a
  *queue request transaction projection*; `canonical_commandbus_transaction_id` stays
  `NULL` until a real CommandBus transaction actually returned its facts. A queue
  `transaction_id` must never be accepted as, or mapped onto, a CommandBus transaction.
- **R4 — Immutability.** Revision, parent, attempt, and lineage facts are append-only;
  retry/recovery may add attempts but must not change historical identity.
- **R5 — Fail closed on contradiction.** The same identity with different content is
  refused and quarantined (`CausalConflictError` + an `event_conflict` record is the
  proven pattern), never silently absorbed.
- **R6 — Unknown stays unknown.** `NULL` plus an explicit `*_known = False` flag; evidence
  is never upgraded without proof.
- **R7 — Determinism.** Every content identity is domain-separated and reproducible across
  processes from canonical JSON; no wall-clock, no process-local counters.
- **R8 — Legacy stays legacy.** Historical rows are never rewritten to look canonical.
- **R9 — The slice must reduce.** The task-240 vertical slice may not introduce a store,
  executor, renderer, UI, or capability; it binds existing authorities and returns bus
  facts into the existing evidence chain.

### Disposition of the open candidates

| PR | Verdict | Exact condition |
|---|---|---|
| #134 (CreativeWork, deterministic content identity, semantic revision) | **ADOPT** — the canonical pure identity/IR layer | rebased on current main, CI green; no persistence, no executor, no bus coupling; it defines `CreativeWork`/content-id/revision semantics and nothing else |
| #131 (Creative IR + semantics) | **SUPERSEDE** by #134 | #134 is the superset of the same package; do not merge both |
| #150 (Intent → CreativeWork → Plan → real execution bridge) | **ADAPT** — canonical bridge shape, after its lease clears | keep the typed `Intent` + strategy + deterministic compiler; its request-local `CreativeGraph` is a projection only and must never be the durable lineage; reduce to the registered `timeline.trim` path; rebase + green CI; reconcile `render_jobs.py` / `jobs/lifecycle.py` with the merged queue contract; release `task-230` |
| #126 (intent-first spine + board referee) | **SUPERSEDE** as a spine contract | only its board-referee/lease pieces may be re-proposed separately on the board tooling; the spine package is not canonical |
| #127 (transaction-scoped rollback, exactly-once replay) | **SUPERSEDE** | stacked on #126; the bus already releases a failed reservation and returns the recorded result for a redelivered idempotency key |
| #128 (identity-aware `system.undo`) | **DEFER** | adopt only if a later slice genuinely needs transaction-aware undo; the V1 lineage slice is append-oriented and needs no undo primitive |
| #129 (staged plan transactions, atomic commit) | **DEFER** | multi-step atomic commit is not required for a single-command slice; revisit only when a real multi-command plan must be all-or-nothing |
| #152 (second causal ledger + passport) | **SUPERSEDE** — merge-as-is forbidden | the merged `provenance/` lane (D-0024, PR #153) is the single durable account; if a primitive from it is genuinely missing, it may return as a thin port over the canonical journal, never as a store |
| #148 / #149 / #151 (security STOP-B/STOP-C/remote-key) | **ADAPT** — orthogonal to identity | the security lane owner consolidates them into one rebased PR (they are near-duplicates); they may not redefine identity fields, and no identity work is stacked under them |
| #156 (durable creative queue evidence) | **ADOPT** — merged foundation | already in main; this ADR builds on it |
| #159 (governance handoff, task-232 release + task-240 deferral) | **ADAPT** | its deferral reasons stand but are incomplete without this contract; note that its proposed release is *not* merged, so the `task-232` lease is still live in main |

### Open owner actions (not taken here)

1. `task-232` lease — the merged-PR branch `arena/01a10af5-nexus-ai-agent` (or a steward
   acting under the orphaned-lease rule) releases the live lease that still fences
   `provenance/models.py`, `provenance/passport.py`, `jobs/creative_passport.py`,
   `adapters/in_process_job_queue.py`, `docs/architecture/JOB_LIFECYCLE.md` and five
   tests until `2026-10-06T07:39:41Z`. `agent_board.py release` refuses cross-branch
   release, and PR #159 proposes but has not merged this state change.
2. `task-230` lease — PR #150's owner rebases, resolves CI, and releases
   `creative/spine/`, `creative/intelligence/`, `orchestration/`, `render_jobs.py`,
   `docs/DECISION_LOG.md` (live until `2026-10-07T17:19:00Z`) or defers explicitly.
3. `task-231` contradictory claim — PR #152's owner releases the branch lease that
   re-claims an already-merged task and fences a second ledger (expires
   `2026-10-05T20:25:57Z`).
4. Decision-log ratification — the implementing PR records a `DECISION_LOG.md` entry for
   this contract. `docs/DECISION_LOG.md` is currently inside PR #150's live lease, and
   open branches already carry competing `D-0024`–`D-0028` ids, so the entry id is
   assigned at merge time.
5. `task-240` itself remains **unclaimed** until actions 1–3 clear and a fresh
   `agent_board.py check` on the intended paths returns no overlap.

## Confirmation

`tests/architecture/test_canonical_identity_contract.py` — the executable half of this
ADR, green on merged main and red on any of the following regressions:

- a queue `transaction_id` (or any `transaction_`-prefixed content id) being accepted as
  a CommandBus transaction, or a `transaction_id` field appearing on `TypedCommand`;
- a CommandBus transaction id leaving the `tx_` namespace, losing uniqueness, or a
  revision chain where `parent_revision` does not equal the previous revision;
- `state_hash` depending on `state_revision`;
- a second module defining the durable ledger table `nexus_causal_journal`, or
  `provenance/passport.py` acquiring a write statement;
- the queue identity fields (`request_id` / `request_fingerprint` / `transaction_id`)
  losing their nullable-unknown defaults;
- a conflicting reassignment no longer raising `CausalConflictError` and being
  quarantined as `event_conflict`, or a fresh journal instance on the same file reading
  back a different account.

Run: `pytest -q tests/architecture/test_canonical_identity_contract.py` and, for the whole
decision surface, `pytest -q tests/unit/test_docs_integrity.py tests/unit/test_agent_board.py`.
CI keeps it true through the `test (pytest -m "not slow")` and `lint (ruff + mypy + version
lockstep)` jobs.

## Pros and Cons of the Options

### Option A — one contract over merged main (chosen)

- (+) No new authority, store, or executor; every identity already has a merged owner.
- (+) Makes the queue/CommandBus distinction explicit instead of implicit (it is already
  encoded in the passport's `null`s and limitation strings).
- (+) Gives #134 and #150 a canonical destination instead of two parallel universes.
- (−) Requires the open branches to rebase, reduce scope, and release leases before any
  lineage slice can land.
- (−) The CreativeWork/Intent identity layer stays pure (no durability) until the slice,
  so evidence for those identities remains a digest reference, not a stored object.

### Option B — #126/#127/#128 spine stack as canonical

- (+) Rollback and replay semantics are already written and tested on that stack.
- (−) Its `Intent`, `CompiledIntent`, `ArtifactRecord`, `EvidenceRecord`, and
  `CreativeGraph` duplicate identities main already owns (D-0024 rejects their in-memory
  graph as durable lineage); merging it would fork Intent/plan identity.
- (−) Branches are 33 commits behind main and conflict directly with the merged queue lane.

### Option C — #152 `causal/` ledger as the provenance authority

- (+) A second implementation could side-step review debt on the merged lane.
- (−) Two durable accounts of the same transitions — the exact second-authority failure
  D-0024 forbids; its own board claim contradicts a task that is already merged.

### Option D — a new identity service or domain store

- (+) One explicit place to look for identities.
- (−) Forks ownership from `CommandBus`, the queue adapter, and the provenance journal;
  creates a new durable store for facts that already have authorities.

### Option E — #129 staged plan transactions first

- (+) Real atomicity for multi-step plans.
- (−) Solves a problem the slice does not have; adds a second transaction model before the
  single-command transaction identity is even projected into durable evidence.

## More Information

- [`../JOB_LIFECYCLE.md`](../JOB_LIFECYCLE.md) — job chain, state machine, artifact
  verification contract; it documents the same queue/projection boundaries.
- [`../PROVENANCE_LEDGER.md`](../PROVENANCE_LEDGER.md) — the causal ledger and the
  read-only passport.
- [`../COMMAND_CAPABILITY_CONTRACT.md`](../COMMAND_CAPABILITY_CONTRACT.md) — the canonical
  command pipeline and revision semantics this ADR reuses (D-0013).
- [`../../DECISION_LOG.md`](../../DECISION_LOG.md) — D-0013, D-0017, D-0020, D-0024; the
  implementing PR adds the ratification entry for this contract.
- Board: `task-232` (merged, lease live), `task-230` / `task-231` (live branch leases),
  `task-240` (deferred successor; no claim taken by this session).
- PR #159 (`LINEAGE_BACKBONE_HANDOFF_2026-10-05.md` on its branch) — the deferral record
  this ADR completes with the missing contract.
