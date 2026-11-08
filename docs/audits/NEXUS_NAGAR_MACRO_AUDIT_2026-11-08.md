# NEXUS / NAGAR macro-architecture audit & roadmap (2026-11-08)

An independent, evidence-based assessment of the whole repository — not a
feature review. Every number below was re-derived from the tree on
`fix/binding-ownership-fence-20261107` (base `main` @ `5ed6d4e1`).

## 1. What the system is

A single Python application (`nexus_ai_agent`) with a **hexagonal** core:

- **Seven ports** (`application/ports/`): `LLMPort`, `JobQueuePort`,
  `ObjectStoragePort`, `ConversationStorePort`, `CheckpointLifecyclePort`,
  `CaptionEnginePort`, `OutboxPort`.
- **Layered** L1–L5 (`MODULE_MAP.md`): dependencies point downward; the
  composition roots (`bot/`, `api/`, `cli.py`, `worker.py`) wire the rest.
- **Three durable authorities**, deliberately singular: the job-queue row, the
  hash-chained causal journal (`provenance/`), and the Artifact Passport.
- **One storage schema**: every table is declared in `storage/models.py`; nothing
  else may declare a table.

Scale (re-derived): **361** source files, **377** test files, **~6476** collected
tests, **10** Alembic migrations, **89** declared dependencies, **4** workflow
files carrying **19** jobs, **37** mutation harnesses, **7** truth-doctor checks.

## 2. Strengths (what is genuinely good)

1. **Boundaries are laws, enforced by AST fitness tests.** Layering, "exactly one
   ledger writer", "exactly one queue/verifier/passport", the durable-state
   package's import allow-list, and the creative-graph "projection not authority"
   law are all structural tests, not prose. This is unusually strong.
2. **Evidence culture.** 37 mutation campaigns each require a guard to be *killed*
   by a suite; the truth doctor (`diagnostics/truth.py`) checks version lockstep,
   docs index, law↔test resolution, law numbering, claim witnesses, fail-open
   defaults and board truth; `pip-audit` is an enforced, self-expiring gate.
3. **Fail-closed defaults.** Revisions, artifact bindings, checkpoints, the SSRF
   guard and the backup tier all refuse rather than degrade.
4. **Restart/CAS durability** with real multi-process PostgreSQL proof, and honest
   crash-consistency semantics (a `COMPLETED` job always has its binding).

## 3. Risks (ordered by impact)

| # | Risk | Evidence |
| --- | --- | --- |
| R-1 | **Board truth can be false.** A slice record claimed a merge that never happened (see `NAGAR_F1_LIVE_TRUTH_2026-11-08.md`). A governance record is only as good as the re-derivation behind it. | corrected 2026-11-08 |
| R-2 | **PR-convergence debt.** 44 open PRs; several are valuable but stale/unmerged (#294 needs only a trivial rebase; #325 delivers the F1 fence). Every merge that lands in a conflict-prone file (board, `docs/README.md`, `OPERATION_TRUTH.json`) forces a union-merge. | GitHub API |
| R-3 | **Schema-2 lane divergence.** Two producers emit schema-2 payloads — the canonical handoff (pinned to a revision) and the IBPC lane (no revision concept). The shared model cannot require the pin without breaking IBPC. | measured: 14 tests RED |
| R-4 | **Deferred wiring.** The `artifact → revision → asset-version` chain is not closed end-to-end (the AssetVersion producer is missing); the column and link exist but no live producer fills them. | `DEFERRED_WIRING_AND_PIN.md` |
| R-5 | **Conflict-magnet files.** `OPERATION_TRUTH.json` (generated stamp) and `docs/README.md` change on almost every PR and collide constantly. | #294 conflicts |
| R-6 | **Scale of the board.** 189 claims / 89 zones, many `queued`/`expired`; the active set is hard to read at a glance. | board stats |

## 4. Roadmap

**Now (this campaign).**
- Land the F1 ownership fence (#325) — fixes a live-on-main orphan-provenance
  defect; full suite green, 29/29 mutants killed.
- Rebase and land #294 (durable creative-graph projection) — sound, additive.

**Next (short horizon).**
- Decide R-3 by **owner action**: lane-scoped worker-side pin requirement (recommended)
  or accept the admission-boundary refusal.
- Close R-4 by handing the Asset Library zone over, or assigning the producer
  wiring to its owner with the durable bind contract as the interface.
- Attack R-5 structurally: stop committing the generated `OPERATION_TRUTH.json`
  stamp, or move the docs index into a generated file, so PRs stop colliding on it.

**Later (structural).**
- A board "active view" (only non-terminal claims) to make R-6 tractable.
- Extend the truth doctor to **re-derive** a sample of `delivered` claims against
  git/API (the class of defect R-1 represents), so a false merge record fails CI.

## 5. Bottom line

The architecture is sound and the verification culture is exceptional; the
principal risks are **governance** (a board record can be false — R-1) and
**integration throughput** (44 open PRs — R-2), not design. The highest-leverage
next actions are to land the already-proven fixes and to make the truth doctor
re-derive merge claims.

_This audit was produced by an AI agent (OpenHands) on behalf of the repository
owner._
