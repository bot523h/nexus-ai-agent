# PHASE 9 — EVOLUTION MAP (STAGE A → B → C)

**Question:** if the system has to grow — more traffic, more users, more contributors, better availability — what exactly breaks at each step, what forces the step, what are the migration options, and can each step be taken without breaking the previous one?
**Method:** each break below is a *mechanism verified in the pinned tree* (or an explicit external corroboration), not a generic "you'll need Kafka someday" claim. Stages are defined by observable properties, not by headcount.

---

## 0. Stage definitions (so the map is falsifiable)

| Stage | Definition (observable) | Current status |
|---|---|---|
| **A — Single process** | Exactly one long-lived app process owns all state-bearing roles: Telegram update handling, job execution, timers, rate limiting, migrations, backups. Durable state (if any) is external (PG, R2). | **A is the documented deployment** (`koyeb.yaml`: one service, one container, no volume; `bot/webhook.py` serves the API and PTB in one event loop) `[V]` |
| **B — Replicas** | ≥2 app processes run the *same* image concurrently, sharing the same durable stores; scale-out is a platform knob, not a code change. | Latent — the platform supports it; the code does not. `[V]` |
| **C — Distributed roles** | Processes are *differentiated*: ingress (webhook), worker pool (jobs/renders), scheduler, admin/API. Each role scales independently; all coordination is through durable shared state. | Not present in any form. `[A]` |

**Important asymmetry:** the platform (Koyeb) can move the system from A to B with a console click, while the *code* stays at A. That is what makes Stage B the most dangerous stage in this map — **it can be entered accidentally**.

---

## 1. Stage A → B: what breaks

| # | What breaks | Why (mechanism, verified) | Migration pressure | Migration option | Backward compatibility |
|---|---|---|---|---|---|
| B-01 | Telegram update handling in **polling** mode | Two pollers both call `getUpdates` and advance the offset independently ⇒ updates are consumed by whichever instance asks first; a doomed instance can confirm and lose them. Independently corroborated by another project's dual-instance incident (`E-21`: "update theft"), where the fix was to confirm only after durable processing | Any second instance while `polling` mode is active; blue/green rollouts | Switch to **webhook mode** (already implemented, `R-04`) and make receipt durable (B-02); or a single-instance guard (pid/lock row) | webhook mode exists today; no API change |
| B-02 | Update loss/duplication on ingress | Webhook answers `200 {"ok": true}` before durable receipt; no `update_id` ledger (`X-01`, `R-04`) | Any replica overlap or restart | Add an `updates` table with `UNIQUE(update_id)`; insert **before** replying 200; processors skip known ids | Additive table + one write path; old code simply lacks dedupe (safe to run alongside) |
| B-03 | Job execution duplicated | `_mark_processing` accepts `pending` or `processing` and checks no row count; the only duplicate guard is an in-process `dict`; `resume_pending()` resets `processing → pending` at startup | Second worker *or* two app replicas | Atomic claim: `UPDATE … SET status='processing', owner=?, lease_until=… WHERE id=? AND status='pending'` + require `rowcount == 1`; treat expired leases as claimable | Adds columns (`owner`, `lease_until`); old rows with NULL leases are treated as expired by new code; old code ignores the columns ⇒ **additive** |
| B-04 | Rate limiting becomes N× weaker | The limiter holds its window **in process memory** (`R-13`) | Any replica count > 1 | Move the window to a shared store (a `rate_events` table with a `(chat_id, ts)` index and a windowed `COUNT`) | Behaviour-preserving; the default limits stay configurable (`10/60 s` verified) |
| B-05 | LLM cooldowns become inconsistent and ineffective | litellm cooldown state is per-process; `allowed_fails=1`, `cooldown_time=86 400 s` (`R-12`, A-31) | Any replica count > 1, or frequent restarts | Externalise provider health (a small table or KV) and consult it before dispatch; or accept per-process state but alert on 429 storms | Additive; the chain order is unchanged |
| B-06 | Scheduled posts / reminders fire twice | Timers live in `asyncio` task maps (`R-08`) | Any second process | Turn timers into rows with `next_fire_at`, claim + `sent_at` guard (see Phase 5 X-04) | Feature APIs unchanged; DB gains a scheduler table |
| B-07 | Migrations race across hosts | `migration_lock` is an `fcntl.flock` on a **local** temp file (`R-07`) | Two containers starting together (a deploy does exactly this) | PostgreSQL advisory lock keyed by a constant (`E-15`) instead of a file lock; keep the file lock as the SQLite/dev path | Same CLI, same idempotency; only the lock primitive changes |
| B-08 | Backup/housekeeping double-run | GitHub Actions cron + a starting instance can both run maintenance; `pg_dump` ×2 is harmless but wasteful; retention logic is idempotent | Blue/green deploy during a cron window | Advisory-lock the maintenance job too; or run maintenance only from CI (already the documented path) | No change to artefacts |
| B-09 | Local SQLite stores silently **fork** | Each instance gets its own ephemeral file ⇒ two divergent "databases" (app DB, queue, creative jobs, checkpoints) with no reconciliation (`R-01`, `R-06`, `R-16`, `R-21`) | *Any* replica count > 1 **while** `NEXUS_DATABASE_URL` is unset | Make the external DB **required** for multi-instance mode; refuse to start in replica mode without it (fail-closed config) | Configuration-level; the SQLite path stays for dev/single-instance |
| B-10 | Shutdown hook reset clobbers another worker's jobs | On shutdown, unfinished jobs are reset `→ pending`; with a shared queue this can resurrect rows a *different* process is still executing | Scale-in events during deploys | Only reset rows this process **owns** (`owner = me`), never a global sweep | New code respects `owner`; old code's global sweep is the thing being replaced |

**Stage B verdict:** the *cheap* half of stage B is B-01/B-02/B-03/B-07 (four small, additive changes that also fix Phase 4/5 defects at Stage A). The *dangerous* half is B-09/B-10, which are configuration and semantics rather than code. **Recommendation shape (not an instruction):** treat ">1 instance" as a mode that requires the external DB and the claim primitive; fail-closed otherwise.

---

## 2. Stage B → C: what breaks / what becomes possible

At Stage C the roles separate, and every in-process assumption that survived B by luck must be replaced by an explicit contract.

| # | What breaks | Why | Migration pressure | Migration option | Backward compatibility |
|---|---|---|---|---|---|
| C-01 | "The queue is a local file" | The `JobQueuePort` abstraction exists precisely for this, but the only implementation is the SQLite sidecar (`R-02`, A-02) | Worker pool on a different host; queue durability requirement | Implement a durable backend behind `JobQueuePort` (PG-backed table is the smallest step: `SELECT … FOR UPDATE SKIP LOCKED` + visibility timeout — `E-15`); an external broker is *not* required for the next order of magnitude | The port boundary keeps call sites unchanged for `bot`/CLI; render lane keeps its handler contract |
| C-02 | Job *payload* compatibility | Payloads are JSON in a local table, read by whichever worker exists (`R-01`) | First time an old worker meets a new payload (or vice versa) | Version the payload (`{"v": 1, …}`) and **deploy workers before enqueue-side new features**; unknown version ⇒ park, never crash (the handler registry already parks unknown handlers, keep the same discipline) | Additive field; unknown versions parked instead of failing |
| C-03 | Progress/notification coupling | Completion notification runs **after** the terminal write, failures swallowed (`R-01`) | When the role that finishes a job is not the role that owns the user's chat session | Outbox row written **in the same transaction** as the terminal state; a notifier role drains it (this also fixes C-07 in Phase 4) | Additive table; if the drainer is absent, the terminal state is still correct |
| C-04 | Renders starve chat | Both run in one event loop today; FFmpeg subprocesses compete for 0.1–0.5 vCPU | Any render volume at MEDIUM+ | Split worker classes by handler category (cheap: an env-driven role filter over the same registry); pin renders to a CPU-richer instance class | Same queue, same registry — a worker simply refuses categories it doesn't own |
| C-05 | No per-role observability | Metrics are process-local (`/metrics` on the web app); a worker fleet would be invisible (`R-21`) | First split deployment | One metric sink with role labels (`nexus_role="worker|web|scheduler"`); keep the current endpoint for the web role | Additive labels; scrapers keep working |
| C-06 | Config drift between roles | One settings object, 74 fields (`M-04`); a worker does not need the dashboard token and must not need the poller | First split deployment | Split settings into *profiles* (validated per role at start-up; fail if a webhook secret is missing on the web role, etc.) | Same env var names; only validation becomes role-aware |
| C-07 | Deploy ordering becomes an interface | A migration + old workers + new workers can coexist for minutes (`R-18`, `R-23`) | Any schema change with multiple roles | Expand/contract discipline, enforced by CI: additive migration → deploy → backfill → contract in a later release; the runbook line already states the intent (`R-23`), CI does not test the *old* code against the new schema (`A-21`) | This is *how* backward compatibility is preserved; without it, splits multiply risk |
| C-08 | Blast radius of a leaked reason | One shared process ⇒ one env scope; per-role processes ⇒ multiple secret scopes (and multiple places to leak) | First split deployment | Least-privilege secrets per role (worker holds no dashboard token; web holds no render temp dir), plus per-role rotation | Config-only |
| C-09 | Scheduler needs a real owner | At Stage C nothing guarantees exactly one scheduler unless state says so | First split deployment | Lease-based scheduler (one row, heartbeat); losers idle. This is the *same primitive* as the job lease (C-01) — build it once | Additive |

**Stage C verdict:** the architecture is **already shaped for C** in the places that matter (dependency ports for LLM/queue/storage/checkpointing; a handler registry with park-on-unknown; a separate render lane with its own discipline). What is missing is not the *interface* but the *state*: leases, outbox, dedupe keys, role profiles, and role-scoped observability. In other words: **this codebase's evolution path is blocked less by design and more by the absence of four primitives.**

---

## 3. Push order (cheapest-first, each step independently valuable)

| Order | Step | Value at Stage A | Value at Stage B | Value at Stage C |
|---|---|---|---|---|
| 1 | Atomic claim + lease for jobs | fixes duplicate execution (C-02) | enables safe replicas (B-03) | foundation for the worker pool (C-01) |
| 2 | `update_id`/receipt ledger at ingress | closes loss+duplication (X-01, B-02) | closes "update theft" (E-21) | prerequisite for any multi-ingress deploy |
| 3 | Outbox for notifications | closes "DB says done, user got nothing" (C-07) | keeps promises during restarts | prerequisite for role split (C-03) |
| 4 | Shared limiter + externalised provider health | predictable abuse/cost posture | makes replicas behaviourally equivalent | per-role cost control |
| 5 | Durable scheduler rows | scheduled work survives restarts (X-04) | stops double-firing (B-06) | enables a scheduler role (C-09) |
| 6 | Advisory lock for migrations; required external DB in multi-instance mode | safer single-host deps | prevents the two-host migration race (B-07) and SQLite forks (B-09) | standard practice |
| 7 | Role profiles + role-labelled metrics | small clarity win | prepares the split | makes C operable |
| 8 | Durable queue backend (PG first, broker later) | survives instance replacement (A-02) | shared queue across replicas | the actual C boundary |

**Note on ordering:** 1–5 are **single-process improvements that happen to enable B/C**. None of them require a new service, a broker, or a platform change; each is additive to the schema and behaviour-preserving at Stage A. That is the strongest statement this map can make: *the growth path and the correctness path are the same path.*

---

## 4. Backward-compatibility matrix (for the additive steps above)

| New state | Old code's reaction | Verdict |
|---|---|---|
| `owner`, `lease_until` columns on the queue | ignores them; its `UPDATE … WHERE status IN ('pending','processing')` still works | safe during rollout |
| Rows with `processing` + **expired** lease | old code may claim them (it already does) | acceptable during rollout; new code's claim is strict |
| `updates` dedupe table | ignores it; may still process a duplicate | duplicates possible only for the overlap window — strictly better than today |
| Outbox rows | ignores them; notification still attempted inline | safe; outbox drainer must tolerate rows already notified (idempotent notify by `(chat_id, key)`) |
| Scheduler table (`next_fire_at`) | ignores it; old in-memory timers keep working | double-fire possible if **both** mechanisms are live — so the migration must switch features, not duplicate them (contract step) |
| New settings profile validation | unaffected (unknown env vars are ignored by pydantic settings unless prefixes are strict) | verify `env_prefix`/`extra` behaviour before shipping |
| PG-backed queue implementation | call sites are behind `JobQueuePort`; old instances keep using their file | **divergent queues** if both implementations run simultaneously ⇒ the switch must be a single deploy with the external DB required (fail-closed) |

---

## 5. FACTS vs ANALYSIS

**FACTS.** The service is a single container per `koyeb.yaml` with no volume (`R-20`); the webhook path serves PTB and the API in one process; limiter, cooldowns, timers and job claims are all in-process; `migration_lock` is host-local; the dependency-port layer and handler registry already exist for LLM, queue, storage and checkpoints. Polling mode exists as a supported alternative and is vulnerable to multi-poller update theft (mechanism verified in-code; independently corroborated by `E-21`).

**ANALYSIS.**
1. **Stage B is reachable by accident and is the highest-risk state**, because a platform action (a second instance, a rolling deploy) creates it while the code remains Stage A. The honest mitigation is to make multi-instance a *mode* with a fail-closed configuration check, not a hope.
2. **The map is not a demand for microservices.** Stage C is only interesting at LARGE (Phase 8: ~1.5 M requests/month) and even then the smallest sufficient move is a PG-backed queue plus a second worker role — not a broker, not a service mesh. The rejected Celery/Redis path (`DECISION_LOG.md` r7) stays rejected; nothing found here requires it at the next order of magnitude.
3. **The evolution bottleneck is four primitives, not one architecture:** a claim/lease, an ingress receipt ledger, an outbox, and role-scoped config/metrics. Every break in §1 and §2 is a special case of one of them.
4. **Backward compatibility is mostly free** because the queue and ports are already abstractions — with two exceptions that must be *cut over*, not run in parallel: the queue backend (divergent queues) and the scheduler (double-fire while both mechanisms live).
