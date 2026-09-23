# RESEARCH V2 — EXECUTIVE SYNTHESIS

**Subject:** `nexus-ai-agent` (Telegram assistant + Nagar creative studio) at branch `arena/01a0cca8-nexus-ai-agent`, HEAD `a997aab` (merge PR#55, 350 commits after `7573249`).
**Method:** read-only inspection of the pinned tree + primary/independent external sources; every mechanism carries an evidence id from `SOURCES.md`; every number is `ASSUMED` or `VERIFIED`; unprovable statements are `[U]` and named as unknowns.
**Isolation:** no product source, board, migration, dependency or PR was modified. All outputs live in `research/v2/`. No claim of a "best architecture" is made anywhere in this package.
**Package:** 12 phase files + source registry + lab index + this synthesis (**15 files total**). Evidence ids referenced below are defined in the phase files and `SOURCES.md`.

**Corpus counts:** 36 assumptions audited · 45 failure modes · 18 consistency scenarios · 10 delivery surfaces · 35 attacks · 15 disaster scenarios · 28 ADR candidates · 16 open contradictions (+3 resolved) · 36 unknowns · 21 external sources + 7 explicitly unverifiable claims · 26 repo sources + 8 re-measurements.

---

## 1. Ten architecture risks (the ones that decide outcomes)

| # | Architecture risk | Evidence | Severity per S1–S5 rules | Phase |
|---|---|---|---|---|
| **AR-01** | **The job queue is not durable in the documented topology.** A SQLite sidecar on an ephemeral disk, in a service with no volume, behind scale-to-zero — while `DATA_AND_STORAGE.md:94` asserts "never on local disk" and the runbook's own incident table admits "state lost after idle — scale-to-zero wiped local disk". The recommended fix (set `NEXUS_DATABASE_URL`) does **not** move the queue: its path is `job_queue_db_path(settings.db_path)`. | `K-01` (docs-vs-docs), `F-04`, `C-04`, `A-02`, `R-20`, `E-02` | S1 (user-visible loss) + S4 (operability) | 1, 3, 4, 11 |
| **AR-02** | **Two persistence planes.** 14 feature modules own sync SQLite engines while `storage/db.py` may be writing PostgreSQL; 3 tables are created by raw DDL outside Alembic; CI grandfathers 37 boundary violations. | `K-02`, `C-16`, `F-09/F-10/F-11`, `M-05`, `A-09/A-10` | S1 (data appears/disappears by read path) | 1, 3, 4, 11 |
| **AR-03** | **No atomic claim on jobs.** `_mark_processing` accepts `pending` **or** `processing` and checks no affected row; duplicate protection is an in-process dict; `resume_pending()` re-arms `processing` rows at start-up. | `X-03`, `C-02`, `F-01/F-02`, `A-04/A-05` | S1 (duplicate side effects: sends, uploads, XP) | 4, 5 |
| **AR-04** | **Coordination state lives in process memory.** Rate-limit windows, provider cooldowns (86 400 s, `allowed_fails=1`), in-flight task maps, and the PTB update queue all reset on every cold start — i.e. exactly when drained quota is re-probed. | `F-31/F-36`, `S-24/S-30`, `A-19/A-31` | S5 (cost/quota) + S2 (abuse surface) | 1, 2, 6 |
| **AR-05** | **Commit-then-notify with swallowed failures.** Completion notification runs after the terminal write and cannot fail the job by design ⇒ the durable record says "done" while the user receives nothing. | `C-07`, `X-05`, `F-34-adjacent` | S1 (broken promise, invisible) | 4, 5 |
| **AR-06** | **The public HTTP surface is served by the bot process and is default-open in places.** Webhook mode serves the app that mounts the dashboard (gate is a no-op without a token; `koyeb.yaml` sets none), and `GET /creative/jobs/{id}` has no auth. | `S-01/S-02`, `K-04`-adjacent, `F-23/F-24`, `A-27` | S3 (PII/IDOR disclosure) | 6 |
| **AR-07** | **Interface/implementation mismatch on idempotency.** `ObjectStoragePort.put(..., idempotency_key)` is declared idempotent; `R2Provider.upload(local_path, remote_key)` ignores it; `LLMPort.complete(idempotency_key=...)` has zero consumers. | `X-09`, `K-03`, `M-07/M-08`, `F-18-adjacent` | S4 (duplicates, cost) | 4, 5, 11 |
| **AR-08** | **Timers inside processes with lying rows.** Scheduled posts/reminders are `asyncio` task maps; the DB row records intent only, so a restart silently drops work while the row says `scheduled`. | `X-04`, `C-08`, `F-08-adjacent` | S1 (missed or duplicated scheduled delivery) | 4, 5 |
| **AR-09** | **Unbounded job concurrency on a 0.1 vCPU / 512 MB instance** with a 900 s FFmpeg timeout: a single user's burst can OOM the bot for everyone. | `F-06/F-41/F-42`, `S-07`, `E-02` | S2 (service down) + S4 (disk) | 2, 3, 6 |
| **AR-10** | **No detection plane.** `/healthz` is DB-free and used as the platform gate; counters are in-process; no alert sink; no unattended alerting ⇒ the highest-severity failures are discovered by users. | `F-37/F-38/F-39`, `F-19`, `DR-02`, `A-20/A-35` | S4 (MTTR) — and it degrades every other row in this table | 3, 7 |

**Reading of the set:** AR-01, AR-03, AR-05, AR-08 are four faces of one missing primitive (a durable claim + effect key). AR-02 and AR-07 are interface/implementation gaps. AR-06 and AR-09 are surface/configuration. AR-10 is what makes all of them survivable-but-invisible.

---

## 2. Ten dangerous assumptions (highest leverage first)

| # | Assumption believed today | Why it is dangerous | Evidence | Status |
|---|---|---|---|---|
| **DA-01** | "The queue sidecar survives restarts in webhook mode" | it is the product's promise surface; false in the documented topology | `A-02` `[C]` | contradicted by `koyeb.yaml` + platform facts |
| **DA-02** | "`_mark_processing` claims a job" | it does not exclude a concurrently-running job and checks no row count | `A-04` `[V]` | single-process guard only |
| **DA-03** | "A completed job never re-runs" | `resume_pending` re-arms anything unfinished, so a duplicate is one restart away | `A-05` `[V]`, `F-02` | conditional, not absolute |
| **DA-04** | "One logical data model, two interchangeable backends" | two planes are written by different engines; reads silently diverge | `A-09` `[C]`, `F-09/F-10` | false in PG mode |
| **DA-05** | "Alembic is the only schema authority" | raw runtime DDL in feature modules + adapter-owned tables | `A-10` `[V]`, `F-11` | false |
| **DA-06** | "Backups make data recoverable / restore exists" | no restore entry point, no drill, no RTO/RPO; the prune job is the only consumer | `A-13/A-14` `[V]`/`[U]`, `K-12` | capability vs hope |
| **DA-07** | "Retries are safe" | no effect-level idempotency on the paths that retry (queue, notify, blob, LLM) | `A-18` `[V]`, `X-01…X-07` | false |
| **DA-08** | "Scale-to-zero has no data consequence" | it resets limiter/cooldowns/timers/update queue and may discard the queue file | `A-19` `[V]`, `K-01` | false |
| **DA-09** | "The manifest reflects production" | the manifest omits the external DB, a volume, an instance class and the dashboard token | `A-22` `[V]`, `M-04`, `R-20` | unverified in the console |
| **DA-10** | "Free tiers are sufficient (and their limits are known)" | independent sources conflict on the very quotas the design depends on; `strict_privacy` covers only OpenRouter free | `A-32` `[C]`, `K-05`, `E-06/E-07` | `[NEED-PRIMARY]` |

*(Other high-severity audit rows carried forward: `A-23` multi-replica as "future concern", `A-26` "SSRF class closed", `A-33` "local models bound the worst case" — the latter is false in this container topology.)*

---

## 3. Ten unknowns that most change the picture

| # | Unknown | Why it matters | Closes in |
|---|---|---|---|
| **U-01** | Is `NEXUS_DATABASE_URL` set in production? | decides live vs latent for AR-01/AR-02 | one console read |
| **U-02** | Is a volume attached? | same | one console read |
| **U-04** | Webhook or polling mode? | different failure modes; polling + replicas = update theft | one Bot API call |
| **U-07** | Is the backup workflow actually running? | the only durability story for PG | one page load |
| **U-12** | Has a restore ever been performed? | an untested backup is a hope | one drill |
| **U-14** | Do the queue file and the backed-up DB file coincide? | decides whether backups protect the right artefact | one config comparison |
| **U-17** | Is `/api/dashboard/*` reachable unauthenticated right now? | highest-likelihood real incident | one authorised request |
| **U-24** | What quotas are *actually* enforced for this account? | the entire SMALL-tier design rests on them | provider console |
| **U-26** | How long does one render really take on the deployed instance? | decides render capacity and the 900 s question | three timed renders |
| **U-36** | Who responds at 03:00? | every recovery procedure assumes a human | one written answer |

---

## 4. Ten failure modes (by data-loss/blast-radius, not by taste)

| # | Failure mode | Mechanism | Data loss | Severity |
|---|---|---|---|---|
| **FM-01** | Job sidecar lost on scale-to-zero/redeploy | `F-04` | **Total** (queue state + terminal results) | S1 |
| **FM-02** | Same job executed twice (in-process or cross-process) | `F-01/F-02` | None (duplicated effects) | S1 |
| **FM-03** | Job silently never runs; row stuck `processing` | `F-03` | None (lost work) | S1 |
| **FM-04** | Same logical table written to two stores | `F-09/F-10` | Partial→Total per table | S1 |
| **FM-05** | Raw DDL creates a shape the model layer disagrees with | `F-11` | Partial (drop/recreate) | S1 |
| **FM-06** | Backup green but useless (wrong store / never restored) | `F-18` | Partial, discovered at incident time | S1 |
| **FM-07** | Backup workflow silently stops | `F-19` | Total since last success | S2 |
| **FM-08** | SQLite corruption (local, non-WAL paths) | `F-17` | Total since last dump | S1 |
| **FM-09** | Forward-only migration + rollback | `F-40` | Partial | S1 |
| **FM-10** | Update accepted (200) then lost / retried and duplicated | `F-34/F-35` | Partial (lost intent) / duplicated effects | S2 |

*(Detection is absent for FM-02, FM-03, FM-06, FM-07 and FM-10 — the Phase 3 detection-gap table lists five missing counters/signals.)*

---

## 5. Ten security concerns (expected-loss order)

| # | Concern | Precondition | Impact | Residual after a reasonable fix |
|---|---|---|---|---|
| **SC-01** | Unauthenticated dashboard PII read on the bot's public port | token unset (default; absent from the manifest) | S3 | low (one env var) |
| **SC-02** | SSRF via `POST /creative/video-edit` (`video_url`, plain httpx, redirects, no size cap) | HMAC key held by any caller | S3 + disk fill | low (route through the existing guard) |
| **SC-03** | Unauthenticated creative job read (IDOR-shaped) | port reachable + a leaked job id | S3 | low |
| **SC-04** | Prompt injection from ingested URL/document content (OWASP LLM01) | normal feature use | S3 | **medium — no complete mitigation exists** |
| **SC-05** | Quota/DoS by an authorised user (unbounded job concurrency, no per-user budget) | any allow-listed user | S2/S5 | medium (cap + budget primitive) |
| **SC-06** | Webhook secret replay/forgery ⇒ owner-privileged commands | secret leak | S3 | medium (rotation + receipt ledger) |
| **SC-07** | Cost/quota drain (LLM10 Unbounded Consumption) with cooldowns that reset | one long loop | S5 | medium (externalised health + ledger) |
| **SC-08** | Secret sprawl: one HMAC key reused for signing; flat env scopes | any single leak | S3 | low–medium (per-purpose keys/rotation drill) |
| **SC-09** | Supply chain: no lock file/SBOM, native builds from ranges | upstream compromise | S3 | medium (pin + SBOM) |
| **SC-10** | Replica-introduced control divergence (limiter/claims/cooldowns per process) | a second instance exists | S1/S5 | medium (Stage-B primitives) |

**Verified positives worth preserving:** deny-by-default Telegram gate, constant-time webhook comparison, fail-closed HMAC (503 when unset), DNS-rebinding-resistant validating transport, `safe_join` path handling, thread-scoped memory queries, no `print()` leakage in `src/`.

---

## 6. Ten ADR candidates (of 28; candidates only — no decisions taken)

| # | Candidate | Trigger | Why it is on this list |
|---|---|---|---|
| **ADR-C-01** | Queue durability contract with the platform | now (promise contradicts topology) | AR-01 |
| **ADR-C-02** | Atomic claim + lease primitive | now | AR-03, AR-08 |
| **ADR-C-05** | Ingress receipt ledger & ack timing | now (loss + duplication both possible) | SC-06, FM-10 |
| **ADR-C-06** | Notification outbox | first "done but nothing arrived" | AR-05 |
| **ADR-C-09** | Provider health/quota state location | first all-fallback window | AR-04, SC-07 |
| **ADR-C-10** | Privacy semantics of the chain | before a privacy-sensitive user | `K-05`, DA-10 |
| **ADR-C-12** | Cost accounting ledger | first paid leg | Phase 8 §6 (cannot price the current design) |
| **ADR-C-13** | Data-plane unification (14 modules) | first vanished-data report | AR-02 |
| **ADR-C-15** | Restore path + RTO/RPO | now | DA-06, DR gaps |
| **ADR-C-18** | HTTP default-deny | now (public reachability) | SC-01, SC-03 |

---

## 7. Scalability pressure points (Phase 2 in one table)

| Load | First binding constraint | Nature of the limit |
|---|---|---|
| ~10/day | nothing (comfortable) | free-tier and cold-start latency dominate |
| ~1,000/day | cold start + ephemeral disk + 512 MB RAM | **correctness**, not throughput |
| 1K–10K/day | Neon 0.5 GB free tier + Gemini ~1,500 RPD (≈1,000 requests at 1.5 calls each) | **quota cliff**, silent fallback |
| 10K–100K/day | split persistence + unbounded job concurrency + 0.5–2 GB disk | correctness and OOM; free tiers become unusable |
| ~2.6 M msgs/day | single Telegram token's practical per-second ceiling | hard external ceiling (`E-09`) |
| ≥ LARGE | everything above is priced, not engineered | Phase 8: ±3.5× cost swing from the model choice alone |

**Pressure-point summary:** the first three orders of magnitude are gated by *durability and quota*, not by CPU or bandwidth. The architecture's scaling story is therefore "get the primitives right" long before it is "get more machines".

---

## 8. Data-consistency pressure points (Phase 4)

| Pressure point | Scenario | Consequence |
|---|---|---|
| Multi-step operations with no unit of work | `C-03`, `C-07` | durable "done" without delivery |
| Sequence of independent transactions (queue lifecycle) | `C-02`, `C-04` | duplicated execution / permanently stuck rows |
| Two planes, one logical table | `C-16` | read-after-write violation, invisible |
| Wall-clock ordering and day boundaries | `C-10`, `C-11` | inverted job order; double/skipped daily awards |
| Migrations without expand/contract enforcement | `C-12`, `C-13` | deploy-window outages; two-host DDL race |
| Ambiguous external writes | `C-14` (R2), `C-05/C-06` (LLM) | silent duplicate cost; unverifiable spend |

**Verified positives to protect:** atomic `.part`→rename publication (`R-15`), checkpoint deletion with a required idempotency key (`R-21`), idempotent re-runnable migrations with a CI stamp assertion (`R-19`), and a fail-closed consent gate.

---

## 9. Disaster-recovery gaps (Phase 7)

| Gap | Statement | Cheapest closure |
|---|---|---|
| **G-1** | **No RTO and no RPO exist anywhere in the repo** — the contingency *requirement* has never been written | one owner decision, recorded |
| **G-2** | **No restore path at all**; the backup's only consumer is the prune job | one CLI path + one drill (also yields RTO) |
| **G-3** | Backup staleness is unmonitored (a silently disabled workflow is invisible) | one "no dump in 36 h" alert |
| **G-4** | The local stores that hold promises (queue, creative jobs, checkpoints) are **not backed up** | external DB required, or volume, or explicit acceptance |
| **G-5** | `/healthz` cannot fail for a DB outage, so the platform reports healthy | add `/readyz` without breaking liveness |
| **G-6** | Credential rotation is manual, untested, and per-secret with no rotation log | one rotation drill per secret; a documented blast-radius map |
| **G-7** | Redis is deliberately absent — the DR checklist item is **N/A** (the coordination state it would hold has no durable home) | decide the home (ADR-C-09) |
| **G-8** | Succession: console-side ownership (Koyeb/Neon/R2/BotFather) is undocumented | one page in `docs/ops/` |

---

## 10. Next-phase research questions (RQ)

| # | Question | Why it is the next research step |
|---|---|---|
| **RQ-01** | What are the *measured* request/day, token and render statistics of production? | every Phase 2/8 number is currently an assumption; requires the ledger (ADR-C-12) or provider dashboards |
| **RQ-02** | Does a crash-injection matrix (kill at each step of each flow) produce the states predicted in Phase 4? | converts 18 scenarios from analysis into evidence |
| **RQ-03** | What does one render cost in wall-clock and bytes across instance classes? | answers U-26 and the render-capacity plan |
| **RQ-04** | What are the vendors' *current* data-use terms per free leg, and is `gemini-2.0-flash` still served? | resolves `K-05`, `K-10`, DA-10 with primary sources |
| **RQ-05** | With a PG-backed queue prototype, what are the real limits of `SKIP LOCKED` at 10K–100K jobs/day? | decides ADR-C-03 without adopting a broker |
| **RQ-06** | Can an authorised prompt-injection harness exfiltrate data through the summariser? | tests SC-04 empirically instead of citing OWASP |
| **RQ-07** | What is the measured RTO of a restore into a scratch environment? | turns G-2 into a number |
| **RQ-08** | Which of the 14 feature modules are alive in production, and which tables do they actually write? | scopes ADR-C-13 before anyone touches it |
| **RQ-09** | Can a CI fitness function gate two or three load-bearing documentation numbers? | prevents the entire drift family (`K-03`, `K-07`, `K-09`) |
| **RQ-10** | What is the minimum viable Stage-B configuration (multi-instance) that keeps every invariant? | prepares ADR-C-25 with evidence instead of opinion |

---

## 11. What this package does **not** claim

1. **No "best architecture" is declared.** Every phase answers *where it breaks / why / how we would know / how we would recover*; no phase ranks architectures or recommends a target design.
2. **No ranking by taste.** Severity uses explicit definitions (`README.md` §3), failure modes carry data-loss and reachability labels rather than opinion scores, and unknown quantities stay `[U]`/UNDEFINED rather than being estimated into confidence.
3. **No product change was made or proposed as a change request.** The ADR candidates are *questions with evidence requirements*; the mitigations are mechanisms, not tickets.
4. **Numbers that could not be sourced are named, not filled in**: quotas (`E-06`/`E-07` conflict), embedding price (`E-16`), model lifecycle (`E-17` conflict), the seven `[U]` claims in `SOURCES.md` §4.

---

## 12. Handoff (for the main team)

- **If only one hour is available:** close `U-01`, `U-02`, `U-04`, `U-07`, `U-17`, `U-24`, `U-30`, `U-36` — eight console/calls/questions that re-severity half of this package.
- **If only one code change is available:** the atomic claim + lease (`ADR-C-02`) — it is additive, it fixes a verified duplicate-execution mechanism, and it is a prerequisite for every later stage.
- **If only one operational artefact is available:** the restore drill (`G-2`, `U-12`) — it converts "we have backups" into "we can recover", and produces the RTO number that does not exist.
- **Read order for the package:** `README.md` → `RESEARCH_V2_EXECUTIVE.md` (this file) → the phase file matching the question at hand. Every phase is self-contained, cites its evidence, and marks its own uncertainty.

---

# RESEARCH V2 COMPLETE

| Stage | Status | Source count (distinct registry ids cited) | Findings | Unknowns | Confidence |
|---|---|---|---|---|---|
| **Sources & method** (`SOURCES.md`, `README.md`) | Complete | 26 repo (R-01…R-26) · 8 re-measurements (M-01…M-08) · 21 external (E-01…E-21) · 7 unverifiable (U-a…U-g) | evidence classes, isolation attestation, marker/severity conventions | 7 explicitly unverifiable claims | HIGH (structure is mechanical) |
| **01 Assumption audit** | Complete | ~24 repo/external ids | 36 assumptions (11 High / 16 Medium / 9 Low) + facts F1–F11 | assumptions marked `[C]`/`[U]` are unknowns, not verdicts | HIGH for `[V]` rows; MEDIUM where marked `[C]` |
| **02 Bottleneck forensics** | Complete | ~20 ids (+E-01…E-09) | 11 ceilings (W1–W11), 6 load tiers 10→1M req/day | 3 falsifiers (U-08…U-10) + load assumptions stated in-file | MEDIUM (load levels are assumptions; ceilings verified) |
| **03 FMEA** | Complete | ~30 ids | 45 failure modes F-01…F-45, 12-mode data-loss subset, detection-gap table | detection signals that do not exist (named individually) | HIGH (mechanisms) / MEDIUM (frequency) |
| **04 Data consistency** | Complete | 10 ids (+M-05, M-08) | 18 scenarios C-01…C-18 with STATE BEFORE/EVENT/AFTER/INCONSISTENCY/RECOVERY; 5 verified positives | frequency of clock/contention events | HIGH (mechanisms) |
| **05 Exactly-once** | Complete | 12 ids (+E-19, E-20, E-09) | 7 primary + 3 secondary surfaces X-01…X-10; 2 of 10 are effectively-once | vendor-side idempotency unknown (named) | HIGH (definitions from primary sources) |
| **06 Red team** | Complete | 10 ids (+E-10, E-13) | 35 attacks S-01…S-35 in 4 precondition classes (incl. tool poisoning, race-as-attack, hostile responses); 10 verified positives | real abuse frequency; mock/live parity | HIGH for LIVE/KEY rows; MEDIUM for LATENT |
| **07 Disaster recovery** | Complete | 10 ids (+E-01, E-11) | 15 scenarios DR-01…DR-15; RTO/RPO **UNDEFINED**; capabilities inventory; safety-net failure table | all RTO/RPO cells (by definition) | HIGH (absence is verified); UNDEFINED where the system is silent |
| **08 Cost model** | Complete | ~12 ids (+E-16, E-17, E-18) | SMALL/MEDIUM/LARGE line items; 5 provider strategies; 8 cost cliffs; SOURCE DATE on every price | quotas (`[C]`), embedding price, real token counts | MEDIUM — prices dated, quotas conflicting, marked `[NEED-PRIMARY]` |
| **09 Evolution map** | Complete | 15 ids (+E-15, E-21) | Stages A→B→C; 10 B-breaks, 9 C-breaks, 8-step push order, backward-compat matrix | real DeployOverlap duration; platform replica controls | HIGH (mechanisms) |
| **10 ADR candidates** | Complete | cross-phase | 28 candidates ADR-C-01…ADR-C-28 (no ADR finalised) | the evidence each ADR names as missing | HIGH (questions are grounded) |
| **11 Contradictions** | Complete | 12 ids + docs/code | 16 open (K-01…K-16) + 3 resolved (K-R1…K-R3); K-01 is docs-vs-docs, K-16 is a broken restore pointer | `[NEED-PRIMARY]` on K-05, K-10 | HIGH (both sides quotable) |
| **12 Unknowns** | Complete | cross-phase | 36 unknowns U-01…U-36 in 6 families with owners and closure cost | the unknowns themselves | HIGH (closure actions are testable) |
| **Executive synthesis** | Complete | all of the above | 10×6 top lists + scalability/consistency/DR pressure points + 10 next-phase questions | — | HIGH for evidence-linked rows; MEDIUM where external sources conflict |

Nothing in the product was modified, and nothing in this package constitutes a decision, a plan of record, or a claim of a best architecture.

**RESEARCH V2 COMPLETE**
