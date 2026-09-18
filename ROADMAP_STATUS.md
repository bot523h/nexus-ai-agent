# ROADMAP_STATUS — NEXUS AI agent (as of 2026-09-18, Phase 5)

## Active workstream: checkpoint lifecycle (PR1/PR2)

Header: **No hidden migration. No hidden mutation. No implicit repair.**

| Stage | Content | State | Anchor commits |
|-------|---------|-------|----------------|
| Phase D ("Leviathan") | Alembic schema management, Postgres/Neon support | MERGED to main (v3.5.0) | `c81f299` (C1) → `f9bcd83` (Alembic) → `f93cb16` (bootstrap) → `79b720d` (D5) → D10 |
| Stage 0 | Frozen domain + port contracts (vocabulary, retention, journal state machine, 5 ports) | DONE on branch | `371926c`, `a145058` |
| PR1 | Read-only SQLite checkpoint adapter contract (schema-v1 fingerprint, golden, lineage, POST_V1 delete guards) + lifecycle metadata store | DONE on branch (NOT merged to main) | `f8a08d4`, `88cdfdd`, `4a06ff2`, `ca615cd`, `6d953e4` |
| PR2 C1 | hooks + context + reconciler + health gate + kill-switch | **DONE** — composition root + atexit, `reconcile` CLI (dry-run default), `purge_allowed`, health gate | `6d172f2` |
| PR2 C2 | inspect product slice (inspect-v1 contract) | **DONE** — `schema="inspect-v1"`, sorted `unknown_fields`, real `would_free_bytes_estimate`, no touch | `dba1702` |
| PR2 C3 | O1 observability (redaction, structured logging, in-process registry, core schema fingerprint) | **DONE** — `log_lifecycle_event` (redacted, fail-safe), `core_schema_fingerprint` (warning-only mismatch), process-wide registry in `metrics snapshot` | `35e0ba6` |
| PR2 C4 | Adversarial test hardening (8 remaining), golden safety, kill-switch + flush-shutdown tests | **DONE** — `test_lifecycle_adversarial.py` (8 tests: boundaries, error containment, lost context, atexit flush, golden no-generate, introspective full delegate, CLI kill-switch, lock contention) | `fdef331` |
| D1 | `make smoke` Makefile fix (`--input` → positional argument) | **DONE** — `make smoke` verified exit 0 | `02f8d18` |
| PR3 (Phase 5) | PG path end-to-end: read-only PG adapter + shared contract; PG composition (isinstance bug fixed, four-way kill-switch matrix, sidecar lifecycle store, option B — zero migrations); backend-aware inspect/reconcile; `golden update` (human-only); `postgres.langgraph.json` golden; Neon runbook | **DONE on branch** (NOT merged to main) | `063e7df` (C1) → `aed8728` (C2/C3) → this commit (C4 docs) |
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

`.nexus/continuum.json` (schema v2): `test_count_expected` refreshed to
**296** on 2026-09-18 (was stale at 179); matches `pytest --collect-only -m
"not slow"`.

## Quality gates (measured 2026-09-18, `02f8d18`)

| Gate | Result |
|------|--------|
| `make lint` | green — `All checks passed!`, 211 files formatted |
| `make types` | green — `Success: no issues found in 140 source files` |
| `make test` | green — **312 passed, 16 skipped in ~24s** (PG-runtime legs skip without `NEXUS_DATABASE_URL`; covered by CI `migrate-postgres` job + local PG verification 327 passed / 0 failed) |
| `make smoke` | **fixed** — full graph via FakeLLMProvider, exit 0 |

Baseline delta: 242 → 295 passed (+53), 1 skipped (unchanged, PG-only);
mypy 134 → 140 files. No test was removed or weakened.

CI (`.github/workflows/ci.yml`): two jobs — `test` (ruff/mypy/pytest, no
service) and `migrate-postgres` (service container `pgvector/pgvector:pg16`,
runs `nexus migrate` idempotently + race regression + head assertion on
`47903d282ede` + the adapter contract suite (PG leg).

## Dependencies (core, 30 declared)

Heavy flags: `sentence-transformers` (pulls torch transitively),
`llama-cpp-python` (compiles llama.cpp C++ from source on this platform),
`chromadb`. No new core dependency was added on the branch; `cryptography`
was removed with the D8 revert; `sqlmodel==0.0.42`, `sqlalchemy==2.0.54`,
`aiosqlite==0.22.1` pinned for ABI stability. Extras: `[dev]` only.

## Known doc/spec friction

- ~~`docs/architecture/DATA_LIFECYCLE.md` described a `nexus_operation_journal`
  table~~ — **resolved**: journal references removed; the pure state machine in
  `domain/policies/retention.py` is documented as the retention policy; the doc
  now maps both real checkpoint backends (SQLite saver 2.x + LangGraph PG
  schema) empirically. The journal table remains forbidden until Stage 3.
