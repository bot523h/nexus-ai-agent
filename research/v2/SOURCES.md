# RESEARCH V2 — SOURCE REGISTRY

**Namespace:** `research/v2/` (isolated; nothing outside this directory was modified)
**Purpose:** every claim marked `[V]`/`[A]`/`[U]`/`[C]` in v2 must resolve to an entry here.
**Snapshot date:** 2026-09-23 (UTC) · **Repo evidence pinned to:** `main` @ `a997aab` (v3.13.0 line, 3.13.0 in `VERSION`)

---

## 0. Evidence classes (the rule this whole lab obeys)

| Class | Meaning | How it was obtained | Allowed use |
|---|---|---|---|
| `REPO-CODE` | Primary source: the implementation itself | `git show`/`sed` on the pinned commit; commands are quoted in each document | Can establish *what the code does today* |
| `REPO-DOC` | Primary source: in-repo authored claims (architecture docs, decision log, runbooks, audits) | file read | Can establish *what the team claims/believes*; never sufficient alone for a behavioural claim |
| `REPO-TEST` | Primary source: executable gate | file read / test inventory | Establishes what is mechanically enforced |
| `REMEASURED` | Primary source: a measurement performed in this lab | command + observed output (quoted) | Overrides stale `REPO-DOC` numbers |
| `EXTERNAL-PRIMARY` | Vendor/foundation publication (pricing page, official docs, standard) | web fetch/search, date recorded | Basis for cost/limit claims |
| `EXTERNAL-INDEPENDENT` | Third-party reporting/analysis of an external system | web search, date recorded | Corroboration only; if it disagrees with another independent source ⇒ `[C]` |
| `EXTERNAL-SECONDARY` | Aggregator/blog pricing or limit summaries | web search | Marked `[NEED-PRIMARY]` when used |

**Fact vs analysis separation:** every document has a `FACTS` section (verifiable statements, with source id) and an `ANALYSIS` section (this lab's reasoning, explicitly labelled). No number appears in an analysis section without its provenance class.

**Absolute-isolation attestation (lab self-check):**

```
git status --porcelain            →  (clean apart from research/v2 additions)
git diff --stat -- src bot features api  →  empty
touched paths                     →  research/v2/** only   (no board, no migration, no dependency, no handlers.py, no Nagar, no Task 159/160/161, no PR47/PR33 file)
```

---

## 1. Repository-internal sources (primary)

| ID | Source | Class | Pin | Used for |
|---|---|---|---|---|
| `R-01` | `src/nexus_ai_agent/adapters/in_process_job_queue.py` | REPO-CODE | a997aab | queue semantics, idempotency key, resume, claim, terminal states |
| `R-02` | `src/nexus_ai_agent/worker.py` | REPO-CODE | a997aab | `job_queue_db_path()`, handler registry, PDF/story/slideshow jobs |
| `R-03` | `src/nexus_ai_agent/bot/app.py` (`_init_v2_engines`, webhook adapter) | REPO-CODE | a997aab | queue wiring from `settings.db_path`; notifier hook |
| `R-04` | `src/nexus_ai_agent/api/app.py` | REPO-CODE | a997aab | `/healthz`, `/webhook/telegram`, `/creative/video-edit` HMAC, `_download_video_to_temp`, `BackgroundTasks` job |
| `R-05` | `src/nexus_ai_agent/api/dashboard.py` | REPO-CODE | a997aab | optional bearer gate; PII-free payloads |
| `R-06` | `src/nexus_ai_agent/storage/db.py` | REPO-CODE | a997aab | dual-backend selection, `create_all_tables` + WAL, per-URL PG engine cache, `_pg_prepared_urls` |
| `R-07` | `src/nexus_ai_agent/storage/migrations.py` | REPO-CODE | a997aab | `migration_lock` = `fcntl.flock` on a **host-local** temp file |
| `R-08` | `src/nexus_ai_agent/features/*.py` (14 modules) | REPO-CODE | a997aab | per-feature **sync** SQLite engines on `settings.db_path` (split-brain risk) |
| `R-09` | `src/nexus_ai_agent/storage/models.py` (29 classes) + `features/referral.py` raw DDL | REPO-CODE | a997aab | table ownership, duplicate `referral`/`referralcode` creators |
| `R-10` | `src/nexus_ai_agent/application/ports/object_storage.py` vs `storage/providers/r2.py` | REPO-CODE | a997aab | port/adapter signature mismatch (`put`/`content: bytes` vs `upload`/`local_path`) |
| `R-11` | `src/nexus_ai_agent/application/ports/llm.py` + `grep idempotency src/nexus_ai_agent/llm/ orchestration/` → empty | REPO-CODE | a997aab | `idempotency_key` on the LLM port is a signature with **no** replay mechanism |
| `R-12` | `src/nexus_ai_agent/llm/litellm_provider.py` | REPO-CODE | a997aab | fallback chain, `num_retries=0`, in-process cooldowns (`86400 s`), `simple-shuffle` |
| `R-13` | `src/nexus_ai_agent/bot/rate_limiter.py`, `bot/access_guard.py` | REPO-CODE | a997aab | in-process (per-replica, non-durable) rate limiting; deny-by-default gate |
| `R-14` | `src/nexus_ai_agent/core/ssrf_guard.py`, `core/http_client.py`, `features/summarizer.py` | REPO-CODE | a997aab | the *guarded* egress path (DNS rebinding closed, https-only) |
| `R-15` | `src/nexus_ai_agent/creative/rendering/executor.py` | REPO-CODE | a997aab | one process, no shell, 900 s/600 s timeouts, `.part` + atomic rename, sha256 + probe |
| `R-16` | `src/nexus_ai_agent/creative/ffmpeg_executor.py`, `creative/job_registry.py` | REPO-CODE | a997aab | second (API) job subsystem: `creative_jobs` SQLite, `BackgroundTasks`, 300 s ffmpeg timeout |
| `R-17` | `src/nexus_ai_agent/maintenance/backup.py`, `maintenance/housekeeping.py` | REPO-CODE | a997aab | pg_dump/SQLite online-backup → R2; 30-day backup retention; 48 h temp sweep |
| `R-18` | `src/nexus_ai_agent/config/settings.py` (74 declared fields), `.env.example` (47 documented env vars) | REPO-CODE | a997aab | configuration surface; count reconciliation |
| `R-19` | `.github/workflows/ci.yml`, `maintenance.yml` | REPO-CODE | a997aab | gates; nightly R2 backup (03:17 UTC) on GitHub runners |
| `R-20` | `koyeb.yaml`, `Dockerfile`, `docker-compose.yml`, `pyproject.toml` | REPO-CODE | a997aab | deploy shape; base dependencies incl. `sentence-transformers`, `chromadb`, `llama-cpp-python`, `litellm`, `boto3` |
| `R-21` | `docs/architecture/{OVERVIEW,RUNTIME_FLOWS,DATA_AND_STORAGE,PORTS,SECURITY,OBSERVABILITY,LLM_PROVIDERS,MODULE_MAP,REFERENCES}.md` | REPO-DOC | a997aab | the architectural claims under audit |
| `R-22` | `docs/DECISION_LOG.md` (revision 7), `docs/architecture/adr/0001-0004` | REPO-DOC | a997aab | accepted/rejected decisions (modular monolith, no Redis/Celery, Neon over sidecars) |
| `R-23` | `docs/ops/{DEPLOY_RUNBOOK,deployment-koyeb,NEON_LIFECYCLE_RUNBOOK,STORAGE_PACK_RUNTIME}.md` | REPO-DOC | a997aab | runbooks, rollback, rotation; **no RTO/RPO numbers anywhere** |
| `R-24` | `docs/audits/AUDIT_REPORT_2026-09-21.md` (P0-1…P0-10), `docs/audits/DEAD_ENGINES_2026-09-22.md`, `docs/audits/PR32_TRIAGE_2026-09-21.md` | REPO-DOC | a997aab | prior audit findings used as Research-v1 input |
| `R-25` | `tests/architecture/**` (19 files), `tests/integration/test_in_process_job_queue.py`, `tests/unit/test_webhook_mode.py`, `tests/unit/test_access_gate.py` | REPO-TEST | a997aab | what is *mechanically* enforced vs merely documented |
| `R-26` | `scripts/deploy_smoke.py` | REPO-CODE | a997aab | what "deploy validation" actually proves |

## 2. Re-measurements performed in this lab (override stale docs)

| ID | Command | Observed (2026-09-23) | Doc claim it overlaps |
|---|---|---|---|
| `M-01` | `find src -name "*.py" \| wc -l` ; `find src -name "*.py" -exec cat {} + \| wc -l` | **231 files / 40,815 lines** | `OVERVIEW.md` §8: 215 files / 35,431 lines (pin `7573249`) |
| `M-02` | `find tests -name "*.py" \| wc -l` | **144 files** | `OVERVIEW.md` §8: 112 files / 19,334 lines |
| `M-03` | `ls tests/architecture \| wc -l` | **19 files** (incl. `legacy_baseline.json` ⇒ 18 gates) | `OVERVIEW.md` §3 Q4 / `REFERENCES.md` §3: "15 files" |
| `M-04` | `grep -cE "^[A-Z_]+=" .env.example` ; settings fields | **47** env vars documented; **74** declared settings fields | `SECURITY.md` §5: "78 settings" |
| `M-05` | `grep -ln "_sync_engine\|create_engine(f\"sqlite" src/nexus_ai_agent/features/*.py \| wc -l` | **14 modules** | `DATA_AND_STORAGE.md` §1 (one owner per store) |
| `M-06` | `git merge-base --is-ancestor 7573249 HEAD` | exit 0 (ancestor) ⇒ docs are *pinned-and-stale*, not wrong-at-the-time | — |
| `M-07` | `grep -rn "idempotency" src/nexus_ai_agent/llm/ src/nexus_ai_agent/orchestration/` | **0 matches** | `PORTS.md`: `LLMPort.complete(..., idempotency_key=None)` |
| `M-08` | `grep -rn "async def put\|async def delete" src/nexus_ai_agent` | only the Protocol; `R2Provider` exposes `upload/download/list_files/delete_objects` | `DATA_AND_STORAGE.md` §1/§5: "idempotent `put`/`delete` behind `ObjectStoragePort`" |

## 3. External sources

### 3.1 Provider plans, quotas and prices (primary where marked)

| ID | Source | Class | Source date | Values used |
|---|---|---|---|---|
| `E-01` | Neon plans documentation — https://neon.com/docs/introduction/plans | EXTERNAL-PRIMARY | doc read 2026-09-23; page content current as of listing | Free: 100 CU-hours/project, 0.5 GB/project, autoscale ≤2 CU (8 GB RAM), scale-to-zero after **5 min**, 5 GB egress; Launch: $0.106/CU-hour, $0.35/GB-month, 500 GB egress then $0.10/GB; Instant restore $0.20/GB-month |
| `E-02` | Koyeb pricing FAQ — https://www.koyeb.com/docs/faqs/pricing | EXTERNAL-PRIMARY | page read 2026-09-23 | Free web service: **512 MB RAM, 0.1 vCPU, 2 GB SSD**, Frankfurt/Washington only, no volumes; free Postgres 1 GB / 5 h monthly; paid example rates (Nano $0.000001/s, eSmall $0.000002/s …) |
| `E-03` | Koyeb Eco-instance announcement — https://www.koyeb.com/blog/new-eco-instances-the-most-affordable-way-to-deploy-apps-globally | EXTERNAL-PRIMARY | page read 2026-09-23 | Instance ladder: eNano 256 MB $1.61/mo → eSmall 1 GB $5.36/mo → eMedium 2 GB $10.71/mo → eLarge 4 GB $21.43/mo |
| `E-04` | Koyeb free-tier summaries — https://www.srvrlss.io/provider/koyeb/ , https://freetier.co/directory/products/koyeb | EXTERNAL-INDEPENDENT | page ages 2025-10-17 / 2026-08-18 | 100 GB/mo outbound included; scale-to-zero after ~1 h no traffic on the free instance; 1 free instance per org |
| `E-05` | Cloudflare R2 pricing analysis (multiple) — https://egresscost.com/cloudflare/ , https://www.budgetforge.dev/tools/cloudflare-r2-pricing-2026 | EXTERNAL-INDEPENDENT | ages 2026-09-12 / 2026-09-08 | Storage **$0.015/GB-month**, Class A $4.50/M, Class B $0.36/M, **egress $0.00/GB**, free tier 10 GB + 1M/10M ops |
| `E-06` | Groq free-tier analysis — https://www.cloudzero.com/blog/groq-pricing/ , https://tokenmix.ai/blog/groq-api-pricing , https://benchlm.ai/free-tier/groq | EXTERNAL-INDEPENDENT | ages 2026-09-04 / 2026-04-29 / 2026-09-14 | **Sources disagree**: 30 RPM / 6,000 TPM / **14,400 RPD** vs **1,000 RPD** (and 250 RPD for `groq/compound`). ⇒ `[C]`, use `[U]` band and require console check. Paid example: Llama 3.3 70B `$0.59/$0.79` per M tokens |
| `E-07` | Gemini free-tier analysis — https://tokenmix.ai/blog/gemini-api-free-tier-limits , https://www.scriptbyai.com/gemini-api-free-tier-limits/ , https://www.cloudzero.com/blog/gemini-pricing/ | EXTERNAL-INDEPENDENT | ages 2026-04-29 / 2026-09-13 / 2026-09-04 | **Sources disagree**: "1,500 RPD, 1M TPM, 15 RPM" vs "20–500 RPD depending on model (post-2025-12 cut)" vs "5–15 RPM, up to 1,000 RPD". ⇒ `[C]`; free-tier data may be used for product improvement (privacy-relevant) |
| `E-08` | OpenRouter free-model limits — https://ask-coreai.com/blog/openrouter-rate-limits-explained-how-to-avoid , https://www.llmrumors.com/news/openrouter-free-model-limits-privacy-fallbacks | EXTERNAL-INDEPENDENT | ages 2026-09-14 / 2026-09-20 | 20 req/min always; **50 RPD** below $10 lifetime credit, **1,000 RPD** at/after; failed attempts consume quota |
| `E-16` | Qwen / Alibaba Model Studio (DashScope) model pricing pages — https://www.morphllm.com/qwen-api , https://pricepertoken.com/pricing-page/model/qwen-qwen3.6-27b , https://techjacksolutions.com/ai-tools/qwen/qwen-pricing/ | EXTERNAL-INDEPENDENT | ages 2026-09-07 / 2026-09-14 / 2026-08-18 | Chat/LLM rates only (e.g. Qwen3.8-Max $2/$6, Qwen coder $0.30/$1.50 per M, open-weight from $0.15/M in). **No verifiable `text-embedding-*` per-token price was found** ⇒ embedding price stays `[NEED-PRIMARY]` in Phase 8 |
| `E-17` | Gemini model pricing/lifecycle — https://docs.cloud.google.com/vertex-ai/generative-ai/pricing (age 2025-11-24, read 2026-09-23), https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing (2026-09-07), https://pricepertoken.com/pricing-page/model/google-gemini-2.0-flash-001 (2026-04-12), https://www.aifreeapi.com/en/posts/gemini-api-pricing-2026 (2026-01-06), https://www.morphllm.com/gemini-api-pricing (2026-08-21) | EXTERNAL-PRIMARY (Vertex/Google pages) + EXTERNAL-INDEPENDENT | see ages | **Conflicting** `[C]`: Gemini 2.0 Flash quoted at **$0.15/$0.60** (Vertex list, Sept 2026) vs $0.10/$0.40 (Jan 2026) vs a claim it was **retired 2026-06-01** (Aug 2026) and replaced by 2.5 Flash-Lite at $0.10/$0.40; Gemini 2.5 Flash $0.30/$2.50, 2.5 Flash-Lite $0.10/$0.40; new Flash models carry introductory pricing through 2026-12-31 with **prices doubling 2027-01-01** ⇒ lifecycle + price both NEED-PRIMARY |
| `E-18` | Grafana Cloud pricing analyses — https://monitoringcost.com/grafana-cloud-pricing (2026-06-30), https://costbench.com/software/developer-tools/grafana/ (2026-07-15), https://cubeapm.com/blog/grafana-cloud-pricing-and-review/ (2026-06-26) | EXTERNAL-INDEPENDENT | see ages | Free tier: **10,000 active series, 50 GB logs/mo, 50 GB traces, 3 users, 14-day retention**; Pro $19/mo platform fee + $6.50/1K series + ~$0.45/GB logs/traces + $8/active user |
| `E-19` | Kafka / Confluent / Strimzi delivery-semantics material — https://docs.confluent.io/kafka/design/delivery-semantics.html (2026-08-11), https://strimzi.io/blog/2023/05/03/kafka-transactions/ (2023-05-03), https://www.conduktor.io/glossary/exactly-once-semantics-in-kafka (2026-08-13), https://dev.to/devopsdaily/what-actually-happens-after-you-send-a-webhook-fao (2026-07-30) | EXTERNAL-PRIMARY (Confluent) + EXTERNAL-INDEPENDENT | Phase 5 definitions: at-most-once / at-least-once / exactly-once; "no exactly-once delivery in distributed systems (two generals), but idempotent append fakes it"; idempotent-producer = broker-side sequence dedup; **a webhook timeout means the outcome is unknown**; receiver-side dedup is mandatory under at-least-once |
| `E-20` | Telegram Bots FAQ (primary) — https://core.telegram.org/bots/faq + `getUpdates` semantics (https://gramio.dev/telegram/methods/getupdates, 2026-05-22) | EXTERNAL-PRIMARY | Phase 5: an update is confirmed **only** when `getUpdates` is called with a higher offset; ≤100 unconfirmed updates returned; re-delivery is the default behaviour ⇒ polling is at-least-once by design |
| `E-21` | Independent "dual-instance" polling incident report + single-instance guard PR — https://github.com/jacoscaz/fondamenta/pull/45 | EXTERNAL-INDEPENDENT (different project, same architecture class) | page age 2026-09-22 | Corroborates the two-replica failure mode analysed in Phase 9: two pollers advance offsets independently ("update theft"); fix = fetch without advancing, **confirm only after durable processing**, bounded retries with dead-lettering |
| `E-09` | Telegram Bot API limits (bot-ecosystem engineering publications + issue reports) — https://hfeu-telegram.com/news/telegram-bot-api-rate-limits-explained-856782827/ , https://grokipedia.com/page/Telegram_Bot_API_Limitations | EXTERNAL-SECONDARY (Telegram does not publish a formal quota table) | ages 2025-12-16 / 2026-01-14 | ~30 msg/s per bot token overall; ~1 msg/s per chat; 20 msg/min in groups; **webhook must answer 200 within ~60 s or Telegram re-delivers**; Telegram stores undelivered updates ≤24 h |
### 3.2 Phase-13 prior art (target-architecture research and GitHub prior art, gathered 2026-09-23)

| ID | Source | Class | Values used |
|---|---|---|---|
| `E-22` | **microservices.io — Transactional outbox pattern** (pattern catalogue, primary) — https://microservices.io/patterns/data/transactional-outbox.html | EXTERNAL-PRIMARY | The canonical statement: write the message into the DB **in the same transaction** as the state change; a relay publishes it; delivery is **at-least-once**; consumers must be idempotent (track processed message ids); 2PC is not used |
| `E-23` | Postgres-as-queue practice — Tembo PGMQ design note (2023-11-07, https://legacy.tembo.io/blog/pgmq-self-regulating-queue/), nerdleveltech guide (2026-06-11, https://nerdleveltech.com/postgres-listen-notify-job-queue), implementation walkthroughs (aminediro.com 2025-06-14; faizahmed.in 2026-07-03) | EXTERNAL-INDEPENDENT | `FOR UPDATE SKIP LOCKED` is the documented pattern "for multiple consumers accessing a queue-like table" (PG 9.5+); a **visibility timeout** removes the need for a separate watcher; a reaper resurrects rows stuck past the lease; the *plain* `SELECT`+`UPDATE` variant is a documented **double-processing race**; `NOTIFY` caveats: global lock on commit order, missed notifications are **never replayed**, payload <8 KB |
| `E-24` | GitHub prior art for DB-backed queues — **riverqueue/river** (https://github.com/riverqueue/river, https://brandur.org/river), **tembo-io/pgmq** (https://github.com/tembo-io/pgmq — 3.1k★), **pg-boss** (via `E-23`) | EXTERNAL-INDEPENDENT (official repos) | River: "enqueue jobs **transactionally** along with other DB changes … jobs aren't visible for work until commit"; features: unique jobs, retries, scheduled/periodic jobs, batch insert, telemetry hooks, ~10k trivial jobs/s on a laptop; PGMQ: SQS-like queue **as a Postgres extension** with visibility timeout; pg-boss: production Postgres queue on the same primitives |
| `E-25` | **Stripe idempotency documentation** (primary) — https://docs.stripe.com/api/idempotent_requests , https://docs.stripe.com/error-low-level , plus an independent spec analysis (2026-07-01, https://shubhamkalsi.com/blog/idempotency-keys-payment-integrations/) | EXTERNAL-PRIMARY | Keys apply to state-changing requests (POST); the **result is cached and replayed, including failures**; retention ≥24 h; same key + different parameters ⇒ error; **concurrent duplicates are resolved by locking**, not by racing; the key must be derived from the logical operation (e.g. order id), **not** minted per attempt; after a 4xx the safe move is a **new** key |
| `E-26` | Telegram Bot API *limits table* (secondary compilation) — https://www.conferbot.com/limits/telegram | EXTERNAL-SECONDARY | Webhook: HTTPS only, ports 443/80/88/8443, `max_connections` 1–100, `secret_token` 1–256 chars (A–Z a–z 0–9 _ -), **retries on non-2xx "a reasonable amount of attempts"**, undelivered updates kept **24 h**, sending guidance ~30 msg/s (bot) · ~1 msg/s per chat · 20 msg/min per group, 429 carries `retry_after` (sleep exactly that) |
| `E-27` | **Expand/contract (parallel change)** — multiple engineering sources (2026): https://blog.theappcode.net/zero-downtime-database-migrations , https://www.datasops.com/blog/database-migrations-zero-downtime , https://tech-champion.com/database/backward-compatible-database-migrations-the-expand-contract-pattern-for-zero-downtime-releases/ | EXTERNAL-INDEPENDENT | "Never change the schema and the code that depends on it in the same step": expand (additive) → migrate (backfill/dual-write) → contract (drop) **in separate deploys**; dangerous-DDL table (reindex/`ALTER TYPE`/renames/drops); CI that runs the migration **twice** to prove idempotency; `CREATE INDEX CONCURRENTLY` and lock timeouts |
| `E-28` | **OWASP LLM01 (Prompt Injection)** + "lethal trifecta" — https://blog.alexewerlof.com/p/owasp-top-10-ai-llm-agents , https://www.alekseialeinikov.com/en/blog/topics/security/prompt-injection-defense-2026-lethal-trifecta-test , https://www.kunalganglani.com/blog/prompt-injection-2026-owasp-llm-vulnerability (Willison, June 2025) | EXTERNAL-PRIMARY (OWASP, via summaries) + EXTERNAL-INDEPENDENT | OWASP states **no fool-proof prevention exists** and that RAG/fine-tuning do not fix it; **lethal trifecta** = private data access + untrusted content + external communication ⇒ exfiltration; mitigations: cut the outbound leg by an enforced allow-list, least privilege on tools, output screening, human approval **only for side-effecting actions**, treat untrusted content as data |
| `E-29` | **Google SRE — Four Golden Signals & SLO/error budget** — https://www.sherlocks.ai/blog/four-golden-signals-of-sre , https://novaaiops.com/golden-signals , https://web-alert.io/blog/four-golden-signals-monitoring-explained | EXTERNAL-SECONDARY (SRE book content) | Latency (as percentiles, split success/failure) · Traffic · Errors (as a ratio) · Saturation (of the *true* bottleneck, with predictive alerts); **alert on symptoms at the service boundary tied to SLO burn rate, not on causes** |
| `E-30` | **CISA — "2026 Minimum Elements for a SBOM"** (joint agency release, July 2026) — https://www.cisa.gov/news-events/news/cisa-and-partners-unveil-updated-software-bill-materials-resource-improves-transparency-security-and | EXTERNAL-PRIMARY (agency publication) | New minimum elements: Component Hash Algorithm, Component License, SBOM Tool Name, SBOM Generation Context; renamed fields; applies to **all** software including OSS, AI software and SaaS ⇒ a concrete standard for the "no SBOM/lock file" finding (`S-26`) |
| `E-31` | **Neon instant restore / history window** (vendor docs, primary) — https://neon.com/docs/postgres/backup-restore/history-window , https://neon.com/docs/postgres/backup-restore/branch-restore , https://neon.com/docs/introduction/plans | EXTERNAL-PRIMARY | Point-in-time restore of a **root branch** to any millisecond in the retained history (timestamp or LSN); **Free = 6 h history, capped 1 GB-month**; Launch up to 7 days at $0.20/GB-month; a restore applies to **all databases on the branch**; CLI example `neon branches restore main ^self@<ts> --preserve-under-name main_pre_restore`; an automatic backup branch is kept |
| `E-32` | **Litestream** (SQLite streaming replication) — https://github.com/benbjohnson/litestream , https://jacar.es/en/litestream-near-real-time-replication-for-sqlite/ , https://deepwiki.com/benbjohnson/litestream | EXTERNAL-PRIMARY (official repo) + independent | Replicates SQLite WAL frames to S3-compatible storage **including Cloudflare R2**; typical lag <1 s; point-in-time `restore -timestamp/-txid`; 1–3% CPU overhead; runs as a **sidecar process**; adds a `_litestream_lock` table to the source DB |
| `E-33` | **Chaos engineering principles** — https://www.pagerduty.com/resources/engineering/learn/what-is-chaos-engineering/ , https://www.educative.io/blog/chaos-engineering-process-principles , InfoQ on Google Cloud's chaos framework (2025-11-12, https://www.infoq.com/news/2025/11/google-chaos-engineering/) | EXTERNAL-INDEPENDENT | Define a **steady-state hypothesis** first; inject realistic faults; minimise blast radius; automate and repeat; Netflix's Chaos Monkey/FIT lineage; the goal is to *falsify* the hypothesis |


### 3.3 Standards and frameworks

| ID | Source | Class | Used for |
|---|---|---|---|
| `E-10` | OWASP Top 10 for LLM Applications 2025 (v2.0, published 2024-11-18) — LLM01 Prompt Injection … LLM10 Unbounded Consumption; OWASP Top 10 for Agentic AI (late 2025) | EXTERNAL-PRIMARY (foundation publication, via secondary summaries) | Phase 6 attack taxonomy mapping |
| `E-11` | NIST SP 800-34 Rev.1 (Contingency Planning Guide for Federal Information Systems) — defines RTO/RPO and requires stated recovery objectives | EXTERNAL-PRIMARY (standard, known text) | Phase 7 RTO/RPO framing; `UNDEFINED` marking |
| `E-12` | NIST SP 800-207 (Zero Trust Architecture) — no implicit trust from network location | EXTERNAL-PRIMARY (standard) | boundary review of `/creative/*` and dashboard |
| `E-13` | CISA/NSA guidance on secure-by-design defaults & SBOM/supply-chain risk | EXTERNAL-PRIMARY (agency publication, general) | supply-chain and dependency findings |
| `E-14` | SQLite documentation: WAL mode / single-writer / busy-handler semantics | EXTERNAL-PRIMARY (vendor docs) | concurrency floors in Phases 2/4 |
| `E-15` | PostgreSQL documentation: MVCC, `READ COMMITTED` visibility, advisory locks, connection limits | EXTERNAL-PRIMARY (vendor docs) | consistency/claim analysis |

> **Citation discipline:** prices/quotas above carry the *page age* reported by the search tool, not my assumption. Any figure whose primary page was unreachable is tagged `[NEED-PRIMARY]` in the cost model and never used as the sole basis of a recommendation.

---

## 4. Claims deliberately marked `[U]` (unverifiable in this session)

| ID | Claim | Why unverifiable here |
|---|---|---|
| `U-a` | Actual production traffic/DAU of this bot | no production telemetry exists in-repo; no observability sink |
| `U-b` | Actual Koyeb instance class currently deployed | `koyeb.yaml` declares no instance type; console state unknown |
| `U-c` | Whether `NEXUS_DATABASE_URL` is set in production | not in `koyeb.yaml`; only console state would show it |
| `U-d` | Real per-request token counts (⇒ real LLM cost) | no token accounting exists in-repo (`CHANGELOG` itself says cost estimates are "not an invoice") |
| `U-e` | Groq/Gemini/OpenRouter quotas as actually enforced for this account | vendors do not publish stable numbers; secondary sources conflict (`E-06`…`E-08`) |
| `U-f` | R2 bucket size / backup growth | no inventory command in-repo; requires credentials |
| `U-g` | Whether an external uptime probe covers the Koyeb app | no monitoring config in repo |

**Rule applied:** nothing in `[U]` was used to *conclude* a risk; it was used only to size an uncertainty (Phases 2, 8, 12).
