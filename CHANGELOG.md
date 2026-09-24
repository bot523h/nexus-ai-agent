# Changelog

All notable changes to NEXUS AI Agent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (apply-lane lifecycle closure — session `arena/01a0d365-nexus-ai-agent`)

- **REGISTERED → RUNNABLE gates** on the apply lane (`creative/rendering/lifecycle.py`):
  path registration is not enough; required FFmpeg filters are probed through
  `executor.py` before encode. Illegal transitions fail typed.
- **Single process site** remains `executor.py` (probe + measure + encode);
  architecture AST ratchet forbids `subprocess` in `lifecycle.py`.
- **Shipped identity LUT** (`assets/luts/identity.cube`) and **Persian burn-in**
  proof via Vazirmatn + `TitleOp`. `LutOp` is duration-neutral; intensity locked
  at 1.0. Telegram `/grade lut` stays refused at the surface (D-0011) until that
  mapper is wired.

### Security (P0 hardening day — session `arena/01a0d23e-nexus-ai-agent`, tasks 165–167)

- **`GET /creative/jobs/{job_id}` is now behind the same fail-closed HMAC gate as the
  POST** (unsigned → 401; no key configured → 503). On the previous baseline it answered
  `200` with full job data — local staging paths and source URLs — to any caller.
- **Upload byte cap on the legacy lane** — multipart bodies over 500 MiB die `413`
  before a job row exists; partial temp files are unlinked on every failure.
- **Both legacy `/creative/*` routes are deprecated** (OpenAPI `deprecated=true`,
  DECISION_LOG D-0010): route set frozen by an architecture ratchet; removal is sequenced
  after PR#58's SSRF scope lands.

### Added (P0 hardening day — creative studio surface, task-166)

- **`/edit` `/caption` `/grade` are wired end-to-end (D-0011)** — Telegram →
  `creative_surface` (pure mapper + workspace staging) → `JobQueuePort.enqueue`
  (`creative_render`, idempotency key anchored to the Telegram message id) → worker
  adapter `creative/render_jobs.py` → packs runtime registry → `CommandBus` → render
  lane → measured artifact (probe + sha256) → translated completion notify with
  workspace ownership. The bogus `mapper` handler key is gone.
- **Honest op matrix** — `edit trim|speed|reverse`, `grade exposure|proxy|otio`,
  `caption transcribe`; `lut` (no shipped `.cube` assets / no lane op) and `burnin`
  (no subtitles instrument in the lane IR) are refused typed at the surface, never
  faked; caption chains fail closed typed when the `[speech]` engine is absent.
- **16 `creative.*` i18n keys × all 15 locales** — raw keys can no longer reach users.

### Fixed (P0 hardening day — backups, task-167)

- **Backup success is now verified, never asserted (D-0012)** — artifacts must exist,
  be non-empty, be sha256-measured, survive a restore-into-temp-SQLite verification
  (`PRAGMA integrity_check` + user-table inventory; the silent empty-dump masquerade
  is rejected) or the pg_dump footer check, and re-download byte-identical after
  upload; any mismatch hard-fails the run (non-zero exit). Success summaries carry
  `sha256` / `size_bytes` / `verified` / `timestamp`.

### Added (dead-engine wiring — session `arena/01a0cb38-nexus-ai-agent`)

- **`bot/surface/ads.py`** — `/ad_create` `/ad_list` `/ad_pause` `/ad_resume` `/ad_delete`
  `/ad_stats` now drive `AdManager`, which previously had **no importer in `src/`** while the six
  commands answered with constants ("Active Ads: 2, Paused: 1", "5k impressions, 200 clicks" —
  numbers no column can produce). Reads are scoped to the chat; `pause/resume/delete`, which the
  engine keys on a bare `campaign_id`, go through an ownership check first (T14).
- **`bot/surface/channel_management.py`** — `/post` `/schedule` `/pin` `/ban` `/unban` `/stats`
  `/welcome` now call `ChannelManager` instead of replying `"(simulated)"`. One manager per
  application, memoised in `application.bot_data` and bound to the live bot; Telegram failures are
  reported instead of masked by a success string; `/stats` shows the live member count and states
  plainly that messages-per-day is not measured.
- **`bot/surface/onboarding.py`** — the `^onboarding_` callbacks reach
  `handle_onboarding_callback`, so the three keyboard buttons show the engine's hint in the
  caller's language (15 locales) instead of one "step completed" sentence that also destroyed the
  message for unknown payloads.
- **`bot/surface/_ptb.py`** — three accessors: `reply_to_message_id`, `reply_to_user_id`,
  `user_language_code`, each tolerant of malformed updates.
- **Guards that keep the stubs out** — `test_surface_registration.py` `EXPECTED` grows from 7 to
  20 commands, gains a callback-pattern map, and forbids 13 more literal replies; a subprocess probe
  asserts `bot/surface` stays importable without `telegram` (MODULE_MAP §3 R12).
  94 new tests: **1728 passed, 1 skipped** against a measured base baseline of
  **1634 passed, 1 skipped**; three injected regressions (a stub lambda, a removed ownership check,
  a dropped registry entry) turn five tests red and are then restored.
  Record: `docs/audits/DEAD_ENGINES_2026-09-22.md`, decision D-0009.

### Fixed

- `AdManager.create_campaign` annotated `interval_hours: int` while `AdCampaign.interval_hours` is
  a `Float`; the first real caller (this wiring) made `mypy` catch it. Intervals such as `0.5` h
  are now type-legal.

### Added (color/exposure lane — session `arena/01a0c58e-nexus-ai-agent`)

- **`exposure` lane op (`creative/rendering/`):** `ExposureOp`, the executable
  twin of the pack operation `color.adjust_exposure`. Photometric mapping
  `gamma = clamp(2**EV, 0.1, 10.0)` — correct by construction because `vf_eq.c`'s
  LUT is `v ** (1/gamma)`, so `2**EV` doubles exposure per stop and positive EV
  brightens. Contrast passes straight through into `eq`; `temperature_k` accepts
  the full 1000–40000 K `colortemperature` declares (6500 = the filter's own
  neutral); `tint/50` maps onto `colorbalance`'s `gm` with **positive = green**;
  `pl` is always emitted explicitly rather than left to each filter's default.
- **Clamping happens in the IR, never in FFmpeg.** `eq` clips gamma to
  `[0.1, 10.0]` and contrast to `[-1000, 1000]` *silently* via `av_clipf`, so the
  lane clamps first: what the argv says is what the pixels get. The usable
  unclamped band is therefore ±log2(10) ≈ ±3.32 EV, and both clamp ends are
  pinned as golden contracts — a future FFmpeg range change turns the suite red
  on purpose instead of quietly altering a master file.
- **One shared pixel-format round trip.** `eq` accepts planar YUV only while
  `colortemperature`/`colorbalance` accept RGB only (disjoint sets), so a
  conversion is unavoidable; it happens once each way and both RGB filters share
  it. At neutral both RGB stages are **elided** — `kelvin2rgb(6500)` is
  ≈ `(1.000, 0.997, 0.981)`, not an exact identity, and neither filter
  short-circuits — which also removes the round trip for a plain exposure edit.
  `eq` is always emitted because `vf_eq.c` `check_values` makes it a genuine
  no-op at neutral.
- **Fail-closed guards:** an `exposure` op on an audio-only lane is a
  `LaneError` (the video chain is never built, so the grade would vanish while
  the journal still claimed it); out-of-range values are rejected by the model,
  and `extra="forbid"` keeps undocumented knobs out.
- **16 golden pins** (`tests/unit/test_rendering_lane_exposure.py`) covering the
  photometric mapping, both clamp ends, the monotonicity of gamma in EV, the
  contrast pass-through and `eq`'s non-LUT fast path (`|contrast| < 7.9`), the
  tint→`gm` endpoints, neutral elision, the shared round trip, explicit `pl`,
  and composition with `trim`/`title`.
- **Duration-algebra property guard** (`tests/unit/test_lane_duration_algebra.py`,
  162 cases): 40 seeded random lanes of 24–32 ops, each checked three ways —
  against a from-scratch restatement of the microsecond algebra that shares no
  code with the compiler, against the `-t` in the real argv (two-sided
  accounting), and for byte-identical recompilation. A new `LaneOp` that the
  restatement has not classified fails a structural guard, so an op that moves
  the clock cannot slip through an `isinstance` chain.
- **Docs:** `docs/ops/COLOR_LANE.md` (mapping table, seven hard contracts, an
  FFmpeg-free validation command, troubleshooting table) and decisions
  **D-0005…D-0008** in `docs/DECISION_LOG.md` — no `SplitOp` until a
  multi-output encode exists; `.nexus/continuum.json` refreshed only at a
  release cut (it is under `task-135`'s exclusive-path lease); every colour
  bound sourced from FFmpeg's filter code rather than prose docs; no
  `tonemap=hable` until the lane can read the source's colour metadata.
- **Real-encode evidence (local, not a committed test):** five grades encoded
  through FFmpeg 7.0.2 (static `imageio-ffmpeg` wheel) on a real 2 s source;
  measured mean luma is monotonic in EV (67.97 → 92.84 → 133.37 for −1/0/+1, a
  65.4-level two-stop spread), white balance measurably moves pixels, duration
  is unchanged, and every graph — including the `yuv420p → rgb24 → yuv420p`
  round trip and `pl=1` — was accepted. Tables in `docs/ops/COLOR_LANE.md`;
  turning this into a permanent gate is staged as
  `color-lane-real-encode-evidence`.
### 2026-09-21 — agent B (`arena/01a0c634-nexus-ai-agent`)

### Fixed (agent B · `arena/01a0c634-nexus-ai-agent`)

- **Gamification — `/daily` was unreachable and its streak bonus was silently lost.**
  `GamificationEngine.claim_daily` created the user's row with `last_daily = now` and then ran the
  24 h window check against it, so a brand-new user always got `already_claimed` and the pending
  INSERT was rolled back (no row, no XP — ever). For a pre-existing row the method called
  `update_streak()`, which commits on a **second** connection; the first session then wrote its
  stale object back over it (SQLAlchemy writes every column), resetting the streak to `0` and
  dropping the streak bonus. Legacy rows additionally raised
  `TypeError: can't subtract offset-naive and offset-aware datetimes`.
  The whole reward is now computed and committed in **one** session, `last_daily` is no longer
  prefilled, `_as_utc()` normalises naive SQLite timestamps, and the payload gained
  `streak_broken`, `title`, `xp` and `remaining_minutes`. (`tests/unit/test_gamification_daily.py`,
  10 tests, A/B proven: 10 fail against the previous blob, 10 pass against this one.)

### Added (agent B · `arena/01a0c634-nexus-ai-agent`)

- **Document RAG is now a real retriever (task-127).** The 96-line engine on `main` sliced every
  document into fixed 1000-character windows with no overlap and imported `chromadb` +
  `flashrank` eagerly. It is replaced by a two-layer design:
  - `features/rag_core.py` — **stdlib-only**: a lossless recursive chunker (paragraph → line →
    sentence → clause → word, 384-token windows, 15 % overlap, offsets into the source text), an
    Okapi BM25 index with Persian/Arabic folding (`كتاب` = `کتاب`, `۴۲` = `42`), weighted
    reciprocal-rank fusion, cosine similarity, a hybrid retriever with an injectable embedder, and
    a `recall@k` / MRR / hit-rate evaluation harness.
  - `features/rag.py` — a thin **adapter**: `chromadb`/`flashrank`/the embedding model are imported
    lazily, `client` / `embedding_fn` / `ranker` are constructor-injected, blocking calls run in a
    worker thread, documents are idempotent per `file_id`, and a missing vector stack raises
    `RAGUnavailable` with an install hint instead of silently storing nothing.
  Measured on the frozen 8-document corpus (6 labelled probes, `k=3`): **BM25-only recall@3
  `0.8333`** (it cannot answer the paraphrased probe) → **hybrid recall@3 `1.0000`**, and the
  engine scores the same `1.0000` end-to-end. `AdvancedRAGEngine.add_document/query` keep their
  signatures, so `worker.py` is untouched.

### Added (agent B · `arena/01a0c634-nexus-ai-agent`)

- **`bot/surface/` — a framework-free command layer for the seven commands that were still
  hard-coded stubs** (`/daily` always answered `+50 XP!`, `/docs` always claimed to be empty,
  `/doc_delete` always claimed success, `/chat_with_doc` always claimed to be active).
  - `_ptb.py` — duck-typed accessors over PTB's `update`/`context`, so no module in the package
    imports `telegram` (frozen import boundary) and every handler is testable with fakes.
  - `gamification.py` — `/daily`, `/profile`, `/achievements`, `/xp_leaderboard` on the real
    `GamificationEngine`, scoped per (user, chat), with the synchronous SQLite calls off-loaded via
    `asyncio.to_thread` so the event loop stays free.
  - `docs.py` — `/docs`, `/doc_delete`, `/chat_with_doc` on the real document store and the hybrid
    retriever, plus free-text routing while doc-chat mode is active. Sessions have a 30-minute TTL
    and a hard cap (no per-user memory leak), the vector stack failing closed raises a Persian
    "not available on this server" message instead of a stack trace, and nothing is ever claimed to
    have happened when it did not.

### Changed (agent B · `arena/01a0c634-nexus-ai-agent`)

- **`bot/handlers.py` — the seven stub commands now run real code.** Only the import block and the
  stub definitions changed (`+30/-30`); every `CommandHandler(...)` registration line is untouched,
  because the handlers kept their names and only their import source changed. The stub closures were
  deleted rather than bypassed — dead code that answers with a fixed string is worse than no command.
  Free text is now offered to the document retriever inside `on_message` **before** the LLM path, so
  `/chat_with_doc` costs no egress and no tokens. The wiring is locked down by an AST contract test
  (`tests/unit/test_surface_registration.py`): which symbols are imported, one registration per
  command, no local definition shadowing an imported handler, no stub literal ever passed to a reply,
  and doc-chat routing ordered ahead of the correlation-id/LLM section.

### Audit (agent B)

- `docs/audits/PR32_TRIAGE_2026-09-21.md` — forensic triage of PR#32 against the merged PR#34:
  15/26 files superseded, 4 conflict-debt, 3 port, 4 adapt; PR#32 is not rebase-and-merge material.


### Added (wave-4 hardening — agent G `01a0c4bb` — 10-step batch, 6 steps delivered)

- **Version-lockstep CI guard (wave4-1):** `VERSION == pyproject.toml == CHANGELOG`
  checked by `tests/unit/test_version_command.py` (mismatched fixture → red,
  green on main) + new `lint` job in `ci.yml` (ruff + mypy + lockstep) and
  `.pre-commit-config.yaml` (pinned ruff v0.8.6 + mypy v1.10) — prevents the
  3.12.0-vs-3.13.0 drift the hygiene pass fixed.
- **Delivery signing spike (wave4-2):** `creative/packs/delivery/signing.py`
  (Ed25519 via PyNaCl when installed, HMAC-SHA256 fallback, canonical JSON,
  base64 transport, constant-time verify, fail-closed `SigningError`) with
  8 unit tests (`test_delivery_signing.py`).
- **Storage resilience (wave4-3):** `storage/resilience.py` (exponential backoff
  with jitter, idempotency `sha256` key, secret-safe `redact_secrets`,
  `retry_with_backoff` for idempotent ops, env-tunable `NEXUS_STORAGE_*`)
  with 11 unit tests including chaos.
- **Memory eval harness + architecture guard (wave4-4):** `memory/eval.py`
  (`recall@k` over deterministic fixture, baseline `0.5` with 15 % tolerance,
  stub LLM offline) + `test_memory_recall.py` + `test_memory_boundaries.py`
  (AST guard: `memory/` must not import `features/`/`bot/`).
- **Creative surface (wave4-5):** `bot/creative_surface.py` (pure mapper +
  PTB glue, `/edit`/`/caption`/`/grade` via `JobQueuePort` `creative_render`,
  30 s limit, typed `CreativeFailure` → i18n, zero touch on `handlers.py`)
  with 7 unit tests.
- **Bench harness (wave4-6):** `scripts/bench_render.py` + `scripts/bench_caption.py`
  (pure IR compile / SRT format, deterministic, GPU-free) + committed baselines
  `tests/bench/baseline_*.json` + `test_render_bench.py` regression gate (15 %
  threshold, 50 % slack in unit test).
- **Ops hardening (wave4-8/9):** `docs/ops/RUNBOOK_HARDENING.md` (version bump,
  signing, resilience, memory, bench, smoke) + `scripts/smoke_e2e.py`
  (offline contract checks, compose half deferred post-#33).

Queued for next agents (still 10-step board, disjoint paths):
`wave4-7` pack coverage 95 %/mutation, `wave4-10` op-gap 6 ops — see
`.agents/board.json → ten_forward_tasks_wave4`.

### Fixed (supersession fix — session `01a0c506`, PR#40 CI root cause)

- **Bench cores moved into the installed package:**
  `bench_render_ir_compile` → `nexus_ai_agent.creative.rendering.bench`,
  `bench_caption_format` → `nexus_ai_agent.creative.caption.bench`;
  `scripts/bench_*.py` remain thin CLIs with identical argparse interfaces.
  Root cause of the red CI `test` job: the unit bench imported the
  unpackaged `scripts/` namespace, which is invisible to the console-script
  `pytest` CI uses (repo root not on `sys.path`) while `python -m pytest`
  masked it locally.  Adds
  `tests/architecture/test_scripts_import_boundary.py` (the class of error
  is now mechanically closed) and allows stdlib `time` in the rendering-lane
  import allowlist for the pure timing harness.
  Evidence & A/B reproduction: `docs/audits/FORENSIC_PR40_CI_2026-09-21.md`.

### Added (session `01a0c460` — tasks 125/129/130/115)

- **Creative op-gap (task-125):** six pure Level-B pack operations —
  `audio.remove_noise`, `audio.deess`, `audio.eq_voice`, `audio.time_stretch`,
  `motion.stabilize`, `motion.add_parallax` — with input models, registry
  entries, manifest capabilities, and 12 unit tests.
- **i18n parity (task-129):** all 15 locales at 63/63 keys with matching
  `{placeholders}` (486 keys filled, 120 verbatim-English onboarding blocks
  translated, `ai.activated` `{rpm}`/`{daily}` restored in 13 locales, 7
  Persian audit fixes) plus a 34-test parity gate
  (`tests/unit/test_i18n_parity.py`).
- **Deploy smoke + runbook (task-130):** `scripts/deploy_smoke.py` (offline
  manifest/contract checks plus live `/healthz` and webhook-gate probes) and
  `docs/ops/DEPLOY_RUNBOOK.md` (preflight → deploy → smoke → rollback →
  incidents), with 21 unit tests.
- **llama.cpp server provider (task-115):** `LocalLlamaServerProvider`
  (OpenAI-compatible `/v1/chat|embeddings`, `/health`; zero new
  dependencies) behind `NEXUS_LLAMA_SERVER_*` settings, wired into
  `build_llm_provider` as an opt-in priority (default off), with 15 unit
  tests and clearer error hints on the legacy in-process GGUF path.

### Maintenance (repo hygiene pass, 2026-09-21 — owner-directed)

- **Docs reorganized.** Session audits moved to `docs/audits/`; v1/v2-era plans,
  phase records and stale todo checklists archived under `docs/history/`;
  ops runbooks grouped in `docs/ops/`. `docs/README.md` added as the single
  documentation index. No content was deleted — archival only.
- **Removed the broken root `termux_install.sh`** (it installed from a
  nonexistent `requirements.txt`); `scripts/termux_install.sh` repaired to use
  the canonical entrypoint (`python -m nexus_ai_agent.cli run-bot`) and the real
  env-var names (`TELEGRAM_BOT_TOKEN`, `NEXUS_OWNER_TELEGRAM_ID`).
- **Remote-branch janitorial work:** 28 fully merged or closed-superseded
  branches deleted on the remote, with per-branch dispositions recorded in
  `docs/DECISION_LOG.md` (r7). Active session branches preserved.
- **PR #33 closed as superseded, then reopened the same day:** the closure
  cited the duplicated security scope (delivered by merged PR #34) and the
  conflicting head; afterwards the `ci-gates-steward` board (15:21Z) designated
  PR #33 as the **task-110 vehicle** (OTIO round-trip + ConversationStorePort
  adapter), so it was reopened and awaits a rebase on current `main`.

### Fixed (PR#51 integration — session `arena/01a0ca9c-nexus-ai-agent`, 2026-09-22)

- **CI `test` job — three board tests were wall-clock time bombs.**
  `tests/unit/test_agent_board.py` read the *repository's* board and asserted lease
  behaviour against the real clock, so the three lease-liveness tests detonated the
  moment the first real `active` lease crossed `claimed_at + ttl_hours`
  (2026-09-22T16:21Z → every push after it was red, while `lint`, `lint-fast` and
  `migrate-postgres` stayed green). Failure modes, exactly as predicted by the
  arithmetic: overlap detection saw an expired lease and reported none, a foreign
  `claim` was accepted instead of refused, and a heartbeat renewal took the
  `--ttl` flag (`assert 10 == 24`). The sibling file had been fixed by task-151
  (PR#49); this one was missed. New `_pin_clock_inside_lease()` freezes
  `agent_board._now` one second inside the claim's own lease window — the same
  hermetic pattern the other two board suites use. **A/B proof, hostile clock
  (2027-03-01): the previous file 3 failed / 15 passed, the repaired file
  18 passed**; on the real clock both agree.
- **CI `test` job — `test_rag_eval.py::test_missing_vector_stack_fails_loudly`
  asserted the state of the machine, not the contract.** It expected
  `AdvancedRAGEngine.client()` to raise "chromadb is not installed", but
  `chromadb` is a **core** dependency (`pyproject.toml`), so CI and every correct
  `pip install -e ".[dev]"` tree have it: the test only passed where the package
  was absent. The absence is now *simulated* (`sys.modules["chromadb"] = None`
  makes `import chromadb` raise `ImportError`, the exact failure mode of a tree
  without the vector stack) and the assertion also pins that the message stays
  actionable (`pip install`). Green in both legs: chromadb installed, and
  chromadb import-blocked.
- **`test_completion_hook_fires_on_terminal_states` raced on notification
  order.** `enqueue` schedules one task per job, so two jobs complete
  concurrently and the order of the two hook notifications is a scheduling
  accident (it flipped under full-suite load — green alone, red in the suite).
  The test now asserts the contract per job: exactly two notifications, one per
  terminal state, each carrying its own status/result/error/payload.

- **Merged to `main` as `315d38a` (PR#52)** together with the PR#51 content it integrates: agent B's
  residual feature wiring — real RAG (`features/rag_core.py` + eval harness), the gamification `/daily`
  single-session fix, the framework-free `bot/surface` package — with PR#47 and PR#51 both auto-marked
  merged. CI run `35776958516`: 4/4 checks green (`test`, `lint`, `lint-fast`, `migrate-postgres`);
  `pytest -m "not slow"` → 1747 passed / 20 skipped / 0 failed. The suite is also green under a frozen
  future clock (2027-03-01), i.e. no remaining wall-clock time bombs.

## [3.13.0] — 2026-09-21

Semver-minor: **P0 Week-1 security batch + feature-engine wiring.** Delivers
the four "stop the bleeding" items from the 2026-09-21 audit
(global auth, dashboard PII, path traversal, README honesty) and wires the
previously dead feature engines to the live Telegram surface
(P0-1/P0-4/P0-8/P0-10, audit §12 items 5-7 and 9).

### Changed (P1-2: event-loop non-blocking)

- **All sync-DB feature engines offloaded via `asyncio.to_thread`.**
  `ReminderSystem`, `ReferralEngine`, `ForceJoinManager` and
  `AnonymousChatManager` previously executed synchronous SQLite sessions
  directly inside the `async` handler coroutines, blocking the event
  loop on every message. Each engine now:
  (1) caches a single `create_engine(..., check_same_thread=False)` per
  `db_path` (the `_sync_engine` is `@lru_cache`'d or stored on `self`);
  (2) separates a pure-sync DB core (e.g. `_persist_reminder_sync`,
  `_create_session_sync`, `_end_sessions_sync`, `_report_sessions_sync`,
  `_is_enabled_anywhere_sync`);
  (3) the async public API calls `await asyncio.to_thread(sync_core)`.
  The `task.cancel()` and `_schedule` calls stay on the event-loop
  thread (not inside the worker thread). `ForceJoinManager.should_block`
  (called on *every* message) and all referral/force-join owner
  command-sites now use the same pattern.

### Security

- **Global deny-by-default access guard (P0-2).** New
  `bot/access_guard.py::AccessGuardHandler` is registered in handler
  **group -1** (before every command, callback and free message).
  `check_update` claims only *denied* updates, so unlisted users get one
  rate-limited denial (3/min) plus an audit log entry and nothing else;
  allowed users flow through untouched. Previously only `on_message` and
  `/imagine` consulted the allow-list — ~80 other commands were open.
  Per-command checks stay as defense-in-depth.
- **Dashboard PII removal + bearer gate (P0-5).** `/api/dashboard/*`
  responses no longer contain `telegram_id`/`username` (`/recent_users`
  returns only the internal DB id + join time). New
  `NEXUS_DASHBOARD_TOKEN` setting: when set, every dashboard request must
  carry `Authorization: Bearer <token>` (constant-time compare, 401
  otherwise); when unset the API is open and `docker-compose.yml` now
  binds port 8000 to `127.0.0.1` by default.
- **Path traversal fixes in `/cloud` and `/download` (P0-6).** User- and
  DB-controlled file names now go through `bot/safe_paths.py`
  (`sanitize_file_name` + `safe_join` with `is_relative_to` containment),
  with a unique suffix to prevent same-name overwrites. The unclosed
  `open(local_path, "rb")` file-handle leak in `/download` is fixed.
- **Duplicate `/start` handler removed (P0-4, part 1).** The second
  `CommandHandler("start")` (which PTB could never fire) is gone; the
  single `/start` handler now parses referral deep links.
- **LLM-egress consent gate for AIMemory (P0-7).** Every `/memory`
  extraction (the path that sends raw user message text to external
  Gemini) now passes a three-stage gate *inside* the engine:
  (1) `NEXUS_AI_MEMORY_ENABLED` global kill switch (default true);
  (2) per-user explicit consent vote (`aimem:grant`/`aimem:deny` via a
  one-time inline-keyboard question, **default-deny** — unset users
  never egress);
  (3) per-user in-process rate limit
  (`NEXUS_AI_MEMORY_MIN_EGRESS_SECONDS`, default 120s).
  `/forget_me` now also wipes the consent record (forget ⇒ revoke).
  New Alembic revision `7c2f9d41e8a3` (revises `f4a9c2e71b08`) adds
  three nullable columns to `usermemory` (zero-drift on `alembic check`).
  CI pinned to the new head.

### Added

- **Safe calculator engine** (`features/calculator.py`): strict AST
  whitelist evaluator — no `eval`, no attribute access, no subscripts,
  no strings/containers; bounded length (200), node count (128), depth
  (64), integer exponents (≤1000), factorial (0-170) and result
  magnitude; Persian/Arabic-Indic digits and `^`/`×`/`÷` normalized.
  `features/tools.py::Calculator` is now a thin UI wrapper over it.
- **ReminderSystem rewrite** (`features/tools.py`): reminders are
  delivered to the **originating chat** (the old code sent to
  `chat_id=user_id`); the bot is **bindable** at startup
  (`bind()`); users can **cancel** their own reminders
  (`cancel_reminder`, ownership-checked) and list them
  (`list_reminders`); `restore_pending()` now actually **delivers
  overdue** reminders after a restart (they were silently marked
  "sent" without any send) and survives SQLite's naive/aware datetime
  round-trip; all sends are timeout-bounded; failures are recorded as
  `failed` instead of lost.
- **Feature-engine container** (`bot/feature_handlers.py`): one shared
  `FeatureEngines` instance (calculator, reminders, translator,
  converter, quiz, number-guess, wordle, poll, referral, force-join,
  anon chat) built in `_init_v2_engines`, stored in `bot_data`, and
  passed into `build_handlers` — the per-call "new instance" pattern
  that dropped game state is gone, and `ReferralEngine` is no longer
  constructed twice (P0-8).
- **Live wiring for the dead engines (P0-1, audit §12.9):**
  `/calc` computes; `/remind <time> <text>` persists + schedules;
  `/cancel_remind <id>` and `/reminds` manage them; `/tr [from to] text`
  uses the real MyMemory engine; `/convert <amount> <from> <to>` uses
  the real converter; `/guess <n>` joins `/guess_start`/`/guess_stop`;
  `/wordle [<5-letter>]` and `/wordle_stop` run the real Persian Wordle;
  `/poll Q | A | B` creates real inline polls with per-user votes and
  live results; `/quiz` answers now score against the shared engine.
- **Referral loop actually records referrals (P0-4, part 2).**
  `/start ref_<code>` calls `ReferralEngine.process_referral` (previously
  dead code): the row is written, the referee sees the reward + their
  own link, self-referrals are silently ignored, bad codes get a gentle
  warning.
- **Force-join with a real bot (P0-3).** The shared
  `ForceJoinManager` is bound to the application bot at startup, so the
  verify button performs a genuine `get_chat_member` check instead of
  failing open. While force-join is enabled, non-members hitting free
  text get the join keyboard instead of the AI.
- **Anonymous chat with a real bot and a delivery path (P0-10).**
  `AnonymousChatManager` is bound at startup; plain messages from paired
  users are routed to their partner (custom `MessageFilter` registered
  before the catch-all) with a sender acknowledgement; `has_active_session`
  added to the engine.
- **Startup/shutdown lifecycle for the wiring:** `post_init` binds the
  bot to reminders/force-join/anon and restores pending reminders;
  `post_shutdown` closes the reminder engine.
- **108 new tests** across `test_safe_calculator.py`,
  `test_reminder_system.py`, `test_safe_paths.py`, `test_access_guard.py`,
  `test_dashboard_api.py`, `test_feature_wiring.py` — behavioural
  assertions (the calculator really computes, the guard really blocks,
  the referral row really lands), including classic `eval` escape
  payloads and DoS inputs.

### Changed

- `/calc` percent: `50%` no longer means "divide by 100"; `%` is now
  proper modulo (`10 % 3 = 1`). Use explicit division for percentages.
- Unlisted Telegram users are now denied on **all** surfaces (previously
  two). If your deployment relied on open access, set
  `NEXUS_ALLOWED_USER_IDS` (and/or `NEXUS_OWNER_TELEGRAM_ID`) explicitly.
- Architecture baseline: `bot/access_guard.py` and
  `bot/feature_handlers.py` are registered in
  `tests/architecture/legacy_baseline.json` (bot-layer files importing
  `telegram`, same category as all existing `bot/*` files).

## [3.12.0] — 2026-09-21

Semver-minor: **Nagar Phase 6 — the Wave 2.5 Telegram slideshow surface and
Wave 3 image generation adapters.** These are additive, user-facing features,
not fixes to v3.11.0: `/imagine`, opt-in slideshow autofill and a new provider
contract. Existing `/image` behavior, upload-only slideshows, public ports and
database schemas remain compatible. Merged through PR#25 (`316ed33`) and
PR#26 (`52329e6`). The planned optional local upscale stage is **not included**.

### Added
- **Wave 3 image generation adapters** (`creative/image_gen/`): the asynchronous
  `ImageGenProvider` contract, Pollinations default and explicitly paid Gemini;
  bounded transport/429/5xx retry with backoff, jitter and capped Retry-After;
  validated image bytes and bounded response sizes; no redirect or paid fallback.
- **Process-local prompt-hash cache:** SHA-256 over provider, model and request
  fields; one-hour TTL, 16 entries / 32 MiB, LRU eviction and serialized requests
  to avoid duplicate in-flight calls. Failures/cancellations are not cached.
- **`/imagine <description>`** sends text to the configured image provider and
  delivers image bytes. New generation paths require the existing owner/allowlist;
  `/imagine` also uses the existing request limiter. Legacy `/image` is unchanged.
- **Opt-in slideshow autofill:** `/slideshow --slides 5 --fill <title>` uses three
  uploaded photos plus exactly two generated images in the existing queue/render
  lane. A count alone is not consent; uploaded photos never go to the generator.
  The worker revalidates consent/counts, preserves five-image/30-second limits,
  and removes partial generated files on failure/cancellation. Successful MP4
  delivery and deletion remain owned by the notifier.
- **Fail-closed paid generation and cost events:** Gemini requires `paid_tier`,
  an API key and a positive operator estimate even before serving a cache hit.
  Cost events record successful HTTP responses, including undecodable responses
  that may be billed; rejected guards and cache hits add no cost event. Estimates
  are not an invoice/spending cap, and ambiguous failures may still incur charges.
- **Regression and architecture coverage:** offline network failures, precise
  cost-log guards, TTL/LRU/byte limits, cancellation, real command registration,
  queue autofill and cleanup; AST checks prevent direct/transitive imports of
  `bot` or `storage` from the image adapters, including relative imports.
- **Wave 2.5 Telegram slideshow surface** (details below): `/slideshow` collects
  photos, queues a single render and returns the measured master through the
  existing completion hook rather than rendering inline.
- **The pack gains its fourth approved target duration**
  (`creative/packs/slideshow/models.py`): `30_000_000` µs joins
  `TARGET_DURATIONS_US`/`TargetDurationUS` so the Wave 2.5 30-second ceiling is
  a representable plan target; the set stays closed and every previously valid
  plan (1/2/5 minutes via CLI) is untouched. Unit coverage proves 30 s tiles
  exactly (`MIN_SHOT_US` respected, shots contiguous) for 4- and 12-image sets.
- **`creative/slideshow/worker_adapter.py`** — the queue-side seam for the new
  `slideshow_render` job type: a strict `SlideshowRenderPayload` envelope (≤
  `MAX_IMAGES=5` images, duration pinned to the 30 s cap, every path confined
  to the job's own freshly created workspace — traversal payloads are refused
  and never deleted), then `asyncio.to_thread(render_from_files, …)` — the
  same pure planning+encode entry point the CLI uses, one FFmpeg process,
  measured evidence back through the bus. Expected failures return typed
  `{"success": false, "error_code": …}` codes
  (`ffmpeg_unavailable | render_failed | unusable_image | invalid_request |
  internal`); input images and any failed output are removed in `finally`, a
  successful master is left for delivery, and a 24-hour sweep prunes
  workspaces a crash orphaned.
- **`bot/slideshow.py`** — the pure product surface: `SlideshowSessionStore`
  (per-chat/user rolling buffer, dedup, hard 5-image refusal at the sixth
  upload, 30-minute idle expiry, FIFO-capped to 512 sessions), prompt-as-name
  validation (≤ 60 chars, allow-listed grammar — the caption names the project
  because the render lane has no prompt field, r7 item 5), and the complete
  Persian message vocabulary: usage, `friendly_success` (measured
  seconds/shots/resolution/size), `friendly_render_error` total over the code
  set. Tests assert no path, traceback or raw exception detail can ever reach a
  chat.
- **`bot/slideshow_handlers.py` + registration** — thin PTB glue:
  `/slideshow` starts collecting, photos buffer with `🖼 n/5` receipts, a photo
  captioned `/slideshow <عنوان>` (or the bare command) downloads the session's
  images into `creative_temp_dir/slideshow_<user>_<uuid>/` and enqueues via
  `JobQueuePort` with the handler replying `⏳` plus the job id; no queue, no
  render, ever. Registered in `build_handlers()` beside the pdf/story jobs.
- **`bot/slideshow_notify.py`** — the D4 completion hook grows one
  `slideshow_render` branch in `bot/app.py`: success sends
  `send_document(master.mp4)` with the measured caption and only then removes
  the workspace (delivery owns the file, r7 item 4); failure sends the mapped
  code message; a vanished artifact degrades to the internal message; without
  an origin `chat_id` the hook is silent but still cleans; the cleanup deletes
  only inside the configured temp root; the hook never raises to the queue.
- **Test surface (+34):** pack 30 s tiling (`tests/unit/test_slideshow_30s_target.py`),
  pure surface limits/session/mapping (`tests/unit/test_bot_slideshow_surface.py`),
  notifier with `telegram` stubbed as a `ModuleType`
  (`tests/unit/test_bot_slideshow_notify.py`), and a real-queue round-trip with
  the encoder mocked (`tests/integration/test_bot_slideshow_flow.py`) covering
  delivery, cleanup ownership, the trust-boundary envelope and the traversal
  refusal. The architecture baseline gains the two new Telegram-facing files as
  grandfathered entries — the import-boundary gate stays frozen otherwise.

### Changed
- Release metadata is synchronized at 3.12.0; README/environment examples explain
  generation consent, provider setup and billing caveats. Roadmap and continuum
  distinguish the shipped image adapters from the still-deferred upscale stage.
- The manual RTL debug script is replaced by an isolated `tmp_path` regression
  test with assertions, avoiding generated files in the repository root.
- `nexus.worker.default_job_handlers()` now maps `slideshow_render`; the queue,
  `JobQueuePort`, the bus, the manifest and the render IR are unchanged.

### Removed
- Stale root lint/type/test reports, the generated `test_story.png` and the unused
  downloaded font ZIP; the actual runtime font asset is retained. Ignore rules
  prevent disposable reports and the root test render from returning.

## [3.11.0] — 2026-09-20

Semver-minor: three backward-compatible feature waves (Wave 2a substrate,
Wave 2b slideshow pack, Wave 2c render lane) landed on `main` after v3.10.0.
Canonical state grew additively (Wave 1 states stay valid), the Wave 1
catalog stays frozen, and no existing command, setting or table changed
meaning. Merged through PR#21 (`865780e`), PR#22 (`aa7b2f4`) and PR#23
(`ebe995a`).

### Added
- **Nagar Phase 6, Wave 2a — the capability-pack substrate
  (`src/nexus_ai_agent/creative/packs/`)** (PR#21): the TDD rule “a pack is
  data plus pre-registered adapters, never arbitrary code” is now mechanical.
  - `manifest.py`: the strict `nexus.capability-pack.v1` manifest — artifact
    digests and relative paths, runtime/network policy, permissions, hardware
    and resource budgets, compatibility, security — with `extra="forbid"`
    everywhere, so `post_install` / `entrypoint` / `shell` / `hooks` fail
    validation instead of being ignored, and `Literal[False]` code gates.
  - `verify.py`: verification that returns **every** finding (never a bare
    boolean) — artifact paths and digests, `runtime_network` forbiddance, media
    egress only with the explicit `egress_media_optin` permission, native
    `network_required` rejection, unknown-permission warnings, signature state
    (`format_only_unverified`, never implied trust), `min_nagar_version`.
  - `registry.py`: `PackRegistry` over the studio capability registry —
    external packs may not introduce unknown operations; builtin packs register
    with *pending* capabilities and activate only once the runtime knows them
    (checked against the live registry, so Wave 2b needed no rework).
  - `slideshow/pack.manifest.json`: the eighth pack
    (`nexus.slideshow.compose`) — five capabilities, `ffmpeg` as a declared
    external binary, opt-in media egress declared truthfully,
    `min_nagar_version 3.10.0`.
  - CLI: `nexus packs list` / `nexus packs verify`; gates in
    `tests/architecture/test_pack_manifest_is_data_only.py` (data-only
    manifests at any depth, import allow-list, no package crossing).
- **Nagar Phase 6, Wave 2b — the slideshow pack plans and composes real
  footage (`nexus.slideshow.compose`)**: five pure operations, one atomic edit.
  - `packs/slideshow/`: the typed payloads (`AssetEvidence`, `BeatGrid`,
    `ImageScore`, `SlideshowAnalysis`, `ComposeInput`, `SlideshowPlan`,
    `RenderInput`), the tone library, the deterministic planning rules and the
    five operations — `slideshow.scan_assets` (B), `slideshow.score_images` (A),
    `slideshow.suggest_tone` (A), `slideshow.compose` (B), `slideshow.render`
    (C, the first level-C operation: heavy export requires `confirmed=true`).
  - `templates/tone_templates.json`: **14 tone templates** (12 primary + 2
    alternates) as *data* — rhythm, transition, motion, color, audio and render
    defaults, never a filtergraph. Plain JSON by design: no new YAML dependency.
  - Planning guarantees: the shots tile the target duration (1/2/5 minutes)
    **exactly**; beat alignment is opportunistic (a sparse grid falls back to
    arithmetic boundaries and says so in `warnings`); auto mode weights shots by
    narrative role; the whole plan is a deterministic function of pinned
    evidence and is re-checked by the plan model.
  - `creative/slideshow/` adapter — the only place that touches the world:
    content-addressed probing (Pillow), WAV decoding and an energy-flux beat
    detector (numpy; **no `librosa`**, see the decision log), local image
    scoring, and an **opt-in** hosted analysis path (Gemini) that is fail-closed
    behind `NEXUS_SLIDESHOW_ALLOW_IMAGE_UPLOAD` and uploads downscaled copies
    only.
  - State extension (additive): `Project.assets`, `Clip.effects`,
    `Track.effects`, `AssetRecord` and `EffectLayerRef` (content-hashed,
    reversible), with `state_hash` extended to cover the asset registry.
  - CLI: `nexus slideshow templates`, `nexus slideshow plan` (auto/manual) and
    `nexus packs activate`.
  - Tests: 90 new unit tests including end-to-end planning over generated
    JPEG/WAV fixtures, plus `tests/architecture/test_slideshow_adapter_boundary.py`
    (manifest ↔ code coherence, heavy imports stay behind the adapter, the
    adapter never bypasses the command bus, templates are data).
- **Nagar Phase 6, Wave 2c — the render lane renders a real master
  (`nexus slideshow render`)**: the plan becomes a file, and the file's measured
  facts become the evidence recorded in state.
  - `creative/slideshow/ffmpeg.py`: the lane is staged so only its last step is
    impure — `render_ir_from_plan` (pure), `build_filtergraph`/`build_command`
    (pure, argv only), `encode` (one process, no shell, timeout, staging file
    published by an atomic rename, never an in-place overwrite).
  - Camera moves come from the template's motion parameters (`zoompan`), grade
    from its color parameters (`eq`/`hue`/`colorbalance`/`vignette`/`noise`),
    transitions from its transition kind and duration (`xfade`) — the pack still
    never writes FFmpeg syntax.
  - Crossfades are **centred on the cut**: each neighbour is authored half a
    transition longer than its slot, so the rendered boundaries stay where the
    pack planned them and the master keeps the promised duration.
  - `probe_video` reads duration, frame size and streams back out of the
    produced file with the same allow-listed binary (no `ffprobe` dependency),
    so the hash and duration recorded in state are measurements.
  - `render_from_files()` runs the whole pipeline and dispatches
    `slideshow.render` (level C) with the pinned facts; the encode happens
    *before* the command, so the bus stays pure and `system.undo` can take the
    record back without touching the file.
  - CLI: `nexus slideshow render --out master.mp4` (auto/manual, `--overwrite`
    to replace a file, `--json` reports the pinned facts).
  - Tests: 19 new tests — the pure IR/argv layer, typed failures, overwrite
    protection, staging cleanup, byte-identical re-encodes of the same IR, and
    **four genuine FFmpeg encodes** (including a 1-minute master verified with
    `probe_video`).

### Changed
- `CapabilityRegistry` gains the Wave 2 slideshow operations through
  `build_slideshow_registry()`; `build_wave1_registry()` stays frozen (Wave 1
  catalog unchanged, still pinned by its architecture gate).
- **litellm no longer reaches the network on first use**: the provider seam sets
  `LITELLM_LOCAL_MODEL_COST_MAP=True` before importing litellm, so the pricing
  map comes from the bundled copy. This removes a hidden network fetch and keeps
  litellm's retry warnings off stdout, which the CLI's `--json` modes depend on.
- The `[dev]` extra gains `imageio-ffmpeg`, so the render tests execute a real
  encoder on any machine; production still uses the system FFmpeg. New settings:
  `NEXUS_FFMPEG_BIN` and `NEXUS_SLIDESHOW_RENDER_TIMEOUT`.
- **Release housekeeping (this cut).** `VERSION` and `pyproject.toml` move to
  **3.11.0** in lock-step (covered by `tests/unit/test_version_command.py`).
  `.nexus/continuum.json` is refreshed for the first time since the PR3 line:
  `step` → `ebe995a` (the Wave 2c merge), the ledger records the merged
  lifecycle line (PR#7) and Nagar Waves 1–2c, and `test_count_expected` →
  **586** — the verifier's invariant counts test *functions* (AST), not
  pytest's 622 collected cases — so `nexus continuum verify` is green again.
  `ROADMAP_STATUS.md` is rewritten around the Phase 6 waves (the stale
  "PR1/PR2/PR3 not merged" rows were wrong: that line merged through PR#7 as
  v3.6.0). `docs/DECISION_LOG.md` **r6** records two owner decisions: image
  generation goes behind an adapter (Pollinations by default, Gemini opt-in
  behind a key, the free core stays free) and **Wave 2.5 — the Telegram
  surface for the slideshow pack — precedes Wave 3**.

## [3.10.0] — 2026-09-20

### Added
- **Nagar Phase 6, Wave 1 — the “Green Cockpit” core
  (`src/nexus_ai_agent/creative/studio/`)**: the first implementation of the
  accepted Nagar architecture (`docs/NAGAR_70_OPERATIONS_TDD.md`). Everything
  is a typed, UI-free command surface — JSON command in, in-memory state
  update out; no React/DOM/Canvas, no torch/transformers/CV, no `storage/`
  or `llm/` imports (enforced by `tests/architecture/test_nagar_studio_isolation.py`).
  - `models.py`: project/timeline/track/clip state, `MediaRef`, `Playhead`,
    markers, the `nagar.command.v1` typed envelope, `EditTransaction`/`CommandResult`,
    and the content-derived `state_hash` contract (`state_revision` excluded and
    monotonic, so revision+hash pairs stay valid across undo cycles).
  - `capabilities.py`: `Domain > Capability > OperationSpec` registry — the
    allow-list the bus consults, the A/B/C/D permission ladder, per-operation
    typed input models and pure `(project, context) -> outcome` handlers.
  - `references.py`: semantic reference resolution (`"اینجا"`, «۵ ثانیه قبل»,
    absolute/relative/start/end, Arabic-Indic digits) pinned **once at command
    receipt** with `captured_at_command=True`.
  - `bus.py`: the single write path — envelope validation → idempotency replay →
    registry lookup → permission gate → typed input validation → reference
    pinning → optimistic-concurrency preconditions → atomic apply. A failing
    handler leaves central state exactly as it was.
  - Exactly **five** operations in the Wave 1 catalog: `media.play` and
    `media.pause` (A), `timeline.mark` and `timeline.split_at_playhead` (B,
    non-destructive — source `MediaRef` untouched), `system.undo` (A, classic
    NLE undo that skips its own `system.undo` records).
  - Tests: `tests/unit/test_nagar_wave1_green_cockpit.py` (33 tests — every
    registered operation proves validate → apply on in-memory state → undo
    restores the previous state hash) plus the three isolation/architecture
    gates.
- **D1 — Job resume CLI (`nexus jobs resume`)**: operator entry point on the
  durable in-process queue. `InProcessJobQueue.resume_pending_jobs()` requeues
  rows still sitting in `pending` and drains them in the calling process with
  the standard application handlers; `processing` rows are never claimed, so
  the command is safe to run beside a live bot (which keeps its own
  process-startup `resume_pending()` recovery).
- **D3 — real PDF extraction via `pypdf`**: `worker.extract_pdf_text()` parses
  the PDF text layer in a worker thread (`asyncio.to_thread`) and feeds the
  RAG engine; PDF bytes are never silently decoded as UTF-8 text. Without
  `pypdf` the job fails durably with an actionable install hint
  (`pip install pypdf`). `pypdf` ships as the optional `[pdf]` extra (and in
  `[dev]` so the suite exercises the real parser); enqueue job type renamed
  `pdf` → `pdf_extract` through the existing `JobQueuePort` contract
  (signature unchanged).
- **D4 — Telegram completion notification**: `InProcessJobQueue` accepts an
  optional, strictly fail-safe `on_job_finished` hook that fires when a job
  reaches `completed` or `failed` (never on cancellation). The bot injects a
  notifier that messages the origin `chat_id` carried in the job payload;
  hook exceptions are logged and swallowed and can never corrupt durable job
  state.

### Security — dashboard API & egress hardening (fresh implementation; manual review required)
- **CORS lock-down (`api/app.py`)**: the previous default
  `allow_origins=["*"]` + `allow_credentials=True` reflected *any* origin
  with credentials, voiding the browser same-origin policy for the whole
  dashboard API. CORS is now an explicit allowlist
  (`NEXUS_API_CORS_ORIGINS`, comma-separated; **empty by default** — the
  served dashboard is same-origin and needs no CORS), with
  `allow_credentials` enabled only when origins are configured and methods
  limited to `GET/POST/OPTIONS`.
- **HMAC endpoint auth (`api/app.py`)**: `POST /creative/video-edit` is
  **fail-closed** — without `NEXUS_API_HMAC_KEY` (unset or empty) it answers
  `503 Security configuration incomplete`; with a key it accepts only
  `X-NEXUS-Timestamp` (±300 s freshness) +
  `X-NEXUS-Signature: hex(HMAC-SHA256(key, "{timestamp}:{raw_body}"))`,
  compared constant-time. (Fail-open legacy behaviour was hardened to
  fail-closed per the owner's pre-merge review, 2026-09-20.)
- **SSRF/DNS-rebinding guard on the shared egress client
  (`core/http_client.py`)**: `ResilientHttpClient` (free_tools,
  web_trainer — which fetches *user-supplied* URLs —, wikipedia_trainer)
  now runs `ssrf_guard.validate_url()` (https-only; *every* resolved
  address must be public) before each request and rides the
  connect-time re-validating `SafeAsyncTransport`, closing the
  rebinding/TOCTOU window even across redirects. Opt-out:
  `ResilientHttpClient(ssrf_protect=False)`.
- **Log redaction (`observability/logging.py`)**: `configure_logging`
  installs a redaction stage into structlog and the stdlib handlers —
  Telegram bot tokens, `Bearer`/`Authorization` values and
  key/value secrets (`api_key=`, `"token":`, …) are masked as
  `[REDACTED]` before they reach the logs. `redact_secrets()` is
  reusable; disable with `configure_logging(redact=False)`.
- **Telegram webhook**: unchanged (already constant-time, fail-closed);
  regression tests added.
- `.env.example` documents the two new variables
  (`NEXUS_API_CORS_ORIGINS`, `NEXUS_API_HMAC_KEY`).

### Changed
- **Release metadata is now consistent**: `VERSION` 3.9.0 → **3.10.0** and
  `pyproject.toml` 3.8.0 → **3.10.0**. The v3.9.0 release bumped `VERSION`
  only, so the packaged distribution had been reporting 3.8.0; both sources are
  now in lock-step and are covered by `tests/unit/test_version_command.py`.
- **`/version` no longer lies**: `bot/update_handlers.py` hardcoded `v3.0.0`
  in two places. Both now read the real running version from the installed
  distribution metadata (`importlib.metadata`), falling back to the repository
  `VERSION` file only for an uninstalled checkout — the file lives at the
  repository root and is deliberately not part of a wheel.

### Removed
- **D2 — dead code**: `worker.nightly_channel_management()` (no callers) is
  gone; with it the only `telegram` import leaked out of the bot layer.

## [3.9.0] — 2026-09-20

### Added
- **Cloudflare R2 — the "technical blob" tier + scheduled maintenance (Phase 5)**
  - `storage/providers/r2.py`: `R2Provider` on the existing
    `StorageProvider` protocol — boto3 against the S3-compatible endpoint
    (`https://{account_id}.r2.cloudflarestorage.com`, `region_name="auto"`,
    pinned `boto3==1.43.98`), client always built with
    `Config(request_checksum_calculation="when_required")` (known R2
    checksum-compatibility trap). Adds `generate_presigned_url(key,
    expires_in)` with the R2 7-day TTL cap (604800 s → beyond raises) and
    batched `delete_objects` for retention pruning. boto3 client is
    injectable — unit tests run fully offline.
  - `storage/ai_storage_manager.py`: a **separate routing branch** for
    technical blobs (`is_blob_key`, `BLOB_KEY_PREFIXES = ("backups/",
    "rag-docs/")`) — database backups and heavy RAG documents go to R2
    only (`upload_blob`/`download_blob`/`blob_presigned_url`). R2 is
    deliberately **not** added to the user-file round-robin
    (`unified_cloud.py` untouched; user files keep their existing path).
  - `maintenance/` + new CLI group `nexus maintenance`:
    `backup` (pg_dump / SQLite online-backup → R2, stateless, fails loudly
    without R2) and `housekeeping` (stale creative temp files + R2 backup
    retention prune, idempotent, stays green without R2); both with
    `--dry-run`.
  - `.github/workflows/maintenance.yml`: schedule-only (plus manual
    `workflow_dispatch`) — `backup-db` nightly 03:17 UTC, `housekeeping`
    weekly Mondays 04:23 UTC (deliberately off the top-of-hour); single
    shared `concurrency` group (`cancel-in-progress: false`); secrets:
    `NEXUS_DATABASE_URL` + the four `R2_*` keys — no bot token.
  - `docs/r2-storage.md`: bucket/token setup (single-bucket token scope),
    `.env` and GitHub secrets guide.

## [3.8.0] — 2026-09-19

### Added
- **Telegram webhook run-mode — real webhook for scale-to-zero deployments (Phase 3)**
  - `bot/webhook.py`: run-mode orchestration — `resolve_run_mode()`
    (CLI `--mode` > `NEXUS_RUN_MODE` > `polling`), `build_webhook_bind()`
    (port priority `PORT` > `DASHBOARD_PORT` > `8000`), and `run_webhook()`
    (PTB application init, `set_webhook` with a shared-secret token,
    uvicorn serving, graceful SIGTERM shutdown for scale-to-zero platforms)
  - `api/app.py`: two new endpoints — `POST /webhook/telegram`
    (constant-time secret check against `NEXUS_WEBHOOK_SECRET`; 403 on
    mismatch, 400 on missing `update_id`, verified payloads pushed to
    `application.update_queue`) and `GET /healthz` (process liveness, no
    database touch — the scale-to-zero health gate)
  - `bot/app.py`: `WebhookApplicationAdapter` — converts raw webhook JSON
    into a PTB `Update`. Lives in `bot/app.py` deliberately: the frozen
    import-boundary baseline only tolerates `telegram` imports in
    grandfathered files (`bot/webhook.py` and `api/app.py` stay
    telegram-free)
  - `cli.py run-bot`: the webhook branch now actually runs webhook mode
    (was "not yet configured" + exit 1); polling path untouched
  - New settings: `NEXUS_RUN_MODE`, `NEXUS_WEBHOOK_URL`,
    `NEXUS_WEBHOOK_SECRET` (+ `.env.example` entries)
  - `koyeb.yaml`: service type `worker` → `web`, health check path
    `/healthz` (was the non-existent `/api/health`)
  - `Dockerfile`: `CMD` honors `NEXUS_RUN_MODE` (default `polling` keeps
    existing always-on deployments working unchanged)
  - New dependency: `uvicorn==0.53.0` (verified latest on PyPI at pin time)
  - Tests: `tests/unit/test_webhook_mode.py` — mode/port priority,
    `/healthz`, enqueue of valid payloads, 403/400 rejection paths
  - Docs: `docs/deployment-koyeb.md` — step-by-step Koyeb `web` deploy and
    the explicitly accepted cold-start trade-off (Telegram retries; the
    multi-second wake-up delay is not a bug)

## [3.7.0] — 2026-09-19

### Added
- **Multi-provider LLM routing chain (litellm) — end of single-provider lock-in**
  - `llm/litellm_provider.py`: new `LiteLLMRoutingProvider(LLMProvider)` built
    on `litellm.Router` with a priority chain of free-tier providers:
    **Ollama (local, unlimited) → Groq → Gemini → OpenRouter `:free`**;
    only configured providers enter the chain
  - Anti retry-storm cooldowns: providers with daily caps
    (Groq/Gemini/OpenRouter) are parked for 86,400s after the *first* 429
    (`allowed_fails=1`); Ollama keeps a short 300s cooldown
  - `NEXUS_LLM_STRICT_PRIVACY=true` removes OpenRouter `:free` deployments
    (which may train on user prompts) from the chain
  - Existing `FallbackProvider` remains the outer layer: a fully drained
    router degrades to `FakeLLM` with the usual disclaimer instead of raising
  - `LLMProvider`, `FallbackProvider`, `GeminiProvider`, `FakeLLMProvider`,
    `LocalLlamaCppProvider` untouched; legacy local-GGUF path preserved when
    routing is disabled or unconfigured
  - New settings: `NEXUS_LLM_ROUTING_ENABLED`, `NEXUS_LLM_STRICT_PRIVACY`,
    `NEXUS_LLM_CLOUD_COOLDOWN`, `NEXUS_LLM_REQUEST_TIMEOUT`,
    `NEXUS_GROQ_API_KEY`/`GROQ_API_KEY`, `NEXUS_GROQ_MODEL`,
    `NEXUS_OLLAMA_BASE_URL`, `NEXUS_OLLAMA_MODEL`,
    `NEXUS_OPENROUTER_API_KEY`/`OPENROUTER_API_KEY`, `NEXUS_OPENROUTER_MODEL`
  - New dependency: `litellm>=1.74,<2` (verified against 1.101.0)
  - Docs: `docs/architecture/LLM_PROVIDERS.md` — chain, cooldowns, privacy
    flag, and the documented (deferred) shared-provider pattern of
    `agents/{gemma,phi,qwen}`

## [3.6.0] — 2026-09-19

### Added
- **Checkpoint lifecycle on the PostgreSQL path, end-to-end (PR3, option A)**
  - `nexus_checkpoint_lifecycle` table in PostgreSQL via the isolated
    Alembic revision `f4a9c2e71b08` (single-table DDL, PostgreSQL dialect
    only — the SQLite path is unchanged)
  - Read-only `PostgresCheckpointAdapter` (server-enforced read-only
    connection) behind the shared `CheckpointReadAdapter` contract with
    the SQLite adapter
  - Runtime PG path: `LifecycleRecordingSaver` over the real
    `PostgresCheckpointer` — lifecycle metadata lands in the same
    database, so serverless (Neon) restarts no longer lose it
  - Backend-aware `nexus checkpoints inspect|reconcile` (same CLI, PG or
    SQLite)
  - `nexus checkpoints golden update` — human-triggered schema golden
    management (write requires `--yes`; CHANGES warning with old/new
    digests)
  - Committed `postgres.langgraph.json` golden (schema-v1-pg)
  - Parameterized lifecycle store contract (SQLite + PG) and Neon
    operations runbook (`docs/ops/NEON_LIFECYCLE_RUNBOOK.md`)
  - Four-way kill-switch matrix (SQLite/PG × on/off) with live-PG
    composition tests
- Read-only checkpoint lifecycle operations: `inspect` contract
  (inspect-v1), reconciler with health gate (dry-run default), retention
  policy, schema goldens (SQLite + Postgres), O1 observability
  (redaction, structured events, core-schema fingerprint), lifecycle
  kill-switch and architecture boundary tests (PR1/PR2, this line of work)

### Changed
- Lifecycle metadata on the PostgreSQL path moved from a local SQLite
  sidecar (interim option B) to the database itself (option A, owner
  decision): serverless local files are ephemeral, so the sidecar would
  lose all lifecycle history on compute restart
- `nexus adopt-pg` now treats a database stamped at head but missing
  `nexus_checkpoint_lifecycle` as drift (fail-fast — never implicit
  repair)
- Migration chain head: `47903d282ede` → `f4a9c2e71b08`;
  `postgres.langgraph.json` fingerprint regenerated via the
  human-triggered CLI (head-only change)
- Reconciler purge (lifecycle rows only, under the full guard stack)
  works uniformly on both backends

### Fixed
- `PostgresCheckpointer` subclassed itself
  (`class PostgresCheckpointer(PostgresCheckpointer)`) and failed
  `ensure_valid_checkpointer`'s `BaseCheckpointSaver` isinstance check on
  every PG boot — it is now a genuine `BaseCheckpointSaver` subclass
  delegating to the official checkpointer
- Graph smoke tests are hermetic on persistent stores (unique thread ids
  per run)
- CI `migrate-postgres` job installs the `[dev]` extra so its pytest
  steps run

## [3.5.0] — 2026-09-17

> Released as **v3.5.0**.  An earlier tag `v0.2.0-D` was cut on the same work
> while this entry was still numbered `0.2.0-D`; the project line is
> 3.x (`VERSION` and `pyproject.toml` were 3.4.1, and tags run v1.2.0 → v3.3.0),
> so a 0.2.0 tag sorted three major versions backwards.  The tag is left in
> place — tags are never rewritten — and v3.5.0 supersedes it.

### Added
- Alembic-based schema management end-to-end (D1–D5)
- `nexus migrate` CLI command (Alembic-first, legacy `create_all` fallback)
- `nexus adopt-pg` CLI command for legacy PostgreSQL (D10), dry-run by default
- PostgreSQL/Neon support via `NEXUS_DATABASE_URL` (C1)
- Token encryption at rest — Fernet (`security/crypto.py`, D8)
- pgvector integration — Alembic revision `2a1c4b6d8e9f` (D7)
- Postgres adoption / fail-fast seam (`storage/adopt_pg.py`, D10)
- Continuum snapshot (`nexus continuum show|verify`, `.nexus/continuum.json`) for
  turn-to-turn state recovery
- Cross-host safety for concurrent schema creation: `create_all_metadata`
  retries a create-conflict, complementing `migration_lock()` (which is a local
  `fcntl.flock` file and therefore cannot see a second host)

### Changed
- PostgreSQL `create_all` stopgap retired (D7); Alembic is the single source
  of schema truth for PostgreSQL
- Startup schema management is Alembic-first on both backends
- `nexus migrate --db-path` marked deprecated (removal 2026-10-01)

### Fixed
- Legacy SQLite databases are auto-adopted (+ stamped) before upgrading (D6),
  preserving user data and avoiding the initial-revision `CREATE TABLE` clash
- Un-stamped PostgreSQL with schema drift now fails fast with an actionable
  message instead of a raw DBAPI error (D10)
- **`nexus migrate` now consults the D10 decision matrix too.** `prepare_postgres`
  guarded the `get_session` path, but `run_migrations` went straight to
  `command.upgrade`, so an un-stamped Postgres still died there with a bare
  `asyncpg.DuplicateTableError: relation "adcampaign" already exists` that never
  mentioned `nexus adopt-pg`. Reproduced against a real PostgreSQL server; a
  zero-drift database is now adopted (stamped at head) and drift is refused with
  the actionable message, before Alembic writes anything.
- Concurrent `create_all` converges instead of colliding. `MetaData.create_all`
  runs `checkfirst=True`, so two processes both see "does not exist", both emit
  the DDL, and the loser died on `table … already exists` — reproduced with four
  concurrent `nexus migrate` subprocesses where all four failed.
- `VERSION` and `pyproject.toml` moved 3.4.1 → 3.5.0 to match this release.

### Known limitations
- Real Neon connectivity test (D9) pending a user-supplied `NEXUS_DATABASE_URL`;
  CI already verifies the full Alembic chain on a real Postgres + pgvector

## [3.4.1] — 2026-09-17

### Security — Phase 0
- **SSRF protection** for URL summarization (`core/ssrf_guard.py`):
  https-only fetching, pre-fetch DNS validation of every resolved
  address, and connect-time re-validation on each connection including
  redirects (closes the DNS-rebinding TOCTOU window by connecting to the
  validated IP literal); 127/8, 10/8, 172.16/12, 192.168/16,
  169.254/16 (cloud metadata), 0/8, ::1, fe80::/10, fc00::/7 blocked
- **Shell tool sandboxed to the workspace** (`tools/system_shell.py`):
  commands run with `cwd=workspace_root`; path arguments of ls/cat/
  grep/find are containment-checked (absolute paths, `..`, symlink
  escapes rejected); `find -exec/-execdir/-delete/-ok/-okdir` blocked;
  file-writing find flags (`-fprintf`, `-fprint`, `-fprint0`, `-fls`,
  `-newer`) restricted to the workspace
- **AuthMiddleware deny-by-default** (`bot/middleware.py`): only the
  owner and explicitly allowed users get in; an empty configuration no
  longer allows everyone (previously fail-open)
- **Self-update gated**: `/update` is owner-only and requires an
  explicit owner approval (`PendingApproval`, 30-minute window); fixed
  the broken `pip install -r requirements.txt` (file never existed) to
  `pip install .` (matches the Dockerfile)
- New `settings.auto_update` (default **off**): the self-update
  check/apply path (now in `agent/self_monitor.py`) only runs when the
  owner explicitly enables it via `AUTO_UPDATE=true`

### Bug fixes (surfaced by mypy strict)
- `/quiz`, `/code`, `/translate`, `/viral_now` called methods that do not
  exist (AttributeError/TypeError at runtime) — repaired and regression
  tested
- `bot/handlers.py`: `_base_state` now builds a valid `NexusState` for
  the LangGraph invocation; `on_message` uses the compiled graph
  instance instead of a non-existent module attribute
- `api/dashboard.py`: async session API (`execute/scalar_one`) and
  counts on columns that actually exist
- `None`-guards for `update.message` / `effective_user` /
  `callback_query.from_user` across bot handler modules
- Missing dependencies declared: `duckduckgo-search`, `fastapi`;
  `httpx` pinned (dependency on a private API in `core/ssrf_guard.py`)

### Maintenance
- Quality gates green: `ruff check` + `ruff format` clean,
  `mypy strict` clean (46 pre-existing errors fixed), 96 passing tests
- `build/` artifacts untracked and added to `.gitignore`
- `VERSION` and `pyproject.toml` aligned to 3.4.1

## [3.4.0] — 2026-09-17

### Added — catch-up entry for the work shipped since 2.0.0

The changelog was last updated at 2.0.0; this entry documents the
features that were implemented afterwards without being recorded.

**Multi-agent framework (`agents/`)**
- Persona agents `gemma_agent.py`, `phi_agent.py`, `qwen_agent.py` with
  distinct system prompts (note: they share one LLM provider instance —
  the difference is personality, not model)
- Core roles: `chat_agent.py`, `planner_agent.py`, `executor_agent.py`
- `agents/store/` — user-selectable specialized agents with per-user
  activation state (`/agents`, `/myagent`, `/agent_stop`)

**LangGraph orchestration (`orchestration/`)**
- `graph.py` — intent router → memory reader → persona selection →
  planner/executor → memory writer, compiled against a SQLite
  checkpointer (`storage/langgraph_checkpoint.py`)
- `router.py` — intent classification and persona routing
- `state.py` — shared `NexusState` graph state

**Tool system (`tools/`) + approvals (`agent/`)**
- `ToolRegistry` with risk levels (safe/guarded/blocked) and policy
  confirmation
- Sandboxed file tools (read/write/list) and an allowlisted shell tool
  (disabled by default; enable with `NEXUS_ENABLE_SHELL`)
- `agent/approval.py` — persistent `PendingApproval` workflow with owner
  notifications and `/approve` / `/reject` handlers

**Self-monitoring (`agent/self_monitor.py`, `bot/monitor_handlers.py`)**
- RAM/disk/uptime health checks with owner alerts (`/health`)
- Instrumented observability hooks across the pipeline

**Memory & knowledge**
- `memory/short_term.py`, `memory/long_term.py` (SQLite + sqlite-vec)
- `knowledge/` — web and Wikipedia trainers with a cached knowledge
  store (`/learn`, `/search`, `/wiki`)

**API & background workers**
- FastAPI dashboard (`api/`) with stats endpoints
- Celery worker (`worker.py`) for story rendering, PDF processing and
  nightly channel maintenance

**LLM layer (`llm/`)**
- Provider abstraction with Gemini, local llama.cpp, fallback and
  fake (test) providers
- Resilient HTTP client with retry and circuit breaker
  (`core/http_client.py`)

## [2.0.0] — 2025-06-12

### Added — Global Expansion Release 🌍🚀

The most ambitious update in NEXUS AI history, transforming the bot from a
community tool into a **globally accessible AI platform** with free cloud
storage, viral referral growth, 15-language support, and powerful AI features
— all powered by 100% free APIs and services.

**Phase 1 — Google Gemini AI Integration**
- `features/ai_chat.py` — GeminiEngine class wrapping Google Gemini 2.0 Flash API
- Rate limiting: 15 RPM, 1M TPM, 1500 requests/day (free tier)
- Conversation memory with per-user session tracking (up to 20 messages)
- `/ai <text>` — conversational AI chat with context memory
- `/ask <question>` — single-turn factual question answering
- `/vision` — image analysis via Gemini Vision (reply to photo)
- `/code <prompt>` — AI code generation with syntax highlighting
- `/translate <text>` — AI-powered translation with auto-detect
- `/summarize <text|URL>` — smart summarization with 5 modes (brief, detailed, key_points, eli5, academic)
- `summarizer_engine` with URL scraping support and structured SummaryResult output

**Phase 2 — Unified Cloud Storage (57GB+ Free)**
- `storage/unified_cloud.py` — UnifiedCloudStorage orchestrator
- 5+ free cloud providers: Dropbox (2GB), pCloud (10GB), Internxt (10GB), MEGA (20GB), GitHub Releases (unlimited)
- Round-robin upload distribution with capacity-aware routing
- Automatic failover between providers
- `/cloud` — upload file to unified cloud (reply to document)
- `/myfiles` — list all your cloud files
- `/download <filename>` — download file from cloud
- `/cloud_status` — view storage status across all providers
- `CloudFile` SQLModel table tracking uploads per user

**Phase 3 — Referral Viral Loop System**
- `features/referral.py` — ReferralEngine with 6 exponential growth tiers
- Auto-generated unique referral codes per user (NEXUS-{uid}-{hash})
- Tiered rewards: 🥉 Inviter (1) → 🥈 Networker (3) → 🥇 Star (5) → 💎 Diamond (10) → 👑 Legendary (25) → 🚀 Viral Master (50)
- Dual-reward system: both referrer and referee get prizes
- `/referral` — view your referral code, link, and current tier progress
- `/referral_board` — global leaderboard of top referrers
- `Referral` and `ReferralCode` SQLModel tables with reward tracking

**Phase 4 — i18n Multi-Language System (15 Languages)**
- `i18n/__init__.py` — I18n manager with 15 supported languages
- `i18n/loader.py` — Language loader with JSON file support
- Languages: English, Persian, Arabic, Spanish, French, German, Russian, Chinese, Japanese, Korean, Portuguese, Hindi, Turkish, Indonesian, Italian
- Per-user language preference persistence via `UserLanguage` SQLModel
- `/language` — interactive inline keyboard for language selection
- `lang_{code}` callback handlers for instant language switching

**Phase 5 — Free Image Generation via Pollinations.ai**
- `features/image_gen.py` — ImageGenEngine with 10 style presets
- Styles: realistic, anime, digital, oil, watercolor, pixel, 3d, comic, minimal, fantasy
- 5 size options: 1024×1024, 1792×1024, 1024×1792, 512×512, 1280×720
- `/image <description>` — AI image generation (e.g., `/image style:anime a cat samurai`)
- Zero API key required — Pollinations.ai is completely free

**Phase 6 — Speech-to-Text & Text-to-Speech**
- `features/speech.py` — SpeechEngine with gTTS + Gemini STT
- `/tts <text>` — convert text to voice message (100+ languages)
- `/stt` — transcribe voice/audio messages to text (reply to voice)
- gTTS for TTS (free, no API key), Gemini for STT (high accuracy)
- Automatic MIME type detection and temp file handling

**Phase 7 — Smart Summarizer**
- `features/summarizer.py` — SummarizerEngine with Gemini backend
- 5 summarization modes: brief, detailed, key_points, eli5, academic
- URL summarization with automatic content scraping
- Structured SummaryResult output with metadata
- `/summarize mode:detailed <text|URL>` — flexible summarization

**Phase 7.5 — Handlers Integration & Menu Redesign**
- All 17 new CommandHandlers registered in build_handlers()
- 7 new CallbackQueryHandlers for interactive menus
- Redesigned main menu with 6 sections: 🤖 AI, 🎨 Image, 🎤 Speech, ☁️ Cloud, 🔗 Referral, 🌐 Language
- Interactive inline keyboard navigation between menu sections
- `/start ref_<code>` deep-link support for referral tracking
- Updated `/help` command with complete v2.0.0 command documentation

### Changed
- Extended `_reply()` helper to accept `reply_markup` kwarg for inline keyboards
- `Settings` model updated with new fields: `gemini_api_key`, `gemini_model`, `gemini_max_rpm`, `gemini_max_daily`, `dropbox_token`, `pcloud_token`, `internxt_token`, `bot_username`
- `vision_cmd` now uses Gemini Vision API with `bytes` input instead of base64
- `stt_cmd` uses temp file approach for Gemini STT compatibility
- `cloud_cmd` uses file-path based upload via `unified_cloud.upload_file(local_path, remote_key)`
- `download_cmd` supports both in-memory and file-based download paths
- Referral methods are synchronous (not async) — removed incorrect `await` calls

### Fixed
- Resolved 22 syntax errors from incomplete line-based replacements in handlers.py
- Fixed missing `except` block in `vision_cmd` after API signature migration
- Fixed orphaned `user_id=user_id,` lines in `cloud_cmd` from old `upload_file` call
- Fixed dead code after `return` in `download_cmd`
- Fixed SQLAlchemy `Table already defined` errors with `extend_existing=True`
- Fixed SQLAlchemy index conflict in `ReferralEngine._ensure_tables()` with raw SQL
- All ruff linting, formatting, and mypy type checks passing
- All 23 unit tests passing

## [1.3.0] — 2025-06-12

### Added — AI Community Operating System (Phases 7–16)

**Phase 7 — Owner Control System**
- `is_owner()` check and `owner_only` decorator for admin-only access
- `/owner` dashboard, `/system` status, `/broadcast` and `/broadcast_all` commands
- `/admin_logs` for recent admin action log review
- `AdminLog` SQLModel table with sync engine CRUD

**Phase 8 — Force Join System**
- Channel membership verification before bot usage
- `ForceJoinManager` with 5-minute cached membership checks
- Anti-bypass: cache invalidation on verify, re-check on expiry
- `/forcejoin_on`, `/forcejoin_off`, `/forcejoin_status`, `/forcejoin_message` commands
- Inline verify button for non-member users
- `ForceJoinConfig` SQLModel table

**Phase 9 — AI Personality Engine**
- 10 distinct AI personalities with Persian greetings, tone, and style
- Per-group personality configuration with persistence
- `/personality list|current|set <name>` command interface
- `PersonalityConfig` SQLModel table

**Phase 10 — AI Community Engagement**
- Auto-engagement engine with ice breakers, jokes, challenges, daily questions, events
- Rate-limited content generation (minimum 60-minute intervals)
- Rich Persian content banks for each engagement type
- `/engagement_on`, `/engagement_off`, `/challenge`, `/joke`, `/event` commands
- `EngagementConfig` SQLModel table

**Phase 11 — Viral Content Engine**
- `ViralEngine` with auto viral post generation and scoring heuristics
- Viral score algorithm: length, hashtags, emojis, questions, call-to-action
- Auto-hashtag generation per category
- Content hash-based duplicate prevention
- Post scheduling with pending/posted lifecycle
- `/viral_now`, `/viral_preview`, `/viral_stats`, `/viral_post` commands
- `ViralPost` SQLModel table

**Phase 12 — Advertisement System**
- `AdManager` with full campaign CRUD lifecycle
- Scheduled ads with configurable repeat intervals and max repeats
- Auto-next-run scheduling and completion detection
- Campaign pause, resume, and delete controls
- `/ad_create`, `/ad_list`, `/ad_pause`, `/ad_resume`, `/ad_delete`, `/ad_stats` commands
- `AdCampaign` SQLModel table

**Phase 13 — Smart Moderation**
- `ModerationEngine` with multi-layer content analysis
- Anti-spam (repeated chars, uppercase abuse, emoji spam)
- Anti-flood (5 messages per 5 seconds rate limit)
- Link filter (URL and t.me link detection)
- Persian profanity regex filter
- Warning system with configurable max warnings and auto-mute
- User reputation tracking with adjustment API
- `/mod_on`, `/mod_off`, `/mod_config`, `/warn`, `/mute`, `/unmute`, `/reputation` commands
- `ModerationConfig` and `UserReputation` SQLModel tables

**Phase 14 — Gamification System**
- `GamificationEngine` with XP, leveling, streaks, daily rewards, achievements
- 16 levels with Persian titles (تازه‌وارد → افسانه‌ای)
- Cumulative XP thresholds for level progression
- Daily streak tracking with 1-day/2-day grace logic
- 8 achievements with JSON array persistence in SQLite
- `/profile`, `/daily`, `/xp_leaderboard`, `/achievements` commands
- `UserXP` SQLModel table

**Phase 15 — Analytics Engine**
- `AnalyticsEngine` with event tracking and multi-dimensional queries
- Active user counts (24h/7d), engagement rate, events per user
- Peak hours analysis by hour of day
- Day-by-day cohort retention tracking
- Command usage statistics
- Combined dashboard summary
- `/analytics`, `/analytics_active`, `/analytics_retention`, `/track` commands
- `AnalyticsEvent` SQLModel table

**Phase 16 — Advanced UI**
- Redesigned main menu with 6-row inline keyboard
- Personality submenu: list, current, set
- Gamification submenu: profile, daily, leaderboard, achievements
- Analytics submenu: dashboard, active users, retention, command usage
- Moderation submenu: on/off, config, reputation
- Admin dashboard panel with nested submenus for: owner controls, viral, ads, moderation, analytics, force join, engagement, system status
- All submenus include back navigation
- Updated help text to v1.3.0

### Changed
- Help command updated to v1.3.0 with all new feature sections
- Settings help panel updated with personality, gamification, and moderation sections
- Main menu back button shows expanded 6-row keyboard

### Technical
- All new features use sync SQLAlchemy engine (`_sync_engine()` pattern) for CRUD
- `col()` from sqlmodel used consistently for type-safe ORDER BY and WHERE clauses
- All Persian content strings include `# noqa: E501` where line length limits prevent breaking
- Ruff + mypy + pytest all green across all phases
- 10 new SQLModel tables added to models.py
- 25+ new command handlers registered
- 12+ new callback query handler patterns registered

## [1.2.0] — 2025-05-29

### Added
- Phase 1: Channel & Group Management (post, schedule, ban, unban, welcome, pin, stats)
- Phase 2: Anonymous Chat (queue-based random pairing, report system)
- Phase 3: Games & Entertainment (quiz, number guess, Persian Wordle, polls)
- Phase 4: Utility Tools (reminders, translation, unit conversion, calculator)
- Phase 5: Inline Keyboard Menu System
- Phase 6: AI Chat Integration (LangGraph routing, persona system, memory)

## [1.1.0] — 2025-05-20

### Added
- Initial Telegram bot with python-telegram-bot v21+
- SQLModel async SQLite database
- User authentication and rate limiting
- LLM provider abstraction (llama.cpp + FakeLLM)
- LangGraph orchestration graph

## [1.0.0] — 2025-05-15

### Added
- Project scaffolding and core architecture
- Configuration management with pydantic-settings
- Observability with structlog
