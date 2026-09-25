# ✅ NEXUS FORENSIC CLOSURE V2 — Task-122 / 12 Commands / Moderation Ingress

**Date:** 2026-09-26 (night shift)
**Session Branch:** `arena/01a0da85-nexus-ai-agent`
**Baseline forensic report:** `e405e07` (`FORENSIC_REPORT_task122.md` v1, verdict NEEDS_FIX, branch `arena/01a0da61-nexus-ai-agent`)
**Closure verification:** `60188f2` — final implementation closure commit; branch head at report time is its direct docs-only descendant (this file + board row).

---

## A — Live Truth

All facts below were re-derived from live Git/GitHub state at session start — none accepted from the v1 report.

| Item | Value | Evidence |
|------|-------|----------|
| remote `main` | `2351f099e2f789b80de05c6b382554b5cdcec0f3` | `git ls-remote origin main` |
| implementation branch (claimed) | `arena/01a0da12-nexus-ai-agent` @ `0199e58fce93d62b6db17ea945811235ec6bb2e9` | `git ls-remote origin "refs/heads/arena/01a0da12*"` |
| ancestry | `2351f09 (main) ← f052843 (board claim) ← 0199e58 (implementation)` | `git log --oneline`; `git merge-base --is-ancestor` |
| audit branch | `arena/01a0da61-nexus-ai-agent` @ `e405e07d57c8020025f5677db5bed85c88191771` (report file only; parent = `2351f09`) | `git diff 2351f09 e405e07 --stat` → 1 file |
| PR for `arena/01a0da12` at session start | **NONE** | `gh pr list --state all --head arena/01a0da12-nexus-ai-agent` → empty |
| PR for `arena/01a0da61` | **NONE** | same method |
| CI on `0199e58` | run `36187605207` → **FAILURE** — `lint`, `lint-fast` (`ruff format --check`), `python-parity (3.11)`; other 9 jobs success | `gh api actions/runs?head_sha=0199e58…`, `gh run view` |
| CI on `e405e07` | run `36191473790` → **FAILURE** — `lint` + `lint-fast` only; **parity 3.11 PASSED** (no parity-log artifact) | same method; artifact list inspected |
| session branch start | `arena/01a0da85-nexus-ai-agent` @ `2351f09` (== main), clean tree | `git status`, `git rev-parse HEAD` |
| relation old → new | `0199e58` fast-forwarded onto session branch, then closure commit `60188f2` on top: `2351f09 ← f052843 ← 0199e58 ← 60188f2` | `git merge --ff-only`; `git log --oneline -4` |
| **CI on `60188f2` (final implementation)** | run `36194369744` → **SUCCESS, 12/12 jobs green** incl. `lint`, `lint-fast`, `test`, `python-parity (3.10/3.11/3.12)`, 4× extras-matrix, `migrate-postgres`, `release-lineage` | `gh run view 36194369744` |

**Disposition of the old implementation SHA:** `0199e58` is no longer presented as final. Final implementation SHA is `60188f2107cb61663cc136faa7d8f78a995c60de` (= `0199e58` + the closure commit described in §J). The two are related by direct first-parent descent; no history rewrite occurred.

**New forensic facts discovered this session (not in v1):**

1. **The v1 report file itself broke the lint gate.** `ruff format --check` (ruff ≥ 0.16 formats Python code blocks inside Markdown) flags the compact one-liners in `FORENSIC_REPORT_task122.md` (v1, lines 82/91). That is why the docs-only audit branch `e405e07` failed `lint`/`lint-fast`. This report uses format-clean code blocks only; verified with `ruff format --check .` before commit.
2. **Two latent mypy errors in `0199e58`** were invisible in CI because the `mypy src` step never ran (the job died at the earlier `ruff format` step): `moderation.py:196` — flood-key union type not reflected in `_flood_tracker` annotation; `handlers.py:330` — `model_path` (`Path | None`) not narrowed before `.stat()`. Both fixed in `60188f2` (behavior-identical).

---

## B — Previous Findings → Closure Status

| # | v1 finding (NEEDS_FIX cause) | Status now | Where |
|---|------------------------------|------------|-------|
| 1 | exact-SHA CI for `0199e58` not green | **CLOSED** — superseded by `60188f2`, run `36194369744` 12/12 green | §L |
| 2 | `ruff format` fails on `test_bot_command_wiring.py` | **CLOSED** — formatted (2 hunks); `ruff format --check .` + `ruff check .` green locally (ruff 0.16.8 pinned + latest) and in CI | §L, §K |
| 3 | `python-parity (3.11)` undetermined | **CLOSED** — parity 3.11 green on `60188f2` (CI) + full suite reproduced green locally on CPython 3.11.2 (`2332 passed, 30 skipped`); old leg's log unobtainable (artifact host EOF) → classified non-reproducible/environment-class, gone on final SHA | §K, §L |
| 4 | no PR for the implementation | **CLOSED** — PR opened from `arena/01a0da85-nexus-ai-agent` (see §M) | §M |
| 5 | mutation C alive (flood key) | **CLOSED** — killed by new tests (live apply→fail→restore proof) | §J |
| 6 | no cross-chat flood test | **CLOSED** — `tests/unit/test_moderation_flood_and_thread.py` (4 tests) | §H |
| 7 | no architecture guard for `asyncio.to_thread` | **CLOSED** — `tests/architecture/test_async_db_offload_guard.py` (2 gates, AST, stdlib-only) | §I |
| 8 | `anon_message_handler` bypasses peer-safety moderation | **OPEN as product decision** — `NEEDS_PRODUCT_DECISION` (engineering intentionally unchanged; no product policy found in repo) | §E |
| 9 | board does not reflect reality | **CLOSED** — task-122 row moved to `active_in_review` with branch pointer + CI evidence (§N) | §N |

---

## C — 12 Command Matrix (extracted from code at `60188f2`, not from v1)

Engine calls marked ⧗ are executed via `await asyncio.to_thread(...)` (arch-gate-enforced, §I).

| # | Command | Registration (`handlers.py`) | Handler def | Auth | Real engine (call site) | DB/State | Typed failures (line) | Test |
|---|---------|------------------------------|-------------|------|--------------------------|----------|----------------------|------|
| 1 | `/leaderboard` | `:1790` | `:398` | public | ⧗ `QuizGame.get_leaderboard` `:405` | `QuizScore` rows read live | `[ERR_ENGINE_UNAVAILABLE]` `:403` | `test_leaderboard_empty_and_populated` |
| 2 | `/newchat` | `:1897` | `:920` | user required | ⧗ `GeminiEngine.clear_history` + ⧗ `conv_store.clear` `:931-934` | conversation store cleared | `[ERR_USER_REQUIRED]` `:924` | `test_newchat_clears_history` |
| 3 | `/viral_preview` | `:1843` | `:1270` | owner (`:1262`) | ⧗ `ViralEngine.get_pending_posts` / `generate_post` `:1277-1286` | `ViralPost` rows | `[ERR_ACCESS_DENIED]` `:1262` | `test_viral_commands_owner_gate`, `test_viral_preview_and_stats_real_db` |
| 4 | `/viral_stats` | `:1844` | `:1295` | owner (`:1299`) | ⧗ `ViralEngine.get_stats` `:1302` | DB counts | `[ERR_ACCESS_DENIED]` `:1299` | same |
| 5 | `/viral_post` | `:1845` | `:1313` | owner (`:1317`) | ⧗ `get_pending_posts`/`mark_posted` `:1324-1327` | `pending→posted` flip in DB | `[ERR_ACCESS_DENIED]` `:1317`; `[ERR_INVALID_ARGUMENT]` `:1330` | same |
| 6 | `/mod_config` | `:1856` | `:1389` | owner (`:1393`) | ⧗ `ModerationEngine.get_config`/`set_config` `:1398-1468` | `ModerationConfig` row | `[ERR_ACCESS_DENIED]` `:1393`; `[ERR_INVALID_ARGUMENT]` `:1428/1443/1452/1461/1465` | `test_mod_config_command` (incl. unknown-key, bad-bool, no-DB-write-on-refusal) |
| 7 | `/warn` | `:1857` | `:1481` | owner (`:1485`) | ⧗ `add_warning` + threshold `mute_user` `:1517-1525` | `UserReputation.warnings`, `is_muted` | `[ERR_ACCESS_DENIED]` `:1485`; `[ERR_INVALID_ARGUMENT]` `:1504`; `[ERR_TARGET_REQUIRED]` `:1512` | `test_warn_mute_unmute_lifecycle` |
| 8 | `/mute` | `:1858` | `:1538` | owner (`:1542`) | ⧗ `mute_user` `:1592-1594` | `is_muted`, `mute_until` (aware UTC) | `[ERR_ACCESS_DENIED]` `:1542`; `[ERR_INVALID_ARGUMENT]` `:1557/1569/1588`; `[ERR_TARGET_REQUIRED]` `:1577` | same (+ non-positive-duration refusal) |
| 9 | `/unmute` | `:1859` | `:1597` | owner (`:1601`) | ⧗ `unmute_user` `:1628` | clears mute | `[ERR_ACCESS_DENIED]` `:1601`; `[ERR_INVALID_ARGUMENT]` `:1615`; `[ERR_TARGET_REQUIRED]` `:1623` | same (incl. reply-to path) |
| 10 | `/reputation` | `:1860` | `:1631` | public | ⧗ `get_reputation` `:1650` | `UserReputation` or default | `[ERR_INVALID_ARGUMENT]` `:1645` | same |
| 11 | `/storage` | `:1768` | `:298` | public | `AIStorageManager.list_files()` (already async, correctly awaited) | filesystem/provider | `[ERR_STORAGE_UNAVAILABLE]` `:304`; `[ERR_STORAGE_LIST]` `:321` | `test_storage_command` |
| 12 | `/model` | `:1769` | `:326` | public | `Path(settings.model_path)` + provider settings | FS + settings | `[ERR_MODEL_NOT_FOUND]` `:364` | `test_model_command` |

No `pass`-stubs remain for any of the 12; no handler reports success without the engine's real answer; every refusal is a stable `[ERR_*]` code emitted at the boundary before the engine call. Naming convention kept: `ERR_ACCESS_DENIED`, `ERR_INVALID_ARGUMENT`, `ERR_TARGET_REQUIRED`, plus existing `ERR_USER_REQUIRED`, `ERR_STORAGE_UNAVAILABLE`, `ERR_STORAGE_LIST`, `ERR_MODEL_NOT_FOUND`, `ERR_ENGINE_UNAVAILABLE`.

---

## D — Moderation Ingress (canonical chain re-proven)

Chain in `on_message` (`handlers.py:938`), single text ingress:

```text
Telegram Update
  → auth.is_allowed                      (handlers.py:945)
  → rate_limiter.is_allowed              (handlers.py:949)
  → asyncio.to_thread(ModerationEngine.check_message, ...)   (handlers.py:955-957)
  → force_gate                           (handlers.py:983)
  → route_doc_text                       (handlers.py:992)
  → memory consent → AgentManager.respond / graph.ainvoke    (handlers.py:1026/1035)
```

- `graph.ainvoke` occurs **exactly once** in the whole bot layer (`handlers.py:1035`); `AgentManager.respond` once (`:1026`); both are **after** moderation in the same function. No alternative handler, callback, background path, or anonymous flow reaches an LLM.
- Commands bypass moderation by design (privileged actions, not user content for the LLM); they enforce their own owner gates (§C).
- Edited messages are dropped early (neither moderated nor forwarded to the LLM) — not an egress path.
- Verdict: **no ordinary LLM path bypasses moderation.**

---

## E — Anonymous Path (`NEEDS_PRODUCT_DECISION` — the only open item)

Fact set (unchanged by this session, by explicit instruction): `anon_message_handler` is registered **before** the catch-all `on_message` (`handlers.py:1789` vs. the final `MessageHandler`), and `anon_message_cmd` (`feature_handlers.py:413`) forwards `message.text` to the paired peer via `engines.anon.send_anon_message` **without** `ModerationEngine.check_message`. This is **not** an LLM bypass (the peer is a human, not the model); it is a **peer-safety moderation bypass**.

The three questions:

- **A — should anon messages be moderated?** No written product policy exists in the repository: `README.md` documents anon chat as a feature (`:13`, `:373-378`) but is silent on moderation scope; `docs/DECISION_LOG.md` and `docs/architecture/SECURITY.md` contain no anon-moderation decision; `REQUIREMENTS_LEDGER.md` does not cover it. The moderation feature itself (`ModerationEngine.check_message`) is chat-scoped by design.
- **B — is the bypass intentional?** Unprovable from the repo. The comment (`handlers.py:383-386`) declares the *routing* intentional ("registered before the catch-all on_message") but says nothing about moderation scope. The v1 report's NEEDS_REVIEW is therefore still accurate.
- **C — change behavior or escalate?** Per the mission rule («بدون حدس product policy، behavior را تغییر نده») behavior was **not** changed: adding moderation to the anon path *is* a product policy choice (it changes what paired peers may say to each other). Registered here explicitly as **`NEEDS_PRODUCT_DECISION`**. If the owner decides "moderate", the surgical fix is to run the same `check_message` gate inside `anon_message_cmd` before `send_anon_message` (block + typed refusal) — approximately six lines plus tests; the architecture gate (§I) already covers `feature_handlers.py`, so the offload pattern will be enforced there too.

Per the death conditions, this blocks `VERIFIED` (and qualifies the verdict as `VERIFIED_WITH_PRODUCT_DECISION`) only if every engineering gate is green — which they now are (§J–§L).

---

## F — Security

- Authorization matrix unchanged and re-verified from code (§C): 7 owner-gated commands (`viral_*`, `mod_config`, `warn`, `mute`, `unmute` + `mod_on/off`), 5 public, single source of truth `is_owner` (`features/owner_control.py:47`); no parallel auth path added.
- Typed failures: stable codes, human-readable text, no secret leakage (`/model` prints provider names and sizes only; `/storage` prints cache dir and file names; exception text only in `[ERR_STORAGE_LIST]`). Deterministic classification asserted by tests, including **no DB write on refusal** (new assertions, §J).
- No new security surface introduced by `60188f2`: the two src hunks are a type annotation and a None-narrowing condition (behavior-identical); the rest is tests.
- Unchanged residual (documented, out of task-122 scope): owner can warn/mute self; `conv_store.clear` is an in-memory dict op (not SQLAlchemy) — noted NEEDS_REVIEW-minimal by v1, unchanged.

---

## G — Persian

Re-verified on `60188f2` by the existing suite (no code change): boundary regex + `_normalize_persian` false-positive fixes (`خرید`/`خروس`/`مغز` clean), Arabic ي/ك and tatweel normalization detections, expanded link schemes (`t.me`, `telegram.me`, `telegram.dog`, `tg://`, `www.`), punctuation-only spam not flagged. Covered by `test_moderation_engine_boundary_and_persian_normalization` (green in the full run).

---

## H — Flood / Mute

- Flood key is `(chat_id, user_id)` (`moderation.py:194`), now **type-honestly annotated** (`moderation.py:81-82`) and **pinned by tests**:
  - `test_flood_same_user_does_not_cross_chats` — A floods Chat 1; A's first messages in Chat 2 are clean; Chat 1's window unaffected.
  - `test_flood_same_chat_does_not_cross_users` — B unaffected by A's flood in the same chat.
  - `test_check_message_flood_is_chat_scoped` — end-to-end via `check_message` with per-chat configs.
  - `test_flood_tracker_stores_composite_keys` — the tracker holds exactly the composite keys.
  - Bounded purge (`> 10000` stale-key sweep) unchanged.
- Mute timezone guard (naive `mute_until` treated as UTC; expiry auto-unmute) unchanged and covered by the lifecycle test.

---

## I — Thread Safety

Invariant re-verified and now **machine-enforced** (`tests/architecture/test_async_db_offload_guard.py`, stdlib `ast` only — deterministic, no heavy deps):

1. **Consumer gate (mutation G):** in `src/nexus_ai_agent/bot/**.py` every call to a sync-DB engine (`ModerationEngine`, `ViralEngine`, `QuizGame`, `ReferralEngine`, `ForceJoinManager`) must be a **direct argument of `asyncio.to_thread(...)`**, except two proven-pure pairs explicitly allow-listed (`ViralEngine.calculate_viral_score`, `ForceJoinManager.get_join_keyboard` — no engine/session opened). Import-alias aware; failure message names file:line and the fix.
2. **Producer gate:** `features/moderation.py` must not hold module-level engine/Session state, must not accept `Session`/`Engine` parameters, and every `Session(` use must be paired with a locally-created `_sync_engine()` in the same function — i.e. no SQLAlchemy object can cross a thread boundary.

Runtime companion (`test_check_message_executes_on_a_worker_thread`, `test_engine_class_holds_no_shared_db_state`): a full `check_message` DB round-trip executes on a distinct worker thread, succeeds, and leaves **zero** SQLAlchemy objects on the class. Scanned call sites at `60188f2`: 12 `ModerationEngine.*` call sites, all to_thread-wrapped; direct engine calls found are exactly the two allow-listed pure functions.

---

## J — Mutation Results (live apply → run → restore; all on this session's tree)

| Mutation | Change applied | Killed by | Result |
|----------|----------------|-----------|--------|
| A | remove moderation block from `on_message` | `test_moderation_ingress_on_message` | FAILED under mutation ✅ killed |
| B | `is_owner` → `return True` | 3 owner-gate tests | FAILED ✅ killed |
| C | flood key → `user_id` only | `test_flood_same_user_does_not_cross_chats` (`is_flooding(2001, -1002)` returned True) | FAILED ✅ killed (**new**) |
| D | remove `_normalize_persian` call | `test_moderation_engine_boundary_and_persian_normalization` | FAILED ✅ killed |
| E | spam regex `(\w)` → `(.)` | same test (punctuation spam) | FAILED ✅ killed |
| F | `mod_config` unknown-key refusal → "✅ updated" | new `test_mod_config_command` assertions | FAILED ✅ killed (**new** — v1's claim that F was killed was **false** for this path; the gap is now closed with tests) |
| F2 | `/mute` non-positive-duration refusal → fake success | new lifecycle assertion | FAILED ✅ killed (**new**) |
| G | `to_thread` wrapper removed from ingress call | `test_bot_layer_offloads_sync_db_engines` (names `handlers.py:953`) | FAILED ✅ killed (**new**) |

**Mutation score: 8/8.** v1's remaining live mutants (C, G) are dead; two additional untested typed-failure paths (F, F2) found alive during closure and killed.

---

## K — Tests (exact commands, Python 3.11.2, venv == CI install path)

```text
pip install -e ".[dev]"                                  # sqlmodel 0.0.42 pin respected
ruff format --check .                                    # 499 files, green
ruff check .                                             # green
mypy src                                                 # Success: no issues found in 240 source files
pytest tests/unit/test_bot_command_wiring.py \
       tests/unit/test_moderation_flood_and_thread.py \
       tests/architecture/test_async_db_offload_guard.py # 18 passed
pytest -q -rs -m "not slow" --tb=short                   # 2332 passed, 30 skipped (CI parity command)
python scripts/check_version_lockstep.py                 # ok: 3.13.0
python scripts/extras_matrix.py check                    # ok
python scripts/agent_board.py check --files <changed> --branch arena/01a0da85-nexus-ai-agent
                                                         # no overlap — safe to proceed
```

Skips are all intentional (PostgreSQL-required, extras-matrix legs proven in dedicated CI jobs).

**Parity 3.11 disposition:** the exact parity command passes on CPython 3.11.2 with the full `.[dev]` install (fresh resolution, 2026-09-25). The `0199e58` CI leg failure was therefore not reproduced in an independent 3.11 environment, and its log/artifact is unobtainable from this sandbox (`productionresultssa0.blob.core.windows.net` → EOF; documented, not an evidence claim). The engineering question is settled authoritatively by CI itself: **parity 3.11 is green on the final SHA** (§L). The only tree where 3.11 is known red in CI is the superseded `0199e58`.

---

## L — Exact-SHA CI

| SHA | Purpose | CI run | Result |
|-----|---------|--------|--------|
| `0199e58fce93d62b6db17ea945811235ec6bb2e9` | original implementation (superseded) | `36187605207` | FAILURE — `lint`, `lint-fast` (ruff format on `test_bot_command_wiring.py:69/441`), `python-parity (3.11)` (log unobtainable, non-reproducible locally); 9 jobs green |
| `e405e07d57c8020025f5677db5bed85c88191771` | v1 forensic report commit (docs-only) | `36191473790` | FAILURE — `lint`/`lint-fast` caused by the report's own non-format-clean Markdown code blocks (new root cause, §A); **parity 3.11 green** |
| `60188f2107cb61663cc136faa7d8f78a995c60de` | **FINAL IMPLEMENTATION** (0199e58 + closure fixes/tests) | `36194369744` | **SUCCESS — 12/12 jobs**: lint, lint-fast, test, extras-matrix ×4, python-parity 3.10/3.11/3.12, migrate-postgres, release-lineage |
| *(branch head = report+board commit)* | forensic closure artifacts | *(run recorded in PR body)* | docs-only on top of `60188f2`; head CI green required before merge |

---

## M — PR

PR opened from `arena/01a0da85-nexus-ai-agent` (head = report+board commit) against `main`, containing `0199e58` + `60188f2` + this report + board row. PR body carries: Problem, Implementation, Evidence (tests/CI/exact SHAs), Security, Known limitations (anon decision), Verification. Number and status recorded in the final session report; no other PR was touched.

Pre-PR checklist verified: branch current with remote; diff vs main contains exactly the 6 task-122 files (board, handlers, moderation, 1 test modified, 3 added) — no unrelated changes (`git diff origin/main --stat`); lint/mypy/tests/lockstep/extras/overlap-check all green locally; CI green on `60188f2` and on the branch head.

---

## N — Board

- Before: `task-122` = `active`, `agent_branch: arena/01a0da12-nexus-ai-agent`, TTL 24h (claimed `2026-09-25T20:03:09Z`), while main's copy showed drift.
- After (this commit): `task-122` = **`active_in_review`**, `agent_branch: arena/01a0da85-nexus-ai-agent`, `prev_agent_branch: arena/01a0da12-nexus-ai-agent`, `ci_evidence` = run `36194369744` (12/12 green on `60188f2`), note updated with closure summary and the open product decision. Board status does not replace CI/code evidence — it points at it (`board.json` validated by `tests/unit/test_agent_board.py` in the green run).

---

## O — FINAL VERDICT

### `VERIFIED_WITH_PRODUCT_DECISION`

Every engineering death condition is closed with live evidence: exact final-SHA CI fully green (`60188f2` → `36194369744`, 12/12), lint green (pinned ruff 0.16.8 + latest), mypy green, full suite green locally on the 3.11 interpreter (`2332 passed`) and in CI on 3.10/3.11/3.12, 12-command matrix fully evidenced from code, moderation ingress proven bypass-free for LLM paths, mutations 8/8 killed (C and G included), thread-offload invariant enforced by two new architecture gates, PR opened from the correct final branch, board reflects the true state.

The single remaining item is **not** an engineering blocker: whether anonymous peer-to-peer messages must pass spam/flood/link/profanity moderation is a product policy with no written decision in the repository (`NEEDS_PRODUCT_DECISION`, §E). Engineering gates stay green either way; the surgical fix is pre-designed and gate-covered the moment the owner decides.

---

*Evidence first. Truth second. Decision third. Code last.*
