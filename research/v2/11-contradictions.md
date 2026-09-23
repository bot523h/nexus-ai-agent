# PHASE 11 — CONTRADICTION REGISTER

**Rule:** a contradiction is only entered here when **both** sides are quotable from a located source. Near-misses discovered by inspection are recorded in §2 as *resolved*, because the answer to "why do two numbers exist?" is itself evidence.
**Fields:** CLAIM A / SOURCE A / CLAIM B / SOURCE B / WHY THEY DIFFER / WHAT CAN BE VERIFIED / CURRENT STATUS.
**Count:** **14** open contradictions (`K-01`…`K-14`) + **3** resolved non-contradictions (`K-R1`…`K-R3`).

Status vocabulary: **OPEN** (both sides currently citable), **OPEN[NEED-PRIMARY]** (one side is a secondary source and needs a vendor/primary citation), **RESOLVED-AS-CLASS-CONFUSION**, **RESOLVED-AS-STALE**.

---

## K-01 — Is the queue on durable storage or on the ephemeral disk?

- **CLAIM A:** "The queue survives restarts because it is a SQLite sidecar on **durable storage** … in webhook/scale-to-zero mode, anything that must survive a redeploy lives in Postgres or R2 — **never on local disk**." — **SOURCE A:** `docs/architecture/DATA_AND_STORAGE.md:94` (verified against `main` @ `7573249`).
- **CLAIM B:** the deployed service has **no volume** and a 2 GB SSD local disk; the queue path defaults to the local filesystem, and the platform scales to zero — **SOURCE B:** `koyeb.yaml` (no `volumes:`, no `NEXUS_DATABASE_URL`), `E-02` (Koyeb free instance: "no volumes", 2 GB SSD), `E-04` (scale-to-zero after ~1 h idle).
- **WHY THEY DIFFER:** the sentence is internally inconsistent — it asserts the rule ("never local disk") and then exempts the queue on the grounds that its file is durable, which is true in local dev and false in the cloud topology. The doc was written for the dev topology; the platform chose the other one.
- **WHAT CAN BE VERIFIED:** whether a volume is attached or `NEXUS_DATABASE_URL` is set in the console (not in the repo); whether a redeploy preserves the file (one controlled redeploy + a queued job settles it).
- **STATUS:** **OPEN** — this is the single most consequential contradiction in the review (it decides whether Phase 4 C-04 and Phase 3 F-04 are "risk" or "current behaviour").

## K-02 — One data plane or two?

- **CLAIM A:** the application DB is `storage/models.py` "(+ 2 tables in `features/referral.py`)", and the architecture defines a single store for user-visible state — **SOURCE A:** `docs/architecture/DATA_AND_STORAGE.md:16,38`.
- **CLAIM B:** at least **three** tables are created by raw DDL outside the model layer — `referral`, `referralcode` (`features/referral.py:77,91`) and `conversation_history` (`features/conversation_store.py:45`) — and **14** `features/*` modules open their own engines; 37 import-boundary violations are grandfathered in `tests/architecture/legacy_baseline.json` — **SOURCE B:** code (`M-05`, `M-01`) + the architecture gate's own baseline file.
- **WHY THEY DIFFER:** the docs describe the intended design; the baseline file is the organisation's acknowledgement that the intended design is not enforced. The gap between them is measured (1 extra table; 14 modules; 37 entries) rather than asserted.
- **WHAT CAN BE VERIFIED:** run the feature modules in a scratch environment with `NEXUS_DATABASE_URL` set and `db_path` pointed at a file — then read where each write lands (deterministic, offline).
- **STATUS:** **OPEN** (C-16, F-09/F-10 depend on it).

## K-03 — Does an idempotency key prevent a second effect?

- **CLAIM A:** "Idempotent writes reuse the same idempotency key and **cannot create a second effect** (`put`, `enqueue`, `delete_thread`)." — **SOURCE A:** `docs/architecture/PORTS.md:29`; the port signatures themselves (`PORTS.md:15,19`).
- **CLAIM B:** `M-07`: no LLM consumer reads `idempotency_key`; `M-08`: `R2Provider` implements `upload(local_path, remote_key)` with no key parameter; only `enqueue` and `delete_thread` actually dedupe — **SOURCE B:** code measurements.
- **WHY THEY DIFFER:** the port *interface* declares a capability that only some adapters implement; docs were written from the interface, not the implementations.
- **WHAT CAN BE VERIFIED:** call `put()` twice with the same key against a scratch bucket and observe whether two objects appear (one command).
- **STATUS:** **OPEN** — the honesty fix is cheap: either implement or delete the claim.

## K-04 — Is the SSRF class closed?

- **CLAIM A:** STRIDE row T9 "SSRF via summariser/WebTrainer/image URLs" → mitigation "DNS/address validation + validating transport (private ranges refused)" → **closed**, evidenced by `test_http_client_ssrf.py` / `test_summarizer_ssrf.py` — **SOURCE A:** `docs/architecture/SECURITY.md:47`.
- **CLAIM B:** `api/app.py::_download_video_to_temp` constructs a plain `httpx.AsyncClient(follow_redirects=True)` with **no** `validate_url`, **no** `SafeAsyncTransport`, **no** size cap, reachable via HMAC-gated `POST /creative/video-edit`; only an `http/https` prefix check exists — **SOURCE B:** `R-04`, code.
- **WHY THEY DIFFER:** the security review enumerated the paths that existed when it was written; the creative API path was added later and its author reused the download helper rather than the guarded transport. The doc's row is true for the listed paths and false for the class.
- **WHAT CAN BE VERIFIED:** point `video_url` at a local listener and observe the request (a two-minute test in a scratch environment).
- **STATUS:** **OPEN** (Phase 6 S-04).

## K-05 — Is strict privacy strict?

- **CLAIM A:** "`NEXUS_LLM_STRICT_PRIVACY=true` removes OpenRouter `:free` deployments from the chain … **Ollama, Groq and Gemini do not train on prompts and always stay.**" — **SOURCE A:** `docs/architecture/LLM_PROVIDERS.md:36–39`.
- **CLAIM B:** independent analyses state that **Gemini free-tier** inputs may be used to improve Google products, with differing account-level details — **SOURCE B:** `E-07` (EXTERNAL-INDEPENDENT, ages 2026-04-29 / 2026-09-13 / 2026-09-04).
- **WHY THEY DIFFER:** CLAIM A is a vendor-terms assertion dated to when the doc was written; CLAIM B describes the free tier's data-use policy as analysed later by third parties. Neither side is the vendor's own current text, which this review could not fetch.
- **WHAT CAN BE VERIFIED:** fetch the vendor's current data-use terms for the free tier and record the effective date (one authoritative page).
- **STATUS:** **OPEN[NEED-PRIMARY]** — the code (`M-04`: only OpenRouter `:free` is skipped) matches CLAIM A's implementation and not CLAIM B's implication.

## K-06 — Does the documented crash-recovery command recover the documented crash?

- **CLAIM A:** the sequence diagram says "crash here → `nexus jobs resume` re-schedules pending jobs" at the point where a running job is interrupted — **SOURCE A:** `docs/architecture/RUNTIME_FLOWS.md:75`; `OBSERVABILITY.md:89` recommends the same for "Job queue stuck".
- **CLAIM B:** `resume_pending_jobs` claims **pending-only**, and a crash leaves the row in `processing`; the startup path that *would* reset `processing → pending` only runs inside a process that is, by hypothesis, gone — **SOURCE B:** `R-01` (verified code + the CLI's own docstring semantics), `DECISION_LOG.md:355` ("pending-only `resume_pending_jobs`").
- **WHY THEY DIFFER:** the diagram describes the *intent* of the recovery command; the command's actual predicate was deliberately narrowed (pending-only) for duplication safety, and nobody updated the diagram.
- **WHAT CAN BE VERIFIED:** kill a job in a scratch environment (or `UPDATE … SET status='processing'`), run `nexus jobs resume`, and observe that nothing is claimed (one command).
- **STATUS:** **OPEN** (Phase 4 C-04; the same fact appears in `DATA_AND_STORAGE.md:94` as "pending-only", which contradicts the diagram in the same doc set).

## K-07 — How many architecture gates exist?

- **CLAIM A:** "`tests/architecture/` (**15 files**)" — **SOURCE A:** `docs/architecture/REFERENCES.md:33`.
- **CLAIM B:** **19** entries in the architecture suite at HEAD — **SOURCE B:** `M-03` (verified by listing/collecting the suite).
- **WHY THEY DIFFER:** ordinary drift: the doc was verified at `7573249` and four gates were added afterwards.
- **WHAT CAN BE VERIFIED:** `ls tests/architecture/` at HEAD and at `7573249` (two commands).
- **STATUS:** **OPEN** (low stakes, high signal: it is a *measurable* instance of K-09's stale-pin problem).

## K-08 — What is the rate limit for a user?

- **CLAIM A:** the docs' operational description ("10 messages / 60 s") — **SOURCE A:** `docs/architecture/SECURITY.md` (access/abuse section) and `bot/middleware.py:8` (`max_messages: int = 10, window_seconds: int = 60`).
- **CLAIM B (apparent):** an earlier reading in this review found "5/60" — **SOURCE B:** `bot/access_guard.py:40–41` `_DENIAL_REPLY_LIMIT = 3, _DENIAL_REPLY_WINDOW = 60` plus a separate limiter instantiation at `access_guard.py:55`.
- **WHY THEY DIFFER:** there are **two or three distinct limiters** (per-user message limiter, denial-reply limiter, plus any feature-level limits); a grep for "max_messages" returns constants that belong to different controls. This was a *classification* error on my side, not a product contradiction.
- **WHAT CAN BE VERIFIED:** instantiate each limiter and read its fields (done); enumerate all limiter classes.
- **STATUS:** **RESOLVED-AS-CLASS-CONFUSION** — recorded because the resolution changes the finding: the protection is real (10/60 for messages, 3/60 for denial spam) and the docs match the message limiter.

## K-09 — Which commit are the architecture docs true for?

- **CLAIM A:** seven documents declare "**Verified against:** `main` @ `7573249`" — **SOURCE A:** `docs/architecture/{OVERVIEW, DATA_AND_STORAGE, MODULE_MAP, OBSERVABILITY, CREATIVE_STUDIO, ...}.md:5`.
- **CLAIM B:** HEAD is `a997aab` with **350 commits** added since, and four independent measurements differ from the documented numbers — src **231 files / 40,815 lines** vs documented 215 / 35,431; tests **144** vs 112; architecture gates **19** vs 15; env vars **47** vs 78 documented — **SOURCE B:** `M-01`…`M-04`, `M-06` (`7573249` is a *verified ancestor* of HEAD, so the docs are stale rather than wrong-at-the-time).
- **WHY THEY DIFFER:** a one-time verification was not repeated; `tests/.../test_docs_integrity.py` gates **structure** (links, mermaid, ADR index, H1) and not accuracy, so nothing failed.
- **WHAT CAN BE VERIFIED:** re-run the documented commands at HEAD; compare (this review already did for four metrics).
- **STATUS:** **OPEN** — the docs are accurate *for their stated commit*, which is exactly why the drift is invisible to CI.

## K-10 — Does the model the chain depends on still exist?

- **CLAIM A:** the configured secondary leg is `gemini-2.0-flash` (settings default, verified in-tree) and the docs describe Gemini as a stable chain member — **SOURCE A:** `M-04` (settings), `docs/architecture/LLM_PROVIDERS.md`.
- **CLAIM B:** one Aug-2026 analysis states Gemini 2.0 Flash **shut down on 2026-06-01**, while a Google Vertex pricing page (Sept 2026) still lists it at $0.15/$0.60 and a Jan-2026 analysis lists it at $0.10/$0.40 — **SOURCE B:** `E-17` (three sources, mutually inconsistent).
- **WHY THEY DIFFER:** endpoint families differ (AI Studio/Gemini API vs Vertex), and secondary sources disagree; nobody fetched the vendor's current model list this session.
- **WHAT CAN BE VERIFIED:** query the provider's model list (one authenticated call) or read the current official model-lifecycle page; the app can also log the model's existence at start-up (ADR-C-11).
- **STATUS:** **OPEN[NEED-PRIMARY]** — if CLAIM B holds, a chain leg is dead and the *only* symptom today is unexplained quota exhaustion.

## K-11 — Redis: absent by decision, or absent by accident?

- **CLAIM A:** the decision log records Celery/Redis as **rejected**, with "Neon is the only Redis replacement" — **SOURCE A:** `DECISION_LOG.md` (r7 history), `R-17`.
- **CLAIM B:** the state that Redis-class systems usually hold — rate-limit windows, provider cooldowns, job claims, scheduler timers — must live **somewhere**; in this codebase it lives in process memory and therefore disappears on every scale-to-zero (`R-13`, `R-12`, `R-01`, `R-08`).
- **WHY THEY DIFFER:** rejecting Redis is coherent (fewer services, lower cost); what was not decided is the *replacement home* for ephemeral coordination state. The decision log's phrase "Neon is the only Redis replacement" describes data, not coordination state.
- **WHAT CAN BE VERIFIED:** enumerate the in-memory state classes (Phase 9 §1) and check whether each has a durable home — none do.
- **STATUS:** **OPEN** (not a contradiction of intent, a gap in the decision's second half).

## K-12 — Is the backup a capability or a hope?

- **CLAIM A:** backups are taken twice daily to R2, with retention and a documented runbook — **SOURCE A:** `maintenance.yml` (cron), `R-17`, `docs/ops/*` runbooks.
- **CLAIM B:** there is **no restore entry point** in the CLI, **no drill** in CI, and **no RTO/RPO** anywhere; the prune job is the backup's only consumer — **SOURCE B:** `A-14`, Phase 7 §3 (searched: no `restore` command; no RTO/RPO token).
- **WHY THEY DIFFER:** "we take backups" and "we can restore" are different claims; the first is implemented, the second is implied.
- **WHAT CAN BE VERIFIED:** attempt a restore into a scratch database from the newest dump (a single documented exercise).
- **STATUS:** **OPEN** (DR-11/DR-15).

## K-13 — Does "queued" mean the job will run?

- **CLAIM A:** the user-facing contract and the docs treat the queue as the durability mechanism for deferred work ("`queued`", job status endpoint, resume command). — **SOURCE A:** `docs/architecture/RUNTIME_FLOWS.md`, `OVERVIEW.md` CLI row, job status endpoint.
- **CLAIM B:** in the documented topology the queue file is on ephemeral disk and its recovery command recovers only `pending`; a scale-to-zero between enqueue and execution loses the job silently — **SOURCE B:** `koyeb.yaml`, `E-02`/`E-04`, `R-01` (pending-only), Phase 4 C-04.
- **WHY THEY DIFFER:** the promise is about *intent* ("we recorded your request"); the user reads it as *delivery* ("you will get the result"). Neither the docs nor the UI distinguishes them.
- **WHAT CAN BE VERIFIED:** enqueue a long job, force a redeploy/scale-to-zero, observe (this is the single most valuable end-to-end test in the whole review).
- **STATUS:** **OPEN** — related to K-01 but distinct: K-01 is about storage, K-13 is about the *promise's* wording.

## K-14 — "Idempotent deploy" vs "old code on a new schema"

- **CLAIM A:** migrations are safe to re-run and the deploy is safe because the runbook requires migrations to stay backward-compatible with the running revision — **SOURCE A:** `R-19` (CI asserts the stamp), `R-23` (runbook rule).
- **CLAIM B:** nothing tests the *old* revision against the migrated schema; the guarantee is a process rule with no enforcement, and the architecture has no expand/contract gate — **SOURCE B:** `A-21`, Phase 4 C-12, Phase 9 C-07.
- **WHY THEY DIFFER:** the runbook states a *practice*; CI verifies only the *stamp*, so a violation is undetectable until a deploy overlaps.
- **WHAT CAN BE VERIFIED:** add a CI matrix job (previous release image × migrated DB) and see whether the first non-additive migration is caught — that is the test of the claim.
- **STATUS:** **OPEN** (low probability, high blast radius: it materialises only during deploys).

---

## 2. Resolved non-contradictions (evidence that the method is discriminating)

| ID | Apparent conflict | Resolution | Why it matters |
|---|---|---|---|
| **K-R1** | "Rate limiter is 5/60 here and 10/60 there" | **Class confusion** — `bot/middleware.py` (10/60, user messages) vs `bot/access_guard.py` (`_DENIAL_REPLY_LIMIT = 3`, window 60, denial replies). Two different controls. | Prevents a future reviewer from "fixing" a limiter that is not broken, and shows the two controls must both be documented. |
| **K-R2** | "Docs say 31 tables; the model file has 29 classes" | **Definitional** — 29 SQLAlchemy models + raw-DDL tables (`referral`, `referralcode`, `conversation_history`, plus the queue and checkpoint tables owned by their own adapters). The count depends on whether adapter-owned tables are included. | The *useful* statement is not the count but K-02 (which plane owns which table). |
| **K-R3** | "The docs pinned a commit that is not HEAD ⇒ the docs are wrong" | **Stale, not wrong** — `M-06` verified `7573249` is an ancestor of HEAD; the documents explicitly state their verification point. | Demotes a would-be "documentation is unreliable" finding to a precise one: *the docs are un-refreshed*, and the drift is measurable. |

---

## 3. FACTS vs ANALYSIS

**FACTS.** K-01, K-02, K-03, K-04, K-06, K-07, K-09, K-13 and K-14 are all citable at exact file/line or by a re-measured count (`M-01`…`M-08`). K-05 and K-10 are `OPEN[NEED-PRIMARY]` because one side is a secondary source. K-08 is resolved against my own earlier reading; K-R1…K-R3 exist so that this register is not a list of everything that looked odd.

**ANALYSIS.**
1. **The contradictions cluster into one root cause: documents and code were each internally consistent at the moment they were written, and nothing measures the gap afterwards.** Four of the six `[V]` contradictions (K-03, K-04, K-06, K-09) are *drift*, not design error: a port gained a claim it never implemented, a security row gained a "closed" that a later route reopened, a diagram kept a recovery command that had been narrowed, and a verification commit was never refreshed.
2. **Two contradictions are *design* contradictions rather than drift** and deserve the main team's attention regardless of drift: K-01 (the durability rule vs the queue's location) and **K-13** (what "queued" promises). They cannot be fixed by documentation alone; they require a decision — which is why `10-adr-candidates.md` contains ADR-C-01, -02, -05, -06 as the direct responses.
3. **A cheap anti-drift control exists and is already half-built:** `test_docs_integrity.py` gates structure; extending it to gate **two or three load-bearing numbers** (current HEAD vs the doc's stated commit; the port/implementation signature match) would have caught K-03, K-07 and K-09 mechanically. That single change prevents the whole drift family — and it is a test, not an architecture change.
