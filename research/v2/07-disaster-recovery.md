# PHASE 7 — DISASTER RECOVERY ASSESSMENT

**Question:** for each plausible disaster, does a detection exist, a recovery procedure exist, and what are the RTO/RPO? Where the repository does not define them, this file writes **UNDEFINED** and treats that as the finding.
**Framework:** NIST SP 800-34 Rev.1 (contingency planning; RTO/RPO as *stated* quantities, `E-11`) — cited as a definition source, not as a compliance claim.
**Sources for recovery mechanics:** `R-17` (backup/retention), `R-06` (migrations/stores), `R-18` (deploy + revision rollback), `R-23` (runbooks), `R-19` (CI/maintenance workflows), `R-24` (repository hardening), plus the Phase 4 scenario analysis.
**Markers:** `[V]` verified from code/docs, `[A]` reasoned, `[U]` unverifiable from here, **UNDEFINED** = the system does not state it.

---

## DR-00 · Baseline posture: what actually survives

| Survives without operator action | Does not survive |
|---|---|
| Code (git + image registry), configuration in the manifest, R2 objects (blobs/backups), Neon data (if `NEXUS_DATABASE_URL` was configured), Telegram-side state (chats, buttons) | Local SQLite file(s) on Koyeb's ephemeral disk: app DB *if PG unset*, `nexus_job_queue`, `creative_jobs`, checkpoints, temp renders |
| GitHub Actions workflows (deploy/maintenance) | In-process timers (scheduled posts, reminders), rate-limit windows, LLM cooldowns, PTB in-memory update queue |

**Verified fact:** `koyeb.yaml` declares **no volume** and **no `NEXUS_DATABASE_URL`** (`R-20`) ⇒ in the documented deployment the durable-store assumption is not established by the manifest; DR posture depends on a console variable. `[V]`

**The single worst structural fact for DR:** *the component whose loss destroys user-visible promises (the local SQLite files) is the component with no backup at all*, while the component that *is* backed up (PG, via `maintenance/backup.py`) is the one that may not be in use (`A-13`). `[V]`/`[A]`

---

## 1. Scenario matrix

| ID | Incident | Detection today | RTO | RPO | Recovery procedure | Data loss | Manual intervention |
|---|---|---|---|---|---|---|---|
| DR-01 | **SQLite database corruption** (app DB) | app errors on read/write; SQLite `malformed` exceptions in logs; no integrity check job exists | **UNDEFINED** (rebuild = redeploy; data = gone) | **TOTAL for local-only stores**; for PG-backed app DB, bounded by Neon PITR (free tier: 6 h window, `[V]` external) | Recreate from migrations (`nexus migrate` recreates schema); no data-restoring tool exists for the lost file | **Total** (local) / provider-bounded (PG) | Manual; may be impossible in cloud (file is gone before it can be copied) |
| DR-02 | **PostgreSQL / Neon outage or suspension** | connection errors (no `pool_pre_ping`-independent alarm); feature-level 500s; `/healthz` stays **green** because it is DB-free | **UNDEFINED**; practical = "until the provider recovers" | 0 (no writes accepted) but see DR-03 | Wait; then verify with `nexus migrate --check`-style stamp query (`R-19`) | None if the outage is clean | Manual monitoring; **no alerting path** (`A-16`) |
| DR-03 | **Neon scale-to-zero + cold start** (normal, not an incident) | user-visible latency spike (300–900 ms typical, `[U]`); no metric distinguishes it | ~seconds | 0 | none needed | None | none |
| DR-04 | **Telegram API outage / global degradation** | PTB errors; webhook returns 200 while enqueue fails ⇒ user sees nothing | **UNDEFINED** | Updates held by Telegram ≤24 h for polling systems; in webhook mode, updates whose 200 was answered are **already lost** | none (wait) | **Partial, silently** (`X-01`) | Manual; no "resume after outage" replay tool exists |
| DR-05 | **Telegram bot token invalidated / revoked** | every send fails; access gate may fail closed | ~minutes | 0 | rotate token (runbook line exists, `R-23`); webhook re-registration is part of start-up | None | Operator + BotFather |
| DR-06 | **LLM provider outage (all four)** | chain exhausts ⇒ `FakeLLM` garbage answers (`R-12`) | immediate **degradation** (not downtime) | 0 | none; the chain recovers on its own | None (answers are junk, marked as such) | none — but see A-27: garbage answers are worse than an honest error |
| DR-07 | **Redis outage** | **not applicable — Redis is not used** | n/a | n/a | n/a | n/a | none (`DECISION_LOG.md` r7: Celery/Redis rejected; `R-17` "Neon is the only Redis replacement") — recorded so the checklist is not re-asked |
| DR-08 | **Filesystem corruption / disk full** (2 GB SSD) | FFmpeg failures, `database is locked`, write errors | **UNDEFINED** (recreate instance) | Local-only data (queue, jobs, checkpoints, temp) | Replace instance; `nexus migrate`; sweep temps | Partial→Total (local stores) | Manual; **no free-space monitor or alert exists** (`R-21` lists no disk metric) |
| DR-09 | **Bad deploy (code)** | smoke test fails (`deploy_smoke.py`) *if* it runs in CI, plus user reports | minutes (revision rollback) | 0 | Koyeb revision rollback (`R-18`); re-point webhook if the URL changed | None | Operator |
| DR-10 | **Bad/partial migration** | CI asserts the Alembic stamp after migration (`R-19`); runtime errors otherwise | **UNDEFINED** | 0–partial (depends on DDL) | Re-run `nexus migrate` (idempotent) or restore PG from the most recent dump and stamp | Possible loss of writes between dump and restore | Operator; **no automatic trigger on smoke failure** (`A-26`) |
| DR-11 | **Loss of R2 backups (bucket deletion / credential loss)** | backup job failure (GitHub Actions notification only); **no stale-backup check** | **UNDEFINED** | = age of the newest surviving dump, **and even that is unverifiable** (never restored) | none | Potentially **Total with no warning** | Manual; depends on Cloudflare-side retention guarantees (unstated) |
| DR-12 | **Credential leak (any secret)** | none in-system (no anomaly detection); discovered externally | **UNDEFINED**; rotation is manual and multi-step (Telegram token, HMAC key, webhook secret, dashboard token, Neon password, R2 keys, GitHub secrets) | n/a | `R-23` has a rotation line per secret; **no exposed-secret inventory, no rotation log, no blast-radius map** | None (if rotated) but exposure is not bounded by the system | **Manual, extensive, untested** |
| DR-13 | **Compromised dependency / build** | none (no SBOM, no provenance) | **UNDEFINED** | n/a | rebuild from a known-good commit; the pinning is partial (`S-26`) | n/a | Manual forensic work; requires knowing which commit was clean — **not recorded anywhere** |
| DR-14 | **Ghost-owner incident (bus factor / lost operator)** | none | **UNDEFINED** | n/a | The repo does contain the operator-independent parts (`Makefile`, runbooks, CI); the console-side parts (Koyeb/Neon/R2/Telegram ownership) are **not documented as transferable** | n/a | High; a **succession gap** (single-owner product by design, but DR for the *account* is absent) |
| DR-15 | **Repo compromise / force-push to `main`** | none in-repo | **UNDEFINED** | n/a | git history + `REPO_HYGIENE` docs; protected-branch state unknown (`U-20`) | n/a | Manual |

---

## 2. RTO/RPO summary (the honest table)

| Quantity | Where it is stated | Value |
|---|---|---|
| RTO (any incident) | repo | **UNDEFINED — searched: no RTO/RPO token anywhere in `docs/`, `.agents/board.json`, `koyeb.yaml`, workflows** |
| RPO (PG/Neon) | repo | **UNDEFINED**; provider-side PITR window is the only bound (free tier 6 h, `[V]` external) |
| RPO (local SQLite) | repo | **infinite** (no backup exists for those files) |
| RPO (blobs) | repo | 0 (R2 is the store of record; no second copy needed for DR-01..03, but DR-11 is total) |
| Backup cadence | `maintenance.yml` cron | **2×/day** (`[V]`) ⇒ best-case RPO = 12 h, *if* PG is in use and the job is running |
| Backup verification | repo | **none — no restore drill, no `pg_restore --list` check found** (`A-14`) |
| Retention | `maintenance/housekeeping.py` | backups >30 days pruned; temp >48 h pruned (`[V]`) |

**NIST framing (`E-11`):** an RTO/RPO that is not written is not a requirement; the repo currently has a *backup mechanism* (a capability) and **no contingency requirement** (a target). Operationally this means every incident is triaged by improvisation, and "how long is acceptable to be down" has never been decided — which the DR review must report as a **gap, not a value**.

---

## 3. Recovery-capability inventory (what can be done *today*, from the repo alone)

| Capability | Command / mechanism | Verified? | Note |
|---|---|---|---|
| Deploy / redeploy | Koyeb Git integration, `koyeb.yaml` | `[V]` | no volume ⇒ state resets |
| Roll back code | Koyeb revision rollback | `[V]` | documented in `DEPLOY_RUNBOOK.md` (unread in full; presence verified by grep) |
| Migrate schema | `nexus migrate` (idempotent; CI asserts the stamp) | `[V]` | no downgrade path |
| Smoke test | `scripts/deploy_smoke.py` | `[V]` | checks `/healthz` + webhook; **not** end-to-end (no LLM, no queue drain) |
| Backup | `nexus maintenance backup` → R2 (`pg_dump` or SQLite online backup) | `[V]` code; **untested restore** | SQLite path backs up the *wrong* file in the documented topology if `db_path` ≠ the queue file |
| Restore | **NO TOOL** | — | No `restore` entry point exists in the CLI (verified: no `restore` command in maintenance/CLI surface) |
| Rotate secret | runbook lines | `[V]` (text) / untested | per-secret, manual |
| Purge temps/backups | `nexus maintenance housekeeping` | `[V]` | run by scheduled workflow |
| Inspect queue | `nexus jobs status`; `nexus jobs resume` (pending only) | `[V]` | cannot rescue `processing` rows |

**The two hardest gaps in this table:** (1) **no restore path at all** — the backup's only consumer is the prune job, so "backup" is currently a *hope*, not a capability; (2) `nexus jobs resume` cannot recover the exact state a crash creates (Phase 4 C-04).

---

## 4. Failure of the safety nets themselves

| Safety net | Its own failure mode | Consequence |
|---|---|---|
| Scheduled backup (GitHub Actions) | workflow disabled (60-day inactivity on some plans), secret expired, cron skipped, **GitHub Actions outage** | silent backup stop; no staleness alarm (`A-14`) ⇒ `F-19` |
| `resume_pending` on startup | restarts `processing → pending` *unconditionally* | a job that was in fact running on another host is re-run ⇒ duplicate (`F-02`) — **the safety net creates a duplicate risk** |
| Scale-to-zero | is itself the trigger for state loss | the resilience feature *is* the incident (`A-19`) |
| Rate limiter in memory | reset on every cold start | protection disappears exactly when load is highest (`A-31`) |
| Cooldowns | same | idem |
| Smoke test | only checks the HTTP surface | a deploy that breaks the LLM chain or the queue drain passes the smoke test |
| Retention (30 d) | prunes the very backups that DR-11 would need | the retention window is an *implicit* RPO ceiling that nobody chose |

---

## 5. FACTS vs ANALYSIS

**FACTS.**
1. No RTO/RPO appears anywhere in the repository (searched).
2. No restore command/script exists; the backup has no verified consumer.
3. `koyeb.yaml` has no volume and no `NEXUS_DATABASE_URL`; the runs on ephemeral disk.
4. Backups run 2×/day via GitHub Actions when the schedule fires, and are pruned after 30 days.
5. Redis is not part of the architecture (rejected in the decision log) — the DR checklist item is **N/A**, and saying so is part of the finding.
6. `/healthz` cannot fail for a database outage (DB-free by design).

**ANALYSIS.**
1. **This is the weakest phase of the architecture.** Detection, procedure, and target are missing for the disasters that matter (local-store loss, backup loss, credential leak); what exists is strong for *code* disasters (rollback, idempotent migrations, CI stamp assertion).
2. **Cheapest high-value DR work, in order of value-per-effort:**
   (a) make `NEXUS_DATABASE_URL` **required** in the manifest (turns DR-01 from total loss into a bounded event);
   (b) add **one** restore drill (restore the newest dump into a scratch DB and run `alembic upgrade head` + a row count) — this validates DR-11 *and* the backup format;
   (c) add backup-staleness alerting (a "no dump in 36 h" check) — closes `F-19`;
   (d) write the RTO/RPO decision down (even as "RTO = best effort, RPO = 12 h"), because an unstated target cannot be engineered against;
   (e) make `/healthz` expose **dependency** status separately (`/readyz`) without breaking the current DB-free liveness contract.
3. **The scale-to-zero trade must be named explicitly in DR terms:** the platform feature that saves money guarantees cold-state amnesia; either state moves off the instance (external DB/queue) or the DR table stays red for DR-01/DR-08.
4. **No DR plan can fix a missing requirement.** The report's obligation is therefore to state the *undefined* cells plainly (as above) rather than to invent numbers.
