# REMEDIATION_MATRIX — Campaign #335 (PostgreSQL / Neon recovery)

Branch: `fix/neon-runtime-database-recovery` · PR: #335 (base `main`) · **do not merge / do not close**.

This file maps arena findings **F-001 … F-012** to root cause, code change, test, result and
residual limitation. Every "result" line names the exact command and the SHA it ran on. Nothing
here is claimed VERIFIED on the strength of CI alone; the status column distinguishes what was
actually executed locally from what is deferred or blocked.

## Status vocabulary

| Status | Meaning |
|---|---|
| **VERIFIED** | Executed locally on a real PostgreSQL 17 cluster; a mutation removes the guard and turns the test RED. |
| **EVIDENCE-BASED** | Executed locally; the behaviour is observed, but no mutation was run to prove the guard is load-bearing. |
| **EXPERIMENTAL** | Implemented and unit-tested, not yet exercised against a live backend. |
| **BLOCKED** | Cannot be executed in this environment (no authorized credential / no managed Neon). Not a pass. |
| **DEFERRED** | A concrete, scoped follow-up task exists with acceptance criteria; the current PR does not claim it done. |

## Findings

### F-001 — Neon async connection options (`sslmode`, `channel_binding`)
- **Root cause**: libpq-style URL query parameters were forwarded verbatim to `asyncpg.connect()`, which rejects them (`unexpected keyword argument 'sslmode'`).
- **Code**: `src/nexus_ai_agent/storage/db.py` — `resolve_asyncpg_connect_args()` / `to_asyncpg_url()` build an explicit allow-list, translate `sslmode` into a driver-supported `ssl` value, and fail loudly on an unsupported security option instead of dropping it.
- **Test**: `tests/unit/test_async_connection_options.py` (direct `create_connect_args()` inspection, not just `asyncpg.connect(dsn=…)`).
- **Result**: **EVIDENCE-BASED** — `pytest tests/unit/test_async_connection_options.py` green on `6f70a94c`.
- **Residual**: A live Neon `-pooler` endpoint and `channel_binding=require` were **not** exercised (no authorized credential). See *Blocked* below.

### F-002 — A failed restore must not destroy the target
- **Root cause**: `pg_dump --clean --if-exists` emits `DROP TABLE`; `psql -v ON_ERROR_STOP=1` ran without transaction atomicity, so a mid-way failure left a half-dropped database.
- **Code**: `src/nexus_ai_agent/maintenance/restore.py` — `_apply_postgres_restore()` adds `--single-transaction`; identity checks reject source == target.
- **Test**: `tests/integration/test_postgres_restore_atomicity.py` (real disposable DB, sentinel row, injected failure after the first `DROP`).
- **Result**: **VERIFIED** — `pytest tests/integration/test_postgres_restore_atomicity.py` on local PG17 = 2 passed; removing `--single-transaction` turns `test_failed_restore_leaves_target_intact` RED (`assert '' == 'SENTINEL-MUST-SURVIVE'`).

### F-003 — Credentials must not leak through logs, argv, or evidence
- **Root cause**: DSNs were printed into `DrillReport` / subprocess diagnostics; a special-case `.replace(":pg@", …)` only caught one spelling.
- **Code**: `src/nexus_ai_agent/maintenance/redact.py` (central `redact_dsn` / `redact_text` / `split_dsn_password`), wired through `restore.py`, `backup.py`, `scripts/p0_evidence.py`.
- **Test**: `tests/unit/test_maintenance_redaction.py` (poison `NEXUS_SECRET_POISON_92831` must appear nowhere).
- **Result**: **VERIFIED** — green on `6f70a94c`; neutering the URL-credential substitution turns `test_redact_dsn_covers_url_and_keyword_forms` RED.

### F-004 — Every destructive script needs an explicit, exact opt-in
- **Root cause**: the four proof scripts defaulted to a live `NEXUS_DATABASE_URL` and mutated it with no opt-in; the test modules enforced `NEXUS_PG_TEST_DESTRUCTIVE=1` but the scripts did not.
- **Code**: `src/nexus_ai_agent/maintenance/pg_safety.py` (`require_destructive_opt_in` — exact `"1"`, not truthiness; `refuse_production_target`); all four scripts call both.
- **Test**: `tests/unit/test_script_destructive_guard.py`.
- **Result**: **VERIFIED** — green on `6f70a94c`; the guard is exercised with `"0"`, `"false"`, `"yes"`, `"2"` and each must exit 2.

### F-005 — One database authority; no SQLite bypass in production
- **Root cause**: several paths could reach SQLite even when `NEXUS_REQUIRE_POSTGRES=true`.
- **Code**: `src/nexus_ai_agent/storage/db.py`, `storage/langgraph_checkpoint.py`, `api/app.py` — every resolution path calls `enforce_postgres_policy()`.
- **Test**: `tests/unit/test_db_authority_policy.py`, `tests/unit/test_database_url.py`.
- **Result**: **EVIDENCE-BASED** — green on `6f70a94c`.
- **Residual**: an explicit `db_path=` remains supported for local tests by design; proving it cannot be reached from a production call site is covered by the call-graph tests, not by a live production run.

### F-006 — Classify the six local SQLite subsystems
- **Root cause**: no machine-checkable answer to "does production depend on this state, and does it survive a restart?".
- **Code**: `src/nexus_ai_agent/maintenance/durability.py` — the single registry (`STORE_AUDIT`) and `production_durability_risks(persistent_volume=…)`.
- **Test**: `tests/unit/test_store_durability_audit.py`.
- **Result**: **EVIDENCE-BASED** — green on `6f70a94c`.
- **Residual / DEFERRED**: the audit *names* the gap (job queue, causal journal, effect outbox are authoritative local sidecars with no volume in the shipped `koyeb.yaml`) but does **not** migrate them. That migration is deliberately out of this PR's scope.

### F-007 — Restore proof must mean what it says
- **Root cause**: naive line-by-line `CREATE TABLE` detection; `expect_app_schema` was SQLite-only; a `{}` sidecar produced `CHECKSUM_VERIFIED`; a structural dry-run could be reported as `RESTORE_PROVEN`.
- **Code**: `src/nexus_ai_agent/maintenance/dump_sql.py` (SQL-aware inventory), `restore.py` (honest state machine), `backup.py`.
- **Test**: `tests/unit/test_dump_sql_tables.py`, `tests/unit/test_restore_proof_states.py`.
- **Result**: **EVIDENCE-BASED** — green on `6f70a94c`.
- **Residual**: the inventory is a structural subset, not a full SQL parser; the exact limitation is asserted by the tests rather than claimed away.

### F-008 — `/readyz` must fail closed with a structured 503, never HTTP 500
- **Root cause**: a malformed URL raised a bare `ValueError` out of `enforce_postgres_policy()`; `_database_readiness` only caught `BackendPolicyError`, so FastAPI rendered HTTP 500 (and risked echoing the credential-bearing URL).
- **Code**: `src/nexus_ai_agent/api/app.py` — the `except ValueError` branch returns `(False, "postgresql (invalid configuration): <redacted>")`.
- **Test**: `tests/unit/test_readyz_fail_closed.py`.
- **Result**: **VERIFIED** — green on `6f70a94c`; removing the `except ValueError` branch turns 4 parametrised cases RED.

### F-009 — Reproducible evidence
- **Root cause**: the bundle did not bind claims to the measured revision; a committed stamp can equal the parent commit by construction.
- **Code**: `scripts/p0_evidence.py` (per-check command + exit status; `source_revision`), `scripts/p0_type_fidelity.py`, `scripts/dr_proof.py` (respects the configured env; no overwrite).
- **Test**: `tests/unit/test_p0_evidence_bundle.py` (drift detection: the committed hashes must match the tree).
- **Result**: **EVIDENCE-BASED** — bundle regenerated against live PG17 on `7eb21c83` with `all_checks_passed=true` (all six checks exit 0); the drift test is green on `6f70a94c`.
- **Residual**: `source_revision` intentionally records the *tested* revision (the parent), not the evidence-artifact commit; `generated_at` / `source_revision` are in `non_deterministic_fields` for the byte-exact gate.

### F-010 / F-011 — CI and test safety
- **Root cause**: no real PG coverage for backup/restore; the writer inventory was hand-kept, so a new module could write to `NEXUS_DATABASE_URL` unguarded; the production image omitted `postgresql-client`.
- **Code**: `.github/workflows/ci.yml` (adds the atomicity module to `migrate-postgres`), `Dockerfile` (`postgresql-client`), `tests/unit/test_pg_integration_safety.py` (auto-discovery of writers).
- **Test**: `tests/unit/test_pg_integration_safety.py`, `tests/unit/test_script_destructive_guard.py`.
- **Result**: **VERIFIED** — green on `6f70a94c`; the hybrid-PG-leg guard is AST-shape checked and weakening `PG_ENABLED` to the DSN alone turns it RED.
- **Residual**: the inventory is a scanner over `tests/integration/test_*.py`; a writer in another directory is still invisible (documented in the module docstring).

### F-012 — Large-artifact scalability + no hidden/deferred prior failure
- **Root cause**: `_verify_postgres_dump` did `path.read_bytes()`; dump-table parsing built an unbounded in-memory map.
- **Code**: `src/nexus_ai_agent/maintenance/backup.py` (streaming `_last_meaningful_line`), `restore.py` (bounded header read), `dump_sql.py` (bounded scan with `truncated` flag).
- **Test**: `tests/unit/test_dump_verification_scaling.py` (the whole file must never be read into memory).
- **Result**: **EVIDENCE-BASED** — green on `6f70a94c`.
- **Residual**: this file is itself the F-012 "no later wave hides a prior critical failure" record — every deferred item above is listed with its blocker, not omitted.

## Commands executed (exact, with SHA)

| Command | Env | SHA | Result |
|---|---|---|---|
| `pytest tests/unit/test_pg_integration_safety.py` | no DB | `6f70a94c` | 15 passed |
| `pytest tests/integration/test_postgres_restore_atomicity.py` | PG17 + opt-in | `6f70a94c` | 2 passed |
| `pytest` (adapter/lifecycle/composition) | PG17 + opt-in | `6f70a94c` | 46 passed |
| `pytest` (same three) | DSN only, **no** opt-in | `6f70a94c` | 23 passed, 18 skipped |
| `pytest` (same three) | no DB at all | `6f70a94c` | 23 passed, 18 skipped |
| `pytest` (7 PG integration modules) | PG17 + opt-in | `6f70a94c` | 58 passed |
| `pytest tests/unit tests/architecture -m "not slow"` | no DB | `6f70a94c` | 5953 passed, 13 skipped, 0 failed |
| `python scripts/p0_evidence.py` | PG17 + opt-in | `7eb21c83` | exit 0, `all_checks_passed=true` |

Mutation proofs (guard removed → RED): F-002 (sentinel destroyed), F-008 (4 cases raise),
F-003 (credential survives), F-011.6 (DSN-only re-arms the write).

## Blocked / not executed

- **Live Neon** (`ep-*.neon.tech`, `-pooler`): **BLOCKED** — no authorized credential; the sandbox
  is forbidden from aiming tests at Neon Production. Not a pass.
- **PostgreSQL 16 vs 17 matrix**: only PG17 was exercised locally; CI uses `pgvector/pgvector:pg16`.
- **`channel_binding=require`** against a real server: **BLOCKED** with the Neon item above.

## Unresolved production dependencies

- The authoritative local SQLite sidecars named in F-006 still have no external durable authority
  (job queue, causal journal, effect outbox). A volume-less scale-to-zero deploy loses them.
- The restore path commits before an independent verifier signs off; promotion is not yet
  transaction-atomic at the storage boundary.

## Verdict

Findings with a **VERIFIED** guard: F-002, F-003, F-004, F-008, F-010/F-011 (test safety).
Findings **EVIDENCE-BASED** but without a mutation proof: F-001, F-005, F-006, F-007, F-009, F-012.
**BLOCKED** (never claimed green): live-Neon connection behaviour.

This is an implementation branch for independent re-audit. It is **not** merged and must **not** be
merged or closed by this work.

*Generated by an AI agent (OpenHands) on behalf of the repository owner.*
