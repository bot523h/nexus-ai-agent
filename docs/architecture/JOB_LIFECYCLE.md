# Job Lifecycle Contract (canonical)

> Status: **living view** — enforced by `tests/unit/test_job_lifecycle.py`,
> `tests/unit/test_failure_semantics.py`, `tests/integration/test_job_lifecycle_queue.py`,
> `tests/integration/test_gate5_closure.py`, and the guarded writes in
> `src/nexus_ai_agent/adapters/in_process_job_queue.py`. Delivered by task-178
> (Agent C, 2026-09-24), closed by task-180 (verification GAPs, 2026-09-24)
> and task-181 (failure semantics + 6-state taxonomy + publication order,
> 2026-09-24). Decision records: `docs/DECISION_LOG.md` D-0013, D-0014,
> D-0015.

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
D-0015 for the compatibility mapping): `pending`, `processing`,
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
execution is fenced out; a crash between publish and ④ leaves the
published bytes + `.prev`, which the re-execution replaces (§6).

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
| `pending → processing` | queue (reservation CAS) | **PENDING-only** CAS (`WHERE status = 'pending'`, `rowcount == 1`); `started_at` set; `attempt += 1` mints this execution's fencing token (`ExecutionClaim`); a `processing` row is **not** re-claimable — takeover happens only through `resume_pending` (§2a) |
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

Why `attempt` and not a UUID / lease column (D-0016): the token must be
minted atomically with the claim, be strictly monotonic per row, and cost
no schema change — `attempt` already is all three. Wall-clock leases were
rejected as the *fence* (clock skew, no ordering) and kept only as the
*takeover policy* (`resume_pending(stale_after=…)`).

Takeover is explicit and never silent: only `resume_pending` may move a
live `processing`/`verifying` row back to `pending`, either at process
startup (`stale_after=None` — the pre-existing recovery, now skipping rows
this process is itself executing) or expiry-gated (`stale_after=Δ` —
rows younger than Δ are left to their owner). A reservation never takes a
`processing` row over (the pre-repair `status IN ('pending','processing')`
claim let two processes own one execution — R5).

**Side-effect fencing point.** For published lanes the irreversible step is
the rename onto the final name. Ownership is re-read immediately before it
(`_owns_execution(claim, VERIFYING)`): a stale execution publishes nothing
(`verification_failed:stale_execution`, no retract). **Residual window
(documented, not hidden):** between that read and the `os.replace` there is
no token the filesystem could check; the window is bounded to microseconds
of a single worker and can only be entered if a takeover happened *in that
interval*, which requires `resume_pending` (startup or expiry) — never a
concurrent reservation. Closing it fully would require token-aware storage
(D-0016 records this as out of scope).

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
4. `idempotency` — durable UNIQUE key collapse at enqueue
5. `revision/precondition` — guarded status CAS (row in expected state)
6. `job reservation` — `pending → processing` owns the execution

Then `SIDE-EFFECT ALLOWED`, and the first lawful side effect is a write to
a **staging** path — never the final destination. The final name appears
only via atomic rename: media through the runtime's `.part` staging
(`creative/rendering/executor.py`), documents through
`render_jobs._atomic_write_text` (temp file → `os.replace`). A
half-written destination is never visible under a final artifact name.

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
| physical | output path (inside the job workspace) + size + `sha256:<hex>` + probe evidence | **re-measured now**: exists → `size > 0` → expected path per operation (`output.mp4` / `captions.srt` / `timeline.otio`) → containment → sha recompute → probe (media) |

Verification invariants (§ of the mission, all enforced in
`jobs/verification.verify_artifact`): the artifact must exist, be non-empty,
be *the* expected artifact of the operation inside the job's own workspace,
carry a stable identity (recorded sha equals recomputed sha), and for media
carry valid probe evidence (a probe failure is exactly "ffprobe failed" by
runtime semantics). Document artifacts are verified structurally (SubRip
timing-block syntax; OTIO must parse as a JSON object).

**Publication order (task-181, D-0015).** For lanes whose artifact
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
D-0016).** `publish` first makes the currently published bytes reachable
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

Workspace-scoped lanes (`creative_render`, `slideshow_render`, `story`) map
the same order as: stage in workspace → verify → publish at delivery (the
notifier sends the verified bytes and owns cleanup). Their handler-side
writes are key-scoped to the job's own workspace and are **handler-owned**,
not fenced by the queue (documented residual; a stale handler can only
overwrite its own key's staging files).

Typed user failures (`{"success": false, "error_code": …}`, the durable
dialect of this repository) claim **no artifact** and — since task-181
(D-0015, GAP-A) — are **failures of the job**: the queue classifies them
(`jobs/failure_semantics`) and persists `failed_retryable` / `failed_terminal`
with `typed_failure:<code>`; the typed result payload is preserved so the
notifier can translate the code. A typed failure never reaches `completed`,
and if one ever reached a verifier anyway, the verifier refuses it
fail-closed (`typed_user_failure`) — no layer of the stack may let a
non-success claim complete. Any `success: true` result MUST carry a
verifiable artifact — there is no success-without-artifact path
(`verification_failed:success_without_artifact_claim`).

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
| same key, same payload | same job id, no second effect (redelivery dedupe) |
| same key, different payload | **first payload wins**: same job id, original effect; conflict logged (`job_idempotency_payload_conflict`) — a retry can never smuggle a second, different effect (M6/M7) |
| retry after valid artifact exists (completed job) | same job id + stored verified result; handler not called again (M8) |
| revision changes | revision is part of the payload ⇒ same-key revision = conflict row above; a genuinely revised request takes a new key ⇒ new job + new workspace |
| crash mid-execution / mid-verification | row recoverable (`resume_pending` takes over orphaned `processing/verifying` rows → `pending`); the next reservation mints a new token, so a zombie of the crashed execution is fenced out; verification is read-only so re-running it is idempotent; `attempt` counts executions |
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
state is already final.

## 8. Negative-test matrix (M1–M10) and the §17 regression

`tests/integration/test_job_lifecycle_queue.py` (real queue, real FFmpeg
for probe paths):

M1 execution failure · M2 probe failure · M3 zero-byte · M4 sha tamper ·
M5 existing-destination survival · M6 idempotency conflict + exact
redelivery · M7 revision conflict · M8 completed-job reuse · M9 invalid
media after "successful encode" · M10 verifier crash fail-closed · M10b
runtime fail-closed never success · A/B (legacy hole vs canonical queue on
the same lying claim) · VERIFYING observability + cancel/recover ·
attempt accounting · PENDING→FAILED claim-time edge.

§17 regression (`test_succeeded_implies_valid_stable_traceable_artifact`,
real chain): `COMPLETED` ⇒ artifact exists on disk, `size > 0`,
`sha256(result) == sha256(bytes on disk)`, probe evidence present, logical
+ spec + physical identities persisted, `attempt` recorded. That test *is*
the durable answer to the mandatory question — see D-0013 for the A/B
evidence that the pre-contract queue could complete a lying claim and the
canonical queue cannot.

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
for this scope (D-0016). Evidence: `tests/integration/test_gate5_closure.py`
(notification matrix + lying-result regression), `tests/unit/test_creative_notify.py`,
`tests/unit/test_bot_slideshow_notify.py`.

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
