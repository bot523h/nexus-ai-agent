# ROADMAP_STATUS — NEXUS AI agent (as of 2026-09-18)

## Active workstream: checkpoint lifecycle (PR1/PR2)

Header: **No hidden migration. No hidden mutation. No implicit repair.**

| Stage | Content | State | Anchor commits |
|-------|---------|-------|----------------|
| Phase D ("Leviathan") | Alembic schema management, Postgres/Neon support | MERGED to main (v3.5.0) | `c81f299` (C1) → `f9bcd83` (Alembic) → `f93cb16` (bootstrap) → `79b720d` (D5) → D10 |
| Stage 0 | Frozen domain + port contracts (vocabulary, retention, journal state machine, 5 ports) | DONE on branch | `371926c`, `a145058` |
| PR1 | Read-only SQLite checkpoint adapter contract (schema-v1 fingerprint, golden, lineage, POST_V1 delete guards) + lifecycle metadata store | DONE on branch (NOT merged to main) | `f8a08d4`, `88cdfdd`, `4a06ff2`, `ca615cd`, `6d953e4` |
| PR2 C1 | hooks + context + reconciler + health gate + kill-switch | IN PROGRESS — hooks/context/kill-switch DONE (`f154848`, `6f21908`); reconciler, health gate, wiring, atexit flush MISSING | — |
| PR2 C2 | inspect product slice (inspect-v1 contract) | IN PROGRESS — command exists (`08766ba`); `unknown_fields` + schema key MISSING | — |
| PR2 C3 | O1 observability (redaction, structured logging, in-process registry, core schema fingerprint) | IN PROGRESS — redaction/registry/metrics snapshot DONE (`e67c3e0`); `core_schema_fingerprint` warning semantics MISSING | — |
| PR2 C4 | Adversarial test hardening (8 remaining), golden safety, kill-switch + flush-shutdown tests | IN PROGRESS — golden + drift/missing-golden/connection-error/kill-switch tests DONE (`1430895`); remaining 8 + flush-shutdown MISSING | — |
| POST_V1 | Per-checkpoint (delta-chain) deletion surgery | DEFERRED — guarded by `POST_V1_DELETE_MARKER` + architecture test | — |

Rescue note (2026-09-18): the PR1/PR2 lineage existed only on
`origin/arena/01a0b123-nexus-ai-agent` (27 commits ahead of main, never
merged). It was fast-forwarded into `arena/01a0b5d7-nexus-ai-agent` and pushed;
no work was lost. Working tree at rescue time was clean; no stashes.

## Open, non-merged (not part of this workstream)

- PR #1 `feat/phase1-control-plane` — control-plane foundation proposal (open)
- PR #2 `feat/phase2-local-llm` — provider-agnostic local LLM engine (open)
- Reverts on the branch (pre-existing, pre-rescue): D8 token encryption
  (`777f537`) and the pgvector migration (`2bb694f`) were removed from the
  branch lineage because neither had a consumer; CI + tests were aligned.

## Continuum

`.nexus/continuum.json` (schema v2) anchors the continuity state:
`plan=C2`, ledger entries for C1/C2/tooling/D7pgvector/D8.
`test_count_expected` is stale (179); measured 2026-09-18: **242 passed,
1 skipped (PostgreSQL-required)**; will be refreshed with the measured value.

## Quality gates (measured 2026-09-18, `6f21908`)

| Gate | Result |
|------|--------|
| `make lint` | green — `All checks passed!`, 195 files formatted |
| `make types` | green — `Success: no issues found in 134 source files` |
| `make test` | green — `242 passed, 1 skipped in ~24s` (skip: PG service required, run by CI `migrate-postgres` job) |
| `make smoke` | **broken** — Makefile passes `--input` but the CLI takes a positional argument (reproduced; fix pending) |

CI (`.github/workflows/ci.yml`): two jobs — `test` (ruff/mypy/pytest, no
service) and `migrate-postgres` (service container `pgvector/pgvector:pg16`,
runs `nexus migrate` idempotently + race regression + head assertion on
`47903d282ede`).

## Dependencies (core, 30 declared)

Heavy flags: `sentence-transformers` (pulls torch transitively),
`llama-cpp-python` (compiles llama.cpp C++ from source on this platform),
`chromadb`. No new core dependency was added on the branch; `cryptography`
was removed with the D8 revert; `sqlmodel==0.0.42`, `sqlalchemy==2.0.54`,
`aiosqlite==0.22.1` pinned for ABI stability. Extras: `[dev]` only.

## Known doc/spec friction

- `docs/architecture/DATA_LIFECYCLE.md` describes a `nexus_operation_journal`
  table; the consolidated spec forbids a journal table. Resolution: keep the
  pure state machine in `domain/policies/retention.py`, do NOT materialize the
  table. Doc update pending.
