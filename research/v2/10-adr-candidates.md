# PHASE 10 — ADR CANDIDATES (candidates only — no decision is made here)

**Rule:** this file produces **decision questions with evidence requirements**, never finalised architecture decisions. An ADR candidate becomes a real ADR only when (a) the *trigger* below has occurred or been scheduled, and (b) the *evidence needed* exists on the record. Until then it is a hypothesis about a decision, and it is labelled **CANDIDATE**.
**Count:** **28 candidates** (`ADR-C-01` … `ADR-C-28`), each with Decision question / Current state / Options / Evidence needed / Trigger / Risk (of not deciding, and of deciding badly).

---

## Cluster 1 — Job execution and durability

### ADR-C-01 — Queue durability contract with the platform
- **Decision question:** Is the job queue allowed to live on the instance's ephemeral disk, or must it be on external durable storage in every non-dev deployment?
- **Current state:** `InProcessJobQueue` writing a SQLite sidecar whose path defaults to the local disk; `koyeb.yaml` declares **no volume** and **no `NEXUS_DATABASE_URL`** (`R-01`, `R-20`, A-02).
- **Options:** (a) external DB required (fail-closed when unset in prod); (b) platform volume attached to the service; (c) accept loss and document it as a product limitation.
- **Evidence needed:** measured job loss rate across deploys/scale-to-zero; the actual console configuration (`U-c`); a written product stance on "queued must mean queued".
- **Trigger:** the first confirmed user-visible lost job — which the current telemetry cannot detect, hence the trigger is also "when job metrics exist".
- **Risk of not deciding:** the queue's central promise is false in the documented topology (F-04, C-04). **Risk of deciding badly:** requiring a DB for local dev raises friction; the option must be *mode-aware*, not absolute.

### ADR-C-02 — Atomic claim and lease primitive
- **Decision question:** what is the one primitive that guarantees at most one executor per job at a time?
- **Current state:** `UPDATE … WHERE status IN ('pending','processing')` with no affected-row check; duplicate protection only in an in-process dict; `resume_pending()` resets `processing → pending` globally at start-up (`R-01`, A-04/A-05).
- **Options:** (a) conditional UPDATE + `rowcount==1` + `owner`/`lease_until` columns; (b) `SELECT … FOR UPDATE SKIP LOCKED` on PG; (c) advisory lock per job id; (d) broker visibility timeout (future).
- **Evidence needed:** a concurrency test that fails today (two claims, one row); lease TTL sized against the 900 s render timeout (`R-15`).
- **Trigger:** any evidence of duplicate execution, or the first decision to run a second process.
- **Risk of not deciding:** duplication is silent and unbounded (F-01/F-02). **Risk of deciding badly:** a lease TTL shorter than the longest handler re-introduces duplicates; needs a heartbeat or a TTL ≥ max handler time.

### ADR-C-03 — Queue backend beyond SQLite
- **Decision question:** at what stage does the queue need a purpose-built backend, and which?
- **Current state:** `JobQueuePort` with exactly one implementation (`R-01`, `R-02`); Celery/Redis explicitly rejected earlier.
- **Options:** (a) keep SQLite + claim/lease (single host); (b) PG-backed queue with `SKIP LOCKED`; (c) managed broker (SQS/Cloud Tasks-class) behind the same port; (d) Celery/Redis (previously rejected — would need new evidence).
- **Evidence needed:** throughput/latency targets at LARGE (Phase 8), the operational cost of a second managed service, and whether jobs will ever need to run off-platform.
- **Trigger:** >1 executing process or >~10 k jobs/day sustained `[A]`.
- **Risk of not deciding:** the port silently becomes a lie ("pluggable" but unplugged). **Risk of deciding badly:** premature broker adoption = new failure domain + a new bill for a workload PG can serve.

### ADR-C-04 — Payload versioning and worker/feature deploy order
- **Decision question:** how do payloads and workers stay compatible across deploys?
- **Current state:** JSON payloads in a local table; unknown handlers park (good); no version field (`R-01`).
- **Options:** (a) `{"v": n}` + "workers before producers" deploy rule; (b) dual-write old and new shape for one release; (c) feature-flag per job type.
- **Evidence needed:** the actual handler registry surface and a rollout timeline; a test that an old worker parks rather than crashes on a v+1 payload.
- **Trigger:** the first time a payload schema changes while jobs are in flight.
- **Risk of not deciding:** deploy-time job loss or crash-loops. **Risk of deciding badly:** parking unknown jobs forever without an alert (silent backlog).

---

## Cluster 2 — Ingress, notifications, scheduling

### ADR-C-05 — Ingress receipt ledger and acknowledgement timing
- **Decision question:** does the webhook acknowledge Telegram before or after durable receipt?
- **Current state:** 200 returned after handing the update to PTB's **in-memory** queue; no `update_id` store; Telegram re-delivers only when the response is not 2xx (`R-04`, `X-01`, `E-20`).
- **Options:** (a) insert-then-ack with `UNIQUE(update_id)`; (b) ack-then-persist asynchronously to an outbox; (c) single-instance guard + keep current behaviour (rejects scale-out).
- **Evidence needed:** measured webhook response budget (Telegram's window, `E-09`), DB write latency at p99 with Neon cold starts.
- **Trigger:** any lost/duplicate update report, or the first multi-instance deploy.
- **Risk of not deciding:** both loss and duplication remain possible (X-01). **Risk of deciding badly:** a slow DB turns a 200-early design into Telegram retries; the ack budget must be measured, not assumed.

### ADR-C-06 — Notification outbox
- **Decision question:** how is "the database says done, the user was never told" prevented?
- **Current state:** completion notification runs after the terminal write and every failure is swallowed by design (`R-01`, C-07).
- **Options:** (a) outbox row in the same transaction + drainer with retry; (b) retry loop inside the notifier (in-process, lost on restart); (c) accept and surface a "delivery unconfirmed" state.
- **Evidence needed:** the share of notifications that fail today (unmeasured — needs the counter that does not exist).
- **Trigger:** the first user report of a "completed" job with no output.
- **Risk of not deciding:** the system's most common user-visible lie persists. **Risk of deciding badly:** at-least-once notification ⇒ duplicate messages unless the notifier is idempotent by `(chat_id, key)` — which Telegram does not provide, so the key must be local.

### ADR-C-07 — Durable scheduler ownership
- **Decision question:** who owns time-based work (scheduled posts, reminders, digests) and where is the fire state stored?
- **Current state:** `asyncio` task maps inside feature modules; DB rows for the *intent* only (`R-08`, X-04).
- **Options:** (a) DB rows + lease claim per fire; (b) keep in-process and declare restart loss acceptable; (c) OS cron/GitHub Actions triggers that call a CLI command.
- **Evidence needed:** user impact of a missed reminder/post (unknown — `U-a`).
- **Trigger:** any report of a missed or duplicated scheduled item.
- **Risk of not deciding:** silent loss + state lie (C-08). **Risk of deciding badly:** double-firing during the migration if both mechanisms run (Phase 9 §4).

---

## Cluster 3 — Quotas, abuse and cost

### ADR-C-08 — Location of rate-limit state
- **Decision question:** is the per-user rate limit a process-local convenience or a system invariant?
- **Current state:** in-memory window, defaults 10 msg/60 s (`R-13`); resets on every restart/scale-to-zero.
- **Options:** (a) DB table with windowed count; (b) keep local and treat it as a soft guard; (c) reverse-proxy/edge rate limiting in front of the app.
- **Evidence needed:** abuse attempts per day (currently unmeasured), and the cost of a DB query per message.
- **Trigger:** >1 replica, or the first abuse incident.
- **Risk of not deciding:** protection vanishes exactly when load is highest (S-07). **Risk of deciding badly:** a DB round-trip per message adds the DB to the critical path of every reply.

### ADR-C-09 — Provider health and quota state
- **Decision question:** where do "this provider is drained" and "this provider is cooling down" live?
- **Current state:** litellm cooldown in process memory, `allowed_fails=1`, 86 400 s, `num_retries=0` (`R-12`, A-31).
- **Options:** (a) externalised health table consulted pre-dispatch; (b) per-process state + alert on 429 storms; (c) provider-agnostic circuit breaker at the port.
- **Evidence needed:** real quota behaviour per provider (`U-e`, `E-06`/`E-07` conflicts) and the 429/`FakeLLM` rate.
- **Trigger:** the first day where every request in a window returns fallback/fake output.
- **Risk of not deciding:** cold starts re-probe drained providers (S-24) and degrade silently. **Risk of deciding badly:** a wrong health table is worse than none — it needs verification/TTL semantics.

### ADR-C-10 — LLM chain composition and privacy semantics
- **Decision question:** which providers may see user content, and what does `llm_strict_privacy` actually guarantee?
- **Current state:** chain Groq → Gemini(free) → OpenRouter`:free`; `llm_strict_privacy` removes **only** OpenRouter `:free` (`R-12` `[V]`); free tiers may train on data (`E-07`, `[EXTERNAL-INDEPENDENT]`).
- **Options:** (a) redefine strict privacy to exclude *all* non-paid/non-training endpoints; (b) document exactly which legs remain; (c) a per-user privacy mode.
- **Evidence needed:** vendor data-use terms for each leg (`[NEED-PRIMARY]`).
- **Trigger:** the first privacy-sensitive user, or any external review.
- **Risk of not deciding:** a documented guarantee that the code does not enforce. **Risk of deciding badly:** removing the free legs drops capacity to ~500–1,000 req/day (Phase 8 cliff).

### ADR-C-11 — Model lifecycle policy
- **Decision question:** how does the system learn that a configured model has been retired or repriced?
- **Current state:** a single pinned model id (`gemini-2.0-flash` verified in settings); conflicting external evidence about its lifecycle (`E-17` `[C]`, `[NEED-PRIMARY]`); no health probe for "model exists".
- **Options:** (a) a start-up capability probe per configured model with a loud log/metric; (b) periodic synthetic call in a canary workflow; (c) manual review cadence documented in the runbook.
- **Evidence needed:** the vendor's actual decommission schedule and current model list.
- **Trigger:** immediate — the ambiguity itself is the trigger, because today the failure surfaces only as unexplained quota exhaustion.
- **Risk of not deciding:** a chain leg dies silently and the cost/capacity model (Phase 8) is wrong. **Risk of deciding badly:** probing costs quota — must be tiny and cached.

### ADR-C-12 — Cost accounting ledger
- **Decision question:** is there a per-request record of provider, model, tokens and outcome?
- **Current state:** none; `LLMPort.complete(idempotency_key=…)` exists but is unread by any implementation (`M-07`, `U-d`).
- **Options:** (a) ledger table written around each dispatch; (b) provider-side dashboards only; (c) accept unknowable cost.
- **Evidence needed:** the approximate token model (Phase 8 §1) and the decision-relevant granularity (per user/day vs per request).
- **Trigger:** the first paid provider, or the first budget question that cannot be answered.
- **Risk of not deciding:** duplicates, retry storms, and abuse are all unpriceable (Phase 8 §5). **Risk of deciding badly:** a ledger on the hot path adds latency and rows — write asynchronously with a bounded queue.

---

## Cluster 4 — Data, backup and retention

### ADR-C-13 — Data-plane unification (two persistence planes)
- **Decision question:** may feature modules keep opening their own SQLite engines, or must all writes go through the central storage layer?
- **Current state:** 14 `features/*` modules own sync SQLite engines on `settings.db_path` (`M-05`, R-08), while `storage/db.py` selects PG when `NEXUS_DATABASE_URL` is set (`R-06`); raw-DDL tables (`referral`, `referralcode`, `conversation_history`) have no Alembic revision; `tests/architecture/legacy_baseline.json` grandfathers 37 such boundary violations so CI accepts them.
- **Options:** (a) migrate modules to `storage/db.py` sessions behind the same facades; (b) declare SQLite the only app store and drop PG; (c) dual-write with reconciliation.
- **Evidence needed:** which tables are written by which plane in production (`U-c`), and the cost of touching a grandfather list that is explicitly tracked.
- **Trigger:** the first "data vanished after deploy" report, or the first PG-only feature.
- **Risk of not deciding:** split-brain data planes produce read-after-write violations that look like product bugs (C-16, F-09/F-10). **Risk of deciding badly:** touching 14 modules is the largest refactor on this list — it must be sequenced, not attempted as a cleanup.

### ADR-C-14 — Schema-change compatibility and migration ownership
- **Decision question:** what exactly must be true for old code to run against a new schema, and who enforces it?
- **Current state:** an intent in the runbook ("keep it backward-compatible", `R-23`), idempotent migrations with a CI stamp assertion (`R-19`), a host-local `flock` (`R-07`), and **no** CI test of old code × new schema (`A-21`).
- **Options:** (a) expand/contract rules + a CI matrix job running the previous release against the migrated DB; (b) single-instance migration windows (downtime accepted); (c) per-release migration review checklist.
- **Evidence needed:** deploy overlap duration on Koyeb, and whether rollback keeps the old DB (it does — that is the point).
- **Trigger:** the first non-additive migration (rename/drop/narrow).
- **Risk of not deciding:** a class of outages that only appear during deploys (C-12, F-40). **Risk of deciding badly:** over-engineering the process for a solo maintainer; the rule should be the minimum that prevents the outage class (no rename/drop in the same release as the code change).

### ADR-C-15 — Restore path and RTO/RPO targets
- **Decision question:** what are the stated RTO and RPO, and what is the *tested* restore procedure?
- **Current state:** backups exist (`pg_dump`/SQLite online backup → R2, 2×/day); **no restore tool, no drill, no RTO/RPO anywhere** (Phase 7 §2, `A-14`).
- **Options:** (a) add `nexus maintenance restore --from <dump> --target <scratch>` + a scheduled verification job; (b) rely on provider PITR (Neon) and write that down as the plan; (c) do nothing and accept total-loss for local stores.
- **Evidence needed:** the target the *owner* accepts (business decision, not an engineering inference); one successful restore exercise.
- **Trigger:** before any real user data accumulates — i.e., now.
- **Risk of not deciding:** an untested backup is a hope; the first real incident becomes improvisation (DR-01/DR-11). **Risk of deciding badly:** declaring an RPO the mechanism cannot meet (e.g. 1 h when the cadence is 12 h).

### ADR-C-16 — Retention and deletion semantics
- **Decision question:** what is deleted when a user asks (or is forgotten), across messages, memories, checkpoints, renders, backups and logs?
- **Current state:** checkpoints carry a 30-day resume policy with keyed deletion (`R-21`); housekeeping prunes temp >48 h and backups >30 d (`R-17`); no unified deletion path for the app DB, R2 renders, or logs; Telegram-side copies are outside the system's control.
- **Options:** (a) a single `nexus data delete --user/--thread` with a per-store checklist and a receipt; (b) document manual per-store procedures; (c) a policy statement that deletion is best-effort.
- **Evidence needed:** an inventory of stores holding user-linked data (29 model classes + raw-DDL tables + queues + blobs) and the legal/business requirement.
- **Trigger:** the first deletion request (or the first privacy question).
- **Risk of not deciding:** the answer to a deletion request is "we looked in three places and hoped". **Risk of deciding badly:** deleting checkpoints without the required key/lineage can strand threads (the port fails closed — respect it).

### ADR-C-17 — Blob key scheme and idempotent publication
- **Decision question:** does the object store honour an idempotency key, and what is the key scheme?
- **Current state:** `ObjectStoragePort.put(key, content, idempotency_key)` declared; `R2Provider.upload(local_path, remote_key)` implemented without it (`M-08`, X-09); backups use timestamp keys ⇒ duplicates on ambiguous retries (C-14).
- **Options:** (a) implement the port as declared (deterministic keys per operation, e.g. `backups/db/<stamp>` → content-hash suffix); (b) remove the key from the port and document at-least-once blob writes; (c) both (deterministic keys for user-visible artefacts, at-least-once for backups).
- **Evidence needed:** who consumes `put` vs `upload`, and whether any caller depends on overwrite semantics.
- **Trigger:** the first cost/duplication audit of the bucket (`U-f`).
- **Risk of not deciding:** the interface documents a guarantee the implementation lacks — a trap for the next contributor (contradiction K-07).

---

## Cluster 5 — Security posture

### ADR-C-18 — HTTP surface: default-deny
- **Decision question:** which routes are public, which require a token, and what is the default when a token is unconfigured?
- **Current state:** the dashboard gate is a no-op when `NEXUS_DASHBOARD_TOKEN` is unset, and webhook mode serves the same app that includes the dashboard router; `GET /creative/jobs/{id}` has no auth; `koyeb.yaml` sets no dashboard token (`R-04`, `R-05`, `R-20`).
- **Options:** (a) default-deny: refuse to start (or refuse those routes) without a token in prod; (b) mount the dashboard only when the token exists; (c) put everything behind the platform's edge auth.
- **Evidence needed:** the console's actual env (does it set the token?), and a route inventory with auth classification.
- **Trigger:** immediate — reachability is enough.
- **Risk of not deciding:** user PII exposed at a guessable path (S-01/S-02). **Risk of deciding badly:** breaking the operator's own dashboard by failing closed without a documented escape hatch.

### ADR-C-19 — Single egress chokepoint
- **Decision question:** must every outbound HTTP fetch go through the validating transport?
- **Current state:** a tested SSRF guard exists (`R-14`) and a later path bypasses it entirely (`api/app.py::_download_video_to_temp`, `R-04`).
- **Options:** (a) enforce by construction (only one client factory is exported; lint/grep gate in CI); (b) code review discipline; (c) per-route guards.
- **Evidence needed:** a full inventory of `httpx`/`requests` client construction sites (count and owners).
- **Trigger:** immediate (a reachable bypass exists).
- **Risk of not deciding:** SSRF class reopens on every new feature (S-04). **Risk of deciding badly:** a chokepoint that is too rigid blocks legitimate redirect-heavy providers — so the factory needs an explicit allow-list parameter, not a bypass.

### ADR-C-20 — Secrets: scopes, rotation, and key separation
- **Decision question:** which process holds which secret, and how is rotation practised?
- **Current state:** one HMAC key signs creative requests and is reused as a "signing secret" elsewhere (`R-16`); webhook/dashboard/Telegram/DB/R2 secrets are flat env vars; rotation is a runbook line (`R-23`), never exercised.
- **Options:** (a) per-role, per-purpose secrets with a rotation drill; (b) platform secret manager with references; (c) status quo + documented rotation cadence.
- **Evidence needed:** the full secret inventory and rotation cost per secret (how many places must change).
- **Trigger:** the first incident, or the first third-party reviewer.
- **Risk of not deciding:** one leak widens to several systems (S-22). **Risk of deciding badly:** rotation that breaks the deploy pipeline is worse than none — the drill is the deliverable.

### ADR-C-21 — Supply chain: pinning and provenance
- **Decision question:** is the image reproducible and its contents known?
- **Current state:** no lock file, several un-pinned base deps, C++ compilation at build time (`S-26`, `R-20`).
- **Options:** (a) hash-pinned lock + wheel-only build; (b) keep ranges, add an SBOM + provenance attestation; (c) both, staged.
- **Evidence needed:** build time budget and whether wheels exist for the required platforms (the native builds are there because wheels were insufficient).
- **Trigger:** the first dependency-related incident or compliance question.
- **Risk of not deciding:** non-reproducible builds and unbounded upstream trust (S-26, DR-13).

### ADR-C-22 — Malicious/poisoned external content policy (LLM01/LLM02)
- **Decision question:** what may an LLM-driven flow do with content fetched from the internet?
- **Current state:** URL summarisation and RAG ingestion place third-party text into prompts (`R-14`); the SSRF guard allows **any public host**; no output gate (`S-10`, `S-11`).
- **Options:** (a) egress allow-list + content delimiting + tool-invocation only from trusted input; (b) no-fetch mode for untrusted sources; (c) accept with detection only.
- **Evidence needed:** which features actually ingest untrusted text, and what tools an injected instruction could reach.
- **Trigger:** the first feature that lets the model act (call a tool, send a message) on fetched content.
- **Risk of not deciding:** prompt-injection becomes data exfiltration (S-11). **Risk of deciding badly:** over-restricting makes summarisation useless; OWASP is explicit that there is no complete LLM01 fix (`E-10`).

---

## Cluster 6 — Operations and scale

### ADR-C-23 — Observability sink and alerting
- **Decision question:** where do metrics/logs go, and who is woken up?
- **Current state:** Prometheus-style metrics and structured redacted logs exist in-process (`R-21`); no sink, no alert rules, no unattended alerting; `/healthz` cannot fail for DB outages.
- **Options:** (a) Grafana Cloud free tier + a few alert rules (F-19-style staleness, backup missing, 429 storms); (b) platform-native checks + an external uptime probe; (c) status quo.
- **Evidence needed:** metric cardinality estimate (to stay under 10 k series, `E-18`) and the failure classes that must page someone (Phase 3 detection-gap table).
- **Trigger:** immediate (nothing detects the highest-severity failures today).
- **Risk of not deciding:** every incident is discovered by a user (F-19, DR-11). **Risk of deciding badly:** alert fatigue from a noisy single-owner setup.

### ADR-C-24 — Liveness vs readiness contract
- **Decision question:** should the health endpoint reveal dependency state?
- **Current state:** `/healthz` is deliberately DB-free (fast, platform-friendly) — good for liveness, blind for readiness.
- **Options:** (a) keep `/healthz` + add `/readyz` (DB, queue file writability, provider config sanity); (b) fold dependencies into `/healthz`; (c) status quo.
- **Evidence needed:** what the platform does with a failing health check (restart storms risk).
- **Trigger:** the first "green health, broken service" incident — which is the current default.

### ADR-C-25 — Multi-instance mode as a fail-closed configuration
- **Decision question:** what must be true before a second instance may exist?
- **Current state:** nothing prevents it; limiter/cooldowns/timers/claims are per-process (`R-13`, `R-12`, `R-08`, `R-01`).
- **Options:** (a) a `NEXUS_INSTANCE_MODE=multi` flag that requires external DB + claim primitive + ingress dedupe and refuses to boot otherwise; (b) documentation only; (c) platform guard (max instances = 1).
- **Evidence needed:** the Koyeb plan's replica controls and the deploy-overlap behaviour.
- **Trigger:** the first scale-out attempt or blue/green rollout.
- **Risk of not deciding:** Stage B entered by accident (Phase 9 §5). **Risk of deciding badly:** blocking legitimate zero-downtime deploys — so the flag should *require primitives*, not forbid replicas.

### ADR-C-26 — Render isolation and instance sizing
- **Decision question:** do CPU-bound render jobs share the conversation process, and what is the concurrency cap?
- **Current state:** one process, unbounded job concurrency, 900 s FFmpeg timeout, 0.1 vCPU free instance (`R-01`, `R-15`, `E-02`).
- **Options:** (a) global semaphore + per-chat cap (small change); (b) separate worker role pinned to a CPU-richer instance (Stage C piece); (c) queue-side admission control by cost class.
- **Evidence needed:** measured wall-clock per render on the free instance (a single benchmark settles it).
- **Trigger:** the first OOM or the first render that hits 900 s.
- **Risk of not deciding:** one render can take down conversations (S-07). **Risk of deciding badly:** a cap that is too low makes bulk usage impossible — the cap must be configurable per mode.

### ADR-C-27 — Budgets and circuit-breakers
- **Decision question:** what stops the system from spending/consuming beyond a daily budget?
- **Current state:** quotas are the only brake; provider cooldowns reset on restart; no per-user or global budget exists (`A-31`, S-24).
- **Options:** (a) a hard daily request/token budget per user and globally, enforced at the port; (b) provider-side spend caps only; (c) status quo + alerts.
- **Evidence needed:** the acceptable daily worst case (business input), and the ledger from ADR-C-12.
- **Trigger:** the switch to any paid leg, or the first quota-exhaustion day.
- **Risk of not deciding:** a single loop can drain the day's capacity for everyone (S-09).

### ADR-C-28 — Documentation authority and drift control
- **Decision question:** what is the source of truth when docs and code disagree, and how is drift detected?
- **Current state:** `docs/architecture/*` is "Verified against main @ 7573249" while HEAD is 350 commits later (`M-06`); the docs-integrity test gates structure (links, mermaid, ADR index), **not accuracy** (`tests/.../test_docs_integrity.py`); this review re-measured 8 values that differ from docs (`M-01`…`M-08`).
- **Options:** (a) architecture docs declare scope + are re-verified per release with a checklist; (b) auto-generate inventory sections (module list, env var list, table list) from code; (c) accept drift and mark docs as historical.
- **Evidence needed:** which doc statements are load-bearing for operators (the runbooks) vs descriptive (the overview).
- **Trigger:** the first time an operator follows a stale doc during an incident.
- **Risk of not deciding:** every future audit re-derives the same eight numbers (this one did). **Risk of deciding badly:** auto-generation that produces documentation nobody reads.

---

## Summary index

| ADR-C | Cluster | Depends on | Earliest sensible trigger |
|---|---|---|---|
| 01 queue durability | Execution | — | now (documented topology contradicts the promise) |
| 02 claim/lease | Execution | — | now |
| 03 queue backend | Execution | 01, 02 | >1 process or >10 k jobs/day |
| 04 payload versioning | Execution | — | first payload change |
| 05 ingress receipt | Ingress | — | now (loss+dup possible) |
| 06 notification outbox | Ingress | — | first "done but nothing arrived" |
| 07 durable scheduler | Scheduling | — | first missed/duplicated scheduled item |
| 08 limiter state | Abuse | — | first abuse or second replica |
| 09 provider health | Quotas | 12 (ledger) | first all-fallback window |
| 10 privacy semantics | Quotas | 09 | before the first privacy-sensitive user |
| 11 model lifecycle | Quotas | — | now (lifecycle `[C]`) |
| 12 cost ledger | Quotas | — | first paid leg |
| 13 data-plane unification | Data | 01/02 sequencing | first vanished-data report |
| 14 schema compatibility | Data | — | first non-additive migration |
| 15 restore + RTO/RPO | Data | — | now |
| 16 retention/deletion | Data | 13 (inventory) | first deletion request |
| 17 blob idempotency | Data | — | first bucket audit |
| 18 HTTP default-deny | Security | — | now (public reachability) |
| 19 egress chokepoint | Security | — | now (bypass exists) |
| 20 secrets/rotation | Security | — | first incident or review |
| 21 supply chain | Security | — | first compliance/dependency question |
| 22 untrusted content | Security | 19 | first tool-using flow on fetched content |
| 23 observability sink | Ops | — | now (no detection) |
| 24 liveness/readiness | Ops | 23 | first green-health outage |
| 25 multi-instance mode | Ops | 01,02,05,08 | first scale-out/deploy overlap |
| 26 render isolation | Ops | 02 | first OOM or 900 s render |
| 27 budgets | Ops | 12 | first paid leg or quota-exhaustion day |
| 28 documentation authority | Ops | — | first stale-doc incident |

**Explicit non-decision:** this phase does **not** recommend an order of adoption as a plan of record, does not select options, and creates no ADR files. The table above exists so that when a trigger fires, the question, the options, and the missing evidence are already on the record.
