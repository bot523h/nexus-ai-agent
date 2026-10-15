# Executive summary — NEXUS mainline convergence, 2026-10-15

**Verdict: `READY_WITH_EXPLICIT_EXTERNAL_BLOCKER`.** PR #218 is complete, green on every gate
the repository owns, and documented to the commit. It is not on `main` because the author
account cannot satisfy the required-review rule (`EXTERNAL_BLOCKERS.md` item 1). No `--admin`
bypass, branch rewrite or review-filter trick was used to get around that.

## What was delivered (all on PR #218: 18 commits through `7bfd628`, plus this package)

| Finding (published audit numbering¹) | Status | One-line proof |
|---|---|---|
| A-01 P0 text-chat path crashed (`.exec` on an `AsyncSession`) | VERIFIED | `test_real_session_factory.py`, `test_a01_message_path.py` (incl. negative control) |
| A-02 / A-02b P0 storage duality (13 feature modules on private SQLite) | VERIFIED | `test_postgres_single_engine.py`, `test_sync_engine_resolution.py`; law R19 |
| A-03 P1 canned commands, ungated moderation stubs, `/mod_on` without ingress | VERIFIED | `test_surface_moderation.py`, `test_surface_viral.py`, `test_surface_registration.py` |
| A-04 P1 `.agents/board.json` as shared state in the merge path (51/64 PRs collide) | **HARDENED_BUT_NOT_COMPLETE** | referee 14/14 + Gate-5 17/17 harnesses attack again in CI (`8690a4c`); the structural remedy (board out of the merge path — per-claim files or an orphan board branch — and a CI rule that code PRs do not touch the board) is **not** implemented; see debt ledger §8 |
| A-05 P1 dependency governance (undeclared imports, drift) | VERIFIED_WITH_LIMITATIONS | `deptry src` clean, drift pin fixed, extras legs in CI; still no lockfile |
| A-06 P1 dashboard fail-open without token | VERIFIED | `test_dashboard_api.py`, OpenAPI-driven `test_api_gate_inventory.py` |
| A-07 P1 self-updater blocking the loop, unbounded | VERIFIED | `test_updater.py` (heartbeat during slow step), capability probe |
| A-08 P1 CI hygiene (unfiltered push, no permissions/timeouts) | VERIFIED | `ci.yml` `push: branches: [main]`, `permissions`, `timeout-minutes` pinned by CI tests |
| A-09 P2 duplicate `Referral`/`ReferralCode` declarations | VERIFIED | `test_referral_models.py`; one table owner (`storage/models.py`) |
| A-10 P2 dead modules | VERIFIED | nine removed; `tests/architecture/test_no_orphan_modules.py` |
| A-11 P2 complexity (`_process_job` CC 63) | VERIFIED | 62 → 17; characterisation 9/9, exec-core mutants 17/17, ratchet law R22 |
| A-12 P2 code-level security/time/async (md5, `/tmp` default, `utcnow`, forgeable referral code, blocking I/O in `async def`) | VERIFIED | SHA-256 + `S324`, `tempfile`-based default, `DTZ` at zero, `secrets`-based codes, ruff `ASYNC` 35 → 0 + `test_async_offload.py` |
| A-13 P2 typing (`mypy --strict` 62 errors) | VERIFIED | strict enforced, 0 errors in 283 files |
| A-14 P2 lint configuration (`E,F,I,B,UP` only; 44 stale `noqa`) | VERIFIED | 22 rule families at zero, `test_ci_lint_parity.py` |
| A-15 P2 docs + deployment drift (MODULE_MAP counts, root reports, root Dockerfile without `USER`/`HEALTHCHECK`) | VERIFIED_WITH_LIMITATIONS | docs fixed and index-enforced; `USER nexus` + mode-aware `HEALTHCHECK` (labelled A-16 in CHANGELOG); multi-stage build and hash-locked install still open |
| A-16 P3 spelling | VERIFIED | no `Referr` remains in README; remaining codespell hits were false positives |
| A-16² container hardening / A-17 Telegram ids BIGINT / A-18 retired-engine disposal / A-19 `alembic check` drift gate (opened during remediation) | VERIFIED_WITH_LIMITATIONS / VERIFIED / VERIFIED / VERIFIED | CI `container` job (stand-in processes, no live bot); `test_telegram_id_width.py`; `test_db_engine_retirement.py`; `test_migrations.py` |
| GOV-1 / GOV-2 mutation harnesses silently inert | VERIFIED | referee 14/14, Gate-5 17/17, execution-core 17/17 — all in CI |
| Backups | BLOCKED_BY_EXTERNAL_DEPENDENCY | five secrets missing (run 38704873852) |
| #209 (open PR) | integrated remainder (`7bfd628`), close after merge | 28/28 primitives tests |

¹ A-01…A-16 are the ids of the full audit (PR #198 comments 6190228525/6190229601).
² The CHANGELOG and Dockerfile label the container work "A-16" — it is the Dockerfile item of
audit A-15, split out when it was fixed; A-17…A-19 were opened during remediation.

## Numbers at the code head `7bfd628`

ruff 0 · format 0 · mypy 0/283 · deptry clean · fast suite **4103 passed / 37 skipped**
(skips are PostgreSQL/extras legs that CI runs) · 594 inventoried files · 283 production
modules: 34 `REMEDIATED_WITH_TESTS`, 21 `VERIFIED`, 39 `VERIFIED_WITH_LIMITATIONS`,
50 `CHANGED_UNREVIEWED` (typing/lint/to_thread edits under repository-wide guards),
139 `DEFERRED` · open PRs: 62 dispositioned, 0 force-closed · A-04 remains `HARDENED_BUT_NOT_COMPLETE` (structural remedy specified, not built).

## What the owner must do, in order

1. Approve and merge #218 (`gh pr merge 218 --merge`); confirm CI green on the merged SHA.
2. Add the five backup secrets and run the maintenance workflow + restore drill once.
3. Record the first real container's health status (upgrades A-16 to `VERIFIED`).
4. Cut the release tag from the merged SHA.
5. Continue the PR integration order in `PR_CONVERGENCE.md` (#210 → #213 → #211/#202 → #198 →
   #122/#190/#206), closing #209 as `SUPERSEDED_BY_VERIFIED_CHANGE`.

## What this package does **not** claim

No live Telegram/LLM/embedding call was made; no production backup exists; the untouched 139
`DEFERRED` modules are not asserted to be correct; Wave 3-C and PR #118 are not described as
verified or fixed; CodeRabbit did not review the change.
