# PR#33 forensic dedupe — task-123 slim-down to the unique remainder

**Date:** 2026-10-08 · **Branch (canonical identity):** `arena/01a0c3aa-nexus-ai-agent` · **Claim:** `task-123` in `.agents/board.json`

| Evidence field | Value |
|---|---|
| Zone | `packaging+interop` |
| Start SHA (branch head at takeover) | `8f2029e75a83eba87edf878b2dc5233d103a6eab` |
| Original PR base | `e80b742d9f15d610c589199729add487a9f52182` |
| Live `origin/main` at takeover | `440d29036c2df3e4f67b9d3f34e686930ea1e8a4` |
| Merge-base (original head × live main) | `978ae161f90349a95f29aef9ab30cd98eb581760` |
| Vehicle commits | `967791c` (tree reset to live main), `e6ac7c0` (absorb main as first-class parent), `618f586` (board claim, pushed) |
| Push verification | `git ls-remote origin refs/heads/arena/01a0c3aa-nexus-ai-agent` → `618f58648d52620ec71e29e699ed930b7fea006f` (claim); remainder head recorded in the board note after this push |
| Live GitHub verification | PR #33: `state=OPEN`, head = this branch, `changedFiles` computed against live main (claim-only at `618f586`; remainder re-checked after the remainder push) |
| Final status | `VERIFIED` for the reconciled remainder on the branch; delivery = `BRANCH_PROVEN` (merge remains gated by the owner/gates rule — merging is out of scope for this claim) |
| Residual risk | PR remains CONFLICTING-free at the tree level but merge is still an owner decision; `[rag]`/`[local-llm]` extras-matrix legs are declared and install locally, but their full CI legs have not run in this sandbox (limitation recorded, not hidden) |

## 1. Method (no blind rebase)

1. **Inventory** — 52 files, +4351/−207 at `8f2029e`; commit split: `230bfa8` (board chore), `96fac7f` (security+wiring), `3b0c670` (merge), `7a6ade2` (packaging+interop), `0a8e443`/`b1962d9`/`8f2029e` (core-only CI fixes).
2. **Semantic classification** — every file diffed three ways (`base→PR`, `base→main`, `main→PR`) and classified **superseded / conflicting / unique** by reading the hunks, never "looks similar".
3. **Vehicle** — because a force-push or history rewrite is forbidden, the branch *absorbed* live main as a real merge parent (`e6ac7c0`) with the tree reset to main (`967791c`); GitHub therefore computes the PR diff against live main. Only the unique remainder was then re-applied on top.
4. **Verification** — targeted suites, static gates, mutation probes (below), and full-suite regression before the final push.

## 2. Reconciliation table (52 files)

Classification: **S** = superseded on live main (dropped), **U** = unique (ported), **R** = both sides changed (reconciled/adapted).

| File | PR#33 change | Live-main equivalent | Class | Action |
|---|---|---|---|---|
| `.agents/board.json` | board state | live schema-2 board | R | keep main + `task-123` claim (CLI-driven) |
| `.env.example` | security vars | main's newer file (not inspected — env-file access is filtered) | S | take main |
| `.github/workflows/ci.yml` | uv switch + `test-extras` job | main's `extras-matrix`/`python-parity` governance (task-132) | R | add 6 new blocking legs + focused cases; drop uv/test-extras (superseded design) |
| `.nexus/continuum.json` | bump to 853 | main's republished snapshot | R | republished by this branch (measured count) |
| `AGENTS.md` | Sep-21 board table | main's rewritten v2 contract | S | take main |
| `AUDIT_REPORT_2026-09-21.md` | v3.13.0 audit addendum | main's later audits (`docs/audits/*`) | S | take main |
| `CHANGELOG.md` | v3.13.0 sections | main's Unreleased sections | R | append packaging/OTIO/conv-store/async-session entries |
| `Dockerfile` | multi-stage, slim default | main's ffmpeg/task-163 single stage | R | port multi-stage; runtime keeps system ffmpeg; drop unused apt pkgs |
| `README.md` | install/extras + Docker + status | main's newer status table | R | port extras/Docker sections; drop superseded status/access text |
| `ROADMAP_STATUS.md` | task-110 rows | main's newer roadmap | S | take main |
| `VERSION` | `3.13.0` | identical (`3.13.0`) | S | no change |
| `docs/CHECKUP_2026-09-21_v3.13.0.md` | session report | main's dated records | S | take main |
| `docs/DECISION_LOG.md` | r6-era text | main's r12 | R | append **r13** decisions (this work) |
| `pyproject.toml` | extras split + PEP 735 | main's fat core deps | R | slim core (32→22 deps) + 9 extras + `[dependency-groups]`; gtts merged into `[speech]` |
| `src/…/adapters/conversation_store_sqlite.py` | new adapter | none | U | port |
| `src/…/api/app.py` | security wiring | main's evolved API | S | take main |
| `src/…/api/dashboard.py` | PII redaction + bearer | main has both (`execute` fix incl.) | S | take main |
| `src/…/bot/app.py` | guard registration | main registers `AccessGuardHandler` in group −1 (`app.py:382`) | S | take main |
| `src/…/bot/handlers.py` | wiring + `AsyncSession.exec` fixes | wiring superseded; **6 `await session.exec()` call sites still live** | R | port only the exec fixes (`_upsert_user`, `_upsert_chat`, `/myfiles`, `/download`, `/language` ×2) |
| `src/…/bot/middleware.py` | in-middleware gate | main's separate `bot/access_guard.py` design + tests | S | take main |
| `src/…/bot/rate_limiter.py` | `MAX_TRACKED_USERS` bound | **absent on main** (unbounded growth under flood) | U | port + regression test |
| `src/…/cli.py` | `OptionalDependencyMissing` guard in `golden-update` | main's newer CLI | R | port the guard onto main's function |
| `src/…/config/settings.py` | dashboard token field | main has `api_dashboard_token` | S | take main |
| `src/…/core/paths.py` | new containment helper | main's `bot/safe_paths.py` (canonical) | S | take main |
| `src/…/creative/packs/delivery/models.py` | `ExternalReference.1` media refs | none (base lacked it) | U | port |
| `src/…/creative/packs/delivery/operations.py` | `global_start_time` + Stack name | main's temporal-core rewrite changed the same function | R | port both emissions onto main's implementation |
| `src/…/features/anonymous_chat.py` | anon chat wiring | main's live anon delivery | S | take main |
| `src/…/features/force_join.py` | `enabled is True` bug + bind | main's hardened manager (`col(...).is_(True)`, fail-closed unbound) | S | take main |
| `src/…/features/gamification.py` | first-daily + tz fixes | main already fixed (`_as_utc`, no pre-set `last_daily`) | S | take main |
| `src/…/features/onboarding.py` | `execute().scalars()` fix | **still `await session.exec()` with a swallowing except** (every user "first-time") | U | port fix + regression test |
| `src/…/features/speech.py` | typed extra guard | main unchanged since base | U | port |
| `src/…/optional_deps.py` | new guard module | none | U | port (+ `faster_whisper` mapping) |
| `src/…/storage/checkpoint_lifecycle_pg_store.py` | typed PG guard | main unchanged | U | port |
| `src/…/storage/checkpoint_pg_adapter.py` | typed PG guard | main unchanged | U | port |
| `src/…/storage/checkpoint_reconciler.py` | degrade `_DB_ERRORS` w/o psycopg | main unchanged | U | port |
| `src/…/storage/db.py` | `asyncpg/psycopg` guard + default-path bug | main's WAL-race hardening; **default-path bug still live** | R | port both hunks onto main |
| `src/…/storage/langgraph_checkpoint.py` | lazy psycopg | main unchanged | U | port |
| `src/…/storage/providers/r2.py` | lazy `boto3` guard | main's R2 error-handling hardening | R | port guard onto main |
| `src/…/worker.py` | typed RAG import error | main's creative_render handler evolution | R | port guard onto main |
| `tests/integration/test_checkpoint_adapter_contract.py` | core-only skip guard | main unchanged | U | port |
| `tests/integration/test_checkpoint_composition.py` | PG-driver skip | main's flush-drain test | R | merge both |
| `tests/integration/test_lifecycle_store_contract.py` | core-only fix | main unchanged | U | port |
| `tests/unit/test_access_gate.py` | access-guard tests | main's `test_access_guard.py` | S | take main |
| `tests/unit/test_conversation_store_adapter.py` | 24 contract tests | none | U | port |
| `tests/unit/test_dashboard_privacy.py` | dashboard PII tests | main's `test_dashboard_api.py` | S | take main |
| `tests/unit/test_database_url.py` | driver bypass + skipif | main's alembic-rev fixture update | R | merge both |
| `tests/unit/test_force_join_gate.py` | gate tests | main's own (11 tests) | S | take main |
| `tests/unit/test_otio_interop.py` | real round-trip tests | none | U | port + adapt to main's canonical rational rates + experimental opt-in |
| `tests/unit/test_packaging.py` | declaration/import-graph/boot contract | none | U | port + adapt (`[speech]` provides `faster_whisper` + `gtts`) |
| `tests/unit/test_postgres_checkpointer.py` | core-only fix | main unchanged | U | port |
| `tests/unit/test_safe_paths.py` | path-sanitisation tests | main's own (12 tests) | S | take main |
| `tests/unit/test_wired_commands.py` | wired-command honesty tests | main's command-capability boundary + task-193 command truth | S | take main |

**New tests introduced by this task (not in the PR):**
`tests/unit/test_async_session_contract.py` (behavior + source ratchet), `tests/unit/test_rate_limiter.py::test_tracked_users_are_bounded_under_a_distinct_user_flood`.

## 3. Removed hunks (with reason)

* **Security/wiring cluster (`96fac7f`)** — global access guard, dashboard PII/bearer, `/cloud`+`/download` containment, force-join verification, anon-chat wiring, engine wiring: all land on live main through merged PR#34 and its successors (witnesses: `bot/access_guard.py` + `tests/unit/test_access_guard.py`; `bot/safe_paths.py` + `tests/unit/test_safe_paths.py`; `api/dashboard.py` + `tests/unit/test_dashboard_api.py`; `features/force_join.py` `col(...).is_(True)` + `tests/unit/test_force_join_gate.py`).
* **Old documentation set** (`AGENTS.md`, `AUDIT_REPORT`, `ROADMAP_STATUS`, `docs/CHECKUP`, `.env.example`) — live main carries newer canonical revisions; the PR's versions are Sep-21 snapshots.
* **CI uv/test-extras job** — superseded by main's `extras-matrix` + slim-core consequence (the default `test` job now installs near-core `.[dev]`).
* **Superseded tests** that duplicated main's own coverage under different names (access/dashboard/force-join/safe-paths/wired-commands).

## 4. Retained hunks (why)

* **Packaging (`task-107`)** — slim core, 9 capability extras, PEP 735 groups, typed fail-closed guards, multi-stage Dockerfile, per-extra CI legs. Nothing equivalent exists on main; `nexus-ai-agent.egg-info/requires.txt` on the branch proves the old declaration shape.
* **OTIO (`task-110`)** — `ExternalReference.1`/`global_start_time` are required for a real NLE read; the pre-fix document opens 100% offline media (negative control in `test_legacy_media_url_shape_is_detected_as_missing_media`).
* **Conversation-store adapter** — implements the existing port (`application/ports/conversation_store.py`) without touching feature internals.
* **Async-session defect closure** — 7 live `await session.exec()` sites on main (6 handlers + onboarding); previously silent (`except Exception: return True`).
* **Rate-limiter bound** — class documented "Bounded" but grew with every distinct `user_id`.
* **Storage/CLI guards** — core-only installs must boot and fail closed (task-107's second half).

## 5. Test mapping (remainder)

| Requirement | Test |
|---|---|
| No heavy package in core; import-graph; boot with every extra blocked | `tests/unit/test_packaging.py` |
| Extras ↔ CI legs ↔ committed matrix doc | `scripts/extras_matrix.py check`, `tests/unit/test_ci_extras_parity.py` |
| Per-leg install/import smoke | `tests/unit/test_optional_extras.py` |
| OTIO round-trip, media refs, rates, idempotence | `tests/unit/test_otio_interop.py` |
| Async-session contract + ratchet | `tests/unit/test_async_session_contract.py` |
| Rate-limiter bound | `tests/unit/test_rate_limiter.py` |
| Conversation-store contract | `tests/unit/test_conversation_store_adapter.py` |
| Checkpoint contracts on a core-only install | `tests/integration/test_checkpoint_{adapter_contract,composition,lifecycle_store_contract}.py`, `tests/unit/test_postgres_checkpointer.py`, `tests/unit/test_database_url.py` |
| PG driver guard / CLI golden update | `tests/unit/test_packaging.py::test_...`, `tests/unit/test_database_url.py` |

## 6. RED → GREEN evidence (recorded runs)

* `test_async_session_contract.py` before the fix: **4 failed** — `AttributeError` in `_upsert_user`/`_upsert_chat`, `is_first_time_user` misreporting, ratchet red on `handlers.py` + `onboarding.py`. After: **290 passed**.
* `test_rate_limiter.py::test_tracked_users_are_bounded_under_a_distinct_user_flood` before: `AttributeError: … has no attribute 'MAX_TRACKED_USERS'`. After: **2 passed**.
* `test_otio_interop.py` against the pre-PR producer: the legacy `media_url` shape parses as `MissingReference` (negative control).
* `mypy src` → *Success: no issues found in 287 source files*; `ruff check` / `ruff format --check` → clean.

## 7. Mutation / adversarial evidence

* **M1 (OTIO):** removing the `global_start_time` emission → `test_frame_rate_and_durations_are_preserved[24/25/30/23.976]` = **4 failed**; restored → 11 passed.
* **M2 (async session):** reverting *one* `execute().scalars()` call site to `.exec` → behavior test + source ratchet = **2 failed**; restored → 290 passed.
* **M3 (extras governance, standing):** `tests/unit/test_ci_extras_parity.py` proves the guard goes red for a missing/unknown leg, a pyproject extra without a definition, and a stale committed matrix.

## 8. Residual risk / limitations

* The merge of PR#33 stays an owner/gates decision (no merge performed — forbidden by the claim).
* `[rag]` and `[local-llm]` extras-matrix legs were validated by `extras_matrix.py check` and local installs of their guard paths; the full CI legs for those two (torch / llama.cpp builds) have not executed in this sandbox.
* `.env.example` was not inspected or modified (env-file access filtered); the corresponding settings fields exist in `config/settings.py`, and the README documents the required variables.
* Two README measurements (pre-split 6.5 GB, core 520 MB/389 MB) are the numbers measured when the split was authored (2026-09-21, Python 3.11.2) and are labelled as such; they were not re-measured here.
* `.nexus/continuum.json` is deliberately **not** republished on this branch: the CI `continuum-evidence` job publishes a snapshot for the SHA under test and reports the committed copy as a non-blocking machine-bound release-cut record, so a branch-local snapshot would pin this sandbox's interpreter/library versions as if they were evidence.
