# P0 Stabilization — Architectural Reality Check & Decision Record

**Date:** 2026-09-24 · **Branch:** `arena/01a0d23e-nexus-ai-agent` · **Base:** `2cf2213` (`main`, squashed history)
**Verdict convention:** every claim below is tagged **FACT** (verified against code/tests/CI), **INFERENCE** (derived from facts), **ASSUMPTION**, **UNKNOWN**, or **UNVERIFIED** (could not be checked from this sandbox).

---

## P0-0 ARCHITECTURE DECISION

### Repository & board state (FACT)

- Working branch `arena/01a0d23e-nexus-ai-agent` at `2cf2213`, clean tree. History is a single squashed merge (PR#62).
- `.agents/board.json` @ `2026-09-23T20:55:00Z` (schema 2): **no live lease fences any file in my scope** — `status="active"`/`active_in_review` claims: none touching `api/app.py`, `bot/app.py`, `bot/creative_surface.py`, `worker.py`, `maintenance/`, `i18n/locales/`, `cli.py`.
- Open PRs with **scope intersection** (verified via `gh pr view <n> --json files`):
  - **PR#58** (`arena/01a0cf4c`, "Security Boundary hardening S1..S5") — edits `src/nexus_ai_agent/api/app.py`, `core/ssrf_guard.py`, `creative/video_director.py`. Its S5 section already delivers the SSRF fix for `POST /creative/video-edit` (fail-fast `validate_url` + `SafeAsyncTransport` with DNS pinning + `::ffff:` handling). Its lease (`sec-boundary-01a0cf4c`, expires 2026-09-24T17:28:34Z) is recorded **only on its own branch board**, not merged to `main`'s board — an "invisible PR scope" of exactly the kind `agent_board.py praudit` exists to flag.
  - **PR#59** (`arena/01a0cf31`, outbox/effect-key idempotency) — edits `docs/architecture/OBSERVABILITY.md`, new `adapters/effect/*`. No code intersection.
  - **PR#60** (`arena/01a0cf50`, M0 instrumentation) — edits `bot/app.py`, `cli.py`.
  - **PR#63** (`arena/01a0cf98`, board-Git truth gate + state tiers) — edits `bot/app.py`, `cli.py`, `config/settings.py`, `docs/architecture/adr/README.md` (adds ADR 0005).
  - **PR#56** (`arena/01a0cb43`, hygiene task-162) — edits `docs/README.md` (audit index region).
  - **PR#33** (`arena/01a0c3aa`, giant v3.13.0 roll-up) — long-open.
- Baseline BEFORE any change (**FACT**): `pytest tests/unit/test_creative_surface.py test_creative_studio.py test_security_hardening.py test_i18n_parity.py tests/integration/test_in_process_job_queue.py test_r2_provider.py test_bot_slideshow_notify.py` → **106 passed**; `pytest tests/architecture/` → **83 passed**; `ruff check src/ tests/` → clean.

### Execution-path trace

**P0-A legacy HTTP pipeline (FACT, from `api/app.py`):**

```text
POST /creative/video-edit            GET /creative/jobs/{job_id}
  └─ require_hmac_signature (fail-closed 503 w/o NEXUS_API_HMAC_KEY)   └─ **NO AUTH AT ALL** → 200 with full input_data (local paths, URLs, filenames)
  └─ form: file → _save_upload_to_temp (NO size limit)
       video_url → **_no URL validation_** (SSRF: loopback/private/metadata reachable; httpx follow_redirects=True, redirects not re-validated; no size cap)
  └─ JobRegistry(own sqlite at creative_temp_dir/creative_jobs.sqlite3)   ← parallel, non-durable queue
  └─ background task: analyze_video_with_gemini (NEXUS_CREATIVE_GEMINI_API_KEY) → execute_ffmpeg_commands (raw subprocess, outside the render lane)
```

- Consumers of both endpoints inside the repo: **ZERO** (FACT — grep over `src/`, `scripts/`, dashboard JS, `docker-compose.yml`, `koyeb.yaml`; only tests + one doc row reference them).
- Live exposure: the Koyeb **webhook** deploy serves this FastAPI app publicly (`bot/webhook.py` mounts the whole `api.app`). With `NEXUS_API_HMAC_KEY` unset the POST answers 503, but the GET stays open. (FACT.)
- `JobRegistry` / `video_director` / `ffmpeg_executor` are, inside `src/`, used only by `api/app.py` (FACT); they are also re-exported from `creative/__init__.py`.

**P0-B canonical surface (FACT):**

- `bot/creative_surface.py` exists (wave-4 step5) but is **wired nowhere**: `build_application` never calls `build_creative_handlers` (proven: source scan + runtime grep → **False**).
- `worker.default_job_handlers()` registers `pdf_extract`, `story`, `slideshow_render` — **not** `creative_render`; an enqueued job would die with `no handler registered for job type creative_render` (InProcessJobQueue fail path).
- `build_creative_handlers` returns `{"edit","caption","grade","mapper"}` — the bogus `"mapper"` key would be registered as a command by a naive registration loop (repro: dict keys `['caption','edit','grade','mapper']`).
- Idempotency key = `creative:{user}:{chat}:{uuid4().hex}` → re-delivery of the SAME Telegram message creates a NEW job every time (reproduced: two invocations, two distinct keys).
- Replies interpolate raw i18n keys (`❌ creative.not_replied (not_replied)`) and hard-coded English; the locale catalogs (15 files × 63 keys) contain **zero** `creative.*` keys (FACT).
- Canonical equivalents exist for all nine surface operations (FACT): edit `{trim→timeline.trim, speed→timeline.speed_ramp, reverse→timeline.reverse_segment}`, caption `{transcribe→caption.transcribe, burnin→caption.burn_in}`, grade `{lut→color.apply_lut, exposure→color.adjust_exposure, proxy→delivery.make_proxy_480p, otio→delivery.export_otio}`; execution substrate = `creative/rendering` lane (TrimOp/SpeedOp/ReverseOp/ExposureOp; `render_lane` probes + sha256s the artifact). `LaneProfile` carries width/height/crf → proxy is executable. OTIO export yields a real document. Caption work goes through `CaptionEnginePort`, whose default is the **documented fail-closed** `caption_profile_unavailable`.
- task-106 (queued, zone `creative-surface`) is exactly this work; its deferrer (`resume_when: PR#32 merged`) is satisfied — PR#32 is **CLOSED**, and its replacement content (framework-free `bot/surface/` + `bot_data["job_queue"]` mapping) is on main via PR#47/#54.

**P0-C backup (FACT unless noted):**

- Scheduler: `.github/workflows/maintenance.yml` — nightly `backup-db` at 03:17 UTC, weekly `housekeeping` Mon 04:23 UTC; `nexus maintenance backup` exits non-zero on any failure by design.
- CI evidence: `backup-db` failed **3/3** scheduled runs (35580701817, 35705502209, 35838057275) at the `nexus maintenance backup` step; the one "success" run (35586584833) was **housekeeping-only with backup-db skipped** — matching the owner's suspicion that no real backup ever completed. (FACT, via `gh run view` + jobs API.)
- Exact CI log lines: **UNVERIFIED** — the log-archive endpoint is not reachable from this sandbox (EOF), and `gh secret list` is 403; so whether the runner failed on `ProviderUnavailable (R2 not configured)` or on a dump/upload `RuntimeError` cannot be read back. INFERENCE (high confidence): env-config missing or invalid — `backup.py`/`r2.py` are unchanged in the failing window and both failure modes are deterministic.
- Local reproduction (FACT): with no R2 env, `nexus maintenance backup` → `R2 is not configured …` exit 1 (fail-closed, by design); `--dry-run` exits 0.
- Current success criteria are insufficient: the code reports success after `upload()` returns — no integrity check, no restore verification, no artifact-exists proof, no `status`/`timestamp` field in the summary. `create_backup` has **zero direct tests** (only `R2Provider` has unit tests). (FACT.)

### Code classification

| Code | Status |
|---|---|
| `api/app.py` POST `/creative/video-edit` + `_save_upload_to_temp`/`_download_video_to_temp`/`_process_video_edit_job` | **legacy, partially defended** (HMAC yes; size limits no; SSRF fixed only in open PR#58) |
| `api/app.py` GET `/creative/jobs/{id}` | **legacy, undefended** (no auth) |
| `creative/job_registry.py`, `creative/video_director.py`, `creative/ffmpeg_executor.py` | **legacy support modules** for the above; sole importer `api/app.py` |
| `bot/creative_surface.py` | **canonical**, partially built (validation+enqueue only) and **unwired** |
| `worker.py`, `adapters/in_process_job_queue.py`, `bot/slideshow_handlers.py` + `creative/slideshow/worker_adapter.py` | **canonical pattern** (durable queue lane; the slideshow flow is the working reference) |
| `creative/packs/*`, `creative/studio/*`, `creative/rendering/*` | **canonical** (57 ops in the runtime registry today — measured, not paraphrased) |
| `maintenance/backup.py` | **canonical but unverified** (no tests, incomplete success criteria) |
| `maintenance/housekeeping.py` | **canonical** (retention; cannot mask backup failure — different job, different signal) |

### Is `creative_surface.py` the intended replacement for the legacy pipeline? (INFERENCE → decision-grade)

The legacy HTTP pipeline and the canonical Telegram surface overlap for **user-driven media editing**, not for machine-to-machine integrations. The canonical path is strictly superior on every architecture axis the repo has standardized on: durable queue vs. background task, permission ladder + typed commands vs. raw Gemini plan, idempotency vs. none, lane evidence (probe+sha256) vs. bare subprocess, i18n vs. hard-coded strings. **The canonical path is declared THE media-editing execution path; the HTTP routes are a legacy integration lane, not a parallel product surface.**

### Decision: KEEP + HARDEN (strictly bounded) → DEPRECATE-track → REMOVE later

I **do not** keep hardening the legacy pipeline as an equal, and I **do not** delete it mid-flight while PR#58's lease on the same functions is live. Concretely:

1. **Close the actual open hole now:** GET `/creative/jobs/{job_id}` gets the same fail-closed HMAC gate as POST (unsigned → 401/503). Any external integration that can POST already holds the key and can sign GETs — **no legitimate consumer breaks** (FACT by construction).
2. **Bound the resource surface:** byte cap on upload bodies (and on downloads, in the region PR#58 does not touch — the URL path is theirs).
3. **Do not duplicate PR#58:** SSRF URL validation is left to PR#58 (evidence-linked). Until it merges, key-configured deploys retain residual SSRF risk; default deploys (no HMAC key ⇒ POST disabled) are not exposed. Recorded as a release-gate dependency, honestly.
4. **Declare the deprecation in an ADR** with a removal path: legacy modules get runtime-visible deprecation markers; a new architecture test ratchets (a) the exact set of `/creative` routes in `api/app.py` (nobody may add more legacy surface) and (b) zero *new* importers of the legacy support modules.
5. **Removal** (routes + `job_registry`/`video_director`/`ffmpeg_executor`) is sequenced as follow-up work once PR#58 resolves — deleting functions another agent is actively patching would be the split-brain the board protocol exists to prevent.

**Why not MIGRATE:** there are no in-repo consumers to migrate (FACT), and no HTTP bridge in the canonical architecture was ever specified; building one would be exactly the parallel-abstraction this mission forbids.

**Security consequences:** unauthenticated job-data disclosure closed; upload resource cap; SSRF dependency explicitly gated on PR#58 instead of silently unowned; attack surface frozen by ratchet test.

**Compatibility consequences:** unsigned GET callers now get 401/503 — intended (that is the vulnerability). Signed consumers are unaffected. POST semantics unchanged.

**Rollback:** single revert of this branch; the HMAC gate is additive config-free code (still fail-closed without a key), legacy behavior otherwise untouched.

---

*(Implementation evidence, test matrix and the release gate are appended in the delivery section of this document.)*

---

## DELIVERY STATUS (end of stabilization day, 2026-09-24)

Board claims: `task-165` (P0-A), `task-166` (P0-B), `task-167` (P0-C), zone
`p0-stabilization`, branch `arena/01a0d23e-nexus-ai-agent`. task-106 is marked
**done — absorbed** into task-166; task-164 is narrowed to **owner-secrets +
real dispatch proof** (repo-side fix landed in task-167).
Decision record: `docs/DECISION_LOG.md` r8 (D-0010/0011/0012).

### P0-A — legacy `/creative/*` HTTP lane: harden + deprecate-track (DELIVERED)

- GET is now behind the same fail-closed HMAC gate as POST: **401** unsigned /
  stale / wrong-key; **503** when `NEXUS_API_HMAC_KEY` is unset. Repro refuted:
  the original "200 to any unsigned caller" transcript is dead.
- `_MAX_UPLOAD_BYTES = 500 MiB`; multipart bodies over the cap die **413**
  aborning the job row, with partial temp files unlinked on any failure.
- Both routes are `deprecated=True` (OpenAPI); removal is sequenced after
  PR#58's SSRF scope merges (D-0010).
- Architecture ratchet added (`tests/architecture/test_legacy_creative_boundary.py`):
  exact `/creative` route set frozen; importer whitelist for
  `job_registry / video_director / ffmpeg_executor`; both handlers must call
  the HMAC gate.
- **Not re-implemented (by design):** the POST downloader SSRF guard — PR#58
  delivers it; merge-file proof in this doc shows my edits are conflict-free
  with theirs (`git merge-file` exit 0, 0 conflict hunks).
- Tests: `test_security_hardening.py` (+GET auth matrix, +413), plus the two
  updated suites — **23 passed** (targeted), 5 architecture ratchets passed.

### P0-B — creative studio surface wiring (DELIVERED)

- Chain live end-to-end: mapper → staging → `JobQueuePort.enqueue(creative_render,
  idempotency=message identity)` → `worker.py` registration →
  `creative/render_jobs.py` (trust-boundary containment + packs registry +
  CommandBus + lane + probe/sha256) → completion notify in `bot/app.py`
  (translated, typed failures, workspace ownership/cleanup).
- Honest op matrix: `edit trim|speed|reverse`, `grade exposure|proxy|otio`,
  `caption transcribe`; `lut`/`burnin` refused typed at the surface AND in the
  worker map — never faked; caption chains fail closed typed without the
  `[speech]` engine (OTIO + caption→SRT deliver real document artifacts).
- i18n: 16 `creative.*` keys × 15 locales (parity gate green); a raw key can
  no longer leak to Telegram.
- Real-execution tests render 2-second clips through the whole chain via the
  imageio-ffmpeg static binary (trim ~1 s artifacts measured, speed 2x ≈ 1 s,
  reverse ≈ 2 s, exposure, 480p proxy height=480, OTIO schema+clips, SRT cues).
- Tests: `test_creative_surface.py` (14), `test_creative_render_jobs.py` (13),
  `test_creative_notify.py` (4), `tests/architecture/test_creative_channels.py`
  (5), `test_i18n_parity.py` — **all green**.
- UNVERIFIED (external dep): a real PTB application boot against the live
  Telegram API — out of scope, marked not a PASS.

### P0-C — verifiable backups (DELIVERED, repo-side)

- Success contract enforced in `maintenance/backup.py`: artifact exists AND
  non-empty AND sha256-measured AND locally verified (SQLite: restore into
  isolated temp DB → `PRAGMA integrity_check` + user-table inventory — the
  silent-empty-dump masquerade is rejected; PostgreSQL: pg_dump completion
  footer + non-empty) AND post-upload round-trip byte-identity; any mismatch
  hard-fails the run. Failure ⇒ non-zero; success ⇒ summary + structured log
  carry sha256/size/verified/timestamp. No new tooling; no `storage/` change
  (`R2Provider.download` already existed).
- Tests: `tests/unit/test_maintenance_backup.py` — **6 passed** (in-memory R2
  fake + real SQLite).
- **Residual (owner-side, task-164 kept open):** R2 repository secrets must be
  configured in GitHub Actions; then one `workflow_dispatch` proves the real
  end-to-end run. Until that runs green, "nightly backups exist" remains
  UNKNOWN (not asserted).

### Residual / follow-up queue (honest)

1. PR#58 merge + legacy-lane removal PR (D-0010 reopen clause).
2. Real dispatch proof of the backup job once secrets exist (task-164).
3. Lane LUT + subtitles instruments (would un-refuse `lut`/`burnin`).
4. PTB-runtime smoke of the three commands on staging (external).
