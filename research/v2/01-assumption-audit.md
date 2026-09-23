# PHASE 1 — ARCHITECTURE ASSUMPTION AUDIT

**Scope:** every load-bearing assumption in the current design *and* in the Research-v1 input set (`R-21`…`R-24`), re-tested against the pinned tree.
**Method:** for each assumption — locate it, cite evidence, state the falsifier, name the test. No assumption is accepted because it is documented; documentation is treated as a *claim source*.
**Result:** **36 assumptions** audited · 11 High risk · 16 Medium · 9 Low · 9 explicitly `[C]`-flagged or `[U]`.

**Marker legend:** `[V]` verified at pin · `[H]` historical · `[A]` assumed/derived · `[U]` unverified · `[C]` contradicted.
**Severity rule used:** S1 lost/corrupted user-visible state · S2 availability · S3 trust boundary · S4 operability/recovery · S5 cost/quota.

---

## FACTS (what the tree and docs actually say)

| # | Fact | Source |
|---|---|---|
| F1 | The job queue is a SQLite sidecar whose path is derived from `settings.db_path`, independ of `NEXUS_DATABASE_URL` | `R-02` (`job_queue_db_path`), `R-03` (bot composition root), `cli.py:894` |
| F2 | 14 feature modules construct sync SQLite engines on `settings.db_path` | `M-05`, `R-08` |
| F3 | `NEXUS_DATABASE_URL` selects PostgreSQL for `storage/db.py` sessions only | `R-06` |
| F4 | `LLMPort.complete` accepts `idempotency_key`; nothing in `llm/` or `orchestration/` reads it | `M-07`, `R-11` |
| F5 | `R2Provider` implements `upload/download/list_files/delete_objects`; the `ObjectStoragePort` protocol defines `put(key, content, idempotency_key)` / `delete(key, idempotency_key)` | `R-10`, `M-08` |
| F6 | `migration_lock` is an `fcntl.flock` on a file under the local temp dir | `R-07` |
| F7 | `healthz` returns 200 without touching DB or engines; it is the platform health gate | `R-04`, `R-21` |
| F8 | Cooldowns (`86400 s`) and rate limits live in process memory | `R-12`, `R-13` |
| F9 | `koyeb.yaml` declares a `web` service, `/healthz`, and 5 env vars; no volume, no `NEXUS_DATABASE_URL`, no instance class | `R-20` |
| F10 | The render lane is one `subprocess.run` per call with no shell, a 900 s encode timeout, `.part` staging + atomic rename + sha256 probe | `R-15` |
| F11 | No RTO/RPO numbers exist anywhere in `docs/` | `R-23`, re-grepped 2026-09-23: `grep -rn "RTO\|RPO" docs/` → only the words "roll back" in the runbook |

---

## A. Runtime & process assumptions

### A-01 — "One process is enough; the modular monolith is the terminal shape"
- **EVIDENCE:** `R-22` (modular-monolith decision, Redis/Celery rejected), enforced by `R-25` (`tests/architecture/test_modular_monolith.py`).
- **WHY IT MAY BE WRONG:** the deployment shape is *already* two processes logically (bot host + GitHub-Actions backup runner) and the API service can be scaled independently on Koyeb; "one process" is true only for the bot runtime.
- **DISPROOF:** finding a second *runtime* process (not CI) that mutates the same stores concurrently, or a documented requirement for concurrent replicas.
- **TEST:** run two bot instances against one DB+queue for 24 h with `NEXUS_DB_PATH` shared and count duplicated `nexus_job_queue` executions.
- **RISK:** Medium · S2 + S1 (duplicate job execution; see Phase 4 C-02) · `[V]` constraint is real, its *sufficiency* is `[A]`.

### A-02 — "The queue survives restarts because it is a SQLite sidecar on durable storage"
- **EVIDENCE:** `R-21` (`DATA_AND_STORAGE.md` §5) vs `R-20` (`koyeb.yaml` has no volume; Koyeb free instance cannot attach volumes, `E-02`) and `R-23` ("Local SQLite under `data/` is ephemeral on Koyeb web instances").
- **WHY IT MAY BE WRONG:** the sidecar is written to the container filesystem; scale-to-zero and redeploys discard it. Durability of the *store* is assumed from the *mechanism* (SQLite) rather than the *medium* (ephemeral disk).
- **DISPROOF:** a volume mount, or `NEXUS_DB_PATH` pointed at a persistent mount, or the queue implemented over Postgres.
- **TEST:** enqueue a job, force scale-to-zero/redeploy, inspect `data/app.sqlite.jobs.sqlite3` before/after.
- **RISK:** **High** · S1 (silent job loss; user was told "queued") · `[C]` (see `11-contradictions.md` K-01).

### A-03 — "`nexus jobs resume` recovers unfinished work"
- **EVIDENCE:** `R-01` (`resume_pending`, `resume_pending_jobs`), `R-02`, CLI `jobs resume` (`R-03`).
- **WHY IT MAY BE WRONG:** recovery requires (a) the sidecar file to still exist, (b) an operator to run the command, (c) the handler to exist for that job type, (d) inputs still present (see A-17). In webhook mode none of (a)–(d) is automatic.
- **DISPROOF:** evidence of an automatic resume on boot in webhook mode, or a scheduled `jobs resume`. (Grep: no such call site outside tests/CLI.)
- **TEST:** kill the process mid-`slideshow_render`; restart via Koyeb; observe whether the job ever reaches a terminal state without human action.
- **RISK:** Medium · S4 (recovery depends on a human who may not know) · `[V]`.

### A-04 — "`_mark_processing` claims a job" 
- **EVIDENCE:** `R-01`: `UPDATE nexus_job_queue SET status='processing' … WHERE id=? AND status IN ('pending','processing')` — **no row-count check, and `processing` is re-claimable**.
- **WHY IT MAY BE WRONG:** the update cannot distinguish "I claim a pending row" from "someone else is already processing it"; the only real mutual exclusion is the in-memory `self._tasks` map of *one* process.
- **DISPROOF:** a `WHERE status='pending'` + affected-rows check, or a lease/`owner_id` column.
- **TEST:** two `InProcessJobQueue` instances on one sidecar file, both scheduling the same id (e.g., both call `resume_pending()`); assert the handler runs once. (Existing suite tests persistence, not mutual exclusion — `R-25`.)
- **RISK:** **High** · S1 (double side effects: double Telegram send, double upload) · `[V]`.

### A-05 — "A completed job never runs again"
- **EVIDENCE:** `R-01` early-return when status is `completed`/`failed`; `_schedule` guarded by the in-memory task map.
- **WHY IT MAY BE WRONG:** the early-return is a *read* followed by a *claim*, without a transaction spanning the handler; two processes can both read `pending` before either writes `processing`.
- **DISPROOF:** a single atomic `UPDATE … WHERE status='pending'` returning the row.
- **TEST:** concurrent `resume_pending()` from two processes with a handler that increments a shared counter.
- **RISK:** **High** · S1 · `[V]` (same root cause as A-04).

### A-06 — "Jobs are pure functions of their payload"
- **EVIDENCE:** `R-02` handlers (`pdf_extract`, `story`, `slideshow_render`) take payloads and touch the filesystem/network.
- **WHY IT MAY BE WRONG:** `slideshow_render` depends on Telegram-downloaded input images whose lifecycle is owned by the notifier (`R-21` Wave 2.5 r7 §4) and on a workspace under `creative_temp_dir`; a resumed job whose files were pruned (24 h sweep, `R-17`) fails permanently — a "recoverable" job that cannot recover.
- **DISPROOF:** handler-level input re-materialisation (e.g., re-download by `file_id`).
- **TEST:** enqueue, delete workspace, run `nexus jobs resume`; observe the failure vocabulary.
- **RISK:** Medium · S2/S4 · `[V]`.

### A-07 — "Background work never blocks the user's answer"
- **EVIDENCE:** `R-21` Flow 2 — handler enqueues and returns; worker runs on the same event loop.
- **WHY IT MAY BE WRONG:** handlers run **in the same process/loop**; a CPU-bound FFmpeg child starves the loop indirectly (thread + GIL for Python parts, I/O for others). On the free instance (0.1 vCPU, `E-02`) the starvation is severe.
- **DISPROOF:** a dedicated worker process/thread pool with isolated resources.
- **TEST:** run a 30 s encode while measuring `/healthz` p99 and message-handling latency.
- **RISK:** Medium · S2 · `[A]` (magnitude), `[V]` (architecture).

### A-08 — "Work is bounded, so cost is bounded"
- **EVIDENCE:** `R-21` (`OBSERVABILITY.md` §6 "bounded work"), limits ≤5 images / 30 s (`R-22` Wave 2.5).
- **WHY IT MAY BE WRONG:** `FFMPEG_TIMEOUT_SECONDS = 900` and `MEASURE_TIMEOUT_SECONDS = 600` (`R-15`) bound *one* call; there is **no per-user concurrent job limit and no queue depth limit**, so 50 queued jobs = 50 × (up to 900 s) of a 0.1 vCPU instance.
- **DISPROOF:** admission control per user/chat, or a queue cap with back-pressure.
- **TEST:** enqueue 50 jobs from one chat; measure wall-clock completion and instance health.
- **RISK:** **High** · S2 + S5 · `[V]` (no cap found in `R-01`/`R-03`).

---

## B. Persistence & schema assumptions

### A-09 — "One logical data model, two physical backends"
- **EVIDENCE:** `R-21` (`DATA_AND_STORAGE.md` §1) vs `R-08`/`M-05` (14 modules hard-code SQLite).
- **WHY IT MAY BE WRONG:** the model is one *in principle*; at runtime the app writes the same logical tables through two different engines depending on the code path. Example: `features/ads.py` imports `AdCampaign` from `storage/models.py` **and** opens `sqlite:///{settings.db_path}` (`R-08`).
- **DISPROOF:** a single engine factory used by every module, or an architecture gate forbidding `create_engine` outside `storage/`.
- **TEST:** with `NEXUS_DATABASE_URL` set, create an ad campaign via the bot surface, then read it through `get_session()`; compare rows in PG vs the local SQLite file.
- **RISK:** **High** · S1 (silent divergence, "my data disappeared") · `[V]` for the mechanism; `[C]` vs docs (K-02).

### A-10 — "Alembic is the only schema authority"
- **EVIDENCE:** `R-21` §2, `R-06` (`decide_sqlite_bootstrap`), `R-07`.
- **WHY IT MAY BE WRONG:** ~10 modules still contain **raw `CREATE TABLE IF NOT EXISTS`** DDL executed at engine construction (`conversation_store.py`, `referral.py`, and peers) — a second schema authority that Alembic never sees and that `IF NOT EXISTS` silently reconciles in favour of whichever ran first.
- **DISPROOF:** removal of all raw DDL, or an architecture gate scanning for `CREATE TABLE` outside `migrations/`.
- **TEST:** `grep -rn "CREATE TABLE" src/ | grep -v migrations` (this lab: matches in `features/*` and `adapters/in_process_job_queue.py`); then create the DB through a feature engine *before* `nexus migrate` and run the migration chain.
- **RISK:** **High** · S1/S4 (drift, migration failure) · `[V]`.

### A-11 — "SQLite WAL is on"
- **EVIDENCE:** `R-06` sets `PRAGMA journal_mode=WAL` inside `create_all_tables` for the **application** DB.
- **WHY IT MAY BE WRONG:** the **queue sidecar** never sets WAL (`R-01`: plain `sqlite3.connect`) and the 14 sync engines don't either; WAL is also *persistent per database file*, so a DB created by another tool may be in rollback-journal mode.
- **DISPROOF:** a `PRAGMA journal_mode` executed by the queue adapter or a shared connection factory.
- **TEST:** `sqlite3 data/app.sqlite.jobs.sqlite3 "PRAGMA journal_mode"` after a fresh install.
- **RISK:** Medium · S2 (writer-blocked reads under load) · `[V]`.

### A-12 — "Two writers to one SQLite file are serialised safely"
- **EVIDENCE:** `R-06` (SQLAlchemy async engine, aiosqlite, default pool) + `R-08` (independent sync engines) + `R-01` (its own `_db_lock` per instance).
- **WHY IT MAY BE WRONG:** `_db_lock` is **per adapter instance**; two processes (bot + `nexus jobs resume`, or bot + dashboard) get no shared lock, and `sqlite3.connect(..., timeout=30)` is the only back-pressure. Long writes (e.g., a 30 MB payload) can exceed the busy timeout ⇒ `database is locked`.
- **DISPROOF:** a measured concurrency test showing no `SQLITE_BUSY` under 2-process write load.
- **TEST:** two processes writing 1 KB rows at 100 Hz for 60 s against `data/app.sqlite`; count exceptions.
- **RISK:** Medium · S2 · `[A]` (magnitude), `[V]` (no cross-process lock).

### A-13 — "Backups make data recoverable"
- **EVIDENCE:** `R-17` (`pg_dump` / SQLite online backup → R2), `R-19` (nightly 03:17 UTC GH Actions).
- **WHY IT MAY BE WRONG:** the backup covers the **app DB**, not the queue sidecar, not the checkpoint store (`data/langgraph.sqlite`), not `data/vector.sqlite`, not Chroma, not `creative_jobs.sqlite`, not the creative temp dir, and not the R2 bucket itself. Also the job runs on GitHub's runner reading production credentials — a *pull* path that fails silently except as a red workflow.
- **DISPROOF:** a restore drill that rebuilds a working instance from the backup set alone.
- **TEST:** restore into a scratch environment; measure what is missing (expect: queue history, checkpoints, vectors).
- **RISK:** **High** · S1 (an "R2 backup green" signal can hide unrecoverable state) · `[V]`.

### A-14 — "Restore exists"
- **EVIDENCE:** `R-23` documents rollback of *deployments*; no restore procedure with commands was located in this review.
- **WHY IT MAY BE WRONG:** backups without a verified restore path are not recoverability; the runbook's rollback line is about images, not data.
- **DISPROOF:** a documented, drilled restore procedure with measured time.
- **TEST:** attempt a restore from a real R2 object into a scratch DB using only the runbook.
- **RISK:** **High** · S4 (recovery undefined) · `[U]` (absence of evidence in-repo).

---

## C. Concurrency & timing assumptions

### A-15 — "Clock/timezone handling is safe"
- **EVIDENCE:** `R-01` writes `datetime.now(timezone.utc).isoformat()` into a TEXT column and orders by it; rendering uses integer microseconds (`R-15`).
- **WHY IT MAY BE WRONG:** ordering by a wall-clock string is vulnerable to NTP steps/backwards jumps; `creative_jobs` uses SQLite `CURRENT_TIMESTAMP` (UTC, second resolution) while the queue uses ISO-8601 with microseconds — two clocks with different precision and different sources in one system.
- **DISPROOF:** monotonic sequence column for ordering.
- **TEST:** step the container clock forward/back between two job inserts; verify FIFO order holds.
- **RISK:** Low–Medium · S4 (order anomalies in resume/notify) · `[V]`.

### A-16 — "Timezone is irrelevant because everything is UTC"
- **EVIDENCE:** queue timestamps UTC; analytics/gamification use `datetime.now(timezone.utc)` in most paths; **daily-boundary features** (e.g. `/daily` rewards) are date-based.
- **WHY IT MAY BE WRONG:** a "daily" award keyed on UTC rolls over at 03:30 Tehran time for Persian users — a product-visible fairness/consistency issue, not a bug in UTC handling.
- **DISPROOF:** explicit user-timezone semantics for daily boundaries.
- **TEST:** run `/daily` at 23:00 and 01:00 UTC for the same user under the same local day.
- **RISK:** Low–Medium · S1 (product-visible) · `[A]` (needs product confirmation; see `12-unknowns.md` U-24).

### A-17 — "Idempotency keys guarantee single execution"
- **EVIDENCE:** `UNIQUE(idempotency_key)` (`R-01`), `enqueue` reuses the key; `delete_thread` requires an explicit key (`R-21` PORTS §Safety invariants).
- **WHY IT MAY BE WRONG:** the key prevents *duplicate rows*, not duplicate *effects*. If the same logical request arrives with a different key (retry after a timeout where the caller regenerated the key), two effects occur. Conversely, `enqueue` returning an existing id hides that the original job already *failed* — the caller believes work is in flight.
- **DISPROOF:** a documented key-derivation rule (deterministic from request identity) plus a terminal-state check in `enqueue`.
- **TEST:** enqueue with a key, let it fail, re-enqueue with a *derived* key; observe two rows/two effects.
- **RISK:** Medium · S1 · `[V]`.

### A-18 — "Retries are safe"
- **EVIDENCE:** `R-12` `num_retries=0` (no in-place retry) + fallback chain; image generation has one bounded retry (`R-21`).
- **WHY IT MAY BE WRONG:** "no retry in place" is not "no retry". The *outer* Telegram/webhook path can re-deliver an update (`E-09`: non-200 ⇒ re-deliver), and `_process_job` re-runs any row still `pending`/`processing` at boot (`R-01`). Those are retries with no dedupe on the *effect* side.
- **DISPROOF:** update-level dedupe (update_id store) or effect-level idempotency (e.g. Telegram `message_id` reconciliation).
- **TEST:** `POST /webhook/telegram` twice with the same `update_id` while the first is in flight; count LLM calls and sends.
- **RISK:** **High** · S1/S5 (double billing, double messages) · `[V]` mechanism, `[A]` frequency.

---

## D. Deployment & topology assumptions

### A-19 — "Scale-to-zero has no data consequence"
- **EVIDENCE:** `R-23` (`deployment-koyeb.md` §"cold-start trade-off"), `R-20`.
- **WHY IT MAY BE WRONG:** cold start discards: in-memory rate limits (`R-13`), provider cooldowns (`R-12`), PTB's update queue, in-flight `BackgroundTasks` (`R-04`), and — where `NEXUS_DATABASE_URL` is unset — all SQLite state (`R-20` shows it is not set in the manifest).
- **DISPROOF:** a documented list of what is lost and why it is acceptable.
- **TEST:** force scale-to-zero with a job in flight; enumerate losses per store.
- **RISK:** **High** · S1/S2/S5 · `[V]`.

### A-20 — "`/healthz` is the right readiness signal"
- **EVIDENCE:** `R-04` + `R-21` (`OBSERVABILITY.md` §4) — deliberately DB-free.
- **WHY IT MAY BE WRONG:** Koyeb routes traffic on this probe (`R-20` `health_check.path: /healthz`). A process whose DB is unreachable, whose migration is mid-flight, or whose queue file is on a wiped disk still answers 200 ⇒ the platform keeps sending updates into a broken instance.
- **DISPROOF:** a separate readiness endpoint (or `healthz` that distinguishes liveness from readiness).
- **TEST:** start the app with an invalid `NEXUS_DATABASE_URL`; confirm 200 and a failing user flow.
- **RISK:** Medium · S2/S4 · `[V]`.

### A-21 — "Deploys are rollback-able"
- **EVIDENCE:** `R-23` (redeploy previous revision; "code rollback is safe only while the migration stays backward-compatible").
- **WHY IT MAY BE WRONG:** rollback safety is asserted, not tested; the migration chain is forward-only (3 revisions, `R-06`/`R-21`) and `nexus migrate` runs at boot for PG (`_ensure_pg_schema`). An old image against a new schema is a *hypothesis*, and no downgrade path exists.
- **DISPROOF:** a rehearsal: migrate forward, deploy old image, exercise the flows.
- **TEST:** CI job that runs old-revision code against migrated schema (chaos test).
- **RISK:** Medium · S2 · `[V]` (no downgrade path), `[A]` (breakage).

### A-22 — "The deploy manifest reflects production"
- **EVIDENCE:** `R-20` (`koyeb.yaml`: env tokens/URL/secret + `GEMINI_API_KEY`), `R-26` (smoke validates the manifest *shape*).
- **WHY IT MAY BE WRONG:** the manifest omits `NEXUS_DATABASE_URL`, `NEXUS_ALLOWED_USER_IDS`, `NEXUS_DASHBOARD_TOKEN`, `NEXUS_API_HMAC_KEY`, `NEXUS_WEBHOOK_*` extras and instance class; the smoke script explicitly treats `NEXUS_DATABASE_URL` as *recommended*, not required (`R-26`). "Smoke green" therefore coexists with "no durable DB".
- **DISPROOF:** the console env list exported and diffed against the manifest.
- **TEST:** `scripts/deploy_smoke.py --strict` passes on a manifest that omits the durability-critical variable (it does — by construction).
- **RISK:** **High** · S1/S4 · `[V]`.

### A-23 — "Multi-replica is only a future concern"
- **EVIDENCE:** `R-22` (modular monolith), `R-06`'s comment explicitly contemplating "two Koyeb replicas booting against the same volume".
- **WHY IT MAY BE WRONG:** the code already documents the two-replica case and mitigates only the `create_all` race — not queue duplication (A-04), rate limits (A-25), cooldowns, or `fcntl` locks (A-24). If anyone bumps replicas for throughput, several silent breakages appear at once (see `09-evolution-map.md`).
- **DISPROOF:** a gate that refuses `min_instances > 1`, or replicated-safe claims/leases.
- **TEST:** run 2 replicas × 30 min synthetic traffic; count duplicate sends and duplicate jobs.
- **RISK:** **High** · S1 · `[V]`.

### A-24 — "The migration lock serialises concurrent migrators"
- **EVIDENCE:** `R-07` — `fcntl.flock` on `tempdir/nexus-migrate-<hash>.lock`.
- **WHY IT MAY BE WRONG:** the lock file is local to the host. Two containers (or a laptop + container) do not share `/tmp`. The docstring says this explicitly.
- **DISPROOF:** a database-side lock (`pg_advisory_lock`) or single-writer migration job.
- **TEST:** run `nexus migrate` simultaneously in two containers against one Neon DB; observe DDL races.
- **RISK:** Medium · S4/S2 · `[V]`.

---

## E. Security assumptions

### A-25 — "Rate limiting protects the LLM budget"
- **EVIDENCE:** `R-13` (`InMemoryRateLimiter`, default 10 msg/60 s), wired at handler group −1.
- **WHY IT MAY BE WRONG:** state is per process and per user-id; a scale-to-zero restart resets it; N replicas multiply it; and it does not bound *cost per message* (a single message can trigger memory extraction + chat + moderation = multiple provider calls).
- **DISPROOF:** a durable/quota-aware limiter keyed on provider budget rather than message count.
- **TEST:** restart the process between 10-message bursts; confirm the 11th message is accepted again immediately.
- **RISK:** Medium · S5/S3 (quota drain is a resource-exhaustion attack vector, `E-10` LLM10) · `[V]`.

### A-26 — "The SSRF class of vulnerabilities is closed"
- **EVIDENCE:** `R-21` (`SECURITY.md` T9 "closed"; tests `test_http_client_ssrf.py`, `test_summarizer_ssrf.py`), `R-14`.
- **WHY IT MAY BE WRONG:** the guard is applied on the paths that use `core/http_client.py`; `/creative/video-edit`'s `_download_video_to_temp` uses plain `httpx.AsyncClient(follow_redirects=True)` with no validation and no size cap (`R-04`). Same class, different door.
- **DISPROOF:** every outbound URL fetch routed through the validated transport (or a gate that forbids raw `httpx.AsyncClient` outside `core/`).
- **TEST:** `grep -rn "httpx.AsyncClient" src/` → enumerate unguarded clients (this lab found ≥3: creative video download, image generation, local server provider).
- **RISK:** Medium (HMAC-gated) → **High if the HMAC key is shared/leaked** · S3 · `[C]` (K-03).

### A-27 — "Deny-by-default covers every entry point"
- **EVIDENCE:** `R-13` (guard at handler group −1), `R-04` (webhook secret), `R-05` (dashboard bearer *when configured*).
- **WHY IT MAY BE WRONG:** the dashboard token is **optional** (`R-05`: unset ⇒ API open) and `GET /creative/jobs/{job_id}` has **no** dependency (`R-04`). The bot is deny-by-default; the HTTP surface is not uniformly so.
- **DISPROOF:** mandatory token/route-level auth with a boot-time failure when unset.
- **TEST:** start the API without `NEXUS_DASHBOARD_TOKEN`; `curl /api/dashboard/recent_users` and `/creative/jobs/<uuid>`.
- **RISK:** Medium · S3 · `[V]`.

### A-28 — "Secrets never leave the process"
- **EVIDENCE:** `R-21` (redaction-first logging), `R-14`/`R-04` (no logging of tokens found in reviewed paths).
- **WHY IT MAY BE WRONG:** the creative/API path writes raw provider errors into `creative_jobs.error` (`R-16`) and returns them from `GET /creative/jobs/{id}`; upstream SDK error strings can embed request URLs/keys. Redaction is applied at the *logging* boundary, not the *persistence* boundary.
- **DISPROOF:** redaction applied to persisted error strings too.
- **TEST:** trigger a provider auth failure; read the job row and the HTTP response.
- **RISK:** Medium · S3 · `[A]` (needs a repro), `[V]` (no redaction at that boundary).

### A-29 — "The FFmpeg boundary is the only process boundary"
- **EVIDENCE:** `R-21` (`SECURITY.md` §4), `R-15` (one process site, no shell), enforced by `test_rendering_lane_boundary.py`.
- **WHY IT MAY BE WRONG:** the *creative legacy* executor (`R-16`) is a second process site (`subprocess.run`, `shell=False`, 300 s) not covered by the render-lane test, and `pg_dump` is a third (`R-17`).
- **DISPROOF:** a repo-wide gate enumerating every `subprocess` call site and its justification.
- **TEST:** `grep -rn "subprocess\." src/` → enumerate; check which are covered by architecture gates.
- **RISK:** Low–Medium · S3 · `[V]`.

---

## F. Provider / LLM assumptions

### A-30 — "The fallback chain is a priority chain"
- **EVIDENCE:** `R-12` (per-provider `model_name`, `fallbacks` rules, `num_retries=0`).
- **WHY IT MAY BE WRONG:** with `routing_strategy="simple-shuffle"` (harmless here because each group holds one deployment) priority holds *today*, but it is an implicit invariant with no test asserting the effective order under partial failure.
- **DISPROOF:** a test that pins the observed order with providers mocked to fail in turn.
- **TEST:** unit test: fail Ollama ⇒ expect Groq; fail both ⇒ Gemini.
- **RISK:** Low · S5 · `[V]` (config), `[A]` (invariant durability).

### A-31 — "Cooldowns prevent hammering a drained free tier"
- **EVIDENCE:** `R-12`/`R-21` (`LLM_PROVIDERS.md`: `cooldown_time=86400`, "process lifetime").
- **WHY IT MAY BE WRONG:** the doc itself scopes the cooldown to process lifetime; the deployment restarts on every wake-up (A-19). ⇒ after each cold start the drained provider is retried, fails, and (per `E-06`…`E-08`) may 429 again — the anti-storm rule is defeated by the topology.
- **DISPROOF:** cooldown state externalised (DB/Redis/DB-row) or a provider-health probe before first use.
- **TEST:** drain a provider, restart, observe a request being sent to it again.
- **RISK:** Medium · S5/S2 · `[C]` (K-04).

### A-32 — "Free tiers are sufficient"
- **EVIDENCE:** `R-21` (chain design), `R-22` (cost = 0 at idle), `E-06`…`E-08`.
- **WHY IT MAY BE WRONG:** quotas are per **organisation**, not per key (`E-06`, `E-08`), and independent sources disagree by up to 14× on daily allowances (`E-06`: 1,000 vs 14,400 RPD). Any capacity plan built on the optimistic figure is a plan built on `[U]`.
- **DISPROOF:** a console-verified quota table + a measured daily consumption.
- **TEST:** log provider responses for 7 days; count 429s and compute daily headroom.
- **RISK:** **High** · S2/S5 · `[C]` (K-05).

### A-33 — "Local models bound the worst case"
- **EVIDENCE:** `R-21` (Ollama first in chain), `R-20` (`llama-cpp-python` is a *base* dependency).
- **WHY IT MAY BE WRONG:** in the deployed container, Ollama is not running (`NEXUS_OLLAMA_BASE_URL=http://localhost:11434` with no sidecar, `R-20`), so the "local, unlimited" leg is a permanent failure in production — the first hop is a guaranteed timeout (default 60 s, `R-12`) per request before falling back.
- **DISPROOF:** a sidecar/embedded local runtime, or an explicit "no local leg in cloud" configuration.
- **TEST:** deploy the current image and time the first LLM call.
- **RISK:** **High** · S2 (latency) + S5 · `[V]` (no local server in the manifest).

---

## G. Cost / operations assumptions

### A-34 — "Cost is zero at idle"
- **EVIDENCE:** `R-21` (Q1), `R-22`.
- **WHY IT MAY BE WRONG:** idle-ness is a *traffic* property; the system also has (a) a nightly GitHub-Actions backup (`R-19`) that is free only while the repo is public/under quota, (b) Neon compute for every cold start (min billed granularity on paid tiers; free tier counts CU-hours, `E-01`), (c) R2 storage growth without a lifecycle rule, (d) build minutes for a ~GB image.
- **DISPROOF:** a 90-day bill/invoice showing $0 with the deployed topology.
- **TEST:** measure Neon CU-hours consumed by a week of cold-start traffic.
- **RISK:** Low–Medium · S5 · `[A]`.

### A-35 — "Observability exists"
- **EVIDENCE:** `R-21` (`OBSERVABILITY.md`: structured events, in-process counters, `nexus metrics snapshot`).
- **WHY IT MAY BE WRONG:** counters vanish with the process (scale-to-zero), logs go to stdout (platform retention), there is no external alerting sink and no trace correlation across the Telegram → LLM → queue path; the "alert list" is a human reading logs.
- **DISPROOF:** an external sink with retention and an alert that fires without a human looking.
- **TEST:** force a failure (kill Ollama) and measure time-to-detection with nobody reading logs — expected: never.
- **RISK:** Medium · S4 · `[V]`.

### A-36 — "Docs are the source of truth"
- **EVIDENCE:** `R-21`/`R-22` are explicitly declared authoritative ("contradictions are bugs"), `R-25` enforces docs integrity for `docs/`.
- **WHY IT MAY BE WRONG:** several counts and mechanisms in those docs no longer match the tree (`M-01`…`M-08`), and the docs-integrity test checks *links/index/titles*, not *numeric or mechanistic accuracy*. Doc authority without numeric verification produces confident wrong decisions.
- **DISPROOF:** doc numbers generated by CI (e.g., the count table in `OVERVIEW.md` §8 regenerated by a script) rather than hand-written.
- **TEST:** `find src -name "*.py" | wc -l` vs `OVERVIEW.md` §8 (this lab did: 231 vs 215).
- **RISK:** Medium · S4 · `[V]` (K-06).

---

## SUMMARY TABLE — the 36 assumptions, ranked by consequence

| ID | Assumption (short) | Marker | Severity class | Risk | First evidence |
|---|---|---|---|---|---|
| A-02 | Queue sidecar survives restarts in webhook mode | `[C]` | S1 | **High** | `R-20`, `R-02` |
| A-04 | `_mark_processing` claims a job | `[V]` | S1 | **High** | `R-01` |
| A-05 | A completed job never re-runs | `[V]` | S1 | **High** | `R-01` |
| A-09 | One logical data model, two backends | `[C]` | S1 | **High** | `R-08`, `M-05` |
| A-10 | Alembic is the only schema authority | `[V]` | S1/S4 | **High** | raw DDL in `features/` |
| A-13 | Backups make data recoverable | `[V]` | S1 | **High** | `R-17` |
| A-14 | Restore exists | `[U]` | S4 | **High** | `R-23` |
| A-18 | Retries are safe | `[V]` | S1/S5 | **High** | `R-01`, `E-09` |
| A-19 | Scale-to-zero has no data consequence | `[V]` | S1/S2/S5 | **High** | `R-13`, `R-12`, `R-04` |
| A-22 | The manifest reflects production | `[V]` | S1/S4 | **High** | `R-20`, `R-26` |
| A-23 | Multi-replica is a future concern | `[V]` | S1 | **High** | `R-06`, `R-01` |
| A-32 | Free tiers are sufficient | `[C]` | S2/S5 | **High** | `E-06`…`E-08` |
| A-33 | Local models bound the worst case | `[V]` | S2/S5 | **High** | `R-20` |
| A-08 | Work is bounded ⇒ cost is bounded | `[V]` | S2/S5 | Medium | `R-15`, `R-01` |
| A-11 | WAL everywhere | `[V]` | S2 | Medium | `R-01` |
| A-12 | Two SQLite writers coexist | `[A]` | S2 | Medium | `R-01`, `R-06` |
| A-17 | Keys guarantee single effects | `[V]` | S1 | Medium | `R-01` |
| A-20 | `/healthz` is the right readiness signal | `[V]` | S2/S4 | Medium | `R-04`, `R-20` |
| A-21 | Deploys roll back safely | `[V]`/`[A]` | S2 | Medium | `R-23` |
| A-24 | `flock` serialises migrators | `[V]` | S4/S2 | Medium | `R-07` |
| A-25 | Rate limiting protects the budget | `[V]` | S5/S3 | Medium | `R-13` |
| A-26 | SSRF class is closed | `[C]` | S3 | Medium→High* | `R-04`, `R-14` |
| A-27 | Deny-by-default covers HTTP too | `[V]` | S3 | Medium | `R-04`, `R-05` |
| A-28 | Secrets never leave the process | `[A]` | S3 | Medium | `R-16` |
| A-31 | Cooldowns stop retry storms | `[C]` | S5/S2 | Medium | `R-12`, `R-19` |
| A-35 | Observability exists | `[V]` | S4 | Medium | `R-21` |
| A-36 | Docs are the source of truth | `[V]` | S4 | Medium | `M-01`…`M-08` |
| A-01 | One process is the terminal shape | `[V]`/`[A]` | S2/S1 | Medium | `R-22` |
| A-03 | `jobs resume` recovers work | `[V]` | S4 | Medium | `R-01` |
| A-06 | Jobs are pure functions of payload | `[V]` | S2/S4 | Medium | `R-02` |
| A-07 | Background never blocks the answer | `[V]`/`[A]` | S2 | Medium | `R-21` |
| A-29 | FFmpeg lane is the only process boundary | `[V]` | S3 | Low–Medium | `R-16`, `R-17` |
| A-30 | Fallback chain stays prioritised | `[V]`/`[A]` | S5 | Low | `R-12` |
| A-34 | Cost is zero at idle | `[A]` | S5 | Low–Medium | `R-19`, `E-01` |
| A-15 | Clock handling is safe | `[V]` | S4 | Low–Medium | `R-01`, `R-16` |
| A-16 | Timezone is irrelevant | `[A]` | S1 | Low–Medium | daily-boundary features |

\* Medium while the HMAC key is private; High if that key is shared with any untrusted caller or leaks (Phase 6 S-04).

---

## ANALYSIS (this lab's reasoning — not repo fact)

**A1. The high-risk assumptions share one shape.** Eight of the eleven High items (A-02, A-04, A-05, A-09, A-13, A-18, A-19, A-23) are all failures of the same reasoning pattern: *a mechanism that works in one topology is assumed to keep working in another* (single process → replicas; local disk → ephemeral container; one engine → many engines). This is not a code-quality problem; it is a **topology-coherence** problem. Fixing any one in isolation (e.g., moving the queue to Postgres) does not fix the others; a topology contract is what is missing. Candidates are registered in `10-adr-candidates.md` (ADR-01…ADR-06).

**A2. Documentation is doing work it cannot do.** The docs are unusually good, which raises the cost of their drift: `[C]`-flagged assumptions (A-02, A-09, A-26, A-31) are exactly those where a strong doc statement suppresses further checking. The cheap systemic fix is *generated* numbers (M-01-type counts) and *mechanism-level* claims ("X is idempotent because Y exists") rather than adjectives ("idempotent").

**A3. What would change my judgement.** If production actually runs with `NEXUS_DATABASE_URL` set *and* a persistent volume for `data/`, A-02/A-09/A-13 soften to Medium and A-19 to Medium — but nothing in the repo proves that, and `koyeb.yaml` suggests otherwise. That single fact is the highest-value verifiable unknown in this lab (see `12-unknowns.md` U-01, U-03).

**A4. Assumptions that survived.** The render lane's evidence discipline (A-08's *mechanism*, F10), the SSRF transport (F5/A-26's guarded path), deny-by-default for Telegram (A-27's bot half), and the checkpoint lifecycle's fail-closed deletion rules are genuinely strong and were verified, not merely read. They are the parts of the system where the docs and the code agree.
