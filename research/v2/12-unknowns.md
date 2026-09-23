# PHASE 12 — UNKNOWN REGISTER

**Rule:** an unknown is only useful if someone can close it. Each entry names the **owner class** (who is able to verify it), the **verification action**, and the **moment it becomes important**. Unknowns that this review *could* have closed with repo-only work were closed in Phases 1–11 and do not appear here.
**Count:** **36 unknowns** (`U-01`…`U-36`) in six families.
**Owner classes:** `OPERATOR` (has console/host access) · `OWNER` (product owner / decision-maker) · `MAINTAINER` (can read/run the repo) · `VENDOR` (only a provider's own page/API can answer) · `TIME` (answerable only by observing production over days).

---

## Family D — Deployment reality (what is actually running)

### U-01 — Is `NEXUS_DATABASE_URL` set in production?
- **Why it matters:** it decides whether the app DB, checkpoints and (potentially) the queue are durable or vanish on redeploy — the difference between Phase 4 C-16 being latent or live.
- **How to verify:** read the Koyeb service env (or call an admin-only diagnostic endpoint that prints the resolved backend kind, never the URL).
- **Who:** `OPERATOR`. **When:** immediately — it changes the severity of five findings.

### U-02 — Is a volume attached to the service?
- **Why it matters:** `koyeb.yaml` declares none; if the console attached one, the queue/checkpoint loss story changes substantially.
- **How to verify:** platform dashboard; or observe whether a sentinel file survives a redeploy.
- **Who:** `OPERATOR`. **When:** immediately.

### U-03 — Which instance class is actually deployed?
- **Why it matters:** RAM/CPU set the render ceiling, the embedding/RAG feasibility, and whether OOM is a live risk (`E-02` vs `E-03`).
- **How to verify:** dashboard; or `/proc/meminfo`-style diagnostics via a guarded admin endpoint.
- **Who:** `OPERATOR`. **When:** before any capacity claim is made.

### U-04 — Webhook mode or polling mode in production?
- **Why it matters:** the failure modes differ (X-01); polling with two instances causes update theft (`E-21`); webhook mode exposes the HTTP surface (Phase 6 S-01/S-02).
- **How to verify:** `getWebhookInfo` (one Bot API call) and the deploy command in the dashboard.
- **Who:** `OPERATOR`. **When:** immediately.

### U-05 — Does production actually scale to zero, and how often?
- **Why it matters:** the frequency of state loss (queue, timers, cooldowns) is proportional to this.
- **How to verify:** platform logs/metrics for cold-start counts; or count process-start log lines per week.
- **Who:** `OPERATOR` + `TIME`. **When:** as soon as a week of logs exists.

### U-06 — Does the platform restart the service after OOM, and does the queue file survive it?
- **Why it matters:** OOM during a render is the most likely "crash mid-job" trigger (Phase 4 C-04).
- **How to verify:** look for `OOMKilled`/restart counts in the platform; correlate with job rows stuck in `processing`.
- **Who:** `OPERATOR`. **When:** after the first heavy render month.

### U-07 — Is CI/maintenance actually green and running?
- **Why it matters:** backups (2×/day) are the *only* durability story for PG; a disabled workflow is a silent DR failure (F-19).
- **How to verify:** Actions run history for the last 30 days (failures, skips, 60-day inactivity warnings).
- **Who:** `MAINTAINER`. **When:** immediately (it costs one page load).

### U-08 — Which commit is deployed right now?
- **Why it matters:** K-09 style drift applies to production too; incident triage needs the deployed SHA.
- **How to verify:** platform build metadata; print the SHA at start-up (`/version`).
- **Who:** `OPERATOR`. **When:** during the first incident.

---

## Family P — Persistence and data

### U-09 — What volume of real job rows exists, and in which states?
- **Why it matters:** the size of the stuck-`processing` population measures how often C-04 actually happens.
- **How to verify:** `nexus jobs status` (or a read-only query) and record the state histogram monthly.
- **Who:** `OPERATOR`. **When:** now — this is a 1-command measurement.

### U-10 — Are the 14 `features/*` modules actually invoked in production?
- **Why it matters:** K-02/C-16 matter only for features that run; activation state determines whether dual-plane divergence is theoretical or happening.
- **How to verify:** per-feature structured-log counters over a week, cross-checked against the board's DEAD_ENGINES audit.
- **Who:** `MAINTAINER` + `TIME`. **When:** before any refactor decision.

### U-11 — Do the raw-DDL tables (`referral`, `referralcode`, `conversation_history`) have rows in production, and in which store?
- **Why it matters:** decides whether a migration into the central store is a data migration or an empty-table cleanup.
- **How to verify:** row counts in both candidate stores (SQLite file and PG).
- **Who:** `OPERATOR`. **When:** before ADR-C-13 is decided.

### U-12 — Has anyone ever restored from a backup?
- **Why it matters:** an untested backup is a hope (K-12, DR-11); the answer is very likely "no", which is itself the finding.
- **How to verify:** attempt a restore into a scratch DB and time it (this also produces the missing RTO number).
- **Who:** `OPERATOR`/`MAINTAINER`. **When:** immediately.

### U-13 — What is the actual size/growth of the R2 bucket, and what does a backup contain?
- **Why it matters:** cost (Phase 8), retention correctness, and whether the SQLite-backup path is capturing the files that matter.
- **How to verify:** `aws s3 ls`-equivalent against the bucket with a read-only key; inspect one dump's object list.
- **Who:** `OPERATOR`. **When:** at the first cost review.

### U-14 — Do the queue file and the backed-up database file coincide?
- **Why it matters:** if `db_path` differs from the queue's sidecar path, backups protect the wrong artefact (`A-13`).
- **How to verify:** compare the configured paths from the running config (a diagnostic printout of *paths*, not contents).
- **Who:** `MAINTAINER`. **When:** immediately.

### U-15 — What is the checkpoint growth rate and the 30-day policy's real effect?
- **Why it matters:** cost/storage and the "silent state rollback" behaviour in C-18.
- **How to verify:** row counts per week; count of threads whose resume fell back to fork.
- **Who:** `OPERATOR`/`TIME`. **When:** at 3+ months of use.

### U-16 — Is RAG retrieval isolated per user/chat, or can one corpus bleed into another?
- **Why it matters:** a data-leak class; `LongTermMemory` is thread-scoped (`R-08`, verified) but the RAG collections were not fully audited in this review (S-15).
- **How to verify:** read every collection name/filter at every call site; or a two-user test with disjoint seeded documents.
- **Who:** `MAINTAINER`. **When:** before any multi-user growth.

---

## Family S — Security

### U-17 — Is `/api/dashboard/*` actually reachable unauthenticated in production?
- **Why it matters:** it is the highest-likelihood real incident in this review (S-01); reachability depends on the token being unset (U-03-adjacent).
- **How to verify:** one `GET` from an unprivileged network with no token; check the platform access logs for prior hits.
- **Who:** `OPERATOR` (authorised test). **When:** immediately.

### U-18 — Does any callback/payload path other than force-join derive privilege from user-supplied data?
- **Why it matters:** callback-data tampering is a classic privilege-escalation route (S-18); only the force-join path was reviewed.
- **How to verify:** grep every `CallbackQuery` handler for trust decisions; write a per-handler test matrix.
- **Who:** `MAINTAINER`. **When:** before adding any new inline-button flow.

### U-19 — What is the HMAC key's distribution and has it been rotated?
- **Why it matters:** the key gates `POST /creative/video-edit` (the SSRF-capable route) and replay is possible within ±300 s (S-05).
- **How to verify:** secret inventory + rotation log (which does not exist — that absence is the answer).
- **Who:** `OPERATOR`. **When:** now.

### U-20 — Is `main` protected, and do workflow changes require review?
- **Why it matters:** CI holds long-lived R2/DB secrets; anyone who can merge a workflow file can exfiltrate them (S-27).
- **How to verify:** repository branch-protection/Ruleset settings and environment secret scopes.
- **Who:** `MAINTAINER`. **When:** before adding any new CI secret.

### U-21 — How many providers can see a given user message, in practice?
- **Why it matters:** privacy claims and the strict-privacy gap (K-05).
- **How to verify:** read the vendor data-use terms for each active leg and record the effective date (`[NEED-PRIMARY]`); correlate with which legs actually served traffic (needs the ledger from ADR-C-12).
- **Who:** `OWNER` + `VENDOR`. **When:** at the first privacy-sensitive user.

### U-22 — Is anything else listening on the public port?
- **Why it matters:** webhook mode serves the whole FastAPI app; an inventory of exposed routes is a precondition for hardening (S-01/S-02).
- **How to verify:** enumerate `app.routes` at start-up and publish it as an architecture artefact (was done structurally in this review; production parity unverified).
- **Who:** `MAINTAINER`. **When:** immediately.

### U-23 — Are dependency vulnerabilities tracked at all?
- **Why it matters:** no lock file/SBOM means "unknown" is the honest state (`S-26`, `E-13`).
- **How to verify:** run a scanner (pip-audit/osv-scanner) once and record the count as a baseline.
- **Who:** `MAINTAINER`. **When:** before the next dependency bump.

---

## Family C — Cost, quota, and capacity

### U-24 — What are the *actual* enforced quotas for Groq/Gemini/OpenRouter on this account?
- **Why it matters:** the entire SMALL-tier design rests on these numbers, and secondary sources conflict (`E-06`/`E-07` = `[C]`).
- **How to verify:** the providers' own console quota pages (authoritative per account).
- **Who:** `OPERATOR` + `VENDOR`. **When:** immediately (cheap; it decides whether the free tier survives).

### U-25 — Real per-request token counts and the real provider mix?
- **Why it matters:** Phase 8's model is an assumption; the ledger does not exist (ADR-C-12).
- **How to verify:** add the ledger, or read provider-side usage dashboards for a week and back out the split.
- **Who:** `OWNER` + `TIME`. **When:** at the first paid month or first cost question.

### U-26 — How long does one render *actually* take on the deployed instance?
- **Why it matters:** the 900 s timeout vs 0.1 vCPU question; one measurement decides the rendering capacity model (ADR-C-26).
- **How to verify:** time three renders end-to-end at the deployed class; record p50/p95.
- **Who:** `MAINTAINER`/`OPERATOR`. **When:** before promising any render throughput.

### U-27 — What is the Neon duty cycle (CU-hours consumed per month)?
- **Why it matters:** the largest post-LLM cost swing (Phase 8 §4).
- **How to verify:** provider usage page for one full month.
- **Who:** `OPERATOR` + `TIME`. **When:** after the first month on PG.

### U-28 — What is the log volume, and does it fit the observability free tier?
- **Why it matters:** 50 GB/month is generous until per-user structured logging arrives (Phase 8 §4).
- **How to verify:** sample 1,000 requests' log bytes and extrapolate; then compare to the tier.
- **Who:** `MAINTAINER`. **When:** when observability is adopted (ADR-C-23).

### U-29 — What fraction of LLM calls are retries/duplicates rather than answers?
- **Why it matters:** it is the difference between "the bill is the product" and "the bill is a symptom" (Phase 8 §6).
- **How to verify:** only possible with the ledger — so the unknown *is* the argument for ADR-C-12.
- **Who:** `OWNER`. **When:** at the first paid invoice.

---

## Family W — Product and usage reality

### U-30 — How many real users/chats are there, and how concentrated?
- **Why it matters:** every abuse analysis changes weight (S-07/S-09 assume an authorised user; if there is exactly one, the threat model is different from a public bot).
- **How to verify:** distinct chat ids in the last 30 days.
- **Who:** `OWNER`. **When:** immediately (it calibrates all severity work).

### U-31 — Which features are actually used, and which are dead?
- **Why it matters:** 14 feature modules exist; the dead-engine audit on disk suggests some are dormant. Cost, security surface, and refactor order all depend on this.
- **How to verify:** per-feature invocation counts for a week (logs/metrics).
- **Who:** `MAINTAINER` + `TIME`. **When:** before any feature-surface decision.

### U-32 — Is there a stated data-retention expectation from users?
- **Why it matters:** ADR-C-16 (deletion) and ADR-C-15 (RTO/RPO) need a requirement, not a guess.
- **How to verify:** ask the owner explicitly; record the answer as a decision input.
- **Who:** `OWNER`. **When:** before the first deletion request.

### U-33 — What is the acceptable downtime and acceptable data loss (in words, not numbers)?
- **Why it matters:** Phase 7 cannot produce RTO/RPO from the code; they are product decisions.
- **How to verify:** one conversation with the owner, recorded in `DECISION_LOG.md` or an ADR.
- **Who:** `OWNER`. **When:** now (it is the cheapest DR improvement available).

### U-34 — What is the timezone contract for "daily" features?
- **Why it matters:** Phase 4 C-11/C-12 — daily rewards and quotas currently depend on an unstated day boundary; Persian users at UTC+3:30 see a 03:30 local reset.
- **How to verify:** ask the owner which behaviour is intended; then check the implementation against the answer.
- **Who:** `OWNER`. **When:** at the first complaint or before any paid/quota'd daily feature.

---

## Family T — Team and process

### U-35 — Which board P0s are actually still open, and are they the same ones this review found?
- **Why it matters:** the board (READ-ONLY here) lists P0s from a previous audit; the overlap between board priorities and this review's top risks is the practical handoff question.
- **How to verify:** re-read `.agents/board.json` next_work and compare with Phases 1–11 findings.
- **Who:** `MAINTAINER`. **When:** at the handoff of this research package.

### U-36 — Who is on call for this system when something breaks at 03:00?
- **Why it matters:** every recovery procedure in Phase 7 assumes a human; if the answer is "nobody", then the *detection* gap (no alerting) and the *response* gap compound.
- **How to verify:** a written answer, not a technical check.
- **Who:** `OWNER`. **When:** immediately.

---

## 2. Closure priority (cheapest first, highest information per unit of effort)

| Rank | Unknowns | Cost to close | What it unlocks |
|---|---|---|---|
| 1 | U-01, U-02, U-04, U-07, U-17, U-24, U-30, U-36 | minutes (console/one call/one question) | re-severities half of Phases 3–7; calibrates the whole threat model |
| 2 | U-09, U-12, U-14, U-22, U-26 | < 1 hour, in-repo/authorised tests | converts four "assumed" findings into verified ones (including the restore drill) |
| 3 | U-10, U-11, U-27, U-31 | a day of observation or a week of counters | decides ADR-C-13's scope and the feature-surface plan |
| 4 | U-16, U-18, U-21, U-23, U-25, U-28, U-29, U-33, U-34 | requires code reading or product decisions | prerequisite evidence for ADRs C-10, C-12, C-16, C-22 |
| 5 | U-05, U-06, U-15, U-19, U-20, U-32, U-35 | process/time | long-run planning and succession |

**Note:** **no unknown in this register blocks a safety improvement.** Every mitigation in Phases 4–10 is a local, additive change that is justified by *verified* mechanisms; the unknowns change **priority**, not **validity**. That distinction is what keeps the package actionable while the console is still unread.
