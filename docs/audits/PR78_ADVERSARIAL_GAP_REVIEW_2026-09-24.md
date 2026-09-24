# PR#78 Final Gate-5 Closure — Adversarial Gap Review + Merge Readiness (task-181 review)

> Review session: `arena/01a0d567-nexus-ai-agent` · Review target: PR#78 (`arena/01a0d4b6`)
> Review date: 2026-09-24 · Method: LIVE GITHUB → SOURCE → RUNTIME → TEST → MUTATION → CI → CONTRACT → DECISION
> Laws applied: 5×1 (GITHUB TRUTH / PRODUCTION TRACE / BEHAVIORAL PROOF / SIX-STATE MACHINE / RETRY SEMANTICS)
> + NEW LAW 1 (NO UNDISPOSITIONED GAP) + NEW LAW 2 (CURRENT-SHA PINNING) + NEW LAW 3 (NEGATIVE-SPACE AUDIT)

**FINAL STATUS: `NEEDS-REPAIR`** — see §19. Three reproduced contract violations (F1 publication
re-probe, F2 unguarded `_mark_pending` terminal reopen, F3 notify-without-durable-state), one
evidence-integrity defect in the delivered truth matrix (F7). Every finding below carries one of
the four mandatory dispositions with evidence. No finding is closed with "low / optional /
theoretical / probably pre-existing / known / informational".

---

## 1. LIVE GITHUB (fetched 2026-09-24T21:5x–22:2xZ, gh CLI)

| Field | PR#78 (live) | PR#71 | PR#74 | main |
|---|---|---|---|---|
| state | **OPEN** | OPEN | OPEN | — |
| merged / merged_at | **no / null** | no / null | no / null | — |
| draft | false | false | false | — |
| base / base_sha | main @ `035a896dd2ed1293de6accf2ef4309da2fd64c89` | main | main | — |
| head / head_sha | `arena/01a0d4b6-nexus-ai-agent` @ **`5f273f08f543ae7916ccdf1eeee8544a44cbf81b`** | `arena/01a0d43e-nexus-ai-agent` @ `9e9c753082c572d1164148be7da50c061ed53311` | `arena/01a0d475-nexus-ai-agent` @ `05d647e9ccc4e9c3c823020972e01d8cd14fc80a` | `035a896dd2ed…` |
| mergeable / mergeStateStatus | MERGEABLE / **UNSTABLE** | — | — | — |
| reviews / reviewDecision | **none / ""** | — | — | — |
| comments | 3 (all `arena-ai-coding-agent`, incl. final CI-evidence comment 21:25:43Z) | — | — | — |
| changed files / Δ | **36 files, +6984 / −89** | — | — | — |
| commit count | **7** (`85f4444b → 9e9c7530 → 59f807d6 → 05d647e9 → 10a09bff → b0304429 → 5f273f08`) | 2 (top of PR#78's stack) | 4 (top of PR#78's stack) | — |

- `gh pr checks 78` (live): **4/4 pass**, all from run `36060596005` on `5f273f08`.
- `mergeStateStatus=UNSTABLE` coexists with 4/4 green `pr checks` because the rollup still carries
  the earlier failed pull_request run `36058609988` on the **same head** (see §14).
- PR#78 is the declared **superset/merge vehicle** of #71 and #74 (both still open, unmerged) —
  commit-for-commit lineage verified in the commit graph.
- HEAD `5f273f08f543ae7916ccdf1eeee8544a44cbf81b` == the mission's "Known current HEAD" ✓ and base
  `035a896…` == "Known base" ✓. Nothing moved during this review (LAW 2 satisfied at fetch time and
  re-verified at report time).

## 2. OWNERSHIP (LAW 17 gate — checked BEFORE any mutation)

`python scripts/agent_board.py check --files <production/test/contract paths> --branch arena/01a0d567-nexus-ai-agent`
→ **exit 1, `STOP — zone actively claimed by another agent`**:

| Path | Claimed by |
|---|---|
| `src/nexus_ai_agent/adapters/in_process_job_queue.py` | task-181-gate5-closure |
| `src/nexus_ai_agent/jobs/feature_verification.py` | task-181-gate5-closure |
| `tests/integration/test_gate5_closure.py` | task-181-gate5-closure |
| `docs/architecture/JOB_LIFECYCLE.md` | task-181-gate5-closure |

Board truth (PR head `.agents/board.json`, updated 2026-09-24T19:04:01Z):

- `task-181-gate5-closure`: status **active**, `agent_branch=arena/01a0d4b6-nexus-ai-agent`,
  `claimed_at=2026-09-24T19:04:01Z`, `ttl_hours=48` → lease **LIVE** (expires 2026-09-26T19:04Z),
  `gates_owner=false`, exclusive paths cover every file a Gate-5 patch would touch.
- `task-178-job-lifecycle` (`arena/01a0d43e`) and `task-180-verification-gap-closure`
  (`arena/01a0d475`): status done, prerequisites lineage intact.
- `python scripts/agent_board.py check --files docs/audits/PR78_ADVERSARIAL_GAP_REVIEW_2026-09-24.md`
  → **exit 0, "no overlap — safe to proceed"** (this report's only write).

**Ownership verdict: FAIL for production/test/contract paths → per LAW 17: no production/test
mutation and no patch to claimed files were performed.** The official mutation harness was
therefore NOT executed (see §12 — recorded **UNVERIFIED**, never PASS). All reproductions ran as
read-only diagnostics (new files under `/tmp`, detached worktree at the audited SHA,
`git worktree /home/user/pr78-head @ 5f273f08`).

## 3. CURRENT SHA (LAW 2 — everything below is pinned here)

- PR head: `5f273f08f543ae7916ccdf1eeee8544a44cbf81b` (7 commits over base; tree audited in a
  detached worktree at this exact SHA).
- Base: `035a896dd2ed1293de6accf2ef4309da2fd64c89`.
- PR#71 head: `9e9c753082c572d1164148be7da50c061ed53311`; PR#74 head:
  `05d647e9ccc4e9c3c823020972e01d8cd14fc80a` (both are ancestors of `5f273f08`).
- No evidence older than these SHAs is transferred to them. Any claim not re-executed at
  `5f273f08` in this review is labeled so in §12–§15.

## 4. 5×1 MATRIX

| Law | Question | Verdict at `5f273f08` | Evidence |
|---|---|---|---|
| 1 GITHUB TRUTH | Is the live PR state consistent with its claims? | **PASS with residuals** | §1: OPEN, not merged, superset of #71/#74; `mergeStateStatus=UNSTABLE` + one red run on same head (dispositioned §14) |
| 2 PRODUCTION TRACE | Are all status write paths mapped and guarded? | **FAIL (one path)** | §5: 6 `_mark_*` write paths + 1 recovery write mapped; `_mark_pending` is an **unguarded blind write** (F2) |
| 3 BEHAVIORAL PROOF | Do typed failures / unknown codes / missing error_code behave per contract? | **PASS with one scoped residual** | §6: runtime proofs R4 + typed matrix tests green at `5f273f08`; bare `success=False` fails closed on all default lanes; opt-out lane completes it (F5, INTENTIONAL + ACCEPTED RISK) |
| 4 SIX STATE MACHINE | Six states + fail-closed edges + named owner/guard per edge? | **PASS on paper, FAIL at one adapter edge** | §5: matrix complete and unit-enforced; adapter's `_mark_pending` can persist edges the matrix forbids (F2) |
| 5 RETRY SEMANTICS | Does a retry scheduler exist; is retry state durable; can duplicates occur? | **PASS (honest) + scoped residual** | §7: `RETRY SCHEDULER NOT IMPLEMENTED` is stated everywhere; `failed_retryable→pending` RESERVED+absent; cross-process duplicate execution possible (F4, INTENTIONAL CONTRACT) |

## 5. SIX-STATE MACHINE (source → caller → guard/CAS → persistence → notification → recovery)

States (enum `application/ports/job_queue.py`, aliases `jobs/lifecycle.py`):
`pending · processing · verifying · completed · failed_retryable · failed_terminal`
(+ legacy read alias `"failed" → failed_terminal` in `parse_job_status`).

| Edge | Caller (source) | Guard / CAS | Persistence | Notification | Recovery |
|---|---|---|---|---|---|
| `pending → processing` | `_process_job` via `_mark_processing` | `WHERE id=? AND status IN (pending, processing)` + `attempt+=1` | `started_at` set | none (non-terminal) | covered by `resume_pending` |
| `processing → processing` | resume reclaim (same guard) | same as above | `attempt+=1` | none | this edge **is** the reclaim |
| `pending → failed_terminal` | claim-time "no handler" via `_mark_failed` | `status IN (pending, processing, verifying)` | `error`, `finished_at` | `JobCompletion(status)` | terminal as implemented |
| `pending → failed_retryable` | claim-time structural (classified RETRYABLE) | same | same | same | terminal as implemented |
| `processing → verifying` | `_mark_verifying` | `WHERE id=? AND status=processing` (returns rowcount>0) | status only | none | `verifying → pending` reset re-runs verification |
| `processing → failed_retryable / failed_terminal` | typed-failure short-circuit / exception path via `_mark_failed` | `status IN (pending, processing, verifying)` | `error=typed_failure:<code>` or text; typed result preserved | failure copy (status-driven) | terminal as implemented |
| `verifying → completed` | `_mark_completed` | `WHERE id=? AND status IN (processing, verifying)` | `result_json` + `artifact_verification` block | success copy — **but fires even if CAS no-ops** (F3) | crash window → requeue (§9) |
| `verifying → failed_*` | verification refusal / verifier crash / publish / re-probe failure via `_mark_failed` | `status IN (pending, processing, verifying)` | `error=verification_failed:<code>`, no result | failure copy | staged temps retracted |
| `processing → pending` | `CancelledError` path via **`_mark_pending`** | **NONE — `WHERE id=?` blind write** (F2) | `started_at=NULL` | none | this edge is recovery |
| `verifying → pending` | same `CancelledError` path / `_reset_unfinished` (guarded `IN (pending,processing,verifying)`) | `_reset_unfinished` guarded; `_mark_pending` unguarded | `started_at=NULL` | none | `resume_pending` / `resume_pending_jobs` |

- **`failed_retryable → pending`: NOT IMPLEMENTED** — `RESERVED`, absent from `TRANSITIONS`,
  `assert_transition` refuses it, `is_terminal` treats both failure states as terminal. Declared as
  such in `JOB_LIFECYCLE.md` §2/§5/§6, D-0015, and the enum docstring. Docs do **not** present it
  as implemented. ✓
- All terminal states have no outgoing edges **in the matrix** — but the adapter's unguarded
  `_mark_pending` can persist `completed → pending` and `failed_* → pending` (F2, reproduced),
  falsifying §2's sentence "a blind write cannot produce an illegal edge".

## 6. FAILURE CLASSIFICATION (behavioral proof at `5f273f08`)

| Case | Contract | Observed (runtime) | Proof |
|---|---|---|---|
| typed failure `{"success": false, "error_code": valid}` | failure status, NEVER completed; `typed_failure:<code>`; classified per code | ✓ exactly that (RETRYABLE iff the world can change; table-driven) | `test_failure_semantics.py` (70 unit tests green) + `test_gate5_closure.py::test_typed_render_failure_lands_in_a_failure_status_never_completed` green |
| unknown typed code | fail closed (TERMINAL) | ✓ `TYPED_CODE_CLASSES.get(code, TERMINAL)` | unit table tests |
| typed failure reaching a verifier | refuse fail-closed (`typed_user_failure`) | ✓ second independent layer | `::test_typed_failure_reaching_a_verifier_still_cannot_complete` |
| **`{"success": false}` WITHOUT `error_code`** | (mission ambiguity) | **determinate, config-split** (repro R4): default registries (all four production lanes; bot/app.py + cli.py compose with defaults, registry ratchet enforced) → `failed_terminal`, `verification_failed:success_without_artifact_claim` — NEVER completed. Explicit opt-out `artifact_verifiers={}` (documented "historical two-phase semantics") → **`completed`** with `success=false` payload; notifier's second guard then sends a failure copy | R4 (3 legs) + `is_typed_user_failure` docstring ("the dialect is exactly `{success: False, error_code: <str>}`") |
| no error_code + non-str code (`error_code: 123`) | — | fails closed (`typed_user_failure` refusal on creative lanes; `success_without_artifact_claim` on story/pdf) | verifier source trace |
| verification reason unknown | TERMINAL fail-closed | ✓ | unit table tests |

Disposition for the no-error_code ambiguity (NEW LAW 1 — cannot be left as "informational"):

- **INTENTIONAL CONTRACT** for the production behavior (dialect boundary documented; all default
  lanes fail closed — proven R4 legs 1 & 3; registry ratchet (`test_verification_registry_ratchet`)
  makes an unverified handler unregisterable in default composition).
- **ACCEPTED RISK** (named residual, owner **task-181-gate5-closure / Agent E `arena/01a0d4b6`**):
  the explicit `artifact_verifiers={}` opt-out completes bare `success=false` results (R4 leg 2).
  Defense exists only at the notifier (`slideshow_notify`/`_notify_creative_completion` treat
  `COMPLETED + success=False` as failure copy). Optional 2-line hardening (recommended, not
  merge-blocking): treat `result.get("success") is False` (with or without code) as
  `typed_failure:untyped` (TERMINAL) in the queue short-circuit.

## 7. RETRY

- **`RETRY SCHEDULER NOT IMPLEMENTED`** — verified in code (no scheduler exists; only
  `storage/resilience.retry_with_backoff`, unrelated to the job queue) and stated truthfully in
  `JOB_LIFECYCLE.md` §5 ("executing retries is not implemented and not claimed"), D-0015 rejected
  alternative (4), `failure_semantics` module docstring, and `JobStatus` docstring. Docs and code
  agree. ✓
- `failed_retryable` is a durable **label of eligibility**, terminal as implemented. Retry state is
  durable (SQLite status column + `error` text) across restarts.
- Duplicate retry possible? **Yes, cross-process** (repro R5): two live `InProcessJobQueue`
  instances on one sidecar ran the same job concurrently (`handler invocations=2`, `attempt=2`)
  because `_mark_processing` accepts `status IN (pending, processing)` by design (resume reclaim).
  Same-instance dedupe (`_schedule` task table + UNIQUE idempotency key) holds.
  **Disposition: INTENTIONAL CONTRACT** — §6 scopes "one execution" as "(per-process task table +
  UNIQUE key)"; `resume_pending_jobs` docstring warns verbatim that re-running in-flight rows "would
  duplicate side effects"; `GATE5_TRUTH_MATRIX.json:known_non_verified` admits
  "`_mark_processing` cross-process race (one process at a time — documented, unchanged)".
  NEW LAW 1 forbids closing this with the word "known" alone — hence the explicit INTENTIONAL
  CONTRACT record here: **the deployment invariant is one live queue process per sidecar DB**;
  outside that invariant the contract makes no single-execution promise.
- CAS sufficiency: per-process yes; cross-process no (by the invariant above). The CAS defect that
  is NOT covered by any invariant is F2 (below).

## 8. CANCEL / RACE

Mission scenarios, tested by reasoning + reproduction (R2):

| Scenario | Expected | Observed | Verdict |
|---|---|---|---|
| `processing → cancel → complete` | cancel wins; row recoverable `pending`; no terminal damage | cancel lands first: row `pending` ✓. But when a second worker (startup `resume_pending`) completes the row and the cancelled worker's `_mark_pending` then runs: **`completed → pending` REOPENED**, then re-executed on next resume (`handler invocations` 2→3 in R2) | **NEEDS-REPAIR (F2, reproduced)** |
| `verifying → cancel → fail` | cancel/pending or fail; never wrong terminal | cancel during verification → `_mark_pending` (unverifying is safe per matrix) ✓; a concurrent fail CAS can also land first, then `_mark_pending` reopens the failure row → **`failed_* → pending`** — the RESERVED-not-implemented retry edge, persistable via the same blind write | **NEEDS-REPAIR (F2)** |
| `completed → late cancel` | no effect | single-instance: `_mark_pending` unreachable for finished tasks ✓. **Cross-instance**: a stale cancelled worker reopens the other instance's `completed` row (R2) | **NEEDS-REPAIR (F2, reproduced)** |

**R2 (verbatim facts)**: `after W2 run status=completed; after W1 cancel status=pending (terminal
reopened=True); handler calls 2→3 (duplicate execution=True); final status=completed` after the
reopened row was processed again. Matrix check printed by the repro: `completed→pending legal?
False | failed_retryable→pending legal? False | edges into pending: [processing->pending,
verifying->pending]`.

`_mark_pending` (in_process_job_queue.py:781) is the single unguarded status write in the adapter:

```sql
UPDATE nexus_job_queue SET status = 'pending', started_at = NULL WHERE id = ?   -- no status guard
```

Concurrency reasoning (why this is reachable, not theoretical): cross-process recovery is
**in-contract** ("A new process can call `resume_pending` to continue rows left pending, processing,
or verifying by an earlier process"); a restart-race where the old process is still cancelling while
the new process reclaims and finishes the row is exactly R2's sequence. Reproduced deterministically
in `/tmp/repro_task181.py::r2` (single script, two queue instances, one cancel).

**Minimal patch (FIX NOW, blocked for this session by ownership STOP — owner: task-181/Agent E):**
guard the write to the two matrix-legal sources and use the rowcount:

```python
def _mark_pending(self, job_id: str) -> bool:
    with self._db_lock, self._connection() as connection:
        cursor = connection.execute(
            """UPDATE nexus_job_queue SET status = ?, started_at = NULL
               WHERE id = ? AND status IN (?, ?)""",
            (
                JobStatus.PENDING.value,
                job_id,
                JobStatus.PROCESSING.value,
                JobStatus.VERIFYING.value,
            ),
        )
        return cursor.rowcount > 0
```

plus one targeted regression test in `test_gate5_closure.py` shaped as R2 (cancelled worker after a
cross-instance complete must leave `completed` intact).

## 9. PUBLICATION (the vital question, mission §7)

Pipeline: `handler stages → verify staged → publish_pdf_text_artifact (os.replace staged→published)
→ re-probe published bytes → persist success`. Failure points traced:

| # | Failure point | Behavior at `5f273f08` | Previous artifact |
|---|---|---|---|
| 1 | stage/handler failure | exception → classified `failed_*`; no publish | preserved |
| 2 | verify refusal (staged) | `verification_failed:<code>`; `retract` unlinks staged | preserved (tested `::test_refused_publication_preserves_old_artifact_and_cleans_staging`) |
| 3 | publish failure (os.replace raises) | `verification_failed:publish_failed` (RETRYABLE); staged already unlinked in `publish_*`'s `except BaseException`; final untouched | preserved (tested `::test_publish_failure_retracts_staging_and_preserves_old_artifact`) |
| 4 | **re-probe failure after replace** | `verification_failed:reprobe_failed` (RETRYABLE); `retract` only unlinks `.staged` (already gone) and ignores the published name | **DESTROYED** (repro R1) |
| 5 | persistence failure after success | n/a-by-SQLite-commit-on-return; job stays `verifying` → recovery re-runs | preserved/re-replaced |
| 6 | crash between rename and persistence | row `verifying` → `resume_pending` → handler re-runs from scratch (RAG side effect repeats — F11) | attempt-1 published bytes survive until re-publish |

**Answer to the mission's vital question: NO — on re-probe failure after `os.replace`, the previous
artifact is NOT preserved.** R1 (verbatim): `status=failed_retryable; final artifact now='NEW
EXTRACTED'; staged exists=False; prior content preserved=False` (previous content was `'OLD VALID
EXTRACTION'`, replaced via `os.replace` before the failing re-probe).

**Is this against the contract? YES — as the contract is written.** `JOB_LIFECYCLE.md` §4:

> "A refusal (verification, publish, **or re-probe**) retracts the staged temp and **never** touches
> a previously published artifact: a failed extraction can no longer destroy a valid
> `<stem>.extracted.txt`, and a partial artifact never occupies a final name."

D-0015's Decision text repeats it ("any refusal retracts the staged temp and preserves the previous
published artifact") and `feature_verification.retract_pdf_text_artifact`'s docstring claims "The
published destination is untouched: a previous valid artifact survives a refused publication".
The tested subset is only refusals **before** the swap. There is **no test** for the re-probe
refusal branch (negative-space: `test_gate5_closure.py`'s 14 tests cover verify-refusal,
first-refusal-without-previous, happy-path, publish-failure — nothing for re-probe failure).

This is **not** covered by the "contract only guarantees atomic publication" escape hatch: the
contract explicitly enumerates "or re-probe". **Therefore: PATCH (per mission rule). Disposition:
FIX NOW (F1).** Two compliant shapes, owner's choice (both require the lease — owned by
task-181/Agent E):

1. **Code (literal §4):** keep the old bytes via a same-dir hardlink/rename backup around the swap
   and restore on re-probe failure (note: D-0015 *rejected* "backup-then-replace" for `.prev` orphan
   crash states — choosing this requires amending D-0015); **or**
2. **Contract truth (aligns with D-0015 rejected-alternative 3):** patch §4 + D-0015 +
   `retract_pdf_text_artifact` docstring + the GATE5 audit/matrix wording to scope preservation to
   **pre-swap refusals** ("stage/verify/publish failures preserve the previous artifact; after the
   atomic swap the new artifact IS the published truth — a failed re-probe fails the job but cannot
   resurrect the old bytes") and record **ACCEPTED RISK** for the post-swap window with D-0015 as
   the contract evidence.

The claim "preserves previous artifact" (mission §16 audit item) is true **only for a subset**:
before replace / verify-and-publish failures — NOT after replace (re-probe), NOT across crash (F11).

## 10. NOTIFICATION

- **Ordering (persist → notify):** true on the normal paths — `_mark_*` awaits precede
  `_notify_completion` in all five terminal call sites; hook exceptions are swallowed fail-safe with
  `job_id` in the warning. The notifier (bot/app.py 3-way, slideshow_notify, creative notify) keys
  success off durable `completion.status is COMPLETED` and double-refuses `result.success is False`.
  `test_notification_matrix_status_drives_copy`, `::test_lying_success_result_cannot_turn_failure_into_success`,
  `::test_verification_state_never_produces_a_success_notification` green at `5f273f08`. ✓
- **F3 — REPRODUCED hole (R3b):** `_mark_completed`/`_mark_failed` **discard their rowcount**. If
  the row was reclaimed mid-flight (in-contract `resume_pending`/`_reset_unfinished` from a second
  process), the CAS silently no-ops and `_notify_completion` **still fires** with
  `JobCompletion(status=COMPLETED)`. Observed: `durable='pending'
  notified=[(…, 'completed')]` — the durable-lifecycle-state-is-truth rule (§9: "the notifier
  delivers after the durable state is already final") is violated in exactly that window.
  `_mark_verifying` returns `rowcount>0` (its CAS-fail path correctly stays silent), so only the
  terminal marks are exposed.
  **Disposition: FIX NOW** (minimal: `return cursor.rowcount > 0` from `_mark_completed` /
  `_mark_failed`; notify only on `True`; on `False` log `job_completion_skipped_reclaimed` with
  `job_id`). Owner: task-181/Agent E (lease STOP for this session).
- missing job_id / stale result / success-mismatch / terminal-mismatch: covered by
  `JobCompletion` construction (always carries durable `job_id`, `payload`; `result` only for typed
  failures) + the tests above. The R3b path is the only mismatch producer.

## 11. NEGATIVE-SPACE FINDINGS (LAW 3 — "where can this fail without the tests noticing?")

Targeted searches run over `src/` at `5f273f08` (direct `UPDATE status`, direct assignment to job
status, direct filesystem publish, verifier bypass, notify-before-persist, direct completed
transition, swallowed exceptions before classification, scheduler/retry/cancel/recovery code,
legacy `"failed"` aliases):

| Search target | Result | Disposition |
|---|---|---|
| direct `UPDATE nexus_job_queue` outside `_mark_*`/`_reset_unfinished` | none | — |
| unguarded write | **`_mark_pending`** (`WHERE id=?`) | **F2 FIX NOW** |
| notify-before-persist | none (all five sites persist first) — but CAS-fail notify-after-failed-persist exists | **F3 FIX NOW** |
| exception swallowed before classification | none in the queue (`except Exception` → classify+persist; notifier exceptions swallowed *after* durable state) | — |
| scheduler / retry implementation | none (`storage/resilience.retry_with_backoff` is storage-only) | INTENTIONAL (§7) |
| legacy `"failed"` spelling | `parse_job_status` alias → `failed_terminal` (read path only, tested). Separate legacy lane: `api/app.py:304` `registry.update_job_status(job_id, "failed")` writes the **legacy `creative_jobs` table** (frozen GAP-D lane, ADR 0006 + D-0010, tripwire `test_legacy_lane_verification_gap.py`, owner record task-165) | **OUT OF SCOPE WITH NAMED OWNER + TASK** (record already in-repo; confirmed present and honest) |
| re-probe-failure branch coverage | **untested**; contract claim false (F1) | **F1 FIX NOW** |
| cross-instance (`resume_pending` + live owner) coverage | untested; duplicated execution + terminal reopen possible | F4 INTENTIONAL (duplicates) / F2 FIX NOW (reopen) |
| job_id on every lifecycle log | 4 explicit lifecycle lines carry `job_id`; `_publish_and_reprobe`/`_verify_safely` warnings rely on `structlog.contextvars.merge_contextvars` (configured in `observability/logging.py` for both structlog and stdlib pipelines) | INTENTIONAL CONTRACT (trace §10 as written; proven `::test_every_lifecycle_trace_event_carries_job_id` + probe #4) |
| staging leftovers / stale staging | `.staged` overwritten atomically by next attempt + retracted on refusal; `.part-<pid>` temps self-clean on write failure but **hard-crash orphans have no janitor** (inert, never a final name) | ACCEPTED RISK (owner task-181; cosmetic FS hygiene; consistent with "a partial artifact never occupies a final name") |
| crash-between-rename-and-persist | re-runs handler from scratch on resume; duplicate external side effects (e.g. RAG ingest in `process_pdf_job`) possible | ACCEPTED RISK (at-least-once recovery is the declared model; `resume_pending_jobs` docstring warns of exactly this class; no exactly-once claim exists in the contract) |

## 12. MUTATION (mission §13 — official harness `scripts/gate5_mutation_probes.py`)

- **Status: `UNVERIFIED` (NOT PASS).**
- Reason (LAW 17): `agent_board.py check` on the harness's own mutation targets
  (`adapters/in_process_job_queue.py`, `jobs/failure_semantics.py`, `jobs/feature_verification.py`,
  `bot/app.py` — all task-181 exclusive paths) returns **STOP**. ABSOLUTE RULE "No mutation without
  ownership" therefore forbids executing the 6 GREEN→MUTANT RED→RESTORE→GREEN cycles in this
  session. The mission's fallback is explicit: «اگر اجرا نشد: UNVERIFIED نه PASS».
- The PR body/audit quote a 6/6 result on intermediate trees (`4a57f37f71d0…`, `b3bce412a1bd…`,
  `f54415f35493…`, `acfa4edeb65d…` content hashes). Per CURRENT-SHA PINNING those are narrative
  until re-executed at `5f273f08` by the lease holder (or CI). Mutation probes #1–#6 are additionally
  **not** wired into the CI workflow (manual harness), so no CI evidence exists for them either.
- Required follow-up (owner: task-181/Agent E): run
  `.venv/bin/python scripts/gate5_mutation_probes.py` at the final head and pin the transcript.

## 13. REGRESSION (executed at `5f273f08` in `worktree pr78-head`, venv `pip install -e ".[dev]"`)

| Gate | Result |
|---|---|
| `tests/unit/test_failure_semantics.py` + `test_job_lifecycle.py` + `test_job_verification_gaps.py` + `test_creative_notify.py` + `test_bot_slideshow_notify.py` | **70 passed** |
| `tests/integration/test_gate5_closure.py` | **32 passed** |
| `tests/integration/test_job_lifecycle_queue.py` + `test_in_process_job_queue.py` | **20 passed** |
| `tests/integration/test_verification_gap_closure.py` + `tests/architecture/` | **112 passed** |
| **full `pytest -m "not slow"`** | **2012 passed, 20 skipped in 103.5s** — RUNTIME-verified against the PR claim "2012 passed / 20 skipped" ✓ |
| collect-only `-m "not slow"` | 2032 collected (= 2012+20) |
| `ruff check .` | All checks passed! |
| `ruff format --check .` | 475 files already formatted |
| `mypy src` | Success: no issues found in 238 source files |
| `scripts/check_version_lockstep.py` | ok — VERSION == pyproject == CHANGELOG == 3.13.0 |
| `tests/integration/test_bot_slideshow_flow.py` / `test_creative_chain_e2e.py` | inside the full run ✓ (Gate-5-adjacent suites) |

Failure classification: the only red executions in this entire review are the **deliberately
reproduced** flakes/gaps (R1–R5, R3b, flake loop) — all classified in §14/§16. **No product-suite
failure exists at `5f273f08`.** PRE-EXISTING items were reproduced on base `035a896` (flake loop:
base 39/75 red vs head 23/50 red under identical load) as required.

TEST CLAIM vs PRODUCTION CLAIM vs RUNTIME CLAIM (mission §12 discipline):

- TEST CLAIMS: "2012/20", "collect diff +41 added / 3 documented renames", "no test deleted/
  skipped/weakened" → RUNTIME-verified 2012/20 + 2032 collected at `5f273f08`; assertion-delta scan
  of the 5 modified pre-existing test files shows **strengthened** asserts only (enum reconciliations
  `FAILED → FAILED_RETRYABLE/FAILED_TERMINAL` and notify-text `==` → `startswith(class line) +
  endswith(...) + "✅" not in text`), zero added `skip`/`xfail` (only two `# type: ignore` on a
  monkeypatch pair in the defense-in-depth helper). The three "renames" are documented repurposing
  of typed-failure rows per D-0015.
- PRODUCTION CLAIMS: §5/§6/§9/§10 contract text — audited line-by-line against source; three
  falsified clauses found (F1 wording, F2 "blind write cannot produce an illegal edge", F3 "delivers
  after the durable state is already final").
- RUNTIME CLAIMS: everything re-executed at `5f273f08` in this sandbox is marked as such above;
  nothing older was transferred (LAW 2).

## 14. CI FORENSICS (runs on current head `5f273f08`, plus named runs)

| Run | Event / head | Result | Exact failing step | Log access | Classification |
|---|---|---|---|---|---|
| **36058609988** | pull_request / `5f273f08` | **FAILURE** — test job `107831754032` step 5 "Run set -o pipefail" (exit 1); lint `107831753746`, lint-fast `107831754047`, migrate `107831754104` green | pytest | **zip unreachable from this sandbox** (Azure blob `productionresultssa17…` EOF ×4 via `gh run view --log`, `--log-failed`, `gh api …/jobs/…/logs`, `gh run download`); annotations contain only "Process completed with exit code 1" | **PRE-EXISTING flake — reproduced on base** (below) |
| **36060596005** | pull_request / `5f273f08` | **SUCCESS** 4/4 (test 6m34s) | — | artifact `pytest-log` exists but blob download EOFs too | green rerun of the same head |
| 36058604853 | push / `5f273f08` | FAILURE (twin of the above; test job `107831737165`, same step) | pytest | same limitation | same flake class (same head, same minute) |
| 36056491410 | pull_request / `b0304429` | FAILURE (earlier flake) | pytest | not needed | consistent with the same flake (Agent E's comment); NOT re-litigated — superseded by head-pin |
| 36057676037 (PR) + 36056456707 (push) | `b0304429` | SUCCESS | — | — | prior SHA (informational only, not transferred) |
| 36062038437 | push / `8233aa39` | SUCCESS | — | — | **different branch** `arena/01a0d547` (task-183 audit of PR#77) — not PR#78 evidence |

**Failed-run identity proof (independent of the unavailable zip):**

1. PR comment (21:18:11Z) quotes the failing assert fragment `assert 'pend…` and names
   `tests/unit/test_reminder_system.py::test_delivers_to_originating_chat_not_user_id`.
2. This review reproduced that exact assertion under load at `5f273f08` — captured verbatim:
   `E AssertionError: assert 'pending' == 'sent'` at `tests/unit/test_reminder_system.py:94`
   (22/36 load-stressed runs red; 30/30 calm runs green).
3. **Base reproduction (mandatory for PRE-EXISTING):** same test, same assert, on base
   `035a896` (PYTHONPATH-shadowed, unchanged file — the PR diff touches no reminder code):
   **39/75 load-stressed runs red.**
4. Root cause (structural, both legs identical): `features/tools.py::ReminderSystem._deliver` sends
   first (`bot.sent` flips inside `send_message`) and only then `await self._mark_status(rid,
   "sent")` via `asyncio.to_thread`; the test polls `bot.sent` (line 88) then reads the row (line 94)
   without polling status — under parallel load the write lags the read.

**Verdict:** the red runs are a **pre-existing notify-then-persist test race** in the reminder
subsystem (outside the Gate-5 job lifecycle), not a PR#78 defect. Classification label:
**PRE-EXISTING** (base-reproduced). Named owner + task for the repair (NEW LAW 1 wording):
**OUT OF SCOPE WITH NAMED OWNER + TASK** — owner: features/tests tenant of
`features/tools.py` + `tests/unit/test_reminder_system.py` (board: `feature-wiring`/tools area is
historically unclaimed for this file at review time; first claimant should take a
`reminder-delivery-race` task: fix = persist `sent` before `send_message` in `_deliver`, and/or make
line 94 poll status like line 192's pattern). This is NOT closed as "flaky": the cause is identified
and reproduced on both legs; only its repair is out of this review's scope.

## 15. DOCUMENT TRUTH (mission §16)

| Document | Claim audited | Verdict |
|---|---|---|
| `JOB_LIFECYCLE.md` §4 | "A refusal (verification, publish, **or re-probe**) … never touches a previously published artifact" | **FALSE after replace** (R1). Must be scoped to pre-swap refusals or implemented — **F1** |
| `JOB_LIFECYCLE.md` §2 | "the adapter enforces each edge with a status-conditioned UPDATE … a blind write cannot produce an illegal edge" | **FALSE** — `_mark_pending` is a blind write (F2) |
| `JOB_LIFECYCLE.md` §6 | "duplicate during PROCESSING \| one execution (per-process task table + UNIQUE key)" | TRUE as scoped (per-process); cross-process is outside the promise (F4 disposition) |
| `JOB_LIFECYCLE.md` §9 | "The durable lifecycle state is the notifier's only source of truth … delivers after the durable state is already final" | **FALSE in the CAS-fail window** (F3) |
| `JOB_LIFECYCLE.md` §5 / D-0015 / module docstrings | `RETRY SCHEDULER NOT IMPLEMENTED`; `failed_retryable→pending` RESERVED+absent | TRUE (verified in code + tests) |
| `D-0015` Decision (3) | "any refusal retracts the staged temp and preserves the previous published artifact" | **over-broad** — same as F1; D-0015's own Rejected-alternative (3) ("Backup-then-replace … worse crash semantics") documents that post-swap restoration was deliberately not built |
| `GATE5_CLOSURE_2026-09-24.md` §7 | "CI green on the PR head — CLOSED (fill run URL at close: see PR)" | **incomplete** — run URLs were never filled in-doc (they exist only in the PR comment); "green" also glosses over the same-head red run `36058609988` (dispositioned §14) — doc-fix folds into F7 |
| **`GATE5_TRUTH_MATRIX.json` claim `PR71-CI-green`** | `git_evidence: "gh pr view 71 --json headRefOid=9e9c75358e3e673d788e0c3549896016b11b112a; gh run list --branch feature/verification-gap-closure --commit 9e9c75358e3e… (5 runs, all gates green)"`, status PASS | **EVIDENCE FABRICATED / UNVERIFIED (F7):** `git cat-file` proves SHA `9e9c75358e3e…` **does not exist**; live `headRefOid` is `9e9c753082c5…`; `git ls-remote` shows **no branch** `feature/verification-gap-closure`; `gh run list --branch feature/verification-gap-closure` returns **zero runs**. The matrix's own rule ("PASS is only recorded where a named executable test, git ref, or gh artifact proves the link") is violated by its own content. Underlying claim is salvageable with real evidence: PR#71 head `9e9c753082c5` has runs **36031844422** (pull_request, success) + **36031809903** (push, success) — 2 runs, not "5", on the real branch |
| `GATE5_TRUTH_MATRIX.json` `known_non_verified` | "_mark_processing cross-process race (one process at a time — documented, unchanged)" | honest admission, but NEW LAW 1 forbids closure by "known" — now dispositioned as F4 INTENTIONAL CONTRACT (§7) |
| `GATE5_TRUTH_MATRIX.json` gate row "previous artifact preserved on refusal" | unqualified "on refusal" | same over-broad scope as F1 — tested subset only |
| PR body | "refusal retracts staging + preserves the previous `<stem>.extracted.txt`" (table row) | over-broad (F1); everything else in the body (6-state, taxonomy, scheduler NOT IMPLEMENTED, 2012/20, renames) RUNTIME-verified true at `5f273f08` |
| board.json task-181 note | "DELIVERED via PR#78 (head b030442, CI green on push 36056456707 + PR 36057676037; one flaky PR run 36056491410)" | true at its pin (`b030442`); head has since moved to `5f273f08` — per LAW 2 not transferable, and superseded by §14 |

## 16. FINDINGS + DISPOSITION (NEW LAW 1 — every finding carries exactly one disposition)

| # | Finding | Reproduced | Contract violation | Action (disposition) | Evidence |
|---|---|---|---|---|---|
| F1 | re-probe failure after `os.replace` destroys the previous published artifact; contract claims it survives "any refusal (… or re-probe)" | **YES** (R1) | **YES** — `JOB_LIFECYCLE.md` §4, D-0015 Decision (3), `retract_pdf_text_artifact` docstring | **FIX NOW** — owner task-181/Agent E (lease STOP for this session): implement restore-on-reprobe-failure (amend D-0015) **or** truth-patch docs + ACCEPTED RISK with D-0015 as the contract evidence; add the missing re-probe-failure regression test | `/tmp/repro_task181.py::r1` output in §9; `feature_verification.py:publish_pdf_text_artifact`; gate5 test list (no re-probe test) |
| F2 | unguarded `_mark_pending` (`WHERE id=?`) reopens terminal rows (`completed→pending`, `failed_*→pending`) after cross-process reclaim + cancel; re-execution follows | **YES** (R2: terminal reopened + 3 handler invocations) | **YES** — transition matrix (only `processing/verifying→pending`), "blind write cannot produce an illegal edge", `failed_retryable→pending` RESERVED fail-closed | **FIX NOW** — minimal guard patch in §8 + R2-shaped regression test; owner task-181/Agent E | `/tmp/repro_task181.py::r2`; `in_process_job_queue.py:781`; matrix check printed in §8 |
| F3 | `_mark_completed`/`_mark_failed` discard rowcount → completion hook announces `completed` while durable row is `pending` (reclaim window) | **YES** (R3b: `durable='pending' notified=[(…,'completed')]`) | **YES** — §7 "delivers after the durable state is already final"; §9 durable-state-truth | **FIX NOW** — return `rowcount>0` from the terminal marks, notify only on success (patch shape in §10); owner task-181/Agent E | `/tmp/repro_r3b.py` output in §10 |
| F4 | cross-process duplicate execution (two live workers run one job; `attempt=2`) | **YES** (R5) | No (outside the per-process promise) | **INTENTIONAL CONTRACT** — one-live-queue-process-per-sidecar is the deployment invariant (evidence: §6 parenthetical, `resume_pending_jobs` docstring warning, matrix `known_non_verified` admission). Not closed with "known": the invariant is the contract | `/tmp/repro_task181.py::r5`; docs quoted in §7 |
| F5 | `{"success": false}` without `error_code` | **YES** (R4, 3 legs) | No on default lanes (fail-closed proven); residual only under explicit `artifact_verifiers={}` opt-out | **INTENTIONAL CONTRACT** (dialect boundary + fail-closed default lanes) + **ACCEPTED RISK** for the opt-out leg (named owner task-181/Agent E; optional 2-line hardening `typed_failure:untyped`) | R4 output in §6; `is_typed_user_failure` docstring; bot/app.py + cli.py compose with default registries |
| F6 | earlier failed CI runs (36058609988 / 36058604853 / 36056491410) | **YES** — identical assert on head AND base | No (pre-existing test race, files untouched by PR#78) | **OUT OF SCOPE WITH NAMED OWNER + TASK** — `reminder-delivery-race` task (persist-before-send in `features/tools.py::_deliver` + status-poll in test line 94); owner: features/tools + tests/unit/test_reminder_system claimant via `agent_board.py next` | §14 proof chain; captured `assert 'pending' == 'sent'`; base 39/75 red |
| F7 | `GATE5_TRUTH_MATRIX.json` cites a **non-existent SHA** `9e9c75358e3e…` and a **non-existent branch/5 runs** as `gh` evidence (claim `PR71-CI-green`); GATE5_CLOSURE §7 never filled its promised run URLs | **YES** (`git cat-file` fatal; `ls-remote` empty; `gh run list` empty) | **YES** — the matrix's own "no_pass_by_existence" rule; LAW 2 evidence pinning | **FIX NOW** — docs: replace fabricated rows with real evidence (PR#71: `9e9c753082c5` + runs 36031844422/36031809903; PR#74: `05d647e9` + runs 36040556195/36040549601), fill §7 run table (incl. honest same-head red run + disposition), restate the preservation gate with F1's scope; owner task-181/Agent E | §15 rows; exact commands + outputs above |
| F8 | hard-crash staging orphans (`.part-<pid>`) have no janitor | structural (source trace) | No (they never occupy a final name) | **ACCEPTED RISK** — owner task-181/Agent E; optional cleanup-on-resume | `publish_text_artifact` source |
| F9 | crash between rename and persistence re-runs the handler on resume (duplicate external side effects, e.g. RAG ingest) | structural (source trace + `::test_crash_after_publish_before_persist_recovers_on_resume` covers recovery, not side-effect uniqueness) | No exactly-once claim exists | **ACCEPTED RISK** — at-least-once recovery is the declared model; owner task-181/Agent E | §11 rows |
| F10 | legacy `/creative` HTTP lane (`api/app.py` `creative_jobs` table, `"failed"`/`"done"` spellings) bypasses the 6-state queue | present (source trace) | No — frozen by design | **OUT OF SCOPE WITH NAMED OWNER + TASK** — GAP-D record (owner: task-165 triage + ADR 0006 + D-0010 freeze; tripwire `test_legacy_lane_verification_gap.py` verified present) | `api/app.py:295–308`; matrix `legacy_creative_proven_gap` |

The mission's four named mandatory-disposition items map to: (1) post-replace re-probe failure →
**F1 FIX NOW**; (2) unguarded `_mark_pending` → **F2 FIX NOW**; (3) `success=False` without
`error_code` → **F5 INTENTIONAL CONTRACT + ACCEPTED RISK (opt-out leg)**; (4) earlier failed CI runs
→ **F6 OUT OF SCOPE WITH NAMED OWNER + TASK** (plus F7 for the evidence-integrity defect their
reporting introduced). No gap is closed with a banned word.

PATCH RULE (LAW 18) compliance note: F1/F2/F3 meet all three patch preconditions (observed +
contract violation + reproduction). This session performed **no patch** — LAW 17 ownership gate
returned STOP on every file a minimal patch touches (lease `task-181-gate5-closure`, live until
2026-09-26T19:04Z). Patch shapes are specified above at engineering precision so the lease holder
can apply them with the required targeted tests + mutation + full regression + CI.

## 17. EXACT SHAS

```
main (base)                 035a896dd2ed1293de6accf2ef4309da2fd64c89
PR#78 head (audited)        5f273f08f543ae7916ccdf1eeee8544a44cbf81b
PR#78 commit stack          85f4444bc166…, 9e9c753082c5…, 59f807d6be72…, 05d647e9ccc4…,
                            10a09bff407c…, b03044295fe1…, 5f273f08f543…
PR#71 head                  9e9c753082c572d1164148be7da50c061ed53311
PR#74 head                  05d647e9ccc4e9c3c823020972e01d8cd14fc80a
board commit (task-181)     5f273f08 (board.json updated_at 2026-09-24T19:04:01Z on head)
non-existent (matrix, F7)   9e9c75358e3e673d788e0c3549896016b11b112a  ← git cat-file: missing
unrelated (not PR#78)       8233aa398206a93dfcccda0723afcd3ae9643f51  (arena/01a0d547, PR#77 audit)
```

## 18. EXACT CI RUN IDS

```
36060596005  pull_request  head 5f273f08  SUCCESS 4/4   (jobs 107838316550 test, 107838316925 lint,
                                                        107838317111 lint-fast, 107838316920 migrate)
36058609988  pull_request  head 5f273f08  FAILURE       (test job 107831754032 FAILED step 5;
                                                        others green)  ← forensics §14, PRE-EXISTING
36058604853  push          head 5f273f08  FAILURE       (test job 107831737165; same flake class)
36057676037  pull_request  head b0304429  SUCCESS       (prior SHA — informational only)
36056491410  pull_request  head b0304429  FAILURE       (prior SHA flake)
36056456707  push          head b0304429  SUCCESS       (prior SHA — informational only)
36031844422  pull_request  head 9e9c7530  SUCCESS       (PR#71 real evidence for F7 repair)
36031809903  push          head 9e9c7530  SUCCESS       (PR#71 real evidence for F7 repair)
36040556195  pull_request  head 05d647e9  SUCCESS       (PR#74 real evidence for F7 repair)
36040549601  push          head 05d647e9  SUCCESS       (PR#74 real evidence for F7 repair)
36062038437  push          head 8233aa39  SUCCESS       (arena/01a0d547 — NOT PR#78 evidence)
```

## 19. FINAL STATUS

**`NEEDS-REPAIR`**

Merge-readiness checklist (mission §19) at `5f273f08`:

| Criterion | State |
|---|---|
| current SHA verified | ✓ (`5f273f08` pinned; base `035a896`) |
| ownership valid | ✓ checked — STOP for mutations; review itself clean (LAW 17 honored) |
| all required claims proven | ✗ — F1/F2/F3 falsify three contract clauses (reproduced) |
| no unresolved blocker | ✗ — F1, F2, F3 (FIX NOW) + F7 (evidence integrity) open |
| every finding has disposition | ✓ (§16 — four mandatory items dispositioned explicitly) |
| CI current SHA green | ✓-with-disposition — latest run `36060596005` green 4/4 and `gh pr checks` green; same-head red run `36058609988` classified PRE-EXISTING (base-reproduced) + owner/task named (F6) |
| docs truthful | ✗ — §4/§7/§9/D-0015 over-broad claims + fabricated matrix evidence (F7) |
| mutation evidence adequate | ✗ — **UNVERIFIED** (ownership-blocked; the PR's 6/6 quote is unpinned narrative) |
| no hidden bypass found | ✓-with-dispositions — negative-space sweep (§11) found only dispositioned items |

**What "MERGE-READY" requires now (owner: task-181-gate5-closure / Agent E, lease live):**

1. F2 minimal guard patch + R2-shaped regression test (terminal states must survive a stale
   cancel across `resume_pending` reclaim).
2. F3 rowcount-gated notification + regression (hook must not announce `completed` for a row that
   is not).
3. F1: pick one — restore-previous-on-reprobe-failure (+ D-0015 amendment) **or** truth-patch §4 to
   pre-swap scope with an ACCEPTED RISK record — and add the missing re-probe-failure test either
   way.
4. F7 doc repair with the real SHAs/run ids listed in §17/§18 (drop `9e9c75358e3e…` and
   `feature/verification-gap-closure`).
5. Mutation harness executed at the final head (GREEN→RED→RESTORE→GREEN transcript pinned) —
   replaces today's UNVERIFIED.
6. Optional but recommended: F5 opt-out hardening + F8/F9 accepted-risk acknowledgments in
   D-0015.
7. Re-gate: full regression + ruff/format/mypy + CI green on the exact new head (LAW 2: everything
   re-pinned).

Re-review trigger: any push moving `5f273f08` invalidates every verdict above until revalidated at
the new SHA (LAW 2). «قبلاً سبز بود» has no evidentiary value.

---

### Appendix A — reproduction transcript (executed at `5f273f08`, scripts in `/tmp`, no repo files mutated)

```
R1 re-probe failure after os.replace: status=failed_retryable; final artifact now='NEW EXTRACTION';
   staged exists=False; prior content preserved=False                → CONTRACT VIOLATED
R2 unguarded _mark_pending: after W2 status=completed → after W1 cancel status=pending
   (terminal reopened=True); handler calls 2→3 (duplicate execution=True)
   matrix: completed→pending legal? False | failed_retryable→pending legal? False                    → CONTRACT VIOLATED
R3b CAS-fail notify: durable='pending' notified=[('712d64168a…','completed')]                        → CONTRACT VIOLATED
R4 success=False w/o error_code: default verifier → failed_terminal
   (verification_failed:success_without_artifact_claim) | opt-out → completed | empty code → failed_terminal
                                                                                                     → INTENTIONAL (F5) + residual
R5 two workers: handler invocations=2 (concurrent), attempt=2                                        → F4 (scoped contract)
Flake: E AssertionError: assert 'pending' == 'sent'  tests/unit/test_reminder_system.py:94
   head 5f273f08 under load 22/36 (and 23/50 earlier) red; base 035a896 under load 39/75 red; calm 30/30 green
```

### Appendix B — commands of record

`gh pr view 78|71|74 --json …` · `gh run list|view|download` · `gh api …/check-runs[?…/annotations]` ·
`git worktree add /home/user/pr78-head 5f273f08` · `python scripts/agent_board.py check …` ·
`pytest -m "not slow"` / targeted suites / collect-only · `ruff check` · `ruff format --check` ·
`mypy src` · `check_version_lockstep.py` · `/tmp/repro_task181.py` · `/tmp/repro_r3b.py` ·
flake loops (3×parallel pytest + 2×CPU burners) on head and on base via PYTHONPATH shadow ·
negative-space greps over `src/` · `git diff 035a896..5f273f08` (tests integrity scan).
