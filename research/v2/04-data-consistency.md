# PHASE 4 — DATA CONSISTENCY LAB

**Question:** when the system is interrupted at every interesting moment, what state is left behind — and is that state *recoverable*, *detectable*, and *explainable to a user*?
**Count:** **18 scenarios**, each with STATE BEFORE / EVENT / STATE AFTER / INCONSISTENCY / RECOVERY.
**Method:** mechanisms read from the pinned tree; where a scenario cannot occur in the current topology I say so explicitly (a *verified negative* is as valuable as a defect).
**Markers:** `[V]` verified mechanism · `[A]` reasoning · `[U]` unverified · all severities use the S1–S5 rule from `README.md` §3.

---

## 0. What the store layer actually guarantees (per store, per operation)

| Store | Atomicity unit | Durability | Idempotent? | Source |
|---|---|---|---|---|
| App DB (SQLite) | one SQLAlchemy session/statement; **no unit-of-work wrapper across steps** | WAL set by `create_all_tables` | only where code checks first | `R-06` |
| App DB (PostgreSQL/Neon) | one session/statement; `pool_pre_ping` guards dropped connections | provider-managed; 6 h PITR on free | as above | `R-06`, `E-01` |
| Queue sidecar (SQLite) | **one statement per context-manager block**; a job's life is ≥3 separate transactions (`pending → processing → completed/failed`) | local file; WAL **not** set; `timeout=30` busy wait | `UNIQUE(idempotency_key)` for *rows*, nothing for *effects* | `R-01` |
| `creative_jobs` (API jobs) | one statement per `aiosqlite` connection | local file, ephemeral disk in cloud | no | `R-16` |
| Checkpoints (LangGraph) | saver-managed; lifecycle metadata written separately | durable per backend | thread-level delete requires a key | `R-21` (`DATA_AND_STORAGE.md` §4, `PORTS.md`) |
| R2 blobs | per-object PUT | durable | `ObjectStoragePort` *declares* an idempotency key; `R2Provider` does **not** implement it | `R-10`, `M-08` |

**Consequence used throughout:** any multi-step user-visible operation in this system is a *sequence of independent transactions*; there is no distributed transaction and no compensating-action engine. The interesting question is therefore never "does ACID hold?" (it does, locally) but "what does the sequence look like after a crash?".

---

## C-01 — Two simultaneous requests from the same chat

- **STATE BEFORE:** history contains the last N turns; `ConversationStore` holds `conv_id` history; rate limiter has a window with <10 events.
- **EVENT:** two updates arrive for the same chat within the same second (PTB processes them concurrently by default); both call the LLM and both append to the same conversation history.
- **STATE AFTER:** two answers are produced from the same base context (the second did not see the first's answer); history contains both appended turns, order determined by completion time, not arrival time.
- **INCONSISTENCY:** the model's answers are mutually unaware; the stored transcript order need not match the user's message order. Neither answer is wrong per se — the *context* is.
- **RECOVERY:** none needed (no durable corruption). Detection: not detectable today (no per-chat serialisation metric). `[V]` mechanism (no per-chat lock; `R-01`/`R-03` show no serialisation), `[A]` severity: Low — S2 at most, unless the two requests are `/daily`-style reward claims (see C-10).

## C-02 — One job executed twice

- **STATE BEFORE:** row `J` exists with `status='pending'`, `idempotency_key='k1'`.
- **EVENT:** two execution contexts claim it. Reachable in three verified ways: (a) `resume_pending()` runs while a task for `J` is live in the same process (`R-01` `_mark_processing` allows `processing → processing` and checks no row count); (b) process A and process B share the sidecar file; (c) `enqueue` is called twice with different keys for the same logical request.
- **STATE AFTER:** the handler ran twice. Both `_mark_completed` calls write the same terminal state; only the *effects* (two Telegram documents, two R2 objects, two XP awards) differ.
- **INCONSISTENCY:** **effect duplication with a consistent-looking record.** The DB says "completed once"; reality is twice. Nothing in the store reveals it.
- **RECOVERY:** none automatic. A human can compare `finished_at` with user reports. To prevent: a claim that only succeeds once (`UPDATE … WHERE status='pending'` + affected-rows) and effect-level idempotency (e.g., deterministic output key).
- **Severity:** **S1** (duplicate user-visible side effects) · `[V]` · see FMEA F-01/F-02.

## C-03 — Process dies mid-transaction (DB write)

- **STATE BEFORE:** an open session, uncommitted `session.add(...)`.
- **EVENT:** SIGKILL (platform scale-in, OOM) before commit.
- **STATE AFTER:** the transaction is never committed ⇒ the row does not exist. SQLite's journal/WAL guarantees the file is consistent; PostgreSQL rolls back.
- **INCONSISTENCY:** **none at the storage layer** — this is the case ACID is designed for. The inconsistency, if any, is at the *product* layer: the caller may have already told the user "done" (C-07).
- **RECOVERY:** retry the request if it is idempotent; otherwise the user must repeat it.
- **Severity:** S2 (lost action) · `[V]` (`R-06`, `E-14`, `E-15`).

## C-04 — Process dies mid-job (queue)

- **STATE BEFORE:** row `J` is `processing`; the handler is running an FFmpeg child.
- **EVENT:** instance is replaced (scale-to-zero / redeploy) or OOM-killed; SIGKILL ⇒ no Python-level `except`/`finally` runs.
- **STATE AFTER:** row stays `processing` forever; the FFmpeg child is reaped by the kernel; a `.part` file may remain in the creative temp dir; the Telegram workspace files may remain (the 24 h sweep only runs on the next job).
- **INCONSISTENCY:** the user was told "queued" and will never receive a result **and** the documented recovery command cannot help: `nexus jobs resume` deliberately claims only `pending` rows (`R-01` `resume_pending_jobs`), while `resume_pending()` (startup path) *does* reset `processing → pending` — but it is only called at process start, which in this topology is exactly what just happened… in a container that no longer exists (C-06 of `Phase 1` A-02, F-04).
- **RECOVERY:** operator runs `nexus jobs resume` → no pending rows → nothing happens; a manual `UPDATE nexus_job_queue SET status='pending'` is required (undocumented, and on ephemeral disk the file may be gone).
- **Severity:** **S1 + S4** · `[V]` · FMEA F-03.

## C-05 — Network timeout to the LLM provider

- **STATE BEFORE:** request sent to a provider; client timeout 60 s (`R-12`).
- **EVENT:** the connection times out. **The provider's outcome is unknown** — it may have completed the completion and billed it.
- **STATE AFTER:** litellm marks the deployment failed (`allowed_fails=1`, cooldown 86 400 s, `R-12`) and the chain falls back to the next provider *in the same user request*. Cooldown state is in-process (lost on restart, C-05 caveat below).
- **INCONSISTENCY:** cost may have been incurred for a call whose result was discarded; the cooldown now suppresses a healthy provider for 24 h (the timeout is treated as a quota event). Conversely, a *hung* provider that never returns burns the full timeout per attempt.
- **RECOVERY:** none automatic. To detect: token accounting per attempt (absent, `U-d`).
- **Severity:** S5 (cost) + S2 (latency stacking: 4 providers × 60 s worst case) · `[V]` config, `[A]` blast radius.

## C-06 — Provider answers, client never receives the answer

- **STATE BEFORE:** as C-05.
- **EVENT:** the response is lost (connection reset after the provider committed the generation, or the process dies between socket read and handler continuation).
- **STATE AFTER:** identical to C-05 from the system's view: the request is retried/falls back, or the update is lost. **There is no request-receipt ledger**, so "did the provider charge me?" is unanswerable in-repo.
- **INCONSISTENCY:** possibly-billed orphaned generation; no record.
- **RECOVERY:** none. Prevention candidate: an LLM-call ledger keyed by `idempotency_key` — note that `LLMPort.complete(..., idempotency_key=…)` exists but **no implementation reads it** (`M-07`, `R-11`), so the ledger is currently impossible without a change.
- **Severity:** S5 · `[V]` (absence of the mechanism).

## C-07 — DB commit succeeds, the message is never sent

- **STATE BEFORE:** handler has computed the answer.
- **EVENT:** the DB step commits (e.g., assistant turn appended, job marked completed) and then the Telegram send fails (403/429/network), or the process dies before the send.
- **STATE AFTER:** the database asserts success; the user sees nothing. For jobs, `_notify_completion` runs **after** the terminal write and its failures are swallowed by design (`R-01`: "a broken notifier must never corrupt the queue or lose a job result").
- **INCONSISTENCY:** **durable "done" with no delivery.** This is the canonical commit-then-notify gap; the queue never retries the notification.
- **RECOVERY:** none automatic. A retry loop for the *notification* (not the job) would fix it; it does not exist.
- **Severity:** **S1** (user-visible lie) · `[V]` · FMEA F-34-adjacent.

## C-08 — Message is sent, the DB is not committed

- **STATE BEFORE:** a scheduled post (`channel_manager.schedule_post`) or a reminder (`tools.ReminderSystem`).
- **EVENT:** the code path sends first and records second, or records via a fire-and-forget task. Verified example: `schedule_post` commits the row, spawns `asyncio.create_task(_send())` that sleeps, posts to the channel, and *then* updates `status='sent'` (`R-08` `channel_manager.py`).
- **STATE AFTER:** if the process dies between `post_to_channel` and the status update, the row still says `scheduled`. If a reconciler (or the same feature after restart) re-runs scheduled rows, the post is duplicated. If nothing runs them, the state lies in the *opposite* direction: it says "scheduled" forever even though the user saw the post.
- **INCONSISTENCY:** message-sent-but-not-recorded; also, scheduled work is **in-memory only** (`self._scheduled_tasks`), so a restart silently drops pending posts while the DB still lists them as scheduled.
- **RECOVERY:** none; the row is misleading either way.
- **Severity:** S1 (duplicate or lost scheduled post) · `[V]` (`R-08`).

## C-09 — Restart mid-render (file-level residue)

- **STATE BEFORE:** `encode_lane` has written `.<name>.part.mp4` and is running FFmpeg.
- **EVENT:** SIGKILL.
- **STATE AFTER:** a `.part` file remains; the previous master (if any) is untouched — this is the *good* case, and it is genuinely good: atomic publish means no half-written master can be mistaken for output (`R-15`).
- **INCONSISTENCY:** only disk residue (counts toward C-42/F-42 disk growth) and a stale job row (C-04). The artifact contract itself is not violated.
- **RECOVERY:** the 24 h sweep (on next job) or `nexus maintenance housekeeping` removes it.
- **Severity:** S4 (disk) · `[V]` — recorded here as a **verified positive**.

## C-10 — Clock changes (NTP step / VM migration)

- **STATE BEFORE:** queue rows ordered by `created_at` (ISO-8601 UTC text, microseconds, `R-01`); `creative_jobs` ordered/created by SQLite `CURRENT_TIMESTAMP` (seconds, UTC).
- **EVENT:** the container clock steps backwards 30 s (rare but real on VM migration), or forward.
- **STATE AFTER:** ordering by `created_at` can invert; a job inserted "earlier" may now sort later ⇒ `resume_pending` (ORDER BY created_at) may run jobs out of order. Daily features keyed on a UTC date can double-award (forward step across midnight) or skip (backward step).
- **INCONSISTENCY:** order and *once-per-day* semantics are wall-clock relative and have no monotonic guard.
- **RECOVERY:** none; a monotonic sequence (SQLite `AUTOINCREMENT`/`rowid`) or a DB-side timestamp would remove the class.
- **Severity:** S1 (double award) / S4 (order) · `[V]` mechanism, `[A]` frequency.

## C-11 — Timezone semantics change (config/regional)

- **STATE BEFORE:** all timestamps stored UTC (correct).
- **EVENT:** a feature computes "today" from UTC while the operator/user expects local days (e.g., Persian users, UTC+3:30 ⇒ the day boundary lands at 03:30 local).
- **STATE AFTER:** no store corruption; a *product* inconsistency: two users in one group can have different "days"; a user's daily quota resets mid-morning.
- **INCONSISTENCY:** not a data-consistency bug, a **contract** gap. Nothing in the repo defines which timezone owns "day".
- **RECOVERY:** not applicable. Fix requires a stated policy (user TZ or fixed product TZ) + migration of any keyed rows.
- **Severity:** S1 (product-visible, "unfairness") · `[A]` (needs owner confirmation) · `12-unknowns.md` U-24.

## C-12 — Schema changes while old code is running

- **STATE BEFORE:** revision `R1` deployed with code `C1`.
- **EVENT:** `nexus migrate` applies `R2` (adds columns/tables); the old revision `C1` keeps serving traffic (Koyeb rolling deploy or a second instance) — the runbook explicitly accepts this: "keep the migration backward-compatible with the currently running revision" (`R-23`).
- **STATE AFTER:** if `R2` is additive, `C1` works. If `R2` renames/drops/narrows, `C1` writes/reads a wrong shape and errors or, worse, writes NULLs.
- **INCONSISTENCY:** the runbook's rule is a *process* control with no test; there is no CI job that runs old code against the new schema (`A-21`).
- **RECOVERY:** forward-fix; downgrade is undefined (`R-06`/`R-23` have no downgrade path).
- **Severity:** S1/S2 · `[V]` (no such gate exists) · FMEA F-40.

## C-13 — Two hosts migrate at once

- **STATE BEFORE:** two containers/one operator and one container start simultaneously.
- **EVENT:** both enter `nexus migrate`. `migration_lock` is an `fcntl.flock` on a **local** temp file (`R-07`) — invisible to the other host. For PG, Alembic DDL races; `create_all_metadata` (SQLite path) retries 6× on "already exists" but Alembic's DDL path has no such retry.
- **STATE AFTER:** either one wins cleanly (common) or one fails mid-chain (possible); a failed mid-chain leaves `alembic_version` at the last successful revision — recoverable by re-running, but with a window where the schema is between revisions.
- **INCONSISTENCY:** transient partial schema; blocked writes for columns not yet present.
- **RECOVERY:** re-run `nexus migrate`; verify with `SELECT version_num FROM alembic_version` (the CI job does exactly this, `R-19`).
- **Severity:** S4/S2 · `[V]` (`R-07`) · FMEA F-12.

## C-14 — R2 upload: ambiguous outcome

- **STATE BEFORE:** `nexus maintenance backup` has dumped the DB; upload begins.
- **EVENT:** the HTTP request times out *after* R2 accepted the object (a classic ambiguous write).
- **STATE AFTER:** the CLI reports failure; the object exists. Re-running produces a **new key** (timestamp-based, `R-17`: `backups/db/<stamp>/<file>`), so the next run is a *different* object, not an overwrite — no corruption, but silent duplication; backup retention (30 days, `R-17`) eventually prunes by stamp, so duplicated stamps are handled.
- **INCONSISTENCY:** "failed" backup that actually succeeded (operator may retry and waste quota). The declared idempotency mechanism (`put(..., idempotency_key)`) is **not implemented** by the adapter (`M-08`), so no de-dup is possible at the port level.
- **RECOVERY:** list the bucket by prefix; delete duplicates manually.
- **Severity:** S4/S5 (operations + quota) · `[V]` · FMEA F-18-adjacent.

## C-15 — SQLite write contention inside a multi-step flow

- **STATE BEFORE:** a feature engine (sync, `R-08`) is mid-write while the async engine writes another table in the same file.
- **EVENT:** the async writer waits up to 30 s (`R-01` sets this only for the queue; the central engine relies on aiosqlite defaults) and then raises `database is locked`.
- **STATE AFTER:** the async transaction rolls back (consistent), but **the surrounding multi-step flow is now half-done**: e.g., an LLM answer was generated, the assistant turn was not stored, the user received the answer anyway (C-07 pattern).
- **INCONSISTENCY:** transcript/state divergence between what the user saw and what the system remembers.
- **RECOVERY:** none automatic; re-sending the message duplicates the answer.
- **Severity:** S2 · `[A]` (needs a load repro) · FMEA F-15.

## C-16 — Split-plane read-after-write

- **STATE BEFORE:** `NEXUS_DATABASE_URL` set; `storage/db.py` sessions hit PG (`R-06`).
- **EVENT:** a feature module (e.g. `features/ads.py` importing `AdCampaign` from `storage/models.py`) writes through its own **SQLite** engine (`R-08`, `M-05`).
- **STATE AFTER:** the row exists only in the local file. Any read through `get_session()` (dashboard, other engines) does not see it; the next deploy/scale-to-zero discards the file.
- **INCONSISTENCY:** classic dual-write divergence with **read-after-write violation**; no reconciliation tool exists.
- **RECOVERY:** none defined. Detection: none (reads just return fewer rows).
- **Severity:** **S1** · `[V]` · FMEA F-09/F-10 · contradiction K-02.

## C-17 — Duplicate webhook delivery of the same update

- **STATE BEFORE:** update `U` (id = 12345) is delivered; the API verifies the secret and returns 200 after enqueuing into PTB's in-memory queue.
- **EVENT:** Telegram re-delivers the same `update_id` (because the first HTTP response exceeded its window, or a proxy duplicated the request, or the process died after responding 200 but before processing). PTB's `Update` objects are *not* deduplicated by the adapter (`R-04`: parse → enqueue; no `update_id` store).
- **STATE AFTER:** the update is processed twice (two answers, two LLM calls, two memory writes).
- **INCONSISTENCY:** duplicated effects; the store shows two assistant turns for one user message.
- **RECOVERY:** none. Prevention: an `update_id` dedupe table (small, bounded, TTL 24 h — matching Telegram's own retention, `E-09`).
- **Severity:** S1/S5 · `[V]` (no dedupe exists) · FMEA F-35.

## C-18 — Checkpoint write fails mid-turn

- **STATE BEFORE:** the graph has produced a response; the checkpointer writes state.
- **EVENT:** the checkpoint write fails (disk full, lock, PG connection drop during Neon idle).
- **STATE AFTER:** by design the answer still returns (`R-21` flow 1 failure contract); the lifecycle hook logs a redacted event and mirrors a metric. Resume ability for that thread is now stale/absent.
- **INCONSISTENCY:** the user's next turn resumes from an older state while messages (the source of truth) are current ⇒ the graph's memory and the transcript disagree.
- **RECOVERY:** the 30-day resume policy forks a new thread from message history when resume is impossible (`R-21` §4) — this is a *designed* recovery, and it is one of the few that exists.
- **Severity:** S2 · `[V]` (documented + implemented) — recorded as a **verified positive with a cost**: silent state rollback.

---

## 3. Guarantee matrix (what survives what)

| Failure | App DB | Queue rows | Queue effects | Checkpoints | Blobs (R2) | Creative jobs |
|---|---|---|---|---|---|---|
| Process kill mid-write | consistent (ACID) | consistent | **may duplicate** | consistent | n/a | row consistent |
| Process kill mid-job | consistent | row stuck `processing` | job lost to the user | stale | n/a | row stuck `pending` (no recovery CLI) |
| Container replaced | **lost if SQLite** | **lost if SQLite** | lost | **lost if SQLite** | safe | **lost (SQLite)** |
| Two processes | contention risk | **double execution** | **duplication** | saver-dependent | safe | double execution possible (no claim) |
| Clock step | ordering only | ordering only | day-boundary effects | ordering only | n/a | ordering only (second precision) |
| Migration + old code | **undefined without expand/contract discipline** | n/a | n/a | n/a | n/a | n/a |

## 4. Verified positives (kept, do not "fix" them)

1. `.part` staging + atomic rename + `overwrite=False` ⇒ a rendered master is never half-written (`R-15`).
2. Migrations are idempotent on re-run and CI asserts the stamp (`R-19`).
3. `delete_thread` requires an idempotency key and fails closed on unknown lineage (`R-21` PORTS).
4. The consent gate (§ `R-08`) is default-deny and its failure mode is "no egress", never "egress anyway".
5. The `create_all` race has an explicit retry with a documented failure taxonomy (`R-06`).

## 5. FACTS vs ANALYSIS

**FACTS.** C-02, C-04, C-07, C-08, C-09, C-14, C-16, C-17 and C-18 are each traceable to specific code paths (`R-01`, `R-08`, `R-15`, `R-16`, `R-17`, `M-08`). C-01, C-05, C-06, C-10, C-11, C-12, C-13 are mechanisms verified by configuration/structure with an assumed frequency.

**ANALYSIS.** The system's consistency story is **"locally ACID, globally best-effort"**. Every single-store operation is safe; every *sequence* is not, and the two places where the sequence is most fragile (queue lifecycle, notify-after-commit) are exactly the places users are given an explicit promise ("queued", "done"). The cheap, targeted class of fixes is therefore not "add transactions" (there is nowhere to put them) but **make each step idempotent and make each promise checkable** — which is precisely the gap Phase 5 quantifies.
