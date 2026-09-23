# PHASE 3 — FAILURE MODE AND EFFECTS ANALYSIS (FMEA)

**Scope:** the runtime as deployed (Telegram webhook/polling → engines → queue → creative lane → stores → providers).
**Count:** **45 failure modes.**
**Severity is not judged by taste.** It is classified by three *defined* rules; each mode also carries a reachability class.

| Axis | Values (defined) |
|---|---|
| **Severity class** | **S1** silent loss/duplication or an observable incorrect durable state · **S2** service stops/degrades for ≥1 user without data loss · **S3** a trust boundary is crossed (read/write/execute/egress) · **S4** recovery or change is blocked, or the evidence needed to diagnose is destroyed · **S5** converts to money/quota with no functional symptom |
| **Reachability** | **L-A** reachable today with no misconfiguration and no external failure · **L-B** needs a specific external failure or a *documented* operator action · **L-C** needs a future topology change (replicas) or a security precondition (key leak/shared key) |
| **Data-loss risk** | **None** (no user-visible durable state lost) · **Partial** (records/artefacts lost but re-derivable) · **Total** (unrecoverable without an external backup that itself is unverified) |

**Marker legend:** `[V]` verified mechanism · `[A]` assumed/derived · `[U]` unverified · `[C]` contradicted.

---

## FMEA TABLE

| # | Sev / Reach | Component | Failure | Trigger | Impact | Detection | Recovery | Data-loss | Security | Operational | Mitigation (candidate, not decided) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| F-01 | S1 · L-A | Job queue (`R-01`) | Same job executed twice in one process | `resume_pending()` called while a task for the same id is live, or two `enqueue` calls map to two tasks after a restart | double Telegram send, double R2 upload, double XP award | none today (no execution counter, no per-job attempt log) | re-run handler again (worsens) | None (duplicated effect) | — | operator cannot tell which run "counted" | atomic claim `UPDATE … WHERE status='pending'` + affected-row check + lease owner id |
| F-02 | S1 · L-B | Job queue | Two processes execute one job | bot + `nexus jobs resume` on the same sidecar file; or two replicas | as F-01, cross-process | none | manual DB surgery | None | — | review of "who ran it" impossible | externalised queue (DB/broker) or DB-side advisory lock per job |
| F-03 | S1 · L-A | Job queue | Job silently never runs after crash | process killed between `_mark_processing` and completion | job row stuck `processing` forever; **no automatic resume outside process start** | `nexus jobs status` (manual) | `nexus jobs resume` (claims `pending` only ⇒ **cannot** recover `processing` rows) | None | — | user was told "queued"; nothing happens | visibility timeout / heartbeat + stale-claim reaper |
| F-04 | S1 · L-A | Job queue | Job sidecar lost | scale-to-zero / redeploy / container replacement (ephemeral disk, `E-02`) | queue history + terminal results gone; "completed" is unknowable | none | none | **Total** for queue state | — | repeated user complaints, no data to inspect | queue in the durable DB (ADR-02) |
| F-05 | S1 · L-A | Job queue | `enqueue` returns an existing id for a **failed** job | retry after failure with the same idempotency key | caller believes work is in flight; user waits forever | none | operator must inspect manually | None | — | silent hang | `enqueue` returns terminal state; explicit `requeue` API |
| F-06 | S2 · L-A | Job queue | Unbounded concurrency | N jobs enqueued in a burst (10 msg/60 s rate limit per user ⇒ 10 renders) | N FFmpeg children on 0.1 vCPU/512 MB ⇒ OOM kill | host OOM (platform), not the app | container restart; **all in-flight rows stay `processing`** | None | — | one user can stop the bot | concurrency semaphore + per-chat admission control |
| F-07 | S5 · L-A | Job queue | Queue table grows without bound | no pruning/retention anywhere in `R-01` | sidecar grows with every job + result JSON | disk usage (manual) | manual DELETE | None | — | "disk filling" appears in the alert list (`R-21` §6) but is only a filesystem check | retention job + row cap |
| F-08 | S2 · L-A | Worker handlers (`R-02`) | `slideshow_render` fails permanently after prune | 24 h workspace sweep deletes inputs of a resumed job | job fails with a typed error; user told to retry | job status shows failure | re-upload required | Partial | — | support burden | re-materialise inputs (Telegram `file_id` re-download) or pin inputs until terminal state |
| F-09 | S1 · L-A | Persistence plane | Same logical table written to two stores | `NEXUS_DATABASE_URL` set; a feature module (`R-08`) writes locally while `storage/db.py` writes PG | data "disappears" depending on read path (e.g. an ad campaign invisible to the dashboard) | none — reads silently return different sets | manual reconciliation (no tooling exists) | **Partial→Total** depending on table | — | undiagnosable without knowing the split | single engine factory; gate forbidding `create_engine` outside `storage/` |
| F-10 | S1 · L-A | Persistence plane | `/ai` conversation history breaks in PG mode | `ConversationStore` always opens SQLite and creates `conversation_history` at construction (`R-08`) | history written to an ephemeral file the rest of the system never backs up | none | regenerate no — history is user data | Partial | — | "the bot forgot our chat" | make ConversationStore use the central session/backend |
| F-11 | S1 · L-A | Schema | Raw `CREATE TABLE IF NOT EXISTS` creates a *different* shape than the model | any module constructing its engine before `nexus migrate` | migration later sees an incompatible table; `IF NOT EXISTS` hides it | none (no schema fingerprint check for these tables) | drop/recreate (loses rows) | Partial | — | "fresh install works, upgraded install doesn't" | forbid runtime DDL; add a drift check covering all tables |
| F-12 | S1 · L-B | Schema | Migration lock ineffective across hosts | two containers run `nexus migrate` | DDL race; partial migration | Alembic error (usually) | manual repair | Partial | — | outage during deploy | DB-side advisory lock or a single migration job in CD |
| F-13 | S2 · L-B | DB (Neon) | Compute quota exhausted | 100 CU-hours free exceeded | all writes fail; reads may still work | provider console (no in-app signal) | wait for month boundary or upgrade | None | — | production stops on a billing edge | quota alert + budget, or paid plan |
| F-14 | S1 · L-B | DB (Neon) | Storage ceiling reached (0.5 GB free) | checkpoint growth (`§Phase 2 3.3`) | writes fail | provider console only | prune checkpoints/upgrade | Partial | — | silent until the first failed write | retention budget + checkpoint vacuum + volume projection |
| F-15 | S2 · L-A | DB (SQLite) | `database is locked` | sync feature engine holds a write while async engine writes | one request fails with an unhandled exception | exception in logs | retry eventually succeeds | None | — | flaky failures under burst | WAL everywhere + busy_timeout + single writer discipline |
| F-16 | S4 · L-A | DB (SQLite) | WAL not enabled for sidecar/feature DBs | new file created by a module that never ran the pragma | writer-blocked reads under load (`E-14`) | manual `PRAGMA journal_mode` check | set pragma once per file | None | — | performance mystery | central connection factory that sets pragmas |
| F-17 | S1 · L-B | DB (SQLite) | File corruption | kernel/power loss during write without WAL/fsync discipline (dev machine, self-host) | unreadable DB | open fails | restore from last backup (nightly) | **Total** since last backup | — | manual work | WAL + `PRAGMA synchronous` policy + restore drill |
| F-18 | S1 · L-B | Backup (`R-17`) | Backup is green but useless | dump succeeds while a *second* store (queue/checkpoints/vectors/`creative_jobs`) is the real state | false confidence; restore is partial | none (the workflow validates upload, not restore) | unknown | Partial | — | incident-time discovery | restore drill CI + store inventory in the backup |
| F-19 | S2 · L-B | Backup | Nightly job silently stops | GH Actions disabled/secret expired/repo fork | no backups at all | only a red workflow (nobody watches) | none until noticed | Total (as of last success) | S3 (secrets in CI) | long blind window | external check + "no backup in N days" alert |
| F-20 | S1 · L-A | Checkpoints | Retention deletes resumable state too early/late | 30-day window policy misapplied | resume forks a new thread (late) or loses resume ability (early) | `nexus checkpoints inspect` (manual) | fork from messages | Partial | — | user-visible amnesia | policy tests exist (`R-25`); add metric export |
| F-21 | S1 · L-B | Checkpoints | Circuit breaker never trips because anomalies are invisible | reconciler anomalies counted only in logs (`R-21` §2) | runaway deletion or freeze | manual log reading | `--apply` off | Partial | — | cleanup dilemma | export the anomaly ratio as an external metric |
| F-22 | S3 · L-A | API (`R-04`) | Unguarded outbound fetch from caller-supplied URL | `POST /creative/video-edit` with `video_url` (HMAC required) ⇒ plain `httpx`, redirects followed, no size cap | SSRF-equivalent probing of internal addresses; disk fill via large download | none (no egress log for this path) | — | None | **S3** | — | route through `core/ssrf_guard` transport + scheme allow-list + size cap (`A-26`) |
| F-23 | S3 · L-A | API | Unauthenticated job read | `GET /creative/jobs/{job_id}` has no auth dependency | filenames/URLs/errors of creative jobs readable by anyone who can reach the port | none | — | None | S3 (IDOR-shaped) | — | route-level bearer or HMAC |
| F-24 | S3 · L-A | Dashboard | Open dashboard when token unset | `NEXUS_DASHBOARD_TOKEN` empty (default) | stats/user list readable if port exposed | none in-app | set token | None | S3 | — | boot-time refusal to serve if token missing and bind ≠ loopback |
| F-25 | S3 · L-C | API | HMAC replay | captured signed request replayed within the ±300 s window | duplicate creative jobs (cost), potential double-provider calls | none (no nonce cache) | — | None | S3 | — | nonce store or idempotency key tied to request body hash |
| F-26 | S3 · L-C | Secrets | HMAC/dashboard/webhook secret shared beyond the operator | a caller given the key reuses it for internal fetches | as F-22 but without precondition caveats | none | rotate | None | S3 | — | per-caller keys; audit of who holds them |
| F-27 | S3 · L-A | LLM egress | Prompt injection via ingested content | user-supplied document/URL content fed into prompts (`R-14` summarizer, RAG) | instruction override; potential data exfiltration via the model | none (no output validation gate) | — | None | **S3** | — | treat retrieved content as data; tool-permission minimisation; output checks (OWASP LLM01/LLM05, `E-10`) |
| F-28 | S3 · L-A | LLM egress | System-prompt/persona leakage | direct extraction prompt | reveals internal instructions/personality config | none | — | None | S3 (LLM07) | — | prompt hardening + refusal policy; assume it *will* leak |
| F-29 | S3 · L-B | Tool execution | Shell tool enabled by mistake | `NEXUS_ENABLE_SHELL=true` in an env file | arbitrary local command execution within allow-list rules | config diff (manual) | disable | None | S3 | — | fail-closed already; add boot warning + audit event |
| F-30 | S2 · L-A | LLM chain | Guaranteed first-hop timeout in cloud | Ollama configured (default URL) but absent in the container | every request pays up to 60 s before fallback | latency only, no error surfaced distinctly | fix config | None | — | users think the bot is slow | disallow local leg unless a local server is reachable |
| F-31 | S5 · L-A | LLM chain | Cooldown lost on restart | scale-to-zero / redeploy | drained provider retried; possible long 429 streaks; quota churn | provider 429s in logs | none automatic | None | — | cost/latency spikes exactly when the instance is cold | externalised cooldown state or provider health cache |
| F-32 | S2 · L-B | LLM chain | Full chain exhausted | all providers 429/down | user gets degraded/FakeLLM answer | disclaimer text (user-visible) | wait/cooldown | None | — | looks like a broken bot | budget-aware queue + explicit UX for degradation |
| F-33 | S1 · L-B | Rename/rotation | Telegram token rotated without restarting the app | BotFather revoke + env update mid-run | sends fail with 401; webhook 403 for all updates | platform logs | restart | None | — | outage | documented rotation procedure (exists in runbook, `R-23`) + readiness check |
| F-34 | S2 · L-A | Webhook (`R-04`) | Update accepted then lost | API answers 200, then the process dies before PTB processes the update | user command vanishes silently; Telegram will **not** re-deliver | none (no "received vs processed" counter) | none | Partial (lost intent) | — | "the bot ignored me" | acknowledge only after durable enqueue, or persist updates before ack |
| F-35 | S2 · L-B | Webhook | Retry storm doubles side effects | upstream non-200 while a handler still ran (e.g., a slow LLM call beyond the 60 s window) | duplicate LLM calls/answers | none (no `update_id` dedupe) | — | None | — | cost + duplicate messages | `update_id` dedupe store |
| F-36 | S2 · L-A | Rate limiting | Limits reset on restart and are per replica | cold start / scale-out | a user exceeds intended limits | none | — | None | S3/S5 (abuse) | — | durable limiter keyed by user + provider budget |
| F-37 | S4 · L-A | Observability | Evidence destroyed by restart | in-process counters only (`R-21` §3) | no history to diagnose after an incident | none | none | None | — | postmortems become speculation | external sink or periodic snapshot to DB |
| F-38 | S4 · L-A | Observability | No unattended alerting | no external monitor in-repo | outage detected by user complaint | user complaint | — | None | — | availability unknown | external probe on `/healthz` + a real readiness probe |
| F-39 | S2 · L-A | Readiness | Traffic routed to an unhealthy instance | `/healthz` is DB-free and used as the platform gate (`R-20`) | failures for all users while the platform reports healthy | platform says healthy | manual | None | — | MTTR inflated | separate liveness vs readiness |
| F-40 | S1 · L-B | Migrations | Forward-only migration + rollback | deploy a bad revision after migrating | old code against a new schema; undefined behaviour | app errors | forward-fix only | Partial | — | rollback promise (`R-23`) not enforceable | expand/contract migrations; test old-code-new-schema |
| F-41 | S2 · L-A | Deploy | Instance class/limits mismatch with workload | free 0.1 vCPU/512 MB used for renders (documented default manifest has no instance class) | OOM/slow renders | platform metrics | resize | None | — | capacity surprises | declare instance class in the manifest + a render feasibility check |
| F-42 | S2 · L-A | Filesystem | Creative temp dir grows | no cleanup on crash; 24 h sweep only runs *on the next job* (`R-21`) | disk full on a 2 GB ephemeral disk | filesystem check (manual) | `nexus maintenance housekeeping` | Partial (workspaces) | — | jobs begin failing for "no space" | periodic housekeeping triggered by the platform, not by traffic |
| F-43 | S2 · L-B | Image provider | Pollinations unavailable/blocked | upstream change (no SLA, `R-21`) | `/imagine` fails | typed error | retry later | None | — | feature outage | second provider behind the same port (Gemini opt-in) |
| F-44 | S5 · L-A | Image generation | Cache is in-process and time-based | restart / new instance | duplicate generation cost (free tier now, paid later) | none | — | None | — | cost creep | content-addressed cache keyed on prompt+params to disk/R2 |
| F-45 | S4 · L-A | Documentation | Doc numbers drift from the tree | any change; docs are hand-maintained for counts (`M-01`…`M-04`) | decisions based on wrong sizes | manual re-measure | update docs | None | — | systemic misjudgement | generate counts in CI; delete hand-written totals |

---

## 1. The data-loss subset (12 modes, worst first)

| Order | Mode | Data-loss class | Why it is worse than it looks |
|---|---|---|---|
| 1 | F-04 queue sidecar lost | **Total** (for queue state) | the user was explicitly told "queued"; there is no record to apologise with |
| 2 | F-18 backup green but partial | Partial→Total | the *signal* that would prevent #1 is itself misleading |
| 3 | F-17 SQLite corruption | Total since last backup | only relevant in local/self-host topologies, but that is where the file lives by default |
| 4 | F-19 backup stops silently | Total as of last success | no watchdog; GH Actions failure is an email |
| 5 | F-09 split persistence plane | Partial→Total | divergence is invisible; no reconciliation tooling exists |
| 6 | F-10 history in PG mode | Partial | `/ai` history lives in a file nothing backs up |
| 7 | F-11 duplicate DDL shapes | Partial | upgrade path differs from fresh install |
| 8 | F-14 Neon storage ceiling | Partial | writes stop; the app has no branch for "disk full" |
| 9 | F-03 stuck `processing` | None (row survives) but *effect* lost | the job never completes and cannot be re-claimed by the documented CLI |
| 10 | F-34 accepted-then-lost update | Partial (user intent) | Telegram will not re-deliver after 200 |
| 11 | F-08 pruned inputs | Partial | a "resumable" job that cannot resume |
| 12 | F-21 reconciler anomalies invisible | Partial | deletion/freeze decisions are made with no historical signal |

## 2. Detection gaps (no signal exists today)

| Gap | Modes affected | What would close it |
|---|---|---|
| No job-execution/attempt counter or lease | F-01, F-02, F-03, F-06 | per-job `attempts`, `owner`, `heartbeat_at` columns |
| No received↔processed counter for updates | F-34, F-35 | increment on webhook receipt and on handler completion, diff them |
| No egress audit for creative/ingested URLs | F-22, F-27 | a single egress chokepoint with a log line per outbound fetch |
| No store-level metrics (rows/bytes/queue depth) | F-07, F-14, F-42 | snapshot job writing to the DB, visible via `nexus metrics` |
| No restore-drill evidence | F-18, F-19 | scheduled drill with a result row in the DB |
| No alert path that does not require a human to look | F-13, F-14, F-19, F-38 | external probe + one webhook to a channel the owner reads |

## 3. FACTS vs ANALYSIS

**FACTS.** All 45 mechanisms above are traceable to `R-01`…`R-26` or `E-*`; the queue-claim weakness (F-01/F-02/F-03), the unbounded concurrency (F-06), the split plane (F-09/F-10), the unguarded creative fetch (F-22), the unauthenticated job read (F-23), the accepted-then-lost webhook update (F-34), and the process-local cooldown (F-31) are **code facts**, not interpretations.

**ANALYSIS.** Three clusters dominate:
1. **Durability cluster (F-01…F-08, F-34):** the system's two "durable" mechanisms (queue, webhook ack) both have a window where an effect is promised and then lost, and the *recovery* tool (`jobs resume`) cannot recover the most common stuck state (`processing`).
2. **Plane-split cluster (F-09…F-11):** correctness depends on which code path reads/writes a table; this is the failure most likely to be misdiagnosed as "data loss by the provider".
3. **Evidence cluster (F-37…F-39, F-18, F-19, F-21):** every cluster above is made worse by the absence of unattended detection; the system is architecturally honest (it logs) but operationally blind (nobody is told).

The mitigation column deliberately proposes *mechanisms* (claim, lease, chokepoint, drill, probe) rather than products; the decision belongs to `10-adr-candidates.md`.
