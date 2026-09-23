# RESEARCH V2 — INDEPENDENT ARCHITECTURE LAB

**Status:** COMPLETE (12/12 phases + Phase-13 target architecture + executive) · **Date:** 2026-09-23 · **Namespace:** `research/v2/` only
**Repo pin:** `main` @ `a997aab` (v3.13.0 line) · **Mode:** READ-ONLY, zero product modification
**Predecessor:** Research v1 (10-phase study) is treated as **input**, not repeated. This lab *audits* it.

---

## 1. What this lab is (and is not)

This is **architectural due diligence**: an attempt to find where the system breaks, why, how we would know, and how we would recover — *before* the main team pays for the discovery in production.

| This lab IS | This lab IS NOT |
|---|---|
| An audit of **claims vs code** (docs say X, the tree shows Y) | A feature proposal or a redesign |
| A **failure/consistency/security/cost** stress test of the current design | An implementation plan, migration, or PR |
| A register of **decisions that must be made** (ADR candidates, unknowns) | A decision. No "best architecture" is declared anywhere in v2 |
| Explicit about `[V]` verified / `[A]` assumed / `[U]` unknown / `[C]` contradicted | A marketing summary. Bad news is stated as plainly as good news |

**Non-negotiable findings of this lab (headline):**

1. **The queue is not durable in the documented production topology.** `job_queue_db_path(settings.db_path)` (`R-02`, `R-03`) always writes the job sidecar to the *local disk*, while the documented Koyeb topology says local disk is ephemeral (`R-21`, `R-23`). `koyeb.yaml` mounts no volume and sets no `NEXUS_DATABASE_URL` (`R-20`). The docs even contradict **each other**: `DATA_AND_STORAGE.md:94` calls the sidecar durable while the deploy runbook's incident table admits "state lost after idle — scale-to-zero wiped local disk", and its recommended remedy (`NEXUS_DATABASE_URL`) does not relocate the queue. ⇒ jobs and their terminal states can vanish on scale-to-zero/redeploy.
2. **Two persistence planes exist at once.** 14 feature modules open their **own sync SQLite engines** on `settings.db_path` (`M-05`, `R-08`), while the rest of the app routes through `storage/db.py` (`R-06`) — which, when `NEXUS_DATABASE_URL` is set, is **PostgreSQL**. Some of those modules write models that are *also* served from Postgres ⇒ the same logical table is written to two stores that never reconcile.
3. **`idempotency_key` exists in two ports with no mechanism behind it.** `LLMPort.complete(..., idempotency_key=…)` has zero consumers (`M-07`), and `ObjectStoragePort.put(content: bytes, idempotency_key)` is not implemented by `R2Provider` (`M-08`). Idempotency is currently a *signature*, not a guarantee.
4. **The strongest security boundary has a sibling that is not guarded.** `core/ssrf_guard.py` is genuinely good (`R-14`), but `/creative/video-edit` downloads a caller-supplied URL with a plain `httpx` client, `follow_redirects=True`, no SSRF validation and no size cap (`R-04`) — behind HMAC, so exploitation needs key misuse/leak, but `SECURITY.md` states the SSRF class is "closed" (`R-21`).
5. **Anti-retry-storm cooldowns are process-lifetime; the deployment is scale-to-zero.** `86400 s` cooldowns and the in-process rate limiter (`R-12`, `R-13`) are lost on every cold start — the exact moment a drained quota is most likely to be hammered again.

All five are developed with evidence in the phase documents, and ranked in [`RESEARCH_V2_EXECUTIVE.md`](RESEARCH_V2_EXECUTIVE.md).

---

## 2. Phase index

| # | Phase | Document | Deliverable shape | Headline number |
|---|---|---|---|---|
| 1 | Architecture Assumption Audit | [`01-assumption-audit.md`](01-assumption-audit.md) | ASSUMPTION / EVIDENCE / WHY WRONG / DISPROVER / TEST / RISK | **36 assumptions**, 11 high-risk |
| 2 | Bottleneck Forensics | [`02-bottleneck-forensics.md`](02-bottleneck-forensics.md) | 6 load tiers × 10 resource axes | first hard wall at **~1 K msgs/day** (provider quota), not at CPU |
| 3 | Failure Mode Analysis (FMEA) | [`03-fmea.md`](03-fmea.md) | Component / Failure / Trigger / Impact / Detection / Recovery / Data-loss / Security / Ops / Mitigation | **45 failure modes**, 12 with data-loss risk |
| 4 | Data Consistency Lab | [`04-data-consistency.md`](04-data-consistency.md) | STATE BEFORE → EVENT → STATE AFTER → INCONSISTENCY → RECOVERY | **18 scenarios** (12 required + 6 added); 5 verified positives kept |
| 5 | Exactly-Once Truth Lab | [`05-exactly-once.md`](05-exactly-once.md) | Guarantee / Boundary / Failure case / Required mechanism × 10 surfaces | only **2 of 10** surfaces are *effectively-once* (artefact publish, checkpoint delete) |
| 6 | Security Red Team | [`06-red-team.md`](06-red-team.md) | Attack / Precondition / Exploit path / Impact / Detection / Mitigation / Residual | **35 attack scenarios** in 4 precondition classes (LIVE / KEY / INSIDER / LATENT); 10 verified positives |
| 7 | Disaster Recovery | [`07-disaster-recovery.md`](07-disaster-recovery.md) | Detection / RTO / RPO / Recovery / Data loss / Manual | **15 scenarios**; RTO/RPO **UNDEFINED** wherever the repo is silent; no restore path exists |
| 8 | Cost Forensics | [`08-cost-model.md`](08-cost-model.md) | SMALL/MEDIUM/LARGE × 8 line items × **5 provider strategies**, every price source-dated | SMALL **$0–22/mo**, MEDIUM **$90–250/mo**, LARGE **$660–2 400/mo**; **±3.5× from model choice alone** |
| 9 | Architecture Evolution | [`09-evolution-map.md`](09-evolution-map.md) | Stage A→B, B→C transitions: what breaks, why, migration pressure/option, back-compat | **10** A→B breakages, **9** B→C breakages, 8-step push order, back-compat matrix |
| 10 | ADR Candidate Register | [`10-adr-candidates.md`](10-adr-candidates.md) | ADR ID / question / current state / options / evidence needed / trigger / risk | **28 candidates** (none decided, none finalised) |
| 11 | Contradiction Engine | [`11-contradictions.md`](11-contradictions.md) | CLAIM A/B + SOURCE A/B + why different + verifiable + status | **16 open contradictions** (`K-01`…`K-16`) + **3 resolved** non-contradictions |
| 12 | Unknown Register | [`12-unknowns.md`](12-unknowns.md) | UNKNOWN / why / how to verify / who / when it matters | **36 unknowns** in 6 families, each with owner class + closure cost |
| — | Synthesis | [`RESEARCH_V2_EXECUTIVE.md`](RESEARCH_V2_EXECUTIVE.md) | the ten lists + pressure points + next-phase questions | — |
| 13 | Target Architecture (proposal) | [`13-target-architecture.md`](13-target-architecture.md) | one truth plane + four primitives (claim/lease, inbox, outbox, effect-key) + 5 layers + M0–M6 roadmap + global prior art | engineered proposal, **no decision taken** |
| — | Source registry | [`SOURCES.md`](SOURCES.md) | every id used above | 26 repo sources (`R-01`…`R-26`), 8 re-measurements, **21 external sources** (`E-01`…`E-21`), 7 unverifiables |

---

## 3. Marker legend (used identically in every file)

| Marker | Meaning | Consequence for the reader |
|---|---|---|
| `[V]` | Verified in this session from a named source (`R-*`/`E-*`/`M-*`) | Treat as fact-at-pin |
| `[H]` | Historical: true at an earlier pin (source says so) | Do not assume it is still true |
| `[A]` | Assumed/derived: arithmetic or reasoning with the method shown | Attack the method, not just the number |
| `[U]` | Unverified: could not be confirmed with available access | Do not build on it; see `12-unknowns.md` |
| `[C]` | Contradicted: two sources disagree (both named) | See `11-contradictions.md` |
| `[NEED-PRIMARY]` | Only secondary sources found | Verify against the vendor console/page before quoting |
| `UNDEFINED` | The system defines no value for this (deliberately or by omission) | A gap, not a measurement |

**Severity convention (no taste-based ranking).** Where a severity is used it is derived from a *defined* rule, stated at the point of use:

| Level | Definition used in v2 |
|---|---|
| **S1 — Integrity** | Causes silent loss/duplication of user-visible data, or an incorrect durable state that a user can observe |
| **S2 — Availability** | Stops or degrades the service for ≥1 user without data loss |
| **S3 — Security** | Crosses a trust boundary: unauthorised read/write/execution/egress |
| **S4 — Operability** | Slows or blocks recovery/change, or destroys the evidence needed to diagnose |
| **S5 — Cost** | Converts to money or quota without an availability/integrity symptom |

---

## 4. Method (reproducible)

1. **Pin.** All repo claims resolved at `a997aab`; every citation names a path.
2. **Re-measure what docs assert.** Where a doc gives a countable fact, re-run it (`M-01`…`M-08`); disagreements are logged as contradictions, not silently corrected.
3. **Read the mechanism, not the docstring.** For each guarantee (idempotency, at-least-once, durability, fail-closed) the *implementation* was read until the mechanism was located — or its absence established (`M-07`, `M-08`, `R-01`).
4. **Separate fact from analysis.** Every file has a FACTS block and an ANALYSIS block; no analysis number is presented as a repo fact.
5. **Separate current from historical.** The 2026-09-21 audit (`R-24`) is treated as *history*: its findings are re-tested against today's tree before being carried forward, and the ones already closed are marked `[H]` rather than repeated.
6. **Externals carry dates.** Prices/quotas cite the page age from the search tool; where independent sources disagree the claim becomes `[C]` and enters the contradiction engine instead of the cost model.
7. **No recommendations phrased as conclusions.** Options are enumerated in `10-adr-candidates.md`; the choice is left to the owners.

## 5. Isolation report

| Check | Result |
|---|---|
| Files created | `research/v2/*.md` (14 documents + this index) |
| Files modified outside `research/v2/` | **none** |
| `src/`, `bot/`, `features/`, `api/`, Nagar (`creative/`), `handlers.py`, `app.py` | untouched (read-only) |
| Board (`.agents/board.json`), tasks 159/160/161, PR#47, PR#33 | untouched |
| Migrations, dependencies (`pyproject.toml`), CI workflows | untouched |
| Commits/pushes/merges | none (per isolation rules; work stays in the working tree) |
