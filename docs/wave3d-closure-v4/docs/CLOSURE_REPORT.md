# NEXUS — Wave 3D closure report

Session branch `arena/aa451c49-nexus-ai-agent` · report prepared **2026-10-14** (UTC)
Bundle: `wave3d/v4` (workspace) = `docs/wave3d-closure-v4` on the branch, verified by re-download.

**Headline: the wave is reconciled and reproducible, but it is NOT closed.** The governance gate
(PR #211) is still blocked by its owner, the /forget_me defect is repaired in a validated patch but
**not integrated**, and PostgreSQL remains untested. Nothing in this wave merged anything, modified
any contributor's branch, or edited a governance record.

## 1. Baselines and identities

| Item | SHA / hash |
|---|---|
| Baseline `main` | `6b398565cc1ce0c51fac77d7623350b299c3938f` (tree `90e7fc2b6480fa1b550d0f4ed57baaf61d937409`) |
| Candidate branch head | `b2aebdb` (checker v3) — tree `023842458329e5304b078e1a82573f1dc64e6c74` |
| Bundle publication commit | `f99ab1c653ec7fe038412306110baa200f154935` (branch `arena/aa451c49-nexus-ai-agent`) |
| Bundle manifest | `MANIFEST.json` sha256 in that file's `bundle` section (self-hash recorded in `DOCS.md`/commit) |
| Patches | checker_v3 `a54daffa…` · tiso01 `b0d17ea4…` · patch_a `aa4feb40…` · patch_a_tests `b3221743…` · patch_b `0240c551…` · patch_b_tests `59e3acc6…` · dispatch `74aa97ff…` |
| PR heads | #198 `25c4965b` · #210 `1d7390cc` · #211 `878842b6` · #213 `29ca3d66`(short) · #118 `d5a205f4`(short) |

Remote CI bound to a SHA: **only one run exists in this wave** — PR #211, run `38351719390` on
`878842b6557eb95b00b1c9d1fdf14d444acfbe43` — and it **failed**. Everything else in this report is a
**local** execution on an exact upstream tree; no local result is presented as remote CI, and no
remote CI result is presented as production verification.

## 2. Per-step status

| # | Step | Status |
|---|---|---|
| 1 | Live truth | **VERIFIED** — refs, trees, merge-bases, PR states and the referee's behaviour captured; snapshot dated |
| 2 | Lost-artifact marking + full-hash verification | **VERIFIED** — v1–v3 LOST; the six reported prefixes occur nowhere in the workspace; no full hash inferred from a prefix |
| 3 | Six-test SQLite A/B on the exact #211 tree | **VERIFIED** — 3.1.2: `6 failed, 108 passed, 19 skipped`; 3.1.1: `114 passed, 19 skipped`; the six IDs flip per node; pin left at `==3.1.1` |
| 4 | PR #211 owner action packet | **VERIFIED** — written, no branch modification performed |
| 5 | Checker v3 on exact main | **VERIFIED** — applies cleanly, reproduces `b43cda742e26ce00fcdef60eb6247bd0abf018a10e7dce0d9ec55b443a0f5410`, board suite `110 passed` |
| 6 | Pagination/batching regressions | **VERIFIED** — 39-test module covering counts 0/1/99/100/101/200/250 and 0/1/24/25/26/49/50/51/>200, terminal pages, API failures, malformed payloads, duplicate heads, disappearing branches, unreadable boards, live lease beyond the 25-branch boundary, stale refs, caller exclusion, batch-failure fallback, fail-closed on an unreadable source; structural query parsing (`page=1` never matches `per_page=100`) |
| 7 | Repair proven without weakening governance | **VERIFIED** — focused module + full `test_agent_board.py` + adjacent board-integrity modules: `110 passed`, ruff and mypy clean |
| 8 | T-ISO-01 | **VERIFIED_WITH_LIMITATIONS** — patched suite 5 passed with **no** ambient `data/app.sqlite`, survives a corrupt ambient database; unpatched red on the same input. Limitation: a read-only CWD still fails at `config/settings.py:447` (pre-existing, out of scope) |
| 9 | Patch A revalidation on the exact #210 tree | **VERIFIED** — unpatched `3 failed, 2 passed` → patched `38 passed`; adjacent suites green; ruff/mypy clean |
| 10 | Patch B revalidation on the exact #198 tree | **HARDENED_BUT_NOT_COMPLETE** — composed `1 failed, 81 passed`; the single failure is the /forget_me transcript finding (repaired by a validated patch, not integrated) |
| 11 | Dispatch ordering on an isolated clone through the registered `on_message` handler | **VERIFIED** — 2 direct-dispatch + 4 hardening tests; base `2 failed, 4 passed` → Patch B alone unchanged → with the dispatch repair `6 passed`; reverse order also applies (disjoint files) |
| 12 | Privacy/deletion regressions with adversarial fixtures + canaries | **VERIFIED** — 27-test module (canaries `CANARY-ALPHA-9f3c1d` / `CANARY-BRAVO-4e7a20`), base `5 failed, 22 passed` → `1 failed, 26 passed` |
| 13 | Source truth matrix | **VERIFIED** — `docs/SOURCE_TRUTH_MATRIX.md`, plus an apply-matrix (`git apply --check` for every patch against main/#198/#210/#211) |
| 14 | Fresh overlap/integration matrix | **VERIFIED** — `docs/INTEGRATION_MATRIX.md`; `.agents/board.json` is touched by all five PRs, #210 ∩ #213 = 41 files, #210 ∩ #211 touches `.github/workflows/ci.yml` |
| 15 | PostgreSQL limitations | **DEFERRED** — `docs/POSTGRES_LIMITATIONS.md`, explicit status + reproducible acceptance plan |
| 16 | Targeted final regression | **VERIFIED** — baseline `71 passed` (3 board modules) → candidate `110 passed` = 71 + 39 new; no duplicate counting; no skips/xfails introduced; ruff+mypy clean; `git status` empty and tree hash unchanged before/after on both trees |
| 17 | #211 six-test requirement on a new owner head | **BLOCKED** — no new head exists; owner action named (§5) |
| 18 | Rebuild v4 bundle | **VERIFIED** — `MANIFEST.json` + `reproduce.sh`; every patch re-applied from the bundle's own copies; `reproduce.sh` exits 0 with all six legs matching the manifest |
| 19 | Durable handoff | **VERIFIED_WITH_LIMITATIONS** — bundle committed and pushed to the session branch, then **re-downloaded from GitHub and byte-compared: 45/45 files identical** at `f99ab1c`. Limitation: GitHub branch history is mutable and there is no independent archive/tag; durability is "recoverable while the branch exists" |
| 20 | This closure report | **DONE** |

## 3. What reproduces, exactly

`bash reproduce.sh` (≈2.5 min, network to pypi.org, builds its own venvs) → exit `0`:

```
leg 3   3.1.2 -> 6 failed, 108 passed, 19 skipped    3.1.1 -> 114 passed, 19 skipped
        all six node IDs flip 1 failed -> 1 passed
leg 5   checker_v3.patch applies on 6b398565; scripts/agent_board.py == b43cda74…
        110 passed; ruff clean; mypy clean
leg 8   patched 5 passed; no ambient data/app.sqlite; corrupt-ambient 5 passed; unpatched 1 failed
leg 9   unpatched 3 failed, 2 passed  ->  patched 38 passed
leg 10  base 5 failed, 22 passed  ->  Patch B 3 failed, 24 passed  ->  composed 1 failed, 81 passed
leg 11  base 2 failed, 4 passed   ->  Patch B unchanged            ->  dispatched 6 passed
```

Six node IDs (step 3/17): `tests/integration/test_checkpoint_adapter.py::test_setup_schema_matches_golden`,
`tests/unit/test_reconciler.py::{test_apply_backfills_missing_with_protected_age,
test_purge_eligible_orphan_is_applied, test_scan_error_disables_apply,
test_cli_reconcile_apply_requires_flag}`,
`tests/unit/test_lifecycle_adversarial.py::test_apply_under_lock_contention_fails_safe`.

## 4. Open findings and risks

| ID | Severity | Finding | Owner |
|---|---|---|---|
| W3D-F1 | high | `/forget_me` announces that all long-term memory was erased while the turn transcript stays retrievable and injectable (the persona-session store has no scoped delete). Reproduced by the single failing test in leg 10. | PR #198 author |
| W3D-F2 | blocker | Governance gate: #211 is behind main, its CI failed on the current head, and the recorded approval is a bot's. | PR #211 owner (human) |
| W3D-F3 | medium | #210 is `CHANGES_REQUESTED`; Patch A is validated against `1d7390cc` only and must not be integrated ahead of an authorized path. | PR #210 owner (human) |
| W3D-F4 | medium | Checker v3 and #211 both change `scripts/agent_board.py` (+ `test_agent_board.py`); the checker patch must be re-validated after #211 lands and must stay a separate reviewable change. | governance reviewer |
| W3D-F5 | low | PR #198 is `CONFLICTING/DIRTY` and #118 is far behind (merge-base `e5b326b2`); all legs were validated against #198's **current** head and are invalidated by a rebase. | PR owners |

Residual risk: all second-leg evidence is local. If a reviewer cannot re-run `reproduce.sh`
(network/pypi unavailable), the logs in `evidence/` are the only record — they are raw pytest output
with no redaction applied and no credentials inside.

## 5. Named next owner and required action

**PR #211 owner (human)** — update the branch onto `main` (`6b398565`), re-run full remote CI on the
new SHA, decide the `langgraph-checkpoint-sqlite` pin explicitly (keep `==3.1.1`, or update the
schema golden with controlled evidence), obtain the required **human** approval on the new head, and
post SHA + run ID in the PR. Details: `docs/PR211_OWNER_ACTION_PACKET.md`.

## 6. Ten-task forward plan

| # | Priority | Scope | Prerequisites | Acceptance | Evidence | Exclusive paths |
|---|---|---|---|---|---|---|
| 1 | P0 | Land #211 with a green exact-SHA run and human approval; then re-validate `checker_v3.patch` on the new main | owner update + human review | checker patch re-applies, `agent_board.py` == `b43cda74…` or a re-derived hash, board suite green | new run ID + SHA, patch log | `scripts/agent_board.py`, `.github/workflows/ci.yml` (owner) |
| 2 | P0 | Integrate the /forget_me scoped delete (W3D-F1) as its own patch on #198 | #198 un-dirtied | leg 10 → `82 passed`, canary disappears, existing privacy tests unchanged | pytest log with canary assertions | `src/nexus_ai_agent/memory/long_term.py`, `features/ai_memory.py` |
| 3 | P1 | Authorized integration of Patch A into #210's branch (or its successor) | #210 unblocked by its owner | composed run green on the new head; `await session.exec(` count == 0 on async paths | diff + pytest + ruff/mypy logs | `src/nexus_ai_agent/bot/handlers.py`, `features/onboarding.py` |
| 4 | P1 | Re-base #198 onto post-#211 main; resolve the `.agents/board.json` conflict **without** hand-editing governance history | #211 merged | clean rebase, legs 10/11 re-run green | reproduce.sh output on the new SHAs | `src/nexus_ai_agent/orchestration/graph.py` |
| 5 | P1 | Reconcile #118 with #198 (LLM/agent taxonomy overlap: `ai_chat.py`, `fallback/gemini/litellm_provider.py`, `base_agent.py`) | #198 merged | one agreed taxonomy; no duplicate provider logic | overlap matrix refresh + reviewer note | `src/nexus_ai_agent/llm/*` |
| 6 | P1 | PostgreSQL A/B per `docs/POSTGRES_LIMITATIONS.md` | a reachable PostgreSQL 16 | recorded divergence or equality for the Postgres marker set | server version, DSN host, wheel versions, logs | `tests/unit/test_postgres_checkpointer.py` |
| 7 | P2 | Fold the T-ISO-01 isolation into the suite's shared fixture (remove the remaining read-only-CWD coupling at `config/settings.py:447`) | none | full suite runnable from any CWD and from a read-only checkout | pytest log + settings diff | `tests/conftest.py`, `config/settings.py` |
| 8 | P2 | Delete the `/forget_me` copy or make it truthful if the scoped delete slips | #2 cancelled | user-visible text and behaviour agree | assertion on the reply text | `src/nexus_ai_agent/bot/handlers.py` |
| 9 | P2 | Extend the dispatch module to `command`/`callback` dispatch (not only free text) and to a rate-limited sender | #3 merged | 6+ new cases green | pytest log | `tests/unit/test_registered_dispatch.py` |
| 10 | P3 | Answer the "44-test suite" question with a named composition or mark it unusable | originating report clarification | a documented file list that totals the claimed number | composition list | — |

## 7. Durability, and what is deliberately not claimed

* Bundle in the workspace: `/home/user/wave3d/v4/` (≈330 KB without `_reproduce`), plus
  `docs/wave3d-closure-v4/` on the session branch (commit `f99ab1c`), re-downloaded and
  hash-verified.
* **Not claimed:** that v1–v3 were restored; that any of this is merged; that remote CI passed;
  that PostgreSQL works; that the governance gate is satisfied; that artifacts are archived
  immutably.
