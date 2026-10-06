---
status: accepted
date: 2026-10-05
deciders: session arena/adr-0008-successor-main (successor after #162 merge)
consulted: live main 8de0edd7d6bd182be2f53ccba8df45cc9fbd09a8; PR #161 source ADR; merged studio/passport/provenance
informed: open PR owners; task-240 deferred until leases clear
---

# 0008. Canonical identity arbitration: one authority per identity, one durable lineage

## Context and Problem Statement

Open branches invented overlapping identity systems for the same facts (`creative/spine/`,
`creative/intelligence/`, second causal ledgers). `task-240` cannot implement durable
project/revision lineage until **one authority per identity** is decided.

Live main (after #162): `8de0edd7d6bd182be2f53ccba8df45cc9fbd09a8`. Merged code already
separates queue transaction projections from CommandBus transactions
(`jobs/creative_passport.py`).

## Decision Outcome

**One contract over merged main** (no second store, ledger, or executor):

| Identity | Source of truth | Writer | Durable where | Never |
|---|---|---|---|---|
| Project | `studio.models.Project.project_id` | composition / CommandBus | queue payload | re-minted per attempt |
| CreativeWork | pure IR content id (`wrk…`) ADOPT #134 shape | sealer only | evidence digest only in V1 | model-authored id |
| Typed Intent | spine Intent model ADAPT #150 | application boundary | queue evidence snapshot | model-supplied id |
| Revision sequence | `state_revision` | CommandBus commit | in-memory V1 | second counter |
| Revision content | `state_hash` (excludes revision) | CommandBus | evidence when returned | silent hash collision |
| Parent revision | prior `state_revision` | CommandBus | nullable evidence | recomputed after fact |
| CommandBus transaction | `tx_` + unique hex | CommandBus only | in-memory; evidence only | used as queue transaction_id |
| Queue request/job | `RequestIdentity` on `nexus_job_queue` | InProcessJobQueue | SQLite row | re-mint on retry |
| Queue attempt | attempt history | fencing CAS | SQLite | rewrite history |
| Artifact | bytes + sha256 | independent verifier | store + queue + passport | handler-declared digest trust |
| Lineage | `provenance.CausalJournal` | journal append only | SQLite sidecar | second ledger |
| Legacy unknown | NULL / `*_known=False` | none | persisted NULL | invented backfill |

### Cross-cutting rules

1. One authority per identity — a second table/class/store is a defect.
2. Passports/graphs are projections — they do not authorize or mutate.
3. Queue `transaction_id` ≠ CommandBus transaction; `canonical_commandbus_transaction_id` stays NULL until real bus facts exist.
4. History is append-only; retries add attempts, never rewrite identity.
5. Contradictory reassignment fails closed and quarantines (`CausalConflictError` / `event_conflict`).
6. Unknown stays unknown — never success-shaped without proof.
7. Content ids are domain-separated and deterministic.
8. Legacy rows stay legacy.
9. task-240 vertical slice binds existing authorities only — no new store/executor/UI.

### Disposition of open candidates

| PR | Verdict |
|---|---|
| #134 | ADOPT pure CreativeWork/IR (rebase + green; no store) |
| #131 | SUPERSEDE by #134 |
| #150 | ADAPT bridge after lease clears; graph is projection only |
| #126/#127 | SUPERSEDE as spine contract |
| #128/#129 | DEFER |
| #152 | SUPERSEDE — second ledger forbidden |
| #148/#149/#151 | ADAPT security lane (orthogonal) |
| #156 | ADOPT (merged) |
| #159 | ADAPT governance handoff |
| #161 | SUPERSEDE by this successor branch after merge |
| #162 | ADOPT (merged — durable free-text handoff) |

### Owner actions (not taken here)

1. Release orphaned `task-232` lease if still active.
2. #150 rebase/release `task-230`.
3. #152 release contradictory `task-231` claim.
4. Implementing PR ratifies in `DECISION_LOG.md` when that path is free.
5. `task-240` stays unclaimed until leases clear.

## Confirmation

`tests/architecture/test_canonical_identity_contract.py` is green on main and must fail if:

- a queue transaction id is accepted as a CommandBus transaction;
- bus ids leave the `tx_` namespace or revision chains break;
- `state_hash` depends on `state_revision`;
- a second module writes `nexus_causal_journal`;
- passport build appends to the ledger;
- nullable queue identity defaults are wiped;
- contradictory reassignment is no longer quarantined.

```bash
pytest -q tests/architecture/test_canonical_identity_contract.py
```

## Consequences

Positive: single identity contract; blocks second-authority merges; unblocks task-240 design.
Negative: multi-step plan transactions and second ledgers are explicitly out of V1.
Risks: stale open PRs may still conflict on files — disposition table is the resolution path.
