# PHASE 2 — BOTTLENECK FORENSICS

**Question:** at 10 / 100 / 1 K / 10 K / 100 K / 1 M requests per day, which resource saturates first — and what does the system do when it does?
**Method:** a stated arithmetic model (`[A]`) applied to *verified* ceilings (`[V]`); every ceiling carries its source. Nothing in this file claims production traffic.
**Headline:** the binding constraint is **never the process**. It is, in order: (1) provider daily quota, (2) durable storage ceilings in the documented topology, (3) unbounded job concurrency on a 0.1 vCPU instance, (4) Telegram's per-group send limit above ~100 K/day.

---

## 0. Measurement model (stated so it can be attacked)

| Symbol | Value | Class | Method / source |
|---|---|---|---|
| `U` | 1 "request" = 1 Telegram update → 1 user-visible answer | assumption | `R-21` flow 1 |
| `L` | LLM calls per request = **1.5** (1 chat completion + 0.5 memory-extraction; moderation assumed local) | `[A]` | `R-21` flow 1 nodes; memory writer only for consented users (`R-08` `features/ai_memory.py`) |
| `T_in` / `T_out` | 300 / 150 tokens per LLM call | `[A]` | `max_short_term_messages=20`, `max_tokens_before_summary=3000`, `top_k_memories=3` (`R-18`) |
| `S_msg` | 1 KB per stored message row | `[A]` | schema shape in `R-09` |
| `S_ckpt` | 20 KB per checkpointed turn (LangGraph state, trimmed history) | `[A]` | `R-21` checkpoint store; needs measurement (U-08) |
| Peak factor | 3× the daily average, concentrated in a 6 h window | `[A]` | messenger traffic shape |
| Instance (SMALL) | Koyeb free web service: **0.1 vCPU, 512 MB RAM, 2 GB SSD** | `[V]` | `E-02`, `E-04` |
| Instance (MED) | Koyeb eSmall: **0.5 eCPU, 1 GB RAM, $5.36/mo** | `[V]` | `E-03` |
| DB (SMALL) | Neon free: **0.5 GB/project, 100 CU-hours, 5 min idle scale-to-zero, 5 GB egress** | `[V]` | `E-01` |
| Telegram | ~**30 msg/s** per token overall, ~1 msg/s per chat, **20 msg/min in groups**, webhook must answer 200 in ~60 s or the update is re-delivered | `[V]` (vendor does not publish a formal table; consistent across independent sources) | `E-09` |
| R2 | $0.015/GB-mo, egress free, 10 GB free tier | `[V]` | `E-05` |
| Queue | per-process, in-memory task map; **no concurrency limit, no queue-depth limit** | `[V]` | `R-01` (`_schedule` creates a task per job immediately) |
| Rate limit | 10 messages / 60 s per user, per process, non-durable | `[V]` | `R-13`, `R-18` |
| Render cost | a 30 s 720p slideshow = **one** FFmpeg process; timeout 900 s | `[V]` (timeout), `[A]` (duration) | `R-15`, `R-16` |
| Bench baseline | the only committed perf baseline measures **pure IR compilation** (p50 **0.042 ms**) — not encoding | `[V]` | `tests/bench/baseline_render.json` |

> **Important caveat.** `L`, `T_in/T_out`, `S_msg`, `S_ckpt`, and the peak factor are *assumptions*. They are stated so that a wrong model can be corrected with one number instead of re-reading the whole document. The **orders of magnitude of the walls do not depend on them** — each wall has a ±5× tolerance before it moves a tier.

---

## 1. Verified resource ceilings (the walls, before any arithmetic)

| # | Ceiling | Value | Consequence when hit | Source |
|---|---|---|---|---|
| W1 | Provider daily quota per **organisation** (not per key) | Groq: 30 RPM + **1,000 or 14,400 RPD** `[C]`; Gemini: 15 RPM / 1,500 RPD configured locally, externally reported 20–1,500 RPD `[C]`; OpenRouter: 20 RPM / 50 or 1,000 RPD | Chain degrades to `FakeLLM` disclaimer; user sees a "degraded" answer | `E-06`, `E-07`, `E-08`, `R-18` (`gemini_max_rpm=15`, `gemini_max_daily=1500`) |
| W2 | Telegram send quota | ~30 msg/s/token; **20 msg/min in a group**; 1 msg/s per chat | HTTP 429 with `retry_after`; retries can double-send unless deduped | `E-09` |
| W3 | Neon free storage | **0.5 GB per project** (hard limit, not just a price) | Writes fail; the app has no "DB full" handling path | `E-01` |
| W4 | Neon free compute | 100 CU-hours/project/month; 5-min idle scale-to-zero | cold-start latency; compute exhausted ⇒ no writes | `E-01`, `R-06` (per-process `_pg_prepared_urls` ⇒ migration work on every cold start) |
| W5 | Instance CPU | 0.1 vCPU (free) | every CPU-bound step (FFmpeg, embedding, JSON, torch import) is ~10× slower than a laptop | `E-02` |
| W6 | Instance RAM | 512 MB (free) / 1 GB (eSmall) | OOM kill; `MAX_RAM_MB=1500` self-monitor threshold is 3× the free instance's RAM ⇒ can never fire before the kernel does | `E-02`, `R-18`, `R-19` |
| W7 | Disk | 2 GB SSD (free), **ephemeral**, no volumes on free | queue sidecar, checkpoints, vectors, creative temp dir, `creative_jobs` all live here | `E-02`, `E-03`, `R-01`, `R-08`, `R-16` |
| W8 | SQLite | one writer, per-file; readers via WAL only where WAL was set | write contention, `database is locked` after 30 s busy timeout | `E-14`, `R-01`, `R-06` |
| W9 | Job concurrency | **unbounded** — `_schedule` creates one asyncio task per job | N queued renders = N FFmpeg children in one process | `R-01` |
| W10 | Process count | 1 (modular monolith) | no CPU/GPU isolation between chat and render work | `R-22` |
| W11 | Observability | in-process counters + stdout logs, no external sink | after a restart, capacity evidence is gone; no alert fires unattended | `R-21` |

---

## 2. The six tiers

Notation: `RPS_avg` = U/86 400; `RPS_peak` = 3×; `LLM/day` = U × 1.5; `RPM` = LLM/day ÷ 1 440 (average) and ×3 for peak.

### 2.1 — 10 requests/day (a private bot)

| Axis | Value / behaviour | Marker |
|---|---|---|
| CPU | unmeasurable; each answer is milliseconds of Python + dominated by provider latency | `[A]` |
| RAM | baseline residency dominates (imports): torch/chromadb/llama-cpp in base deps (`R-20`) — hundreds of MB before any request | `[A]`, `[V]` for deps |
| DB | ≤ 10 rows/day; Neon free CU-hours untouched | `[A]` |
| IO | negligible except the nightly backup (`R-19`) | `[V]` |
| Network | ≤ 15 LLM calls/day ⇒ well inside every free tier | `[A]` |
| LLM | 15 calls/day; **the first call per cold start may burn 60 s against the unreachable Ollama hop** (`A-33`) | `[V]` + `[A]` |
| Queue | ≤ a few jobs; single-task execution fine | `[V]` |
| Locks | none contended (one process) | `[V]` |
| Telegram | far below every limit | `[V]` |
| Providers | free tier sufficient | `[A]` |
| **First wall** | **cold start, not load**: Neon 5-min idle scale-to-zero + Koyeb scale-to-zero + migration check at boot (`R-06`) ⇒ every first message after idle pays DB wake + import time; `healthz` says "ok" throughout (`A-20`) | `[V]` |

### 2.2 — 100 requests/day (a small user group)

| Axis | Value | Marker |
|---|---|---|
| CPU | ~0.1 vCPU is enough for chat; **1 render per day is ~10 % of the day's CPU budget** (see §3.2 arithmetic) | `[A]` |
| RAM | 512 MB cap: one FFmpeg 720p encode + Python + torch baseline is the first OOM candidate | `[A]` |
| DB | 100 rows/day + checkpoints (~2 MB/day) — free Neon fine | `[A]` |
| LLM | 150 calls/day ⇒ inside the optimistic Groq/Gemini readings, **outside the pessimistic ones** (Groq 1,000 RPD is fine; OpenRouter 50 RPD is not if it is reached) | `[C]` |
| Queue | ~1–3 jobs/day; the notifier path (`R-03`) sends files ≤50 MB (`E-09` file-size limit) | `[V]` |
| Telegram | a single active group can hit **20 msg/min** with 2–3 concurrent users | `[V]` (`E-09`) |
| **First wall** | **group-chat send limit** and **free-instance RAM during a render** | `[V]`+`[A]` |

### 2.3 — 1 000 requests/day (≈0.012 RPS avg, 0.035 peak — a real small product)

| Axis | Value | Marker |
|---|---|---|
| CPU | ~1 500 LLM calls/day is provider-bound; Python work negligible; each render still monopolises the single core | `[A]` |
| RAM | free instance: 512 MB is *below* the comfortable floor for the declared dependency set (`R-20`); eSmall (1 GB) is the realistic minimum | `[A]` |
| DB — **the first hard wall appears here** | checkpoints ≈ **20 MB/day**, messages ≈ 1 MB/day. Neon **free 0.5 GB** project ceiling ⇒ **~25 days to full** at 20 KB/ckpt; at 50 KB/ckpt ⇒ **10 days**. Reaching the cap is a write failure, not a bill. | `[A]`(sizes) + `[V]`(cap `E-01`) |
| IO | R2 backups: dump size grows ~0.6 GB/month (`[A]`); nightly upload is small; free tier (10 GB) holds ~16 months | `[A]` + `[V]` `E-05` |
| Network | egress: Neon free 5 GB/mo (`E-01`); dashboard polling every 30 s (`R-04` root page `setInterval(loadStats, 30000)`) consumes egress only when open | `[V]` |
| LLM | 1 500 calls/day = **1.04 RPM avg / 3.1 peak** vs Groq 30 RPM ⇒ RPM fine. RPD: **Groq's pessimistic reading (1,000)** is exceeded by 50 %; the optimistic reading (14 400) is not. Gemini configured cap is 1,500/day ⇒ **exactly at the ceiling**. | `[C]` + `[V]` (`R-18`) |
| Queue | tens of jobs/day; still no concurrency cap, but average pressure is low | `[V]` |
| Locks | SQLite single writer + the 14 sync feature engines (`R-08`) start to interleave with the async engine; `SQLITE_BUSY` becomes possible under bursts | `[A]` |
| Telegram | 1 000 msgs/day is nowhere near 30/s; a busy group can still trip 20/min | `[V]` |
| Providers | image generation (Pollinations) unauthenticated, no quota (`R-21` §7) — but no SLA either | `[V]` |
| **First wall** | **Neon free 0.5 GB storage** (≈25 days), immediately followed by **the configured Gemini 1,500/day cap** and the **ambiguous Groq RPD** | `[V]`+`[C]` |

### 2.4 — 10 000 requests/day (0.12 RPS avg, 0.35 peak)

| Axis | Value | Marker |
|---|---|---|
| CPU | ~15 000 LLM calls/day; Python/JSON work still small; renders: see §3.2 — 10 renders/day consumes ~1.5–5 h of the single 0.1 vCPU | `[A]` |
| RAM | free instance infeasible; eSmall (1 GB) marginal with one concurrent render; eMedium (2 GB, $10.71/mo) realistic | `[V]` (`E-03`) + `[A]` |
| DB | checkpoints ≈ **200 MB/day ⇒ 6 GB/month**; messages 30 MB/month. Neon free cap breached in **~2.5 days**; paid storage at $0.35/GB-mo ⇒ ~$2/mo for 6 GB, but **connection/CPU pressure** (100 CU-hours free = 0.25 CU always-on) is now the real constraint | `[A]` + `[V]` `E-01` |
| IO | pg_dump nightly on a 6 GB DB: minutes of runner time; SQLite path irrelevant here | `[A]` |
| Network | LLM egress ≈ 6.75 M tokens/day ≈ 27 MB/day — trivial; **Neon egress** 5 GB free may bind if the dashboard/API is read-heavy | `[A]` |
| LLM | 15 000 calls/day = **10.4 RPM avg / 31 peak** ⇒ **exceeds Groq free 30 RPM at peak and matches it on average**; exceeds Gemini's 15 RPM by ~2×; must either use several free providers or pay | `[V]`(config) + `[V]`(quotas) |
| Queue | hundreds of jobs/day; **unbounded concurrency becomes a real hazard**: a burst of 20 `/slideshow` requests = 20 concurrent FFmpeg children on ≤1 GB RAM ⇒ OOM kill (all jobs red, queue rows left `processing`, no automatic re-drive outside `resume_pending`) | `[V]` `R-01`, `[A]` burst shape |
| Locks | cross-process writers appear (bot + `nexus jobs resume` + dashboard + GH-Actions backup holding a `pg_dump` snapshot) | `[V]` |
| Telegram | 10 000/day ⇒ ~0.35 msg/s peak; fine globally, but a **single group** is capped at 20/min (≈28 800/day) ⇒ only ~2.9 k group messages/day per chat | `[V]` `E-09` |
| **First wall** | **provider RPM (peak) and instance RAM** simultaneously; then **job-concurrency blow-up** | `[V]` |

### 2.5 — 100 000 requests/day (1.16 RPS avg, 3.5 peak — the design's hard ceiling without re-architecture)

| Axis | Value | Marker |
|---|---|---|
| CPU | 150 k LLM calls/day; process CPU still not the limit, but the **single process** is now doing chat + renders + lifecycle hooks + dashboard + webhook parsing | `[A]` |
| RAM | ≥2 GB instance required; **`InProcessJobQueue` has no isolation**, so chat latency and render concurrency fight for the same heap | `[A]` |
| DB | checkpoints ≈ **2 GB/day ⇒ 60 GB/month**. Neon paid storage $0.35/GB-mo ⇒ **~$21/mo storage alone**, plus compute: 1 CU always-on ≈ 730 CU-hours ≈ **$77/mo** (`E-01`/independent arithmetic in `08-cost-model.md`) | `[V]`(rates) + `[A]`(volume) |
| IO | the SQLite-backed stores in the *other* persistence plane (`R-08`) are now definitively wrong: they cannot be backed up, scaled, or vacuumed without downtime; a 2 GB ephemeral container disk (`E-02`) fills in hours if the creative temp dir leaks | `[V]` + `[A]` |
| Network | LLM egress ≈ 67.5 M tokens/day (~270 MB/day) ⇒ ~8 GB/month; **Neon free egress (5 GB/mo) is exceeded**; paid: 500 GB included | `[V]` `E-01` + `[A]` |
| LLM | **104 RPM avg / 313 peak** ⇒ requires ≥11 free-tier organisations or paid capacity. Paid at Llama-3.1-8B rates ($0.05/$0.08 per M): 45 M in + 22.5 M out tokens/day ⇒ **≈$3.5/day ≈ $105/mo** | `[V]`(rate) + `[A]`(volume) |
| Telegram | 3.5 msg/s peak — **under** 30/s, but 100 k/day means ~4.5 msg/s of *responses plus file sends*; a media-heavy mix can approach the ceiling; per-chat 1/s is never the binding limit at this tier | `[V]` `E-09` |
| **First wall** | **provider throughput** (paid capacity required) and **DB storage/compute cost**, in that order. Nothing in the process layer survives this tier unchanged. | `[A]` |

### 2.6 — 1 000 000 requests/day (11.6 RPS avg, ~35 RPS peak)

| Axis | Value | Marker |
|---|---|---|
| CPU / RAM | single process is finished as a model: requires worker separation, a real pool, and per-workload isolation. **W9/W10 (unbounded concurrency, one process) are disqualifying.** | `[V]` + `[A]` |
| DB | checkpoints ≈ **20 GB/day ⇒ 600 GB/month**: Neon paid at $0.35/GB-mo ⇒ **≈$210/mo storage**, plus ~$77–160/mo compute; connection pool must grow or be proxied (Neon pooled endpoint, `R-06` uses the plain URL + `pool_pre_ping`) | `[V]`+`[A]` |
| IO | pg_dump of a 600 GB DB is no longer a backup strategy; the nightly GH-Actions job (`R-19`) becomes a multi-hour transfer with a runner-side timeout risk | `[A]` |
| Network | LLM egress ≈ 8 GB/day ≈ **240 GB/month** | `[A]` |
| LLM | **1 041 RPM avg / 3 125 peak**. Paid at 8B rates: 13.5 B input + 6.75 B output tokens/month ⇒ **≈ $675 + $540 = $1,215/mo**; at 70B rates (×10) ⇒ **≈ $12 k/mo**. Free tiers are not a factor at this tier. | `[A]`(volume) + `[V]`(unit rates `E-06`) |
| Queue | needs a broker or a sharded DB lease; the SQLite sidecar's write rate (1 job row per request minimum) would be ~35 inserts/s with a single writer and a per-call connection open/close (`R-01` opens a connection per operation) | `[V]`+`[A]` |
| Telegram | **~35 msg/s peak exceeds the 30 msg/s per-token ceiling** ⇒ requires sharding across multiple bot tokens or aggressive batching (`E-09` mentions multi-bot fan-out as the only lawful path) | `[V]`+`[A]` |
| **First wall** | **Telegram per-token send ceiling at ~2.6 M/day**, effectively; and before that, the provider contract and the DB bill | `[V]`+`[A]` |

> **Cross-check on the Telegram ceiling.** 30 msg/s × 86 400 s = **2.59 M messages/day** theoretical maximum for one bot token with zero reply fan-out. A 1 M/day product that sends one response per request occupies ~39 % of the entire global bot quota — at 3× peak it is over 100 %. This is why the 1 M tier is the first tier where the *platform contract*, not the code, is the limit.

---

## 3. Where it actually breaks (mechanism-level, not tier-level)

### 3.1 The queue does not bound anything
`R-01`'s `_schedule` creates an `asyncio.Task` per job immediately, with a per-process `dict` as the only guard. There is no semaphore, no worker pool, no depth cap, and no per-user admission control. `DECISION_LOG.md` (Wave 2.5, rejected alternatives) states "the queue bounds *concurrency*, not *cost per job*" — **the code contradicts that sentence**: the queue bounds *durability*, not concurrency. Ten simultaneous `/slideshow` requests from one allowed user (rate limit is 10 msg/60 s, `R-13`) produce ten simultaneous FFmpeg processes. On a 512 MB / 0.1 vCPU instance this is an OOM-kill vector reachable by a *single* authorised user. → S2/S5, `[V]`.

### 3.2 Render arithmetic on the deployed CPU class
- A 30 s 720p slideshow with a handful of xfade/zoom operations: ~30–90 s of CPU on a modern laptop core `[A]`.
- Koyeb free = 0.1 vCPU ⇒ multiply by ≈10 ⇒ **5–15 minutes per render**, inside the 900 s timeout but frequently near it `[A]`.
- One render therefore occupies **20–50 % of an hour** of the only core; 10 renders/day ≈ **1.7–8 h of CPU** — i.e. a service that renders 10 videos/day can saturate the free instance doing nothing else `[A]`.
- The committed benchmark does **not** measure this (it measures IR compilation: p50 0.042 ms). **There is no encode-time baseline anywhere in the repo** ⇒ capacity claims about rendering are currently unfalsifiable from CI `[V]`.

### 3.3 Checkpoints are the storage bottleneck, not messages
Messages are ~1 KB/row (`R-09`); LangGraph checkpoints carry the workflow state and are re-written per turn. At 20 KB/checkpoint the checkpoint store is **20× the message store**; at 100 K/day it is ≈20 GB/day. The retainer (30-day resumability, `R-21`/`R-22`) is therefore a **storage multiplier**, not just a policy: 30 days × 20 GB/day = 600 GB of retained checkpoints at the 1 M tier. Nothing in the repo states a storage budget, so this is an unbudgeted cost centre. `[A]` (size) + `[V]` (retention policy).

### 3.4 LLM wall order, with the ambiguity made explicit
At `L = 1.5` calls/request:

| Tier | Calls/day | Avg RPM | Peak RPM | Verdict against verified config | Verdict against external quota readings |
|---|---|---|---|---|---|
| 1 K | 1 500 | 1.0 | 3.1 | Gemini local cap (1 500/day) exactly hit | Groq `[C]`: OK at 14 400 RPD, **failed** at 1 000 RPD |
| 10 K | 15 000 | 10.4 | 31 | Gemini cap exceeded 10× | Groq optimistic OK (RPM-tight), pessimistic impossible |
| 100 K | 150 000 | 104 | 313 | every free tier exceeded | paid required |
| 1 M | 1 500 000 | 1 041 | 3 125 | — | paid required, cost ×10 between 8B and 70B class |

`[V]` for the local configuration, `[C]` for the quota band (see `11-contradictions.md` K-05).

### 3.5 CPU-bound import cost at every cold start
Base dependencies include `sentence-transformers` (torch), `chromadb`, `llama-cpp-python` (`R-20`). The 2026-09-21 audit measured a ~7 GB environment `[H]`. On a 512 MB instance the *imports alone* are a memory and latency event; on scale-to-zero that cost is paid per wake-up. `[H]` + `[A]`.

### 3.6 Locks and write paths at each tier

| Tier | Contending writers | Realistic failure |
|---|---|---|
| 10–1 K | bot process only | none observed `[A]` |
| 10 K | bot + dashboard + `jobs resume` CLI | SQLite `database is locked` after 30 s busy timeout on the **app DB** when a sync feature engine (`R-08`) holds a write while the async engine writes `messages` `[A]` |
| 100 K | + GH-Actions nightly `pg_dump` (holds a long transaction on Neon) | long-running dump interferes with vacuum/IO; Neon free tier egress is consumed `[A]` |
| 1 M | + sharded bots | requires a distributed claim/lease; today's in-memory claim fails `[V]` (`A-04`) |

### 3.7 What is *not* a bottleneck (verified negatives — worth keeping)
- **File I/O in the render lane:** `.part` + atomic rename is O(1) and single-file — not a scaling limit. `[V]`
- **The architecture gates:** 18 files, pure-AST/stdlib (measured runtime in CI is seconds, not minutes). `[V]` (`M-03`, `R-25`).
- **The bot's handler dispatch:** PTB's handler chain is microseconds; the LLM call dominates by 5–6 orders of magnitude. `[V]` + `[A]`.
- **Image generation through Pollinations:** unauthenticated and quota-free, but with **no SLA** — a reliability risk, not a throughput wall. `[V]` (`R-21` §7).

---

## 4. FACTS vs ANALYSIS

**FACTS (source-traceable)**
1. Job concurrency is unbounded per process; the queue is a durability mechanism, not a throttle (`R-01`).
2. Rate limiting and provider cooldowns are in-process and non-durable (`R-12`, `R-13`).
3. The only committed performance baseline measures IR compilation, not encoding (`tests/bench/`, `M-03` adjacent).
4. 14 feature modules bypass the central engine and open local SQLite engines (`R-08`, `M-05`).
5. Neon free = 0.5 GB/project + 100 CU-hours; Koyeb free = 0.1 vCPU / 512 MB / 2 GB ephemeral; Telegram ≈30 msg/s per token and 20 msg/min per group (`E-01`, `E-02`, `E-09`).
6. Documented free-tier LLM quotas conflict between independent sources by up to 14× (`E-06`…`E-08`).

**ANALYSIS (this lab)** — the ordering below is a *model*, not a measurement:
1. At **≤1 K/day**, the binding constraints are topology artefacts (cold start, ephemeral disk, RAM during a render), not load.
2. Between **1 K and 10 K/day**, storage and provider *daily* quota bind before CPU.
3. Between **10 K and 100 K/day**, the split persistence plane and unbounded job concurrency become correctness problems, not performance problems.
4. Above ~**2.6 M messages/day** the platform contract (one bot token) is the hard ceiling.
5. The cheapest capacity lever is **not** a bigger instance: it is (a) a per-chat job concurrency cap, (b) trimming checkpoint retention, (c) making the LLM chain quota-aware. All three are ADR candidates (`10-adr-candidates.md` ADR-07…ADR-09).

**What would falsify this phase:** real token counts per request, real checkpoint sizes, and a real encode-time measurement on the deployed instance class. Until those three exist, every tier boundary above is ± one tier. They are registered as unknowns U-08, U-09, U-10.
