# PHASE 8 — COST MODEL (SMALL / MEDIUM / LARGE)

**Rule for this file:** every price carries the **date of the source page** and its evidence class. Where a price could not be verified this session, the cell says `[NEED-PRIMARY]` and the model uses an explicit band instead of a false precision. Where independent sources disagree, the row is marked `[C]` and the *planning number* is chosen **against the app** (the more expensive/capable one), never the cheapest optimistic one.
**What this model is not:** a quotation. It is a *shape* of the cost curve plus the identification of the cliffs.

---

## 0. Facts the model is anchored on

| Ref | Fact | Source | Date |
|---|---|---|---|
| Neon Free | 100 CU-hours, 0.5 GB/project, autoscale ≤2 CU, **scale-to-zero after 5 min idle**, 5 GB egress | `E-01` | read 2026-09-23 |
| Neon Launch | $0.106/CU-hour, $0.35/GB-month, instant restore $0.20/GB-month | `E-01` | read 2026-09-23 |
| Koyeb free web service | 512 MB RAM, **0.1 vCPU**, 2 GB SSD, **no volumes**, ~1 h idle → scale-to-zero, 100 GB/mo egress | `E-02`, `E-04` | 2025-10-17 / 2026-08-18 |
| Koyeb Eco ladder | eNano 256 MB $1.61 · eSmall 1 GB $5.36 · eMedium 2 GB $10.71 · eLarge 4 GB $21.43 (per month) | `E-03` | read 2026-09-23 |
| Cloudflare R2 | $0.015/GB-month storage · Class A $4.50/M · Class B $0.36/M · **egress $0.00/GB** · free 10 GB + 1M/10M ops | `E-05` | 2026-09-08 / 2026-09-12 |
| Grafana Cloud Free | 10,000 active series, 50 GB logs/mo, 3 users, **14-day retention**; Pro $19/mo + $6.50/1K series + ~$0.45/GB logs | `E-18` | 2026-06-30 / 2026-07-15 |
| Gemini paid (planning) | Gemini 2.0 Flash **$0.15/M in · $0.60/M out** (Vertex list) `[C]` with $0.10/$0.40 (Jan 2026) and with a claim that the model was retired 2026-06-01 | `E-17` | 2026-04-12 / 2026-09-07 / 2026-08-21 |
| Gemini 2.5 Flash (replacement) | $0.30/M in · $2.50/M out | `E-17` | 2026-08-21 |
| Groq paid (Llama 3.3 70B) | $0.59/M in · $0.79/M out `[EXTERNAL-INDEPENDENT]` | `E-06` | 2026-04-29 / 2026-09-04 |
| Telegram Bot API | no charge for bots (cost = limits, not money) | `E-09` | — |
| Embeddings (Qwen/DashScope) | **price not verified this session** — `[NEED-PRIMARY]` | — | — |

---

## 1. Load definitions (assumptions, stated so they can be falsified)

| Tier | Requests/day | Requests/month | Renders/month | Concurrent jobs (peak) |
|---|---|---|---|---|
| **SMALL** (single owner) | ≤ 500 | 15,000 | 20 | 1 |
| **MEDIUM** (small community) | 5,000 | 150,000 | 300 | 3–5 |
| **LARGE** (public-ish) | 50,000 | 1,500,000 | 3,000 | 20 |

**Per-request token model** (from verified settings: 20-message window, 3 000-token summarisation threshold, top-3 memories, `M-04`):
input ≈ **1,200 tokens** · output ≈ **250 tokens** · LLM calls per request ≈ **1.5** (Phase 2 assumption `L`).
⇒ per request ≈ **1,800 in / 375 out tokens**.

| Tier | Input tokens/mo | Output tokens/mo |
|---|---|---|
| SMALL | 27 M | 5.6 M |
| MEDIUM | 270 M | 56 M |
| LARGE | 2,700 M | 563 M |

---

## 2. Provider strategies (five, with the trade-off named)

| # | Strategy | Marginal LLM cost | Where it wins | Where it breaks |
|---|---|---|---|---|
| **P1** | **Free-tier chain (current)** — Groq → Gemini free → OpenRouter `:free` | $0 | SMALL | ~1,000 requests/day (Gemini 1,500 RPD ÷ 1.5 calls/request); quotas conflict between sources (`E-06`/`E-07` = `[C]`); failed attempts still consume OpenRouter quota; free tiers may train on data, and `llm_strict_privacy` only removes **OpenRouter** `[V]`, not Gemini free `[V]` |
| **P2** | **Paid primary + free fallback** | $0.0007–0.0028/request | SMALL→LARGE, budget predictability | vendor lock, but the port is a thin litellm layer ⇒ cheap exit |
| **P3** | **Self-hosted small model in-process** (`llama-cpp-python` is *already a dependency*, `S-26`) | $0 marginal tokens | privacy-sensitive SMALL, offline dev | needs RAM/CPU: the free 512 MB/0.1 vCPU instance cannot run it usefully; eLarge 4 GB $21.43/mo *might* serve a 3–4 B quantised model at very low throughput `[U]` |
| **P4** | **Local-first hybrid router** (cheap intents local, hard intents paid) | 30–60% below P2 `[A]` | MEDIUM | requires an intent classifier and a *measured* split; not free to build |
| **P5** | **Batch/off-peak API for non-interactive work** (50% batch discounts on Gemini per `E-17`) | 50% off those calls | summaries, digests, bulk back-fill | does not apply to interactive chat |

**Embedding strategies:** E1 self-host (`sentence-transformers` + `chromadb` are already in the image) — $0 marginal, ~200–400 MB RAM, CPU-bound `[U]`; E2 hosted (price `[NEED-PRIMARY]`); E3 lexeme search (FTS5) as the cost floor. **Recommendation input:** at SMALL/MEDIUM the self-hosted path is dominated by RAM cost, not by token price — which is exactly why the 512 MB free instance is the constraint.

---

## 3. Line-item cost tables

### 3.1 SMALL — 15 k requests/month

| Line | Assumption | Cost/mo |
|---|---|---|
| LLM — P1 | free tier, ~750 calls/day within ~1,500 RPD | **$0** (hard ceiling) |
| LLM — P2 | 27 M in × $0.15 + 5.6 M out × $0.60 | **$7.4** (Gemini 2.0-class) / $20.4 (Groq 70B) / $22.2 (Gemini 2.5 Flash) |
| LLM — P3 | co-located model | $0 + instance upgrade |
| Embeddings | self-host (E1) | $0 + RAM |
| DB | Neon Free, idle ≈ 0.05–0.25 CU × active hours ⇒ inside 100 CU-hours | **$0** `[A]` |
| Queue | in-process (no broker) | **$0** — *and this is a risk, not a saving* (Phase 4/5) |
| Storage | R2 ≤ 10 GB free tier: ~20 renders ≈ 0.3 GB + dumps ≈ 1.5 GB | **$0** |
| Network | R2 egress $0; Koyeb ≤ 100 GB | **$0** |
| Observability | Grafana Cloud Free (10 k series, 50 GB logs) | **$0** |
| Compute | Koyeb free web instance (512 MB, 0.1 vCPU) — cold starts accepted | **$0** — with the Phase 1/4 consequences |
| Telegram | n/a | **$0** |
| **Total** | P1 / P2 | **$0–7.4/mo** (P1) · **$7.4–22/mo** (P2) |

### 3.2 MEDIUM — 150 k requests/month

| Line | Assumption | Cost/mo |
|---|---|---|
| LLM — P2 | 270 M in / 56 M out | **$74** (2.0-class) · $221 (2.5 Flash) · $203 (Groq 70B) |
| LLM — P4 | 40% local-first split `[A]` | **$44–133** |
| LLM — P1 | **infeasible**: 7,500 calls/day vs 1,500 RPD Gemini | **quota cliff, not a price** |
| Embeddings | self-host on the app instance | $0 + ~0.4 GB RAM `[U]` |
| DB | Neon: ~0.25 CU average × 720 h = 180 CU-hours ⇒ exceeds Free by ~1.8× | **$0 if idle-heavy, else ≈ (180−100) × $0.106 ≈ $8.5**; if always-on at 0.25 CU the whole 180 CU-hours bill ≈ **$19** (`E-01`) |
| Storage | 300 renders × 15 MB ≈ 4.5 GB + dumps | **$0.07** (mostly inside free tier) |
| Network | ≤ 100 GB egress on Koyeb; R2 free | **$0** |
| Observability | Free tier (log volume ≈ 600 MB/mo `[A]`) | **$0** |
| Compute | must leave the free instance: **eMedium 2 GB $10.71** (or eSmall 1 GB $5.36 with renders offloaded) | **$5.4–10.7** |
| Workers | renders are CPU-bound; a second small worker is the honest minimum at 300 renders/mo | **+$5.4** `[A]` |
| **Total** | | **≈ $90–250/mo**, spread driven entirely by model choice and Neon duty cycle |

### 3.3 LARGE — 1.5 M requests/month

| Line | Assumption | Cost/mo |
|---|---|---|
| LLM — P2 | 2,700 M in / 563 M out | **$743** (2.0-class) · $2,214 (2.5 Flash) · $2,038 (Groq 70B) |
| LLM — P4 | hybrid `[A]` | **$450–1,300** |
| LLM — P3 | self-host only | technically possible, ~**$50–300** of GPU-class compute `[U]` — not available on this platform as configured |
| Embeddings | 1.5 M requests × ~300 embedding tokens ≈ 450 M tokens — self-host RAM/CPU becomes real | **$0–? `[NEED-PRIMARY]`** if hosted |
| DB | Neon autoscale to 2 CU under load ⇒ up to 1,440 CU-hours ≈ **$152** + storage (5 GB × $0.35 = $1.8) | **≈ $154** |
| Queue | still in-process — at this tier it is no longer a saving but a **correctness ceiling** (Phase 4 C-02/C-04) | $0 + engineering |
| Storage | 3,000 renders × 15 MB ≈ 45 GB + dumps ⇒ 45 GB × $0.015 = $0.68 + Class A ops ≈ $0.02 | **≈ $0.7** |
| Network | 45 GB up to Telegram + 100 GB allowance: still inside, but the margin is thin; R2 egress $0 | **$0** (until it is not) |
| Observability | free tier 10 k series is reachable with per-user metrics; expect **Pro $19 + ~$5–20 usage** at LARGE `[A]` | **$0–40** |
| Compute | eLarge 4 GB $21.43 × 2 (one web + one render/preview worker) | **$42.9** |
| Telegram | free | **$0** |
| **Total** | | **≈ $660–2,400/mo**, i.e. **±3.5× from the model choice alone** |

**Cost per request:** SMALL P2 ≈ **$0.0007–0.0015**; MEDIUM ≈ **$0.0006–0.0017**; LARGE ≈ **$0.0004–0.0016**. The interesting result: *unit* cost is roughly flat across three orders of magnitude — **the model provider, not the scale, sets the price**, so the economics argument for "get bigger" is weak and the argument for "pick the right provider" is strong.

---

## 4. Where the money actually goes / the cliffs

| Cliff | Trigger | Consequence |
|---|---|---|
| **Free-quota exhaustion** | ~1,000 requests/day (Gemini RPD ÷ 1.5) or 6,000 TPM bursts | the chain silently falls to the next provider, and then to `FakeLLM` garbage (S-28) — *cost $0, product broken* |
| **Cold-start tax** | >1 h idle ⇒ scale-to-zero ⇒ Neon wake + Koyeb boot | latency roughly 0.3–1 s extra `[U]`; retries can create duplicate work |
| **Retry storm** | cooldowns reset on restart (A-31) while quota is drained | every request re-probes drained providers; at paid rates this invoices the same work repeatedly (S-24) |
| **Render CPU** | FFmpeg on 0.1 vCPU | 900 s timeout is a *timeout*, not a throughput plan; a render that takes 900 s is billed in wall-clock user-perceived terms and blocks concurrency |
| **Neon duty cycle** | any always-on traffic pattern | flips the DB line from $0 to ~$150/mo — the single largest post-LLM swing in the whole model |
| **Backup duplication** | ambiguous upload outcome (C-14) with timestamp keys | silent double storage + double Class A operations |
| **Log volume** | verbose structured logs × 1.5 M requests | crosses Grafana's 50 GB free tier; observability becomes a line item right where it matters most |
| **Silent model retirement** | `gemini-2.0-flash` lifecycle status is `[C]` across sources (`E-17`); the app pins that exact model (`M-04`) | if retired, a whole leg vanishes and the chain's remaining capacity collapses; there is **no alert** for "a configured model is gone" — the symptom is quota exhaustion |

---

## 5. Cost-risk register (things that look free and are not)

| Item | Why it is not free |
|---|---|
| In-process queue | "no broker" saves ~$15/mo and costs duplicate effects + stuck jobs (Phases 4/5) |
| Free-tier LLM chain | the cost is *availability* and *privacy*: ~1,000 req/day ceiling, conflicting quota reports, and `llm_strict_privacy` does not remove the Gemini free leg `[V]` |
| Scale-to-zero | the cost is state amnesia (Phase 7) and cold-start latency |
| 512 MB free instance | the cost is RAM: FFmpeg + embeddings + Chroma do not coexist comfortably; OOM is the hidden invoice |
| Self-hosted embeddings | $0 tokens, real RAM/CPU on an instance that has neither to spare |
| Egress | R2 is $0 and Koyeb allows 100 GB — but *both* are quotas, and the failure mode at the limit is a hard stop, not a bill |

---

## 6. FACTS vs ANALYSIS

**FACTS (dated, per §0).** Platform prices and quotas for Neon, Koyeb, R2, and Grafana Cloud are recorded with source dates; the LLM paid rates are `[C]`-flagged because independent sources disagree and the vendor quota/pricing pages for the free tiers could not be fetched directly; embedding pricing is `[NEED-PRIMARY]`. The app's own knobs (message window, summarisation threshold, top-k, model names, timeouts) are verified from the pinned tree (`M-04`).

**ANALYSIS.**
1. **The architecture's cost profile is not "cheap" — it is "unpriced".** SMALL genuinely costs $0, and that is the reason the correctness gaps survive (nothing forces a durable queue, a lease, or a backup when the bill is zero). The first real invoice arrives at MEDIUM, and it arrives as a *cliff*, not a slope.
2. **At every tier, a single decision dominates:** the LLM provider/model. Everything else (DB, storage, compute, observability) is 5–15% of the bill at MEDIUM and LARGE. Therefore the highest-leverage cost work is **making the model choice measurable** — which requires the call ledger that Phase 5 finding X-07 says does not exist.
3. **The cheapest defensible configuration at MEDIUM is roughly $90–150/mo** (2.5-Flash-class or a cheaper equivalent + eSmall/eMedium + Neon Free/Launch on an idle-heavy pattern + everything else on free tiers). The most expensive plausible configuration is ~$250 without changing a single line of architecture — the gap is entirely procurement, which means the cost model cannot be used to justify architectural change, only to *fund* it.
4. **Do not scale an unpriced system.** Because duplicates and retry storms are invisible (no ledger), the current design *cannot answer* "did last month's $200 come from users or from retries?" — so the honest recommendation is to add accounting **before** increasing spend, not after.
