# Forensic Architecture Audit & Maintenance Truth Report
**Date:** 2026-10-05
**PR:** #158 (`bot523h/nexus-ai-agent`)
**Head Commit SHA:** `d68703b1d99bb31f5cac14c73ac93f833d979d84` (updated with remediation)
**Base Commit SHA:** `main @ 6e41123b40f15a63241c8db27cd884010f55db38`

---

## 1. Executive Summary & Status Classification

This forensic pass was conducted autonomously to verify, harden, and audit the repository architecture across execution boundaries, database persistence, time handling, CI/CD supply chain, and governance rules.

### Overall Status: **HARDENED_BUT_NOT_COMPLETE** (Production Blockers Identified in Governance & Legacy Storage Duplication)

- **Datetime/UTC Contract:** **VERIFIED** — Deprecated `datetime.utcnow()` removed across all shipped models and code. Replaced with explicit `naive_utcnow()` helper (for naive DB columns) and `datetime.now(timezone.utc)` for aware timestamps. AST architecture test `tests/architecture/test_datetime_hygiene.py` enforces zero `datetime.utcnow()` in `src/`.
- **SQLite Concurrency & Lock Resilience:** **VERIFIED** — Disentangled transient SQLite lock retries on `PRAGMA journal_mode=WAL` (`_is_sqlite_lock_conflict`) from DDL duplicate schema creation races (`_is_concurrent_create_conflict`). Eliminated broad substring matching (bare `"locked"`).
- **PostgreSQL Integration & Timestamp Parity:** **VERIFIED** — Added `tests/integration/test_aimemory_postgres.py` proving `AIMemoryEngine` consent grant/revoke/save logic and naive UTC timestamp compatibility on PostgreSQL.
- **CI / Supply Chain:** **VERIFIED** — Removed legacy Node 20 workaround `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24`. First-party GitHub Actions remain pinned to immutable commit SHAs.
- **Governance Inspection:** **PRODUCTION_BLOCKER** — Main branch rulesets and protection require formal verification/enforcement.
- **ORM & Persistence Forensics:** **HARDENED_BUT_NOT_COMPLETE** — Identified duplicate `Referral`/`ReferralCode` model declarations between `storage/models.py` and `features/referral.py`, and isolated SQLite persistence in `ConversationStore`.

---

## 2. Forensic Findings & Architectural Audit

### Finding A: Duplicate ORM Model Declarations (`Referral` & `ReferralCode`)
* **Location:** `src/nexus_ai_agent/storage/models.py` vs `src/nexus_ai_agent/features/referral.py`
* **Severity:** Medium / Architectural Debt
* **Root Cause:** Early feature development introduced standalone SQLModel classes inside `features/referral.py` with `__table_args__ = {"extend_existing": True}`, while central SQLModel metadata registered duplicate definitions in `storage/models.py`.
* **Blast Radius:** Minor runtime confusion during `SQLModel.metadata.create_all()` or Alembic auto-generation, where conflicting field definitions or defaults could cause schema drift.
* **Remediation Recommendation (NOW/NEXT):** Standardize `features/referral.py` to import models directly from `storage.models` rather than re-declaring SQLModel table classes locally.

### Finding B: Competing Persistence Substrates (`ConversationStore`)
* **Location:** `src/nexus_ai_agent/features/conversation_store.py`
* **Severity:** Medium
* **Root Cause:** `ConversationStore` manages its own synchronous SQLite engine (`sqlite:///data/app.sqlite`) and raw SQL queries outside the async SQLAlchemy/SQLModel session pool in `storage/db.py`.
* **Blast Radius:** Higher risk of SQLite database locks under high concurrency because raw synchronous connection pools bypass `storage/db.py` retry mechanisms and session management.
* **Remediation Recommendation (NEXT):** Refactor `ConversationStore` to use the central `get_session()` async context manager from `storage/db.py`.

### Finding C: Python 3.12+ `datetime.utcnow()` Deprecation Risk
* **Location:** `src/nexus_ai_agent/storage/models.py`, `src/nexus_ai_agent/agent/updater.py`, `src/nexus_ai_agent/features/ai_memory.py`
* **Severity:** Medium (Resolved in PR #158)
* **Root Cause:** Legacy calls to `datetime.utcnow()` produced Python 3.12 `DeprecationWarning`s and mixed naive/aware datetimes during comparison in `AutoUpdater`.
* **Remediation Applied (VERIFIED):** Centralized time policy using `integrations.external.naive_utcnow()` for SQLModel default factories and `datetime.now(timezone.utc)` for aware time math. AST test added in `tests/architecture/test_datetime_hygiene.py`.

### Finding D: Broad Exception Matching in SQLite Retries
* **Location:** `src/nexus_ai_agent/storage/db.py`
* **Severity:** Low/Medium (Resolved in PR #158)
* **Root Cause:** Broad matching on bare `"locked"` caused DDL conflict handlers to accidentally retry non-DDL locks or mask test assertions.
* **Remediation Applied (VERIFIED):** Separated `_is_sqlite_lock_conflict` (for `PRAGMA journal_mode=WAL`) from `_is_concurrent_create_conflict` (for duplicate schema object creation).

### Finding E: Governance & Main Branch Protection
* **Location:** Repository Settings / Governance Rulesets
* **Severity:** High / Production Blocker
* **Finding:** Branch protection rules and required status checks for `main` must be verified and enforced via GitHub repository rulesets to prevent unverified direct pushes or bypasses.

---

## 3. Classification Summary (NOW / NEXT / LATER)

| Action | Classification | Description |
|---|---|---|
| UTC & `datetime.utcnow` elimination | **NOW (Delivered)** | Standardized time policy and added AST hygiene test. |
| SQLite Lock & DDL Race Separation | **NOW (Delivered)** | Precision exception classification in `storage/db.py`. |
| CI Node 24 Clean-up | **NOW (Delivered)** | Removed `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24` in `ci.yml`. |
| PostgreSQL Integration Test | **NOW (Delivered)** | Added `tests/integration/test_aimemory_postgres.py`. |
| Deduplicate `Referral` Models | **NEXT** | Consolidate `features/referral.py` to import `storage.models`. |
| Migrate `ConversationStore` to `storage/db.py` | **NEXT** | Unify SQLite connection management under `get_session()`. |
| Main Branch Ruleset Enforcement | **LATER / Governance** | Configure GitHub Rulesets for strict status check enforcement. |

---

## 4. Mechanical Proofs & Test Invocations

The following commands were run locally to verify the changes:

```bash
# 1. Architecture Datetime Hygiene Guard
.venv/bin/pytest tests/architecture/test_datetime_hygiene.py -v

# 2. SQLite Concurrent Schema & PRAGMA Retry Race Tests
.venv/bin/pytest tests/unit/test_create_all_race.py -v

# 3. PostgreSQL AIMemory Integration Test
.venv/bin/pytest tests/integration/test_aimemory_postgres.py -v

# 4. Linter and Type Checks
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/ tests/unit/test_create_all_race.py
```

All local verification checks passed with **zero errors**.
