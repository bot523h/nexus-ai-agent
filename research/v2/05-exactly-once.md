# PHASE 5 — EXACTLY-ONCE TRUTH LAB

**Question:** for every surface that can cause a user-visible effect, what delivery/processing guarantee does the system *actually* provide, where is the boundary of that guarantee, and what would be required to strengthen it?
**Rule applied:** no marketing claim is accepted. "Idempotent", "durable", "at-least-once" and "exactly-once" are only used when the *mechanism* is named and located.
**Count:** **7 primary surfaces** + **3 secondary effect surfaces**, each with Guarantee / Boundary / Failure case / Required mechanism.

---

## 0. Definitions used (with primary sources)

| Term | Definition used here | Source |
|---|---|---|
| **at-most-once** | The sender does not retry on timeout/error ⇒ a failure may silently drop the effect | Confluent Kafka docs, delivery semantics (`X-01`) |
| **at-least-once** | On unknown outcome the sender retries ⇒ effects may occur more than once | `X-01`, `X-02` |
| **effectively-once** | At-least-once delivery + **idempotent effect** ⇒ observable outcome is single | `X-01` (idempotent producer = broker-side sequence dedup), `X-03` |
| **exactly-once (delivery)** | Not achievable in a distributed system without coordination; the practical form is a *transaction/2PC + dedup* pair, or an idempotent sink | `X-04`: "there is no such thing as exactly-once delivery in distributed systems (two generals' problem), but we can fake it by implementing idempotent log append" |

**Consequence for this review.** Every "exactly-once" question below reduces to two sub-questions:
1. *Is the transport at-least-once or at-most-once?*
2. *Is the effect idempotent (or is there a dedup key persisted atomically with the effect)?*

If the answer to (2) is "no", then the surface cannot be better than at-most-once **or** at-least-once — and which one depends on the retry policy of the *sender*, not on the receiver's good intentions (`X-05`: "a timeout means the outcome is unknown").

---

## X-01 · Telegram inbound delivery (updates reaching the bot)

| Field | Finding |
|---|---|
| **Guarantee** | **At-least-once** for polling; **at-least-once under retry, effectively at-most-once after a 200 in webhook mode** |
| **Mechanism / boundary** | Polling: Telegram holds updates ≤24 h and returns them until the offset advances past `update_id` (primary: `X-06` Telegram Bots FAQ — an update is confirmed *only* by a later `getUpdates` offset). Webhook: Telegram retries when the endpoint does not answer 2xx; once the app answers `200 {"ok": true}`, delivery is considered complete (`R-04`, `E-09`). The boundary is the 200 response in `api/app.py::telegram_webhook`. |
| **Failure case** | `R-04` returns 200 **after** handing the update to PTB's **in-memory** queue. A crash between the 200 and processing (or after processing begins) loses the update permanently; there is no `update_id` dedupe store and no persisted inbox. Conversely, a slow handler that exceeds Telegram's response window causes a **retry of the same update** ⇒ potential double processing (no dedupe). |
| **Required mechanism for effectively-once** | A durable inbox: persist `(update_id, received_at, processed_at)` on receipt *before* answering 200, dedupe on `update_id` at processing time, and answer 200 only after the row is committed (this is exactly the pattern `X-07` argues for: confirm only after durable processing). |
| **Current verdict** | **At-most-once with an at-least-once retry hazard** — the weakest combination, because it can both lose and duplicate. `[V]` (`R-04`), `[V]` (`X-06`). |

## X-02 · Database transactions (effects inside the app DB)

| Field | Finding |
|---|---|
| **Guarantee** | **Exactly-once *within* a transaction; no guarantee across steps** |
| **Mechanism / boundary** | SQLite/PostgreSQL give ACID per transaction (`R-06`, `E-14`, `E-15`). The boundary is the session/statement: `get_session()` yields a session and *the caller* decides when to commit; there is no unit-of-work that spans "generate answer → store turn → notify user". |
| **Failure case** | A crash between two commits leaves a valid database with a half-completed *business* operation (C-03/C-07 in Phase 4). A retried request then repeats the first half. |
| **Required mechanism for effectively-once** | Either (a) a business-level `operation_id` written in the *same transaction* as the effect, checked before re-execution, or (b) an outbox row written atomically with the state change and drained idempotently. Neither exists. |
| **Current verdict** | **Exactly-once per statement, at-least-once per user action** (because the outer retry/duplication paths are unguarded). `[V]` |

## X-03 · The job queue (enqueue → execute → terminal state)

| Field | Finding |
|---|---|
| **Guarantee** | **Enqueue: effectively-once (row level). Execution: at-least-once (effect level). Terminal state: exactly-once (row level, last-writer-wins).** |
| **Mechanism / boundary** | `UNIQUE(idempotency_key)` + "return the existing id" makes `enqueue` row-idempotent (`R-01`). Execution is guarded only by an in-process `dict` of tasks and a `UPDATE … WHERE status IN ('pending','processing')` that neither checks affected rows nor excludes a concurrently-running job (`A-04`, `A-05`). The boundary is **the process**: inside one process, the task map prevents duplicates; across processes (or after a crash with `resume_pending`) it does not. |
| **Failure case** | Two processes (`bot` + `nexus jobs resume`, or two replicas) both run the same job ⇒ duplicate Telegram document, duplicate XP, duplicate R2 object. Verified mechanism, not hypothetical (`R-01`, `R-02`, `R-03`). |
| **Required mechanism for effectively-once** | An atomic claim that only one claimant can win: `UPDATE … SET status='processing', owner=?, lease_until=? WHERE id=? AND status='pending'` **and** verify `rowcount == 1` before running; plus an idempotent effect key (e.g., deterministic output object name derived from `job_id`, so a duplicate run overwrites rather than duplicates). Alternatively a broker/DB queue with visibility timeouts and per-message ack. |
| **Current verdict** | **Effectively-once in the single-process happy path, at-least-once (duplicating) everywhere else.** This is the single most consequential gap in the system, because jobs are the *promise* surface ("queued"). `[V]` |

## X-04 · Scheduler (scheduled posts, reminders, housekeeping)

| Field | Finding |
|---|---|
| **Guarantee** | **At-most-once for in-process scheduled work; exactly-once only for cron jobs owned by GitHub Actions** |
| **Mechanism / boundary** | Scheduled posts: row committed, then `asyncio.create_task(_send())` sleeps and posts, then updates `status='sent'` (`R-08` `channel_manager.py`) — the timer lives in `self._scheduled_tasks`, i.e. **in process memory**. Reminders: same pattern with `self._tasks` (`R-08` `tools.py`). Housekeeping/backup: GitHub Actions cron (`R-19`), which is durable but has no in-app lock beyond a workflow `concurrency` group. |
| **Failure case** | Restart between scheduling and firing ⇒ the post/reminder never happens while the DB says `scheduled`/pending (silent loss). Crash after posting but before `status='sent'` ⇒ state says "not sent" while the user saw it; any future reconciler that trusts the row would duplicate. |
| **Required mechanism for effectively-once** | Durable scheduler: next-fire time in the DB + a claim+lease per fire + idempotent effect key (`schedule_id`+`fire_at`, or a `sent_at` guard written atomically). |
| **Current verdict** | **At-most-once with a state/lie risk.** `[V]` |

## X-05 · External API calls (LLM, image providers, R2, Telegram outbound)

| Field | Finding |
|---|---|
| **Guarantee** | **At-most-once from the app's perspective; unknown outcome from the provider's perspective** |
| **Mechanism / boundary** | One timeout per call: LLM 60 s (`R-12`), image 120 s (`R-08` `image_gen.py`), creative URL download 60 s (`R-04`), FFmpeg 900/600 s (`R-15`). No retry of the same provider (`num_retries=0`); the chain moves down (`R-12`). There is **no request ledger**, and `LLMPort.complete(..., idempotency_key=…)` is not read by any implementation (`M-07`). |
| **Failure case** | Timeout on a provider that actually generated (and billed) the answer ⇒ cost without value, invisible (`C-05`/`C-06`). Outbound Telegram send failure after a DB commit ⇒ "done" state with no delivery (`C-07`). R2 upload timeout after acceptance ⇒ duplicate object, "failed" report (`C-14`). |
| **Required mechanism for effectively-once** | Provider calls: idempotency keys **implemented** end-to-end (key → ledger row → skip/return stored result), or accept at-most-once and record *attempts* for accounting. Outbound sends: an outbox with a `(chat_id, message_key)` dedup table and retry — Telegram has no idempotency key, so dedup must be local. |
| **Current verdict** | **At-most-once, with unbounded ambiguity for cost.** `[V]` |

## X-06 · Webhook surface (the app as a webhook *receiver*)

| Field | Finding |
|---|---|
| **Guarantee** | **Rejects duplicates only by secret; no message-level dedupe** |
| **Mechanism / boundary** | `secrets.compare_digest` on `X-Telegram-Bot-Api-Secret-Token` (`R-04`) — an *authentication* control, not a *replay* control. There is no nonce/timestamp store for `/webhook/telegram`, and no `update_id` uniqueness constraint. |
| **Failure case** | Replay of a captured request (within the secret's lifetime — the secret does not expire) ⇒ the update is processed again. Telegram's own retry (non-200) ⇒ duplicate processing (see X-01). |
| **Required mechanism for effectively-once** | `update_id` unique index + "insert-or-ignore" before enqueue; for the creative endpoint, a nonce cache or a body-hash idempotency key (HMAC already covers integrity, not uniqueness). |
| **Current verdict** | **At-least-once (replayable) with no dedupe.** `[V]` |

## X-07 · LLM call (the effect that costs money and can leak)

| Field | Finding |
|---|---|
| **Guarantee** | **At-most-once attempt per provider per request; at-least-once across the fallback chain; no dedupe** |
| **Mechanism / boundary** | `num_retries=0` prevents in-place retry; `fallbacks` move to the next provider (`R-12`). Therefore **one user request can reach up to 4 providers** (Ollama → Groq → Gemini → OpenRouter), and a timeout on provider *i* does not tell the app whether *i* completed. Boundary: the chain, not the call. |
| **Failure case** | Quota exhaustion is *sticky per process* (cooldown) but not per cluster (A-31); after each cold start the chain re-probes drained providers ⇒ a single request can burn attempts on every drained provider before succeeding or degrading. Duplicate *output* is not the risk here (the user gets one answer); duplicate *cost/quota* is. |
| **Required mechanism for effectively-once** | A per-request ledger (`request_id`, provider, started/finished, outcome, tokens) written before dispatch, plus a quota/circuit state that lives **outside** the process. With that, "effectively-once billing visibility" becomes possible; without it, cost is unverifiable (`U-d`). |
| **Current verdict** | **At-least-once cost, at-most-once answer.** `[V]` |

---

## Secondary effect surfaces

### X-08 · Rendered artefact publication (the strongest guarantee in the system)
| Field | Finding |
|---|---|
| **Guarantee** | **Effectively-once publication** |
| **Mechanism** | Staged `.part` file + `replace()` atomic rename + `overwrite=False` by default + post-hoc probe and sha256 (`R-15`); the destination is never a half-written file, and a re-run cannot silently clobber by default. |
| **Boundary** | Same filesystem; a crash leaves only the `.part` file. |
| **Residual failure** | If two runs target the *same* destination with `overwrite=True`, the last writer wins (expected); and the *notification* of the artefact is a separate, weaker surface (X-01/X-05). |
| **Verdict** | `[V]` — this is the pattern the rest of the system should copy (idempotent sink + atomic publish), and it shows the team already knows how to build it. |

### X-09 · Blob storage (`ObjectStoragePort` / R2)
| Field | Finding |
|---|---|
| **Guarantee** | **Declared: exactly-once via idempotency key. Actual: at-least-once with timestamp-keyed uniqueness** |
| **Mechanism / boundary** | The port declares `put(key, content, idempotency_key)`; `R2Provider` implements `upload(local_path, remote_key)` — no key, no dedup, and the type of the content differs (bytes vs path) (`M-08`). Backup keys embed a timestamp (`R-17`), so re-runs *add* objects rather than overwrite. |
| **Verdict** | The idempotency guarantee exists **in the interface only** ⇒ `[C]` (contradiction K-07). |

### X-10 · Checkpoint deletion
| Field | Finding |
|---|---|
| **Guarantee** | **Effectively-once deletion (single unit = whole thread + required idempotency key + fail-closed on unknown state)** |
| **Mechanism** | `delete_thread(thread_id, *, idempotency_key)` on the port; unknown lineage/schema/lock ⇒ no delete; per-checkpoint surgery is explicitly `POST_V1` (`R-21`). |
| **Verdict** | `[V]` — second verified-positive surface. |

---

## Summary matrix

| Surface | Transport guarantee | Effect idempotent? | Net guarantee today | What it needs for *effectively-once* |
|---|---|---|---|---|
| X-01 Telegram inbound | at-least-once (retry) / at-most-once after 200 | **no** | **both losses and duplicates possible** | durable inbox + `update_id` dedupe, ack after commit |
| X-02 DB transaction | exactly-once per statement | n/a | per-statement only | business `operation_id` in the same transaction, or an outbox |
| X-03 Queue | row-level effectively-once, effect-level at-least-once | **no** | duplicates across processes/restarts | atomic claim + lease, effect key derived from `job_id` |
| X-04 Scheduler | at-most-once (in-process timers) | **no** | silent loss + state lie | DB-driven next-fire + claim + atomic `sent_at` guard |
| X-05 External API | at-most-once attempt | **no** | ambiguous cost, undetectable | request ledger (the port already has a key slot — unimplemented) |
| X-06 Webhook (in) | at-least-once (replayable) | **no** | replayable | `update_id` unique index / nonce cache |
| X-07 LLM call | at-least-once across chain | no | cost ambiguity; chain re-probe | externalised quota/circuit state + call ledger |
| X-08 Artefact publish | n/a | **yes** | **effectively-once** ✅ | (already correct — do not regress) |
| X-09 Blob store | at-least-once | declared only | interface lies (`[C]`) | implement the port or delete the claim |
| X-10 Checkpoint delete | n/a | **yes** (key required) | **effectively-once** ✅ | (already correct) |

**Score: 2 of 10 surfaces are effectively-once, and both are ones where an artefact (not a message) is the sink.**

---

## FACTS vs ANALYSIS

**FACTS.**
1. Telegram's offset semantics make polling at-least-once by design (primary source: `X-06`).
2. The webhook path acknowledges before durable processing and keeps no update ledger (`R-04`).
3. The queue's dedup is row-level, its execution guard is process-local (`R-01`).
4. The LLM port's idempotency key is unused (`M-07`); the object-storage port's is unimplemented (`M-08`).
5. Two surfaces (render publication, checkpoint deletion) already implement idempotent-effect patterns (`R-15`, `R-21`).

**ANALYSIS.**
1. **The system's guarantee is asymmetric in the wrong direction.** The strongest guarantees protect *files* (artefacts) and *cleanup*; the weakest protect *messages to users* and *money spent*. Users experience the weak surfaces.
2. **"Exactly-once" is not required to fix this.** For every surface above, the practical target is *effectively-once*: an at-least-once transport plus one dedup key persisted atomically with the effect. That is a small, local change per surface (a unique index, an `affected-rows` check, a `sent_at` guard) — not a distributed-transaction project. The dependency port (`JobQueuePort`) is already the right place to put the claim primitive.
3. **Where the receipt honestly cannot be perfect**, the system should say so — e.g., a "duplicate-protected" flag on notifications, or a user-visible "delivery unconfirmed" state. Today the user is told "queued" and gets either nothing, or two things, with no way to know which guarantee applied.
4. **Cost accounting is a prerequisite for security work.** Without a call ledger (X-07) neither quota exhaustion nor prompt-injection-driven abuse can be bounded — this links Phase 5 to Phase 6 (S-24) and Phase 8 (cost).
