# Job Lifecycle Contract (canonical)

> Status: **living view** — enforced by `tests/unit/test_job_lifecycle.py`,
> `tests/unit/test_failure_semantics.py`, `tests/integration/test_job_lifecycle_queue.py`,
> `tests/integration/test_gate5_closure.py`, and the guarded writes in
> `src/nexus_ai_agent/adapters/in_process_job_queue.py`. Delivered by task-178
> (Agent C, 2026-09-24), closed by task-180 (verification GAPs, 2026-09-24)
> and task-181 (failure semantics + 6-state taxonomy + publication order,
> 2026-09-24). Task-232 adds queue-only stable request identity, JSON attempt
> history, and a re-readable verified passport for `timeline.trim`; it does
> not persist the canonical CreativeWork/Project/CommandBus lineage. PR #154's
> historical task-231 queue label was corrected after task-231 became the
> merged provenance-ledger identity (§10bis). Decision records:
> `docs/DECISION_LOG.md` D-0017, D-0018, D-0019.
>
> **task-283** (queue ownership & publication closure) adds §3a (process
> ownership, startup recovery, crash/restart), §4a (publication/rollback
> states) and §11 (the handler side-effect boundary), and rewrites the lock
> primitive described in §3a.  Locks are no longer described as a PID
> lock-file because they no longer are one.

## 1. The canonical chain

Every step is explicit. None is implicit, and no step may be skipped by a
handler or a caller:

```mermaid
flowchart LR
    A[Command<br/>surface / API / CLI] --> B[Job<br/>durable row, idempotency-keyed]
    B --> C[Runtime Execution<br/>packs registry → CommandBus → lane]
    C --> D[Artifact Verification<br/>independent re-measurement]
    D --> P[Publication<br/>atomic rename + re-probe (pdf lane)]
    P --> E[Result<br/>verified facts / typed failure]
    C -- raise --> F[FAILED_RETRYABLE / FAILED_TERMINAL<br/>fail-closed, classified]
    D -- mismatch --> F
```

Ownership map:

| Step | Owner | Module |
|---|---|---|
| Command validation, staging, idempotency key | surface | `bot/creative_surface.py` |
| Durable job row, state machine, verification phase | **Job layer** | `adapters/in_process_job_queue.py`, `application/ports/job_queue.py`, `jobs/` |
| Execution semantics, artifact publication, probe/sha evidence | **Runtime (Agent 1)** | `creative/rendering/*`, `creative/packs/*`, `creative/studio/*`, `creative/slideshow/ffmpeg.py` |
| Worker adapter (chain assembly, typed failures) | worker | `creative/render_jobs.py` |
| Result rendering to the user | notifier | `bot/app.py` (grandfathered) |

The Job layer never re-implements runtime semantics: the verification
evidence functions *are* the runtime's (`sha256_file`, the allow-listed
binary probe `probe_video` — the architecture's ffprobe-equivalent).

## 2. State machine

Persisted values (enum `JobStatus`, task-181 failure taxonomy — see
D-0019 for the compatibility mapping): `pending`, `processing`,
`verifying`, `completed`, `failed_retryable`, `failed_terminal`.
Canonical aliases: `RUNNING ≡ PROCESSING`, `SUCCEEDED ≡ COMPLETED`
(`jobs/lifecycle.py`). Rows written by pre-task-181 code as `"failed"` read
back as `failed_terminal` (`jobs.lifecycle.parse_job_status` — the old
contract had one undifferentiated terminal failure).

```mermaid
stateDiagram-v2
    [*] --> pending : enqueue (durable, UNIQUE key)
    pending --> processing : ① reservation CAS — WHERE status='pending', attempt+=1 mints the fencing token
    pending --> failed_terminal : claim-time structural failure (no handler; unfenced PENDING-only CAS)
    processing --> verifying : ② fenced CAS (attempt = claim) — handler returned a dict
    processing --> failed_retryable : ② fenced CAS — typed failure / raise RETRYABLE
    processing --> failed_terminal : ② fenced CAS — typed / bare success:false / raise TERMINAL
    verifying --> completed : ③ ownership re-read → publish (.prev kept) → re-probe → ④ fenced CAS = DURABLE COMMIT → notify ✅ → finalize (.prev dropped)
    verifying --> failed_retryable : refusal → retract (staged temp, or .prev restored) → fenced CAS → notify ⚠
    verifying --> failed_terminal : refusal → retract → fenced CAS → notify ❌
    processing --> pending : cancel / shutdown — fenced (own row only)
    verifying --> pending : cancel / shutdown — fenced (own row only)
    processing --> pending : ⑤ takeover — resume_pending (startup, or started_at older than stale_after)
    verifying --> pending : ⑤ takeover — resume_pending
    completed --> [*]
    failed_retryable --> [*]
    failed_terminal --> [*]
```

Legend — ① ownership boundary (R5); ② fencing token on every worker
transition (F2); ③ side-effect fencing point (`_owns_execution` immediately
before the rename); ④ the only licence to notify success is `rowcount == 1`
of this UPDATE (F3); ⑤ the only takeover path — never a reservation. A
stale execution's ②/④ write matches zero rows → `job_transition_rejected`,
nothing announced, nothing published, nothing retracted. Crash recovery:
a crash anywhere before ④ leaves a `processing`/`verifying` row that ⑤
reopens; the next ① mints a higher token, so a zombie of the crashed
execution is fenced out. For queue-only `timeline.trim`, task-232 also
checkpoints the verified passport and attempt before ④; recovery can re-read
and re-verify that checkpoint, then reconcile completion without invoking the
renderer again. A crash before that checkpoint still allows at-least-once
handler execution. Other job types retain their existing re-execution
behavior; a crash between publication and ④ leaves the published bytes +
`.prev`, which the re-execution replaces (§6).

**Both failure states are terminal as implemented.** The reserved
`failed_retryable → pending` scheduler retry edge is deliberately **outside**
the transition matrix — there is no retry scheduler in this repository, and
`assert_transition` refuses the edge (fail-closed). `failed_retryable`
records retry-*eligibility* for that future edge; it never implies a retry
happened.

Transition matrix with owner + invariant (`jobs/lifecycle.TRANSITIONS`,
unit-enforced; the adapter enforces each edge with a status-conditioned
`UPDATE … WHERE status IN (…)` and — for every edge a *worker* performs —
`AND attempt = <claim>` (§2a), reading `rowcount` as the verdict — a blind
write cannot produce an illegal edge, and a stale execution's write affects
zero rows):

| Edge | Owner | Invariant |
|---|---|---|
| `pending → processing` | queue (reservation CAS) | **PENDING-only** CAS (`WHERE status = 'pending'`, `rowcount == 1`); `started_at` set; `attempt += 1` mints this execution's fencing token (`ExecutionClaim`) and appends its attempt record to `attempt_history_json` in the same queue-row transaction; a `processing` row is **not** re-claimable — takeover happens only through `resume_pending` (§2a) |
| `pending → failed_retryable` | queue (claim-time) | structural failure classified RETRYABLE (a deploy can make the identical job succeed); zero side effects |
| `pending → failed_terminal` | queue (claim-time) | structural failure classified TERMINAL (e.g. no handler); zero side effects |
| `processing → verifying` | queue (fenced) | handler returned a dict; nothing trusted yet; `attempt = claim` |
| `verifying → completed` | queue (fenced) | verification OK (+ ownership re-check + atomic publish + re-probe on published lanes); verified facts persisted under `artifact_verification`; `attempt = claim`; **the `rowcount == 1` of this UPDATE is the only licence to notify success** |
| `verifying → failed_retryable` | queue (fenced) | typed `verification_failed:<code>` persisted (artifact-damage class); this attempt retracted (staged temp, or previous artifact restored after a refused re-probe) |
| `verifying → failed_terminal` | queue (fenced) | deterministic handler defect (no claim / malformed sha / mis-routed publication); this attempt retracted |
| `processing → failed_retryable` | queue (fenced, fail-closed) | typed user failure / bare `success: false` (`unspecified`) / raise classified RETRYABLE — NEVER converted to success |
| `processing → failed_terminal` | queue (fenced, fail-closed) | typed user failure / raise classified TERMINAL |
| `processing/verifying → pending` | queue (cancel/shutdown, fenced) | process-lifecycle recovery of **this execution's own row only** (`attempt = claim`); a newer owner's row is never reopened |
| `processing/verifying → pending` | `resume_pending` (takeover, unfenced by design) | startup recovery (`stale_after=None`: rows this process is not executing) or expiry-gated reclaim (`stale_after=Δ`: only rows with `started_at` older than Δ); `attempt` untouched — the next reservation mints a new token and fences the previous owner out |
| terminal states (incl. both failures) | — | no outgoing edges (guarded UPDATEs); `failed_retryable → pending` is RESERVED for a future scheduler — not implemented, fail-closed |

### 2a. Execution ownership (Gate 5 final repair — fencing token)

**Who owns a job?** Exactly one *execution* at a time: the one whose
`pending → processing` CAS committed. That CAS increments `attempt`, and the
value it minted is the execution's **fencing token** (`ExecutionClaim(job_id,
attempt)`, `jobs/lifecycle.py`; `FENCING_COLUMN = "attempt"`). Every
transition a worker performs afterwards (`_mark_verifying`, `_mark_pending`,
`_mark_completed`, `_mark_failed`) is one SQL statement of the form

```sql
UPDATE nexus_job_queue SET status = ?, … WHERE id = ? AND status IN (<expected>) AND attempt = ?
```

and returns `cursor.rowcount == 1`. A superseded execution (cancelled worker,
crashed-and-recovered row, a second process) holds an older token: its
writes match zero rows, it is told so, and it must then **do nothing** — no
state change, no notification, no publication, no retraction of the
destination. This is Kleppmann's fencing-token rule applied to the row
itself (the storage compares the token, not the client), the same repair
pg-boss adopted for its stale-`complete()` bug (pg-boss #925).

Why `attempt` and not a UUID / lease column (D-0020): the token must be
minted atomically with the claim, be strictly monotonic per row, and cost
no schema change — `attempt` already is all three. Wall-clock leases were
rejected as the *fence* (clock skew, no ordering) and kept only as the
*takeover policy* (`resume_pending(stale_after=…)`).

Takeover is explicit and never silent: only `resume_pending` may move a
live `processing`/`verifying` row back to `pending`, either at process
startup (`stale_after=None` — the pre-existing recovery, now skipping rows
this process is itself executing) or expiry-gated (`stale_after=Δ` —
rows whose **heartbeat** is fresher than the cutoff are left to their
owner; `started_at` age is no longer the liveness signal, task-256). A
reservation never takes a `processing` row over (the pre-repair `status IN
('pending','processing')` claim let two processes own one execution — R5).

**Heartbeat liveness (task-256).** In-flight rows carry a queue-owned
`heartbeat_at` written atomically with the reservation attempt and refreshed
every 5 s in `processing`/`verifying` under an attempt-fenced CAS. Recovery
takes over only a row whose valid heartbeat is strictly older than the
cutoff; a fresh/equal heartbeat or an UNKNOWN (missing/null/malformed/
naive/non-UTC/future) heartbeat is refused — startup recovery also refuses
UNKNOWN legacy rows rather than resurrecting an unprovable attempt. No fake
heartbeat is ever injected.

**Side-effect fencing point.** For published lanes the irreversible step is
the rename onto the final name. Ownership is re-read immediately before it
(`_owns_execution(claim, VERIFYING)`): a stale execution publishes nothing
(`verification_failed:stale_execution`, no retract). **Residual window
(documented, not hidden):** between that read and the `os.replace` there is
no token the filesystem could check; the window is bounded to microseconds
of a single worker and can only be entered if a takeover happened *in that
interval*, which requires `resume_pending` (startup or expiry) — never a
concurrent reservation. Closing it fully would require token-aware storage
(D-0020 records this as out of scope).

**Q1 — when does a Job become RUNNING?** At the reservation compare-and-set
(`_mark_processing`), *before* the handler is invoked. It is not "after
authorization" (authorization/preflight lives inside the handler, where the
capability registry and payload schema actually are) and it is not defined
by "the first side effect" (unknowable from the queue). Evidence: the
reservation must be durable *before* the world is touched, so crash
recovery (`resume_pending` claims orphaned `processing` rows) has exactly
one owner to blame, and `started_at`/`attempt` are meaningful. The
side-effect boundary itself is defined below and enforced by contract +
runtime staging.

## 3. Side-effect boundary (exactly one)

`NO EXECUTION SIDE-EFFECT` before **all** of:

1. `authorization` — surface/HMAC gate + payload schema validation
2. `capability` — operation ∈ packs `CapabilityRegistry` allow-list
3. `references` — input media contained in its workspace, readable
4. `idempotency` — durable UNIQUE-key collapse at enqueue; the `creative_render` lane also rejects a same-key payload fingerprint mismatch before scheduling
5. `revision/precondition` — guarded status CAS (row in expected state)
6. `job reservation` — `pending → processing` owns the execution

Then `SIDE-EFFECT ALLOWED`, and the first lawful side effect is a write to
a **staging** path — never the final destination. The final name appears
only via atomic rename: media through the runtime's `.part` staging
(`creative/rendering/executor.py`), documents through
`render_jobs._atomic_write_text` (temp file → `os.replace`). A
half-written destination is never visible under a final artifact name.

## 3a. Process ownership, startup recovery and crash/restart (task-283)

**One queue sidecar, one executor.** `InProcessJobQueue` opens `<db>.jobs.sqlite3`;
if two *live* processes open it, both can recover and run the same rows. Row
level CAS fencing (§2) prevents most duplicate *commits*, but it cannot undo an
**external side effect a duplicate handler already caused** (see §11). So the
process layer, not the row layer, is the first authority boundary.

### The ownership invariant

> *At most one live process holds the exclusive lock on the sidecar's
> rendezvous inode, and a process holds it for exactly the interval between
> `acquire_ownership()` and `shutdown()` — or its own death.*

The primitive is an **operating-system-managed advisory lock**:
`fcntl.flock(fd, LOCK_EX | LOCK_NB)` on a descriptor for `<db>.jobs.sqlite3.lock`.
Because ownership is mandatory, a platform without `fcntl` **fails closed** with
`QueueOwnershipError` rather than falling back to a weaker lock-file/PID scheme
(`_require_posix_locking`). NEXUS targets Linux — Dockerfile, Koyeb, and every
CI job run `ubuntu-latest`. Consequences that the previous implementation
lacked:

| Property | Why it holds |
|---|---|
| crash-safe release | the kernel drops the lock when the owner's descriptors close — `SIGKILL` included |
| no stale-lock reclamation | there is no "read PID → test liveness → unlink → retry" step, so there is no window to race |
| PID reuse cannot transfer ownership | ownership is a descriptor property, not a number |
| a garbage/absent PID is harmless | the PID written into the file is an **operator hint only**, never evidence |
| one inode forever | the anchor file is never unlinked; unlinking it lets a concurrent opener lock a *different* inode while another still holds the old one (two owners on two inodes) |

`QueueOwnershipError` is raised, never swallowed: **a process that cannot prove
ownership must not recover or execute.** `_release_flock()` unlocks, closes the
descriptor and is idempotent (safe after a failed acquisition, safe twice).

### Startup sequence (every composition root)

```
1. build queue  →  register handlers  →  register publications
2. acquire_ownership()        # FAIL CLOSED: RuntimeError / typer.Exit(2)
3. journal backfill           # recovery-adjacent, ownership already held
4. resume_pending()           # startup recovery
5. serve / drain
   6. shutdown()              # cancel workers → cancel heartbeat children
                              # → reopen their attempts → RELEASE ownership
```

Ownership is acquired **before** step 3, and recovery is never reached without
it. Shutdown releases **after** the workers and their heartbeat children have
stopped; releasing earlier lets a second process acquire the queue and recover a
row this one is still executing.

| Root | Acquires | Releases |
|---|---|---|
| `bot/app.py::_post_init` / `_post_shutdown` (polling) | step 0 of `_post_init` | `_post_shutdown` → `job_queue.shutdown()` |
| `bot/webhook.py::_serve_webhook` | re-asserted before `resume_pending()` (idempotent; `post_init` normally owns it) | `_cleanup_webhook_application()` → `_shutdown_webhook_job_queue()` → `job_queue.shutdown()` (the `post_shutdown` hook repeats it; both are idempotent) |
| `nexus jobs drain` (`cli.py::_run`) | before backfill/`resume_pending_jobs` | `finally` → `shutdown()` on every exit path |
| one-shot intent runner (`cli.py::_run_all`) | before the first enqueue | `finally` → `shutdown()` |

**A root that acquires must release by itself (review finding 2).** The webhook
root used to rely on PTB's `post_shutdown` hook to reach `job_queue.shutdown()`.
That hook is skipped when the application object is built elsewhere and is
bypassed when `application.shutdown()` raises first — in both cases the flock
outlived the process's useful life and every successor failed to open the queue.
`_cleanup_webhook_application()` now runs an explicit, bounded, idempotent
release after `stop()` and before `application.shutdown()`, and it runs **even
when an earlier cleanup step failed** (each failure is captured and the first one
re-raised, in acquisition order, so a broken `stop()` cannot strand workers,
heartbeat children or the lock).

**Task cancellation is not thread cancellation (review finding 1).**
`publication.publish` runs on an executor thread. Cancelling the awaiting worker
task stops the *await*, not the thread, so the previous shutdown could gather its
tasks and release the lock while a rename was still in flight — a successor could
then own the queue while the dying owner rewrote the same destination. Shutdown
now:

1. closes publication (no *new* publish may start: it fails closed with
   `shutdown_in_progress` and publishes nothing),
2. cancels and awaits worker tasks,
3. cancels and awaits heartbeat children,
4. **drains every tracked publication thread**,
5. reopens this queue's attempts under their own `ExecutionClaim` fence,
6. releases the lock.

The drain handle is the future returned by `run_in_executor`, awaited through
`asyncio.shield` so cancellation cannot mark it done while the thread runs, and
unregistered by its done-callback (i.e. when the thread has really finished), not
by the cancelled awaiter. Proven by a barrier-controlled test
(`tests/integration/test_queue_ownership_publication_barrier.py`): the
publication is held open, `shutdown()` is shown *not* to return, ownership is
shown still held, a contender is refused, and the publication's own thread
observes the lock still held when it finishes. `M9`/`M10` in
`scripts/queue_ownership_mutations.py` prove both halves are load-bearing.

**Residual limit (stated, not hidden):** a publication that was already inside
`os.replace` when shutdown began cannot be un-renamed; draining guarantees it
*completes before ownership is released* and that the row stays non-terminal, so
recovery re-executes rather than assuming success (§4a, S3 crash).

Enforced structurally by
`tests/architecture/test_queue_ownership_startup_contract.py` (AST: acquire must
precede recovery in every root) and behaviourally by
`tests/integration/test_queue_ownership_lifecycle.py` /
`tests/integration/test_queue_ownership_processes.py` (real processes).

### Crash / restart behaviour

* **Clean shutdown** → workers stopped, in-flight attempts reopened to `pending`
  under their own `ExecutionClaim` fence, lock released.
* **Crash (`SIGKILL`, OOM, power loss)** → the kernel releases the lock; the next
  start acquires it and `resume_pending()` takes the orphaned rows. The
  heartbeat rule (§2, PR #283) still decides *which* orphaned rows are safe to
  take over: a proven-live heartbeat is never taken over, and an unknown
  heartbeat refuses takeover. **Ownership makes recovery singular; the heartbeat
  makes it safe.**
* **Ownership failure** (another live process) → startup aborts; nothing is
  recovered, nothing executes.

## 4. Artifact verification contract

**Execution success ≠ Job success.**

```
execution success + artifact verification success = SUCCESS eligibility
```

For job types with a registered verifier, the queue moves the row to
`verifying` after the handler returns and re-measures the claim
**independently** — the verifier reads the filesystem; it never trusts
the handler's word, and a crashing verifier fails the job closed
(`verification_failed:verifier_crashed`).

Default registry (task-178 established it; task-180 closed the GAPs —
every job type in `worker.default_job_handlers` is now verified):

| Job type | Verifier | Artifact dialect |
|---|---|---|
| `creative_render` | `jobs.creative_verification.creative_render_verifier` | `output.mp4` / `captions.srt` / `timeline.otio` in the job workspace; media probe |
| `slideshow_render` | `jobs.creative_verification.slideshow_render_verifier` | the rendered master at the dispatched `output_path` inside the job workspace; real media probe |
| `story` | `jobs.feature_verification.story_verifier` | the Pillow-rendered PNG at the dispatched `output_path`; Pillow structural decode |
| `pdf_extract` | `jobs.feature_verification.pdf_extract_verifier` | the extracted text layer **staged** at `<stem>.extracted.txt.staged`, then published (atomic `os.replace`) to `<stem>.extracted.txt` and re-probed — see the publication order below; whole-file UTF-8 decode |

`tests/architecture/test_verification_registry_ratchet.py` fails if a
handler is ever registered without a verifier — no job type can silently
regress to unverified "execution success = job success" semantics.

Three identities (found in the tree, not assumed; evidence in
`creative/render_jobs.py`, `creative/rendering/compiler.py`):

| Identity | Content | Verified how |
|---|---|---|
| logical | `project_id = shot-<idempotency_key>` + output asset id | re-derived from the payload's key at verification; persisted in the result |
| spec | canonical operation id + compiled lane IR hash (`CompiledLane.ir_hash = sha256(canonical payload)`) | recorded by the worker (`spec_ir_hash`), persisted in the result |
| physical | output path (job workspace, or the queue-owned attempt store for archived `timeline.trim`) + size + `sha256:<hex>` + probe evidence | **re-measured now**: exists → `size > 0` → expected path per operation (`output.mp4` / `captions.srt` / `timeline.otio`) → verifier-workspace containment → sha recompute → probe (media) |

Verification invariants (§ of the mission, all enforced in
`jobs/verification.verify_artifact`): the artifact must exist, be non-empty,
be *the* expected artifact of the operation inside the verifier's declared
workspace (for archived trims, the job/attempt evidence directory), carry a
stable identity (recorded sha equals recomputed sha), and for media
carry valid probe evidence (a probe failure is exactly "ffprobe failed" by
runtime semantics). Document artifacts are verified structurally (SubRip
timing-block syntax; OTIO must parse as a JSON object).

**Publication order (task-181, D-0019).** For lanes whose artifact
destination lives *outside* the job workspace — today exactly
`pdf_extract`, its `<stem>.extracted.txt` sidecar — the order is:

```
execute → stage → verify → publish atomically → re-probe published bytes → persist success
```

The handler stages at the payload-derived `*.staged` name and claims it; the
queue verifies the staged bytes, re-checks execution ownership (§2a),
publishes via `os.replace` (registered `ArtifactPublication.publish`),
re-runs the same verifier against the published claim, persists `completed`
(fenced CAS), and only then finalizes (`ArtifactPublication.finalize`).

**Recoverability of the previous artifact (F1, option A — backup/restore,
D-0020).** `publish` first makes the currently published bytes reachable
under `<stem>.extracted.txt.prev` (same-directory hard link; copy fallback),
then renames the staged file onto the final name. The three refusal points
behave as follows:

| refusal | what is undone | previous artifact |
|---|---|---|
| verification of the staged bytes | staged temp removed | never touched |
| `publish` raises | staged temp + `.prev` removed | never renamed away |
| re-probe of the published bytes | `.prev` renamed back onto the final name (atomic); with no previous artifact the refused bytes are removed | **restored** |

Only a committed `completed` removes `.prev`. Before this repair the
re-probe refusal deleted nothing but the staged name — the previous artifact
had already been replaced and was lost (reproduced in
`test_gate5_execution_fencing.py::test_t9_*`; the earlier "never touches a
previously published artifact" claim was true for the first two rows of the
table and false for the third). The re-probe is **kept**: it is not a
duplicate of the staged-bytes verification — it measures the bytes under the
final name after the rename (the claim that downstream readers will see) and
is the only check that catches a wrong-destination or truncated rename.

Workspace-scoped lanes (`slideshow_render`, `story`, and creative renders
other than `timeline.trim`) map the same order as: stage in workspace →
verify → deliver (the notifier sends the verified bytes and owns workspace
cleanup). Their handler-side writes are key-scoped to the job's own workspace
and are **handler-owned**, not fenced by the queue (documented residual; a
stale handler can only overwrite its own key's staging files).

The queue-only `timeline.trim` lane is the task-232 exception: after the
ordinary independent workspace verification, the queue snapshots and copies
the input and output into a sibling `<sqlite-path>.artifacts` store under a
job/attempt key, compares copied hashes and sizes, and runs the registered
verifier again against the archived output. The passport, attempt checkpoint,
and result are then stored in `nexus_job_queue`; notification may clean the
temporary render workspace without destroying the retained input/output.
Each evidence file is bounded to 100 MiB. A fresh queue instance can load the
passport by storage key, re-hash both files, and re-run verification. The
source is sampled before and after rendering, but the legacy worker does not
bind that sampled digest to the bytes actually opened by CommandBus; a
change-and-restore race is still possible. An archive written before its
checkpoint can be orphaned, and this task adds no retention/garbage-collection
policy.

Typed user failures (`{"success": false, "error_code": …}`, the durable
dialect of this repository) claim **no artifact** and — since task-181
(D-0019, GAP-A) — are **failures of the job**: the queue classifies them
(`jobs/failure_semantics`) and persists `failed_retryable` / `failed_terminal`
with `typed_failure:<code>`; the typed result payload is preserved so the
notifier can translate the code. A typed failure never reaches `completed`,
and if one ever reached a verifier anyway, the verifier refuses it
fail-closed (`typed_user_failure`) — no layer of the stack may let a
non-success claim complete. Any `success: true` result MUST carry a
verifiable artifact — there is no success-without-artifact path
(`verification_failed:success_without_artifact_claim`).

## 4a. Publication and rollback states (task-283)

A published artifact must be attributable to **one complete identity: job *and*
attempt.** Ownership evidence is written **before** the destination is mutated,
because a crash between "backup created" and "bytes published" otherwise leaves
a destination nobody can prove ownership of.

Layout (per job, so two jobs emitting the same file name never meet):

```
<artifact_root>/published/<job_id>/<name>          published bytes
<artifact_root>/published/<job_id>/.<name>.prev     previous bytes (rollback)
<artifact_root>/published/<job_id>/.<name>.owner    {"schema":1,"job_id":…,"attempt":…}
```

| Step | State on disk | Recoverable by |
|---|---|---|
| S0 stage | staging only | `retract` removes this attempt's staged temp |
| S1 claim | sidecar written (identity proven) | — |
| S2 backup | `.prev` holds the previous artifact | `retract` sees `.prev` ∧ destination missing → restores it |
| S3 publish | destination holds the new bytes | refusal → `retract` restores `.prev`, or removes the refused bytes when there was none |
| S4 durable commit | row `completed` | `finalize` drops the staging tree; a stray `.prev` is replaced by the next publication |

`retract` **fails closed**: a missing, unreadable, malformed, wrong-schema or
truncated sidecar is treated as *absence of proof*, not permission to delete.
An older attempt can never retract a newer attempt's artifact, a different job
can never retract this one's, and an identity without an attempt never matches
an attempt-scoped publication.

**The attempt is an identity, not a field (review finding 3).** The sidecar
reader used to validate `schema` and `job_id` only, and the retract comparison
ran on raw values — so a sidecar whose `attempt` was missing or `null`
(`{"schema":1,…,"attempt":null}` is exactly what a call without queue-minted
identity writes) and a caller with no attempt satisfied `None == None` and
authorised a rollback on an ownership claim that never existed. Both sides are
now canonicalised through `_parse_attempt()`: a positive integer, or its decimal
spelling (`"2"`, the historical sidecar form). `None`, `true`/`false` (a `bool`
is an `int` subclass), `0`, negatives, floats, non-numeric strings, lists and
objects are unprovable, on **either** side, and the read itself refuses a
sidecar that has no valid attempt. The matrix in
`tests/integration/test_artifact_publication_safety.py` (§D2) covers
missing/null/bool/zero/negative/string/float/list/object attempts, a missing key,
a wrong job id, a wrong attempt, a caller with no attempt, malformed and absent
sidecars, and the valid retract. Workspace membership is decided by resolved-path
containment (`Path.resolve().is_relative_to`), never by string prefix — the old
`str(child).startswith(str(parent))` treated `/work/ws-evil` as inside
`/work/ws` and misclassified a post-publication refusal as a pre-publication
temp.

**Crash *after* publish, before the durable commit (review finding 4).** The
state "artifact published, row still `verifying`" is a real crash window
(S3 → S4), and it is the one where "the file exists" most tempts a reader to
assume success. It is covered by a real process death:
`tests/integration/test_publication_crash_recovery.py` runs a child process that
publishes and then `os._exit`s inside its post-publication re-probe, then
asserts (a) the artifact is on disk, (b) the row is `verifying` — **never**
`completed`, (c) a *new* owner acquires the freed lock, recovers, re-executes
under a strictly higher fence (attempt 2), replaces the artifact and commits
`completed`, (d) the previous bytes survive as `.prev`, and (e) an unrelated
artifact under the same publication root is untouched. A second test pins that
recovery alone never promotes a row to success: with no handler registered the
requeued row ends `failed_terminal`, not `completed`.

**Not claimed:** no atomicity is asserted across steps S2–S4 as a whole. Each
mutation is atomic (`os.replace`/`rename`), and each *intermediate state* is
enumerated above and covered by an adversarial test; a crash is always
recoverable to S2 or S3, never to an unattributable destination.

## 5. Failure semantics (testable contract, task-181 taxonomy)

Every failure lands in exactly one of the two durable failure states —
never in `completed`:

| Case | Result | Class | Enforced by |
|---|---|---|---|
| typed user failure (`success=false` + code) | `typed_failure:<code>` + typed result preserved | per code (table below) | queue short-circuit + verifier refusal (GAP-A regression) |
| execution raised / non-dict | error persisted, no result | per exception (table below) | `_process_job` except path (M1) |
| probe failed on claimed media | `verification_failed:probe_failed` | RETRYABLE | verifier (M2) |
| zero-byte artifact | `verification_failed:empty_artifact` | RETRYABLE | verifier — no exception path bypasses it (M3) |
| sha/size mismatch (tamper/stale claim) | `verification_failed:sha256_mismatch` / `size_mismatch` | RETRYABLE | verifier (M4) |
| success claimed without artifact claim | `verification_failed:success_without_artifact_claim` | TERMINAL | verifier |
| verifier itself crashed | `verification_failed:verifier_crashed` | RETRYABLE | `_verify_safely` fail-closed (M10) |
| publish / re-probe failure | `verification_failed:publish_failed` / `reprobe_failed`, staged retracted | RETRYABLE | `_publish_and_reprobe` (Gate 5 suite) |
| runtime publish → own probe fails | RenderError escapes → fail-closed conversion — **never success**, no verification block | RETRYABLE (corrupt output) | worker fail-closed (M10b) |

The Job layer has no code path that converts a runtime failure into a
success: completion with a verification block only happens on `ok=True` +
successful publication.

### Classification table (`jobs/failure_semantics`)

One principle: **RETRYABLE iff the world can change to make the identical
request succeed; TERMINAL iff the identical request must fail again unless
the request, the credentials, or the code changes.**

| failure family | class | why |
|---|---|---|
| temporary IO (`TimeoutError`, `ConnectionError`, `OSError` errno EAGAIN/EBUSY/ETIMEDOUT/ENOSPC/…) | RETRYABLE | environment can change without the payload changing |
| dependency unavailable (`ImportError`; codes `ffmpeg_unavailable`, `caption_profile_unavailable`) | RETRYABLE | installing the dependency makes the identical job succeed |
| worker crash (unexpected exception) | RETRYABLE | cause unknown and potentially transient; bounded retry is conservative — a deterministic crash re-classifies identically and stays visible |
| invalid input (codes `invalid_request`, `media_missing`, `unusable_image`; `ValueError`/`TypeError`/`KeyError`) | TERMINAL | the identical request must fail again — the payload must change |
| unsupported operation (code; `NotImplementedError`) | TERMINAL | the capability set must change (a deploy, not a retry) |
| artifact verification failure (missing/empty/sha/size/duration/probe, `verifier_crashed`, `publish_failed`, `reprobe_failed`) | RETRYABLE | transient artifact damage is exactly what one clean re-execution is for; verification is read-only and idempotent |
| deterministic handler defects (no claim, malformed sha, mis-routed publication, typed failure at a verifier) | TERMINAL | the identical retry repeats the lie |
| permission error (`PermissionError`, EACCES/EPERM) | TERMINAL | the same credentials fail identically — an operator must act |
| corrupt output (probe of produced artifact fails) | RETRYABLE | a clean re-production may fix transient damage |
| code `render_failed` (the render of THESE inputs failed) | TERMINAL | same inputs + same binary ⇒ same failure; the request must change |
| codes `image_generation_failed`, `internal` | RETRYABLE | environment-side catch-alls |
| unknown typed code / unknown verification reason | TERMINAL | contract drift must be visible, never hidden behind silent retry-spam |

**SCHEDULER: NOT IMPLEMENTED.** The classification is complete; executing
retries is not implemented and not claimed. `failed_retryable` is terminal
as implemented (both failure states have no outgoing edges).

## 6. Retry / idempotency contract (deterministic matrix)

| Scenario | Deterministic behavior |
|---|---|
| retry after failed execution (same key) | same job id; the failure state is terminal — no re-execution; operator retry = new key (new message) |
| retry after failed verification | same as above; the typed `verification_failed:<code>` is durable |
| duplicate after terminal failure | same job id, same failure, **no re-execution** (Gate 5 idempotency matrix) |
| duplicate during PROCESSING | same job id, one execution (per-process task table + UNIQUE key) |
| destination exists | the runtime refuses blind replacement (`overwrite` gate) and publishes by atomic rename; `overwrite=True` at the worker is scoped to the job's own key-derived workspace path; a failed re-render leaves previous bytes in place (M5) |
| same key, same payload | same job row; no second logical row (execution may repeat after crash recovery) |
| same key, different payload | `creative_render`: record the duplicate observation, then raise `CreativeRequestConflictError` before scheduling or a second mutation; other legacy job types retain **first payload wins** and log `job_idempotency_payload_conflict` |
| retry after valid artifact exists (completed job) | same job id + stored verified result; handler not called again (M8) |
| creative request changes | changed intent/args in a `creative_render` payload under the same key is rejected as a conflict; a genuinely revised request takes a new key ⇒ new job + new workspace |
| crash before a durable verification checkpoint | row is recoverable (`resume_pending` takes over orphaned `processing/verifying` rows → `pending`); the next reservation mints a new token, so a zombie is fenced out; handler execution may repeat (at-least-once) |
| crash after the `timeline.trim` verified-attempt checkpoint but before job completion | a new reservation re-hashes the retained input/output, re-runs the verifier, and completes with `reconciled_from_attempt_id`; the renderer is not called again if the checkpoint is valid; invalid evidence fails closed |
| crash mid-verification/publication for other job types | existing behavior: recover and re-execute; verification is read-only and idempotent; `attempt` counts reservations, not successful effects |
| crash between publish and re-probe / before `completed` | the published bytes and `.prev` survive; recovery re-executes, re-stages, re-publishes (a stray `.prev` is replaced) and completes; `.prev` is removed at finalize |
| concurrent workers (two processes, one DB) | exactly one `pending → processing` CAS wins; the loser's handler never runs (T5) |

Hard rule: **a retry cannot covertly overwrite a valid existing artifact.**
Terminal jobs never re-execute; non-terminal retries only ever replace
their own previous attempt's bytes, and only after the new artifact passed
its own runtime checks — the old bytes are replaced by atomic rename, not
deleted first.

## 7. Result contract

`queue.get_result_chain(job_id)` assembles `jobs.lifecycle.JobResult` from
durable facts only: `command_id`, `job_id`, `project_id`, `operation_id`,
`attempt`, `execution_status`, `verification_status`, the three identities,
`sha256`, `size_bytes`, `probe`, `failure_reason`. The verified facts also
ride inside the persisted result under the queue-owned
`artifact_verification` key, so any consumer of `get_result` sees the
verification truth, and the completion notifier delivers after the durable
state is already final. Task-232 adds a separate `request_identity` object to
this chain; its transaction ID is a deterministic queue-request projection,
not a CommandBus transaction ID.

## 7a. Durable creative queue evidence (task-232; queue-only foundation)

This addition is deliberately confined to the existing SQLite queue and the
already registered `creative_render` handler/verifier for `timeline.trim`.
It does not add a second domain store or change the worker/CommandBus
contract.

### Persisted queue facts

Schema migration adds `request_id`, `request_fingerprint`, `transaction_id`,
`artifact_passport_json`, and `attempt_history_json` to
`nexus_job_queue`. The attempt ledger is a JSON array **on the queue row**;
there is no `nexus_job_attempts` table. A reservation appends its attempt
record with the new monotonic `attempt` fencing token, and verification,
recovery, failure, completion, and reconciliation update the ledger under the
same queue-row fence. Existing rows receive derivable request identities;
uncanonicalizable legacy payloads are logged and remain identity-incomplete,
and `get_request_identity` fails closed rather than inventing a value. Rows
receive at most one `legacy_observed_*` latest-attempt record; missing
historical attempts are not invented. `get_job_facts` projects these queue-owned
request, attempt-history, and creative-passport columns into `JobFacts`; attempt
history and passport parsing carry explicit known flags. The provenance
passport exposes them as a read-only `queue_evidence` projection. Neither
projection nor the causal
observer authorizes execution or changes queue state. Every queue connection
sets and verifies `PRAGMA synchronous=EXTRA` before I/O. SQLite documents
`EXTRA` as adding a
directory sync after rollback-journal unlink in rollback mode; in WAL mode it
is equivalent to `FULL` ([SQLite synchronous pragma](https://sqlite.org/pragma.html#pragma_synchronous)).

Identity rules (`jobs.creative_passport`):

- `request_id` is domain-separated canonical JSON of job type + idempotency
  key; `request_fingerprint` is SHA-256 of the exact canonical payload JSON
  snapshot persisted in the row; and `transaction_id` binds those two values.
  The tuple is persisted with the
  job and recomputed on duplicate-enqueue and passport re-verification paths.
- An exact creative duplicate returns the existing job and records an
  observation. Reusing a `creative_render` idempotency key with a changed
  type/payload records the post-commit duplicate observation, then raises
  `CreativeRequestConflictError` before scheduling or a second mutation.
  Other legacy job types retain the older logged first-payload-wins behavior.
- `attempt_id` is derived from durable job ID + monotonic reservation number.
  It identifies a queue execution attempt; it does not certify that the
  external renderer ran only once.

### `timeline.trim` passport and recovery boundary

For a fresh trim attempt with no reusable verified checkpoint, the queue
preflights source evidence after reservation and before invoking the handler.
Deterministic evidence-infrastructure failures, a missing preflight snapshot,
and a file-backed-queue requirement failure produce a terminal queue failure
with no handler/render call, result, or passport. Typed render-input failures
from the existing workspace/input guards (including `media_missing` and
`invalid_request`) continue through the real default handler so the public
typed failure result is persisted; they produce no passport. A source that
exceeds the evidence size bound fails before the handler with the existing
user-translatable `invalid_request` code and no result. A `:memory:` queue cannot start a new trim render. After a successful handler
return, the queue's independent verifier accepts the output and archives both
input and output under
`<sqlite-path>.artifacts/jobs/<job-hash>/<attempt-id>/`. File bytes are
bounded to 100 MiB per asset. It checks source stability across the render,
checks the copied hashes/sizes, and independently re-runs the registered
verifier against the archived output. Each staged file is flushed and
`fsync`'d before atomic rename; the affected directory chain is `fsync`'d as
well. A sync failure aborts passport creation and completion. The canonical
passport JSON, result, verification facts, runtime evidence, and the attempt's
`verified` checkpoint are persisted before the terminal `COMPLETED` update.
The final result, passport pointer, and `completed` attempt transition are
then committed under the existing fenced queue update. A clean adapter can call
`get_artifact_passport(job_id)` to resolve the storage keys, re-hash the input
and output, re-run the verifier, rebuild the passport, and compare it with the
persisted record. The retained input/output therefore do not depend on the
notifier's temporary workspace surviving.

The passport links stable request/transaction/job/attempt IDs to measured
input/output assets, the verifier's logical `project_id`, canonical operation
and lane-IR spec hash, a payload-derived intent projection, a plan projection,
a revision projection, and recorded capability/policy evidence. It labels
these as projections rather than asserting that canonical project state was
persisted. It explicitly records `canonical_commandbus_transaction_id =
null`, no actor authorization decision, and no canonical project-state hash.
The payload's `user_id` is not treated as authorization. The observed
capability/policy decision is not an actor permission check.

Recovery contract:

| Crash point | Durable fact | Recovery behavior |
|---|---|---|
| before handler effect | reserved attempt + queue row | mark the attempt interrupted; reserve a higher fencing token; execute again |
| after external render but before verified checkpoint | no completed evidence checkpoint | handler may run again; renderer semantics remain at-least-once |
| after verified-attempt checkpoint, before `COMPLETED` | passport + both archived assets + verification/result in `attempt_history_json` | new attempt re-hashes and re-verifies the earlier checkpoint, records `reconciled_from_attempt_id`, and completes without invoking the renderer if evidence still matches |
| corrupted/missing checkpoint or asset | mismatch or verifier refusal | fail closed; do not trust the previous passport as a successful result |
| after terminal commit, before notification | completed row and result | notification may be lost; no transactional outbox is added |

On the successful-render path, the source is sampled before handler
invocation and sampled again after rendering. The legacy CommandBus path does not bind
its actual opened source digest to this queue record; a change-and-restore
race remains. An archive written before its checkpoint can leave
unreferenced files. Attempt-history JSON grows with each reservation; no
aggregate artifact-store quota, compaction, retention/garbage-collection
policy, cross-device replication, or artifact-store backup coordination is
added. The SQLite file and its
`.artifacts` sibling must be backed up/moved together. The recovery tests kill
processes, not the operating system or storage device: power-loss behavior and
device-cache/VFS honesty are not proven. `fsync` cannot validate hardware
honesty. Linux documents that file `fsync` alone does not persist
the directory entry, which is why the directory chain is separately synced
([fsync(2)](https://man7.org/linux/man-pages/man2/fsync.2.html)). Trim evidence
fails closed where POSIX directory sync is unavailable. These facts do not
establish exactly-once effects, reproducible rendering across runtimes, actor
authorization, persisted CreativeWork/project revisions, or a durable
CommandBus transaction. The wider project/revision/CommandBus mission remains
open.

Focused evidence lives in `tests/unit/test_creative_passport.py`,
`tests/integration/test_creative_execution_recovery.py`, and the real FFmpeg
§17 path in `tests/integration/test_job_lifecycle_queue.py`. The recovery
tests inject actual child-process exits before render effect and after the
verified checkpoint, then reopen the same SQLite queue and asset store. They
prove only those tested boundaries; they do not constitute independent
assurance or a production-readiness verdict.

## 8. Negative-test matrix (M1–M10) and the §17 regression

`tests/integration/test_job_lifecycle_queue.py` (real queue, real FFmpeg
for probe paths):

M1 execution failure · M2 probe failure · M3 zero-byte · M4 sha tamper ·
M5 existing-destination survival · M6 idempotency conflict + exact
redelivery · M7 creative request conflict · M8 completed-job reuse · M9 invalid
media after "successful encode" · M10 verifier crash fail-closed · M10b
runtime fail-closed never success · A/B (legacy hole vs canonical queue on
the same lying claim) · VERIFYING observability + cancel/recover ·
attempt accounting · PENDING→FAILED claim-time edge.

§17 regression (`test_succeeded_implies_valid_stable_traceable_artifact`,
real chain): `COMPLETED` ⇒ artifact exists on disk, `size > 0`,
`sha256(result) == sha256(bytes on disk)`, probe evidence present, logical
+ spec + physical identities persisted, `attempt` recorded. That test *is*
the durable answer to the mandatory question — see D-0017 for the A/B
evidence that the pre-contract queue could complete a lying claim and the
canonical queue cannot. Task-232 extends this real trim path with persisted
input/output hashes, a passport re-read by a fresh adapter after temporary
workspace cleanup, fail-closed re-read after output tampering, and embedded
attempt-history assertions. Process-exit
fault cases are in `tests/integration/test_creative_execution_recovery.py`.

Task-181 adds `tests/integration/test_gate5_closure.py` (typed-failure
regression across codes/classes, notification matrix, refused-publication
scenarios, trace `job_id`, idempotency matrix, crash-window recovery) and
`tests/unit/test_failure_semantics.py` (the classification table), plus the
mutation harness `scripts/gate5_mutation_probes.py` (6 probes, each
GREEN→RED→restore→GREEN — see `docs/audits/GATE5_CLOSURE_2026-09-24.md`).

## 9. Notification contract (task-181)

**The durable lifecycle state is the notifier's only source of truth.**

| durable state | user-visible outcome |
|---|---|
| `completed` | success notification / artifact delivery |
| `failed_retryable` | failure notification (visibly "temporary / retryable") |
| `failed_terminal` | terminal-failure notification (visibly "definitive") |
| `verifying` / `pending` / `processing` | silent — a success message is impossible outside `completed` |
| verification failure | failure notification — never success, never a delivery |

`result.success == true` under a failure status can never produce a success
notification (the claim is a second, independent refusal, never the truth
source). Inside the queue, `on_job_finished` fires **only after the fenced
UPDATE reported `rowcount == 1`** — a CAS miss (row no longer ours) is
logged (`job_transition_rejected`) and announces nothing. The contract is
"never lie", not "eventual delivery": a crash between the commit and the
notification loses that notification (the durable state remains true and
`get_status` reflects it); a transactional outbox was evaluated and rejected
for this scope (D-0020). Evidence: `tests/integration/test_gate5_closure.py`
(notification matrix + lying-result regression), `tests/unit/test_creative_notify.py`,
`tests/unit/test_bot_slideshow_notify.py`.

## 10bis. Causal recording (task-231)

Every durable edge of this state machine is **journaled**, after its commit,
into the causal ledger (`provenance/`, `architecture/PROVENANCE_LEDGER.md`):
`job_enqueued` / `job_enqueue_duplicate`, `job_reserved`, `job_verification_started`,
`job_completed`, `job_failed`, `job_reopened`, and `job_takeover` (the
`→ PENDING` recovery edges of §2). The recording is strictly fail-safe — a
broken ledger is logged degradation (`causal_ledger_observe_failed`) and can
never change a job outcome — and the ledger itself is evidence, never
authority: the row above remains the only source of truth for status,
attempt, payload and result. The **Artifact Passport** (`nexus jobs passport
<job_id>`) is the read-only projection that reconciles chain against this
row, re-measures the artifact bytes, and answers, fail-closed, "why does this
artifact exist?".

## 10. Trace contract (task-181)

Every event emitted while a job runs carries the durable `job_id`:
structlog contextvars bind it at `_process_job` entry (the worker's events
— e.g. `creative_render_start` — inherit it), the queue's lifecycle lines
name it explicitly (`job_processing` / `job_verifying` / `job_completed` /
`job_failed`), `JobCompletion` and `JobResult` carry it, and the notifier
prints it. A lifecycle event belonging to a job can never record
`job_id = null`. Evidence:
`test_gate5_closure.py::test_every_lifecycle_trace_event_carries_job_id`,
`::test_result_and_completion_carry_the_durable_job_id` (and mutation
probe #4, which turns the first RED).

## 11. Handler side-effect boundary — what the queue can and cannot fence (task-283)

**Queue fencing cannot undo an external side effect a stale handler already
caused.** Fencing stops a stale attempt from *recording* a result; it cannot
un-send a message, un-charge a card, or un-upload an object.

| Operation (as wired today) | Idempotent? | Provider idempotency key? | Repeatable after UNKNOWN? |
|---|---|---|---|
| `pdf_extract`, `creative_render`, `slideshow_render` (local filesystem + FFmpeg) | yes — same workspace, same bytes, replaced by atomic rename | n/a (local) | yes: re-execution is safe |
| artifact publication (§4a) | yes — same (job, attempt) republishes the same bytes | n/a (local) | yes, under the sidecar proof |
| Telegram message send (`on_job_finished`) | **no** | **no** — a connect-lost-after-send followed by a retry *is a second message* | **no**; at-least-once by construction |
| HTTP callback (`adapters/effect/effect_delivery.py`) | only if the receiver honours `Idempotency-Key` | yes — header sent on every attempt | only with receiver support |
| Object upload (PUT, key-names-the-object) | yes | effectively yes | yes |
| Database insert on the effect key | at-most-once by construction | yes (unique key) | yes |

Rules:

1. A handler whose external effect is **not** idempotent and has **no** provider
   key must call `queue.mark_unknown(claim)` when the outcome is unproven. The
   row then carries `unknown_outcome = 'unknown'`, `recover_job` refuses to
   recover it, and only an explicit `queue.reconcile(job_id, outcome, evidence)`
   reopens the path. UNKNOWN is a **fail-closed outcome, not a retry hint.**
2. Exactly-once execution is **not** claimed anywhere in this system. The honest
   statement is: at-most-once *commit* (CAS + fence), at-least-once *execution*,
   and explicitly-reconciled external effects.
3. Complete handler-side-effect fencing would need a **transactional outbox with
   provider-side keys for every external effect** — a separate architectural
   change, deliberately out of scope here. The contract it would require: an
   effect intent row committed with the job transition, a per-effect adapter
   with either a provider key or a reconciliation probe, and a test per adapter
   proving `UNKNOWN → reconcile` never sends twice. Until then, the boundary is
   `adapters/effect/effect_delivery.py`'s semantic ladder, and §11 is the honest
   statement of it.

## 12. Known limitations (honest, task-283)

1. **POSIX/Linux only.** Windows has no supported ownership backend: the queue
   refuses to start there (`QueueOwnershipError`) instead of shipping an
   untested `msvcrt` branch. Adding one requires a real Windows concurrency test
   first.
2. **Single-host only.** `flock` is local. The storage layer already documents
   that two hosts cannot coordinate through a local lock
   (`src/nexus_ai_agent/storage/db.py`); multi-host deployment needs a database
   advisory lock or a lease table, which is not implemented. **Do not deploy two
   replicas against one shared sidecar** and expect exclusivity.
3. **Network filesystems.** `flock` semantics depend on the mount; NFS/SMB
   deployments were not tested. Local ext4/overlayfs is what is evidenced here.
4. **Legacy flat publications.** Artifacts published before task-283 live at
   `<artifact_root>/published/<name>` with an attempt-only sidecar. New
   publications use the job-scoped layout; a `retract` for a legacy path finds no
   job-scoped sidecar and therefore fails closed (leaves the artifact). No
   migration is performed — legacy artifacts are read-only in practice.
5. **Handler side effects** remain at-least-once (§11). No outbox with universal
   provider keys exists.
6. **The lock hint is advisory.** The PID written into the anchor file is
   diagnostic only; operators must not use it to decide anything.
7. **A one-shot CLI run competes with the bot.** By design: `nexus jobs drain`
   refuses to run while the bot owns the queue (`typer.Exit(2)`) rather than
   draining beside it.
