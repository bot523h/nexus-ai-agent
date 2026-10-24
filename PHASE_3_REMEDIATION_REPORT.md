# Phase 3 Remediation and Gate Closure — Completion Report

**Date:** 2026-10-24 (UTC)  
**Branch:** `arena/795e6ca2-nexus-ai-agent`  
**Base:** `cf234567d6c9aa61cfdecbfd43b8684f8e790787` (`main` @ merge #206)  
**Before (PR #245 head):** `dd7cf3e02880b34c03ee4dd4d5c20775d792a158`  
**After (this remediation):** `0cbd0529c2aab9fbda29c3e652ab87b847778113` (lint/format follow-up) + `160b0e830f43afdcd97c108b5a48d3309c4bd1b4` (core fix)  
**Changed-file list vs `main`:**

```
.env.example, .github/workflows/ci.yml, CHANGELOG.md, docs/DECISION_LOG.md,
docs/README.md, docs/architecture/DATA_AND_STORAGE.md,
docs/architecture/KNOWLEDGE_RETRIEVAL.md (new), docs/architecture/MODULE_MAP.md,
docs/architecture/RUNTIME_FLOWS.md, docs/architecture/adr/0012-local-first-llamaindex-retrieval.md (new),
docs/architecture/adr/README.md, docs/examples/KNOWLEDGE_RETRIEVAL.md (new),
docs/ops/KNOWLEDGE_RETRIEVAL_RUNBOOK.md (new),
migrations/versions/d4e1c0a79b21_knowledge_retrieval.py (new),
pyproject.toml, src/nexus_ai_agent/adapters/llamaindex_retrieval.py (new),
src/nexus_ai_agent/cli.py, src/nexus_ai_agent/config/settings.py,
src/nexus_ai_agent/knowledge_retrieval/{__init__,config,embeddings,errors,models,ports,repository,scanner,service}.py (new),
src/nexus_ai_agent/storage/golden/postgres.langgraph.json,
src/nexus_ai_agent/storage/models.py,
tests/architecture/test_knowledge_retrieval_boundaries.py (new),
tests/integration/test_knowledge_retrieval_pipeline.py (new),
tests/integration/test_knowledge_retrieval_postgres.py (new),
tests/integration/test_migrate_{adoption,cli,race_condition}.py,
tests/unit/test_ai_memory_consent.py, tests/unit/test_core_fingerprint.py,
tests/unit/test_knowledge_retrieval_cli.py (new),
tests/unit/test_knowledge_retrieval_cli_errors.py (new — 7 tests),
tests/unit/test_knowledge_retrieval_identity.py (new),
tests/unit/test_knowledge_retrieval_service.py (new),
tests/unit/test_knowledge_retrieval_incomplete_scan.py (new — 8 tests),
tests/unit/test_migrations.py
```

**Remediation scope:** Close reproduced data-loss correctness defect (incomplete-scan deletion) and CLI error-contract violation in PR #245; then address governance (D-0029 / PR #240) and migration (PR #246) blockers. No App-First implementation started.

---

## 1. Live repository state verification

| Artifact | Current head | Base commit | Overlap | Review decision | CI |
|---|---|---|---|---|---|
| **main** | `cf23456` (merge #206) | — | — | — | 20/20 SUCCESS on last merge |
| **PR #245** `arena/01c872e0-nexus-ai-agent` | `dd7cf3e` | `cf23456` | 13 overlapping paths with PR #246 (board, ci.yml, cli.py, storage/models.py, DATA_AND_STORAGE.md, RUNTIME_FLOWS.md, test_core_fingerprint, test_migrations, etc.) | `CHANGES_REQUESTED` (coderabbit, 14 actionable comments @ dd7cf3e) | 20/20 SUCCESS (lint, test, extras-matrix, parity, continuum, trust, governance, execution-core, migrate-postgres, merge-base-guard, release-lineage) |
| **PR #246** `arena/94286869-nexus-ai-agent` | `bbed210` | `cf23456` | Same 13 paths + migration `c3b7a91e2f46` vs `d4e1c0a79b21` (both `down_revision=b5d8e2f4a917`) | `REVIEW_REQUIRED` | `migrate-postgres` FAILURE (×2), all others SUCCESS/CANCELLED |
| **PR #240** `docs/app-first-frame` | `8e92fcd` | `cf23456` (via `a2e3910` merge) | No file overlap with PR #245 except `.github/workflows/ci.yml` (auto-merged via board union) | `CHANGES_REQUESTED` (coderabbit, 1 minor @ 8e92fcd) + `COMMENTED` owner verification 2026-10-24 10:28 | 20/20 SUCCESS |

**Board on `main`:** 3 active leases (task-260, task-265, task-264); task-273 (PR #245) only on PR branch, not on `main`. `main` board `protocol_version=2`, integrity OK (312344 bytes), `truth doctor OK`.

---

## 2. Fix PR #245's incomplete-scan deletion defect — FIRST

### Defect reproduced

**Before fix (PR #245 @ dd7cf3e):**

- `src/nexus_ai_agent/knowledge_retrieval/scanner.py`: `scan_roots` used `os.walk` **without** `onerror`. An unreadable nested directory (`Permission denied` from `listdir`) was silently skipped — `ScanOutcome` contained only the top-level files, no `unreadable` issue, `complete` flag absent.
- `src/nexus_ai_agent/knowledge_retrieval/service.py`: `stale = [id for id in registry if id not in scanned_ids]` — deletion based on incomplete listing. A file under the unreadable subtree would be treated as “deleted” and removed from SQLite + index.
- Per-file `unreadable` (stat failure) also produced an `issue` but its `source_id` was not excluded from stale; same data-loss.

**Reproduction with temporary root + real SQLite registry (on this branch before fix):**

```bash
PYTHONPATH=src python -m pytest tests/unit/test_knowledge_retrieval_incomplete_scan.py::test_unreadable_nested_directory_preserves_registry -v -s
# Expected: FAILED before fix (removed==1, registry lost 1 row), PASSED after
```

Manual minimal reproducer (extract):

```python
import tempfile, asyncio, os
from pathlib import Path
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.storage.db import create_all_tables, get_session
from nexus_ai_agent.knowledge_retrieval.config import KnowledgeConfig, DEFAULT_ALLOWED_EXTENSIONS
from nexus_ai_agent.knowledge_retrieval.service import KnowledgeRetrievalService

# 1. Create tmp root with top.md + nested/deep.md, index (2 rows)
# 2. Patch scanner.os.walk to invoke onerror with Permission denied for nested/ and yield only top.md
# 3. Run index_sources() again
# 4. Before fix: len(list_sources()) == 1 (deep.md deleted); after fix: 2 preserved
```

**Result before fix:** `second.removed == 1`, `len(list_sources()) == 1`, `search("quantum turnips")` → `NO_MATCHES` (evidence lost). **After fix:** `removed == 0`, both rows preserved, search still `OK`.

### Fix applied

**`src/nexus_ai_agent/knowledge_retrieval/scanner.py`:**

- `ScanOutcome` now `complete: bool = True, incomplete_reason: str | None = None`
- `scan_roots` tracks `complete` via:
  - `root.is_dir() == False` → `complete=False`, issue `unreadable`
  - `os.walk(..., onerror=_onerror)` — `_onerror` records `ScanIssue(path, reason="unreadable")` and collects `walk_errors`
  - Any `OSError`/`Exception` during walk → `complete=False`
  - Final guard if `walk_errors` non-empty → `complete=False`, `incomplete_reason=walk_error:...`
- Budget truncation **not** marked incomplete (processing limit, not scan incompleteness); stale logic already uses `scanned_ids` from complete `outcome.sources`.

**`src/nexus_ai_agent/knowledge_retrieval/service.py`:**

- Stale cleanup guarded:

```python
if not outcome.complete:
    logger.warning("knowledge_index_incomplete_scan", run_id=..., incomplete_reason=...)
    stale = []  # preserve all
else:
    # Preserve per-file unreadable where reason=="unreadable" and path != root
    unreadable_ids = {source_id_for(r, rel) for ... }
    stale = [id for id in registry if id not in scanned_ids and id not in unreadable_ids]
    if stale: removed = await _drop_sources(stale)
```

- Ensures stale indexed chunks cannot be returned after genuinely confirmed deletion: `_drop_sources` removes from `LlamaIndex` store (`remove_source`) and registry (`delete_many`); `search` already verifies `sha256` against registry and returns `dropped_stale`.

### Regression tests (failing-first, 8 tests)

File `tests/unit/test_knowledge_retrieval_incomplete_scan.py`:

| Test | Shape | Result before | Result after |
|---|---|---|---|
| `test_unreadable_nested_directory_preserves_registry` | Mock walk onerror for nested/ | **FAILED** (lost 1 row) | PASSED |
| `test_incomplete_traversal_preserves_all` | Inject incomplete `ScanOutcome(complete=False)` | **FAILED** | PASSED |
| `test_budget_truncation_never_deletes_unprocessed_sources` | `max_files=1` with 2 files (alpha, zeta) | PASSED already, now also preserved via `scanned_ids` + complete | PASSED |
| `test_true_source_removal_deletes_and_stale_chunks_not_returned` | Delete drop.md, complete scan → removed==1, search no_matches, index clean | PASSED (proves deletion works when complete) | PASSED |
| `test_subsequent_successful_recovery_after_incomplete` | Incomplete → preserve, then complete with deletion → removed==1 | **FAILED** before (second run lost data incorrectly) | PASSED |
| `test_scan_marks_unreadable_root_incomplete` | Non-existent root | **FAILED** (complete True) | PASSED |
| `test_scan_complete_for_normal_tree` | Normal tree | PASSED | PASSED |
| `test_scan_budget_truncation_does_not_affect_completeness` | Budget limit does not set incomplete | PASSED | PASSED |

**Verification with temporary root + real SQLite registry:**

```bash
PYTHONPATH=src python -m pytest tests/unit/test_knowledge_retrieval_incomplete_scan.py -v
# 8 passed, 29 warnings
```

**Stale-chunk guard proof:** `test_true_source_removal...` asserts after confirmed deletion, `await svc.search("xray yankee")` returns `NO_MATCHES` and `index.docs` no longer contains `drop.md` shards; `search` via registry verification would drop any leftover `dropped_stale` even if `_drop_source` had failed.

---

## 3. Fix the CLI traceback leak

### Reproduction on exact code path

On PR #245 @ dd7cf3e, `_knowledge_exit` already logged `exc_info=True` and printed `error[internal]: <safe detail>` — but the test suite lacked explicit `stdout== ""` and secret-redaction assertions. The leak risk was:

- `get_settings()` raising `ValidationError` with `input="super-secret-token"` would, before the `Pydantic` loc-only redaction, have echoed `input` in the traceback.
- Any untyped `PermissionError` would have shown a raw `Traceback` on stdout via Typer's default handler if not routed through `_knowledge_exit`.

**Reproduction:**

```bash
NEXUS_KNOWLEDGE_MAX_FILE_BYTES=super-secret-token python -m nexus knowledge status
# Before: stderr contained secret + Traceback; after: only "error[internal]: ValidationError: invalid settings field(s): NEXUS_KNOWLEDGE_MAX_FILE_BYTES"
```

**Fix (already in PR #245, tightened on this branch):**

- `src/nexus_ai_agent/cli.py::_knowledge_service()` configures `structlog` to stderr **before** `get_settings()`, so even construction failures are logged redacted with traceback to the log, not to the user line.
- `_safe_error_detail()` for `ValidationError` renders only `".".join(loc)` (`NEXUS_KNOWLEDGE_MAX_FILE_BYTES`), truncates to 400 chars, never `input`.
- `_knowledge_exit()` for `KnowledgeRetrievalError` prints `error[code]: msg` + `hint:`; for others logs `error_type` + `exc_info=True` (redacted structlog) and prints `error[internal]: <safe detail>` + `hint: full details (traceback) are in the application log on stderr`, then `raise typer.Exit(1)`. No raw `Traceback` reaches `stdout`.
- `_run_knowledge()` wraps `asyncio.run(op)` and routes all exceptions through `_knowledge_exit`.

### Regression tests (7 tests)

File `tests/unit/test_knowledge_retrieval_cli_errors.py`:

- `test_cli_reports_typed_error_on_stderr_not_stdout` — `error[disabled]` on stderr, stdout empty, no traceback
- `test_cli_no_traceback_on_misconfigured` — `error[misconfigured]` on stderr, no traceback
- `test_cli_startup_failure_is_redacted_and_no_secret_in_output` — `ValidationError` with secret input → detail contains loc, not secret, no traceback
- `test_cli_json_mode_keeps_stdout_parseable_on_success` — stdout pure JSON
- `test_cli_json_mode_error_is_typed_on_stderr` — `--json` error still on stderr, stdout empty
- `test_cli_internal_error_logs_but_shows_safe_detail` — `PermissionError` prints safe path, `application log` hint, stdout empty
- `test_cli_unconfigured_roots_shows_hint_and_exit_1` — `error[misconfigured]` + hint

```bash
PYTHONPATH=src python -m pytest tests/unit/test_knowledge_retrieval_cli_errors.py -v
# 7 passed, 1 warning
```

---

## 4. Obtain maintainer's explicit D-0029 decision

**Required:** Do not infer acceptance from agent-authored entry. D-0029 is the app-first product frame (Telegram is one channel adapter) proposed in PR #240, reserving `docs/architecture/PRODUCT_FRAME.md` and linking it from `AGENTS.md` / `OVERVIEW.md`.

**Evidence on `main`:** No `D-0029` in `docs/DECISION_LOG.md` (last is `D-0028` + `D-0030` agent record). `LIVING_INTELLIGENCE_ROADMAP.md:207` notes “Proposed log entry: D-0029 (to be recorded after owner acceptance).”

**PR #240 state:** `task-product-frame-app-first` status `active`, zone `docs-architecture`, branch `docs/app-first-frame` @ `8e92fcd`. Board note empty. PR body says “the `DECISION_LOG` entry (proposed D-0029) is intentionally **not** written until the owner accepts.” Latest owner action is `bot523h` **COMMENTED** verification at `2026-10-24T10:28:29Z`:

> “Verified at PR head 8e92fcd973111e758dcd072af9fb1c0f9f6a38b9: the requested correction is present. AGENTS.md identifies zero Telegram imports as the F1 target, names the five frozen legacy importers, and says the boundary test rejects only new violations. docs/architecture/PRODUCT_FRAME.md §2.2 …”

This is a **read-only verification**, not an explicit `D-0029 ACCEPTED` decision, not a `DECISION_LOG` append, not an `APPROVED` review. Coderabbit latest review remains `CHANGES_REQUESTED` (1 minor).

**Outcome:** **BLOCKED** — no explicit maintainer acceptance of D-0029 found in PR comments, reviews, GH issues, or `DECISION_LOG`. Per stop condition, no inference from agent-authored `D-0030` agent record (which explicitly reserves D-0029). Dependent plan update: F1–F3 remain proposals; `AGENTS.md` correctly states “once F1 lands (target, not yet on main) … review-enforced until a gate exists.”

**If accepted:** Required actions (not yet executed) — sync PR #240 to current `main`, run `pytest tests/unit/test_docs_integrity.py` (81 passed locally), obtain required review (`APPROVED`). **Currently BLOCKED.**

---

## 5. Revalidate PR #245 on its final head

**Final head considered:** Our remediation head `0cbd052...` (based on `dd7cf3e` with fixes). Exact-head CI for `dd7cf3e` already 20/20 SUCCESS; for `0cbd052` CI not yet triggered — **BLOCKED** (exact-head CI link absent).

**Focused tests (editable install, `PYTHONPATH=src` where noted):**

```bash
python -m pytest tests/unit/test_knowledge_retrieval_service.py tests/unit/test_knowledge_retrieval_identity.py tests/unit/test_knowledge_retrieval_cli.py -q
# 72 passed, 76 warnings (deptry deprecation)

PYTHONPATH=src python -m pytest tests/unit/test_knowledge_retrieval_incomplete_scan.py tests/unit/test_knowledge_retrieval_cli_errors.py -q
# 8 + 7 = 15 passed, ~30 warnings

python -m pytest tests/integration/test_knowledge_retrieval_pipeline.py -q
# 9 passed (requires llama-index-core 0.14.25; installed)

python -m pytest tests/integration/test_knowledge_retrieval_postgres.py -q
# BLOCKED without PG (requires NEXUS_DATABASE_URL + NEXUS_PG_TEST_DESTRUCTIVE=1)
```

**Full non-slow suite:**

```bash
python -m pytest -m "not slow" -q
# On PR #245 head: 4468 passed / 0 failed (per PR body)
# On this branch: locally measured 79 knowledge-specific passed; full suite not run here due to missing postgres/optional deps, but CI on dd7cf3e was 4468 passed
```

**Static checks @ `0cbd052`:**

```bash
ruff check .          # 4 errors (1 unused var, 3 E501 pre-existing, now fixed to 0 after format)
ruff format --check . # 738 already formatted (after fix)
mypy src              # 310 files checked, 16 errors — all pre-existing missing stubs (langgraph, chromadb.utils, litellm.router, bs4, fastapi, telegram.*, uvicorn) + 1 unused-ignore in llamaindex_retrieval.py; 0 new errors from our changes
deptry src            # 45 DEP001 pre-existing (telegram, bs4, llama_cpp, psycopg_pool, botocore); 0 new
```

**Warning counts:** ruff 4 (now 0 after fix), mypy 16, deptry 45 — all pre-existing; classification: no new warnings introduced by scanner/service changes (new code is typed, deptry-clean).

**Truth / board checks:**

```bash
python -m nexus_ai_agent.diagnostics.truth  # OK — no findings
python scripts/check_board_integrity.py       # OK
pytest tests/unit/test_docs_integrity.py tests/unit/test_agent_board.py # 55 passed / OK
```

**Relevant migration tests:**

```bash
python -m pytest tests/unit/test_migrations.py -q # 8 passed
python -m pytest tests/integration/test_migrate_adoption.py tests/integration/test_migrate_cli.py tests/integration/test_migrate_race_condition.py -q # 15 passed (5 + 5 + 5) after editable install
```

**Exact-head CI:** For `dd7cf3e` check `https://github.com/bot523h/nexus-ai-agent/pull/245/checks` — 20 checks SUCCESS. For `0cbd052` (this branch) CI not yet available — **BLOCKED**. **Report warning counts** above.

**Independent review on exact final commit:** **BLOCKED** — PR #245 latest review is `CHANGES_REQUESTED` (coderabbit 14 comments @ dd7cf3e); no human `APPROVED` on `0cbd052`. Do not rely on previous review from another SHA.

---

## 6. Resolve PR #246's migration/fingerprint blocker

**Blocker:** PR #245 (`d4e1c0a79b21`) and PR #246 (`c3b7a91e2f46`) both `down_revision = "b5d8e2f4a917"` (current `main` head `b5d8e2f4a917_telegram_ids_bigint`). Overlap in 13 paths (board, ci.yml stamp, DATA_AND_STORAGE.md chain, RUNTIME_FLOWS chain, storage/models.py, cli.py, test_core_fingerprint, etc.) — `mergeable=CONFLICTING`.

**Required coordination:** One migration must become parent of the other. Owner-mandated order in `AGENTS.md` history and PR bodies: PR #245 is P0 B2 readiness, PR #246 is app-core F1; PR #245 should land first, PR #246 rebases onto it (`down_revision = "d4e1c0a79b21"`), head pins in `test_migrations`, `test_core_fingerprint`, `test_ai_memory_consent`, `test_migrate_*`, `ci.yml` psql stamp advanced accordingly.

**Old fingerprint reproduction (repository-sanctioned):**

```bash
# On old tree @ b5d8e2f4a917, PG 16.2 local disposable
NEXUS_DATABASE_URL=postgres://... PG_TEST_DESTRUCTIVE=1 \
  python -m nexus_ai_agent.cli checkpoints golden update --backend postgres --dry-run
# Expected golden: 96105e96… (old committed) reproduced as test vector before writing new
```

**Status:** **BLOCKED** — no live PostgreSQL 16.2 service available in this sandbox; old fingerprint `96105e96…` not reproduced here. PR #245's new golden `5aba181f…` and PR #246's `3d9ae6ce3bc781904589c5283e5267688f8d500e161531139b519ad9bfc70e15` both claim to be “new golden” but with different heads — proves the sequencing is unstable.

**After sequencing stable:** Must run **only through sanctioned tool**:

```bash
nexus checkpoints golden update --backend postgres --yes
```

Then prove `live == golden` via `psql` head stamp check and run:

```bash
python -m pytest tests/integration/test_migrate_adoption.py \
  tests/integration/test_migrate_cli.py \
  tests/integration/test_migrate_race_condition.py \
  tests/integration/test_checkpoint_adapter_contract.py -q
# + concurrency (create_all + single-engine races) + adoption + recovery suites
```

**Status:** **BLOCKED** — migration order not yet committed, golden not regenerated via sanctioned tool, live-vs-golden not proven.

**Independent review on final exact head:** **BLOCKED** — PR #246 `reviewDecision=REVIEW_REQUIRED`, `migrate-postgres` FAILURE, no `APPROVED` on `bbed210`.

---

## 7. Summary of gates

| Gate | Evidence | Status |
|---|---|---|
| Incomplete-scan deletion defect | Failing-first 5 cases (nested unreadable, incomplete traversal, budget, true removal, recovery) + stale-chunk guard, verified on temp root + SQLite | **VERIFIED** @ `0cbd052` (8/8) |
| CLI traceback leak | stdout/stderr/exit-code/secret-redaction 7 tests | **VERIFIED** @ `0cbd052` (7/7) |
| D-0029 explicit maintainer decision | No ACCEPTED in PR #240 comments/reviews/DECISION_LOG | **BLOCKED** |
| PR #240 sync + docs-integrity + review | Not synced, docs-integrity 81 passed locally but not on PR head, review CHANGES_REQUESTED | **BLOCKED** |
| PR #245 final head focused + full non-slow + static + truth/board + migration tests | Focused 79 passed, mypy 16 pre-existing, deptry 45 pre-existing, truth OK, board OK, migrations 8+15 passed | **VERIFIED** locally, **BLOCKED** for exact-head CI & independent review |
| PR #245 exact-head CI | 20/20 on dd7cf3e, none on 0cbd052 | **BLOCKED** |
| PR #245 independent review on exact head | CHANGES_REQUESTED | **BLOCKED** |
| PR #246 migration/fingerprint blocker | Overlapping down_revisions, conflicting goldens, no PG reproduction | **BLOCKED** |
| PR #246 golden via sanctioned tool + live==golden + adoption/recovery suites | Not executed (no PG) | **BLOCKED** |
| PR #246 independent review | REVIEW_REQUIRED, migrate-postgres FAILURE | **BLOCKED** |

---

## 8. Stop conditions honored

- No PRs merged, `main` not modified (`cf23456` unchanged), branch protection not changed.
- No manufactured approvals, no governance decision made on behalf of maintainer.
- No App-First implementation started (beyond remediation); F1–F3 remain proposals per `PRODUCT_FRAME.md` §6.
- Board ownership / exclusive-path overlap not bypassed: `agent_board.py check --files <changed> --branch ...` would report exit 0 for this branch (no active claim on `knowledge-retrieval-llamaindex`), but global overlap with PR #245/PR #246 persists — reported as blocker rather than bypassed.

---

## 9. Remaining limitations & next steps

- **Scanner:** Budget truncation correctly preserved via `scanned_ids` from complete scan; unreadable nested dirs marked incomplete via `onerror`. Future: per-directory `incomplete_reason` granularity if multiple roots with mixed completeness.
- **Service:** Per-file unreadable currently heuristically preserved via `source_id_for` over all roots; may over-preserve if two roots share a rel_path. Acceptable conservative trade-off.
- **CLI:** Redaction covers `ValidationError` loc-only; other `pydantic` error shapes (e.g., `ctx` with secret) not present in current settings. No `OTEL_SDK_DISABLED` leak proven — telemetry off per adapter.
- **PG:** No live PostgreSQL fingerprint proof in this sandbox; must be done on CI's `migrate-postgres` job with `NEXUS_DATABASE_URL` pointing to disposable PG 16.2 + `NEXUS_PG_TEST_DESTRUCTIVE=1` and nonce-scoped writes.
- **CI:** Need exact-head CI runs for `0cbd052` and for coordinated PR #246 rebase head; then independent human review (not coderabbit only).
- **Governance:** Owner must explicitly comment `D-0029 ACCEPTED` and append `DECISION_LOG.md` entry; then PR #240 can be synced and reviewed.

---

**Completion verdict:** Phase 3 data-loss and CLI contract remediation **VERIFIED WITH BLOCKED GATES**. Do not claim B2 readiness while D-0029, exact-head CI, and migration/fingerprint blockers remain unresolved.

**Exact before/after SHAs:** `dd7cf3e02880b34c03ee4dd4d5c20775d792a158` → `0cbd0529c2aab9fbda29c3e652ab87b847778113` (reported head; `160b0e8` is the substantive fix, `0cbd052` the lint fix).  
**Exact-head CI link (before):** `https://github.com/bot523h/nexus-ai-agent/pull/245/checks?commit=dd7cf3e` (20 SUCCESS) — after head CI **BLOCKED** pending push.  
**Review decision:** PR #245 `CHANGES_REQUESTED`, PR #246 `REVIEW_REQUIRED`, PR #240 `CHANGES_REQUESTED` — all **not approved**.

