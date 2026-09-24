# Gate 5 FINAL REPAIR — execution fencing, artifact publication, durable state truth (task-181-gate5-closure)

Owner: `arena/01a0d567-nexus-ai-agent` (real owner of `task-181-gate5-closure`, stewardship
takeover recorded in `.agents/board.json` `takeover_log` at `2026-09-24T22:50:05Z`).
Mission: Gate-5 FINAL REPAIR (§§0–32). This document is the repair + proof record. Nothing from
the prior audit (`PR78_ADVERSARIAL_GAP_REVIEW_2026-09-24.md`) or previous agents was accepted as
truth; every claim below carries live evidence from THIS session (LIVE GITHUB → SOURCE → RUNTIME →
TEST → MUTATION → CI → CONTRACT → DECISION).

---

## 0. SHA lock (live re-read 2026-09-24/25 UTC — pinned values)

| Object | SHA / value | How verified |
|---|---|---|
| old main (session start) | `035a896dd2ed1293de6accf2ef4309da2fd64c89` | `git rev-parse` |
| **new main** | `16daebcede5f914e950fb1aeb6757f74d1985ab9` | `git fetch` + `git rev-parse origin/main` (PR#72 `1099c76` + PR#77 merged) |
| PR#78 head (pre-repair delivery) | `5f273f08f543ae7916ccdf1eeee8544a44cbf81b` | `gh pr view 78` (CONFLICTING/DIRTY vs new main) |
| PR#78 lineage stack | `85f4444b → 9e9c7530 (PR#71) → 59f807d6 → 05d647e9 (PR#74) → 10a09bff → b0304429 → 5f273f08` | `git log --oneline` |
| session branch base | `035a896…` + audit `765d9f8` | `git log` |
| lineage merge (commit-for-commit, no rebase) | `2449194` | `git log` |
| **OLD-RED baseline** (merged tree before repair) | `6b8663307bce7f10f35434215de3c53ed0015339` | `git rev-parse HEAD` during R1–R5 capture |
| new main's `9e9c753…` claim from the old truth matrix | `9e9c75358e3e673d788e0c3549896016b11b112a` | **NOT A GIT OBJECT** (fabricated; see F7). Real PR#71 head = `9e9c753082c5…` |
| `feature/verification-gap-closure` | — | **NO SUCH REF** (fabricated; see F7) |

CI runs cited below were read with `gh run list/view` (log/artifact downloads are dead in this
environment (Azure EOF) — run conclusions are classified by step status + local reproduction only).

## 1. Ownership gate (PASSED before any mutation)

* identity: `arena-agent` (git actor `arena-agent <297053741+arena-agent@users.noreply.github.com>`;
  `gh api user` is 403-for-integration in this sandbox — identity proven via push/authenticated git).
* canonical task: **`task-181-gate5-closure`** (full id; the board's `task-181-runtime-service-grants`
  is a different task — never short-id matched).
* session branch: `arena/01a0d567-nexus-ai-agent` (only this branch pushed to).
* live lease: `claimed_at 2026-09-24T22:50:05Z`, `ttl_hours 48` (expires `2026-09-26T22:50:05Z`).
* exclusive paths: 26 (the zone `job-lifecycle-gate5-closure` + `tests/unit/test_reminder_system.py`
  added to the zone with reason (CI-blocking flake repair); `test_agent_board.py` zone rule GREEN).
* competing agents: `task-166/167` DONE+released on main's board (paths freed);
  `sec-boundary-salvage-01a0d563` (security zone, disjoint); `task-183` `active_in_review` with
  **empty** exclusive paths; `task-179` (PR#72 **MERGED**) stewardship-released per the
  orphaned-lease rule (pr47/p0-stabilization precedent) — its `docs/DECISION_LOG.md` /
  `docs/README.md` fence was the only collision with this task's required edits.
* PR head: PR#78 stays the pre-repair delivery vehicle; its lineage was merged intact.
* working tree: clean after each board/mutation step; `agent_board.py check --files <23 paths>
  --branch arena/01a0d567…` → **exit 0** ("no overlap — safe to proceed").

## 2. Five-path research (Evidence → Options → Trade-offs → Decision)

| Path | Sources | Takeaway |
|---|---|---|
| A official docs | Python `os.replace` docs (atomic rename, same-fs), SQLite single-writer + `UPDATE … WHERE` optimistic-CAS semantics (sqlite.org / concurrency write-ups) | conditional `UPDATE` + `rowcount` is the storage-side CAS; `os.replace` is atomic but **destroys the replaced inode** — preservation needs an explicit backup |
| B mature GitHub impls | [riverqueue/river PR#1373](https://github.com/riverqueue/river/pull/1373) (stale JobRescuer must match `id + state + attempted_at < horizon`, 0 rows on stale), [mdc159/shizzle PR#42](https://github.com/mdc159/shizzle/pull/42) (stage-outcome writes fenced by lease ownership), [eh3aneba/maritime-claims-platform PR#455](https://github.com/eh3aneba/maritime-claims-platform/pull/455) (lease fingerprint `(job_id, attempt_count, locked_by, locked_at)` revalidated before every flush), [zhaochy1990/running PR#470](https://github.com/zhaochy1990/running/pull/470) (CAS `running→queued` with LeaseBefore guard), [Thanane15M/postgres-first #10](https://github.com/Thanane15M/postgres-first/issues/10) ("completion/failure updates must match both job id and the current token; a stale token must update zero rows") | the mature pattern is exactly: **generation/fence token + lease fingerprint validated in every durable write predicate** |
| C real failures | river#1302 (rescuer overwrote a completed job), shizzle review threads (TOCTOU between lease check and remote effect; "a second local ownership check alone narrows but does not eliminate the window"), running#470 (dead worker stranded `running` forever) | the failure class is real and recurring: stale writers overwriting newer executions; fixes are predicate-fencing + explicit fenced takeover |
| D research | M. Kleppmann, [How to do distributed locking](https://kleppmann.com/2016/02/08/how-to-do-distributed-locking.html) ("include a **fencing token**… a number that increases every time a client acquires the lock… the storage system maintains the ratchet… **a UUID cannot tell the storage whether it is stale**"), etcd/ZooKeeper revision fencing, Pat Helland idempotency (at-least-once side effects), microservices.io transactional-outbox (explicitly evaluated, see §12) | fence value must be **monotone** per row → `attempt`; UUID may only be the identity half; the storage side must reject `token < current` |
| E repo architecture | `jobs/lifecycle.py` TRANSITIONS matrix, `adapters/in_process_job_queue.py` (`_mark_*`, `_publish_and_reprobe`, `_reset_unfinished`), `jobs/feature_verification.py`, `worker.py` (handlers pure `dict→dict`), `bot/app.py` notifier | handlers hold no queue identity — the fence belongs queue-internally at every `_mark_*` + the publication protocol; one shared predicate (`_fence_update`) is the smallest common abstraction |

## 3. Root cause — one class: Execution Ownership / Commit Fencing

F2 (unguarded `_mark_pending`), F3 (notify without durable state) and R5 (dual claim) are one
defect class: **post-reservation durable writes carried no execution identity**. F1 (artifact
destruction) is the same class one level up (the side-effect namespace): the swap, its rollback
and its commit carried no ownership/journal, so a refused or stale execution could change the
final artifact. The repair is therefore one small shared abstraction plus its disciplined use:

* `jobs/fencing.py` — `ExecutionToken(job_id, generation, owner_token)` (lease fingerprint) +
  `lease_cutoff` + `PUBLICATION_JOURNAL_KEY`;
* `InProcessJobQueue._fence_update` — **the** predicate (`id + attempt + owner_token + status IN
  (…)`) every post-reservation transition goes through;
* the publication protocol (publish/retract/recover/retire) — inode-guarded, journaled, fenced at
  commit.

Independence proof that the shared root is real: mutation **M1** (break the predicate in
`_fence_update` alone) kills T1+T2/T3+T8 simultaneously (16/16 mutation run, section 20).

## 4. WHO OWNS A JOB (answers, all executable)

| Question | Answer | Proof |
|---|---|---|
| HOW is ownership represented/represented on transfer? | `ExecutionToken = (job_id, attempt, owner_token)`: `attempt` increments at every reservation (monotone fence), `owner_token` (fresh UUID) = durable identity; takeover transfers ownership by invalidating `owner_token` (NULL) and re-reserving (new token, `attempt+1`) | T5, T6 |
| HOW does an old worker prove it is stale / a new one prove it is current? | its token matches 0 rows (`WHERE … AND attempt=? AND owner_token=?`); the new generation matches | T1–T3, M1 |
| WHEN does the lease expire and HOW is ownership claimed? | `started_at + lease_ttl_seconds` (default 300s, constructor override); claimed only through `resume_pending` → `_reclaim_stale` (lease-expired rows only) | T6, T7 |
| WHAT happens to the old worker after takeover? | every fenced write (complete/fail/release/journal/notify) matches 0 rows → refused; its publication rolls back inode-guarded | T1–T4, T8, T15 |
| HOW is a new generation issued? | strict CAS `PENDING→PROCESSING` (`_mark_processing`): never `PROCESSING→PROCESSING`; rowcount must be 1 | T5, T7, M3 |
| HOW can stale executions be excluded BEFORE side effects? | the reservation CAS; and before each namespace-changing effect (`_mark_verifying` gate → swap; journal; fenced commit) | T8, M9 |

## 5. Fencing options compared (mission §5 — exact criteria)

| Criterion | A attempt fencing | B execution_token (UUID) | C lease + owner token + expiry | D single-process invariant + fail-closed reclaim | **E hybrid (CHOSEN)** |
|---|---|---|---|---|---|
| Correctness | strong (monotone) | weak alone (Kleppmann: UUID unordered) | strong | weak cross-process | **strong** (`attempt` monotone + `owner_token` identity) |
| Crash recovery | needs takeover rule | needs takeover rule | built-in (expiry) | rebuild | **expiry takeover + token invalidation** |
| Cross-process safety | yes (CAS) | partial | yes | no | **yes (proven two-instance tests)** |
| Side-effect safety | DB only | DB only | DB only | process-local | **DB + inode-guarded publication rollback** |
| Schema impact | `attempt` (exists) | +token column | +token + timestamps | none | **+`owner_token` (guarded ALTER)** |
| Complexity | low | low | medium | low | medium (one predicate + 3-verb publication) |
| Testability | high | medium | high | low | **high (T1–T15, M1–M10)** |
| Backward compat | yes | yes | yes | n/a | **yes (legacy NULL-started_at rows reclaimable; legacy status spellings mapped)** |
| Operational behavior | strict claim | strict claim | strict + takeover latency ≤ TTL | strict in one process | **strict + bounded takeover (TTL 300s default)** |

**DECISION: E — hybrid.** Monotone `attempt` generation fencing (A) + `owner_token` durable
identity (B as *identity only*, never the fence — Kleppmann's rule) + lease-expiry takeover (C) +
fail-closed rowcount semantics everywhere (D's discipline), and the side-effect namespace fenced
by the journaled, inode-guarded publication protocol.

## 6. R5 reclassification (mission §6)

Inspected the reservation SQL at the old baseline: `UPDATE nexus_job_queue SET status='processing',
started_at=?, attempt=attempt+1 WHERE id=? AND status IN ('pending','processing')` — **PROCESSING
was accepted as a new claim** and no EXECUTABLE invariant forbade multi-process execution (the
docs' "single-process by design" is not a runtime invariant; `resume_pending` is public API and two
processes may share the SQLite sidecar — `worker.py` even documents one durable queue shared by
bot + CLI). Reproduced at `6b86633`: two `_mark_processing` calls → `attempt=2`, both live
(R5 VIOLATED). **REAL OWNERSHIP BUG** (F4/R5). Fixed by the strict CAS (M3/M5 guard it).

## 7–10. F2/F3 reproduction and the fenced transition set

OLD-RED at `6b86633` (2026-09-24T23:08:54Z, harness `/tmp/repro_gate5_final.py`, evidence
preserved in this report): R2 `completed reopened to pending`; R3 `durable=pending
announced=['completed']`; R5 `attempt=2 dual claim`. Every post-reservation transition now runs
through `_fence_update` with `(id, attempt, owner_token)` + expected states:

| Transition | Expected states | Fenced | Returns |
|---|---|---|---|
| `_mark_processing` | `PENDING` only (strict CAS) | mints token, `attempt+1`, rowcount must be 1 | `ExecutionToken \| None` |
| `_mark_verifying` | `PROCESSING` | yes | `bool` |
| `_mark_completed` | `PROCESSING\|VERIFYING` | yes | `bool` = **COMMIT_CONFIRMED** |
| `_mark_failed` (post-reservation) | `PROCESSING\|VERIFYING` | yes | `bool` |
| `_mark_failed` (claim-time structural) | `PENDING` only | scoped (no execution exists) | `bool` |
| `_mark_pending` (self-release) | `PROCESSING\|VERIFYING` | yes (a stale cancel can never reopen a newer execution) | `bool` |
| `_reclaim_stale` (startup takeover) | `PROCESSING\|VERIFYING` AND lease expired (or legacy NULL `started_at`) | invalidates `owner_token` | ids |

## 11–12. Completion durability + notification contract (explicit decision)

**Truth ("never lie") is enforced; delivery ("eventually notify") is best-effort and documented as
such.** `_notify_completion` fires only after the fenced terminal UPDATE returned `rowcount>0`
(COMMIT_CONFIRMED) — one truthful notification per durable outcome; a displaced generation is
silent (its outcome is void; the current owner announces its own). The hook remains strictly
fail-safe (a broken notifier cannot corrupt the queue) but **notifier failure after commit is not
retried: delivery is at-most-once per outcome, truth is exactly-once**. A transactional outbox was
evaluated (microservices.io pattern) and **rejected as unjustified migration** for this codebase
(the durable row IS the outbox for truth purposes; delivery-grade outbox/retry is an explicit
non-goal, recorded here and in D-0016). Proven by T4/T15 + R3 (and M5 kills the gate).

## 13–16. F1 solved as an architecture problem (options A–E, mission §14)

| Criterion | A backup→restore | B versioned artifacts + pointer | C stage+verify+replace, drop re-probe | D two-phase FS journal | E publish transaction abstraction |
|---|---|---|---|---|---|
| Crash safety | good (backup) | excellent | good | excellent | excellent |
| Failure recovery | restore old | pointer flip | **previous destroyed on post-replace failure** | journal replay | restore/abort |
| Previous-artifact preservation | **yes** | yes | **no (violates T-list)** | yes | yes |
| Concurrency | needs guard | good | needs guard | good | needs guard |
| FS semantics | hardlink/copy + `os.replace` | many files | atomic swap only | journal file | combined |
| Complexity | low | high (manifest/pointer everywhere) | lowest | medium | medium |
| Performance | +1 link | +n writes | baseline | +journal writes | +journal (row) |
| Backward compat | high | low | high | medium | high |

**DECISION: A' = Option A hardened (backup + inode-guarded restore + row-journal + fenced commit)
with Option E's transaction shape.** The post-publish **re-probe is KEPT** (mission §15's serious
evaluation): it is the only independent measurement of a *pluggable* publisher's claim at the
final name (mis-routed/damaged publication detection), the T-list/M-list require its decision
guard (T9/M8), and with A' its failure path is now safe (restore), which was the only argument
against it. "Previous artifact is never touched on refusal" **now survives with proof** (T9/T10 +
R1 NEW-GREEN) — and was previously false (R1 OLD-RED). Publication order: **stage → verify →
backup+journal+swap → re-probe → fenced commit (COMMIT_CONFIRMED) → retire backup → notify**;
any refusal after the swap restores the pre-swap bytes **iff the destination is still this swap's
inode** (a newer owner's bytes are never overwritten — T8). Crash windows (T11): (i) backup only →
duplicate dropped, destination intact; (ii) journaled swap, commit lost → pre-swap bytes restored
(no uncommitted publication survives); (iii) commit confirmed, retire lost → committed bytes stand;
stray backup retired later. Publication race (§16): every `os.replace`/`write_*`/`unlink` in the
job-artifact zone is owned by `jobs/feature_verification.py` (stage/publish/retract/recover/retire)
+ `_fence_update`'s DB writes; the negative-space scan (section 21) classifies every other write
site in `src/` as out-of-zone (creative lane / screenshots / backups / storage providers — each a
different subsystem with its own contract).

## 17. Side-effect fencing (DB alone is not enough)

Trace: CLAIM (CAS PENDING→PROCESSING) → HANDLER (pure `dict→dict`; its artifact write is the
staged candidate) → VERIFY → SIDE EFFECT (backup+swap+journal, fenced `_record_publication`) →
RE-PROBE → DB COMMIT (fenced `_mark_completed`) → NOTIFY (gated). A stale execution becomes
ineffective **from token invalidation (takeover/release) onward**: `_mark_verifying` refuses →
it never reaches publish; if it already swapped, the fenced commit refuses → inode-guarded
rollback; its journal writes are fenced (M9); its notify is gated (M5). Documented residual: a
handler's *external* effects (RAG ingestion, network sends inside handlers) started before the
steal are at-least-once — the durable row never claimed exactly-once external effects; recorded in
JOB_LIFECYCLE §8 and as ACCEPTED RISK below.

## 18. Cited research (full list in section 2 + these)

Kleppmann 2016/2017 (fencing tokens, Redlock critique — UUID fences explicitly rejected);
etcd/ZooKeeper revision-as-fence; river#1302/#1373 (stale-rescue horizon predicates);
maritime-claims#455 (lease fingerprint revalidation before every flush); shizzle#42 (stage-outcome
writes fenced by lease ownership + remote-effect TOCTOU warnings); running#470 (LeaseBefore CAS
reclaim); postgres-first#10 (stale token must update zero rows — the exact acceptance shape);
Python `os.replace` atomicity docs; atomic-write patterns (temp same-dir + fsync + `os.replace` +
rolling `.bak` backup); SQLite single-writer + conditional-UPDATE optimistic locking; Helland
(idempotency, at-least-once); microservices.io transactional outbox (evaluated §12, rejected).

## 19. T1–T15 (tests/integration/test_execution_fencing.py — 12 tests covering 15 ids)

| T | Assertion of non-breach | Result |
|---|---|---|
| T1 | stale cancel cannot reopen newer execution (mid-run and after terminal) | PASS |
| T2 | stale worker cannot mark newer attempt completed | PASS (combined test) |
| T3 | stale worker cannot mark newer attempt failed | PASS (combined test) |
| T4 | old worker cannot notify success | PASS |
| T5 | two processes cannot both own one execution (sequential + 4-thread race: exactly one winner, `attempt=1`) | PASS |
| T6 | takeover after valid expiry works (token invalidated, generation preserved, new generation mints) | PASS |
| T7 | takeover without valid expiry rejected (fresh lease untouched, steal attempt returns `None`) | PASS |
| T8 | stale publication cannot overwrite current owner's artifact (FS inode guard + fenced journal write) | PASS |
| T9 | re-probe failure preserves previous artifact (queue flow, poisoned re-probe) | PASS |
| T10 | publish failure preserves previous artifact (exploding publish + claim-mismatch + swap-raise; staged retracted) | PASS |
| T11 | crash during publication recovers correctly (windows i/ii/iii) | PASS |
| T12 | completed never reopens (fence + matrix `assert_transition` refusal) | PASS |
| T13 | failed_terminal never reopens | PASS |
| T14 | failed_retryable cannot silently retry (no scheduler: resume ignores it, matrix refuses the edge) | PASS |
| T15 | no success notification without durable completed state | PASS |

## 20. M1–M10 mutations (scripts/gate5_mutation_probes.py — 16/16 caught, exit 1 each)

Protocol per probe: BASELINE GREEN → apply mutant → **RED** (targeted tests flip, exit 1) → byte
restore → SHA match → GREEN. Full transcript: `mutation_run2.txt` (session evidence). M1 remove
ownership predicate; M2 allow stale pending transition; M3 allow duplicate PROCESSING claim; M4
drop rowcount/transition-result check; M5 notify regardless of CAS; M6 remove artifact protection;
M7 break previous-artifact preservation; M8 remove re-probe decision guard; M9 remove fencing from
publication; M10 bypass current-attempt validation — **each killed** (legacy #1–#6 also still
killed: verification removal, typed-failure force-COMPLETED, non-atomic publish, trace job_id drop,
notifier trusting result.success, classification bypass).

## 21. Negative-space scan (where could this fail without the tests noticing?)

| Pattern | Hits | Classification |
|---|---|---|
| `UPDATE nexus_job_queue` | 4 (all `in_process_job_queue.py`: `_reclaim_stale`, `_fence_update`, `_mark_processing`, `_mark_failed` claim-time) | all owned/fenced; no blind `SET status` survives |
| `os.replace`/`write_text`/`write_bytes`/`unlink`/`shutil.copy` in `src/` | 23 files | in-zone: `jobs/feature_verification.py` only (5 verbs, all fenced/journaled). Out-of-zone (named): creative lane (`creative/*`, slideshow), chat screenshots (`bot/handlers.py`, `slideshow_handlers.py`), backups (`maintenance/*`), storage providers (`storage/*`), `tools/files.py`, `personality/engine.py` (snapshot), `api/app.py`, `continuum/snapshot.py`, `cli.py`, `features/image_gen.py`, `ffmpeg_executor.py`, `image_post.py` — each a different subsystem contract, listed here so the claim "owned" stays falsifiable |
| `_notify_completion` call sites | 6 | all gated on `mark_*(…) is True` or post-COMMIT_CONFIRMED fall-through (the one ungated claim-time site found by this very scan was fixed and re-verified) |
| `result.success` reads | `bot/app.py:100` (notifier dual-check — mutation #5 guards), display-only reads in `agents/executor_agent.py`, `bot/feature_handlers.py`, `bot/handlers.py` | notifier truth derives from durable `completion.status` first; display paths are other result dialects (out of queue zone) |
| `resume_pending`/`resume_pending_jobs` callers | `bot/app.py`, `bot/webhook.py`, `cli.py` | startup recovery = lease-gated; CLI = pending-only |
| `_mark_*` callers outside the adapter | none | fence is queue-internal (handlers are pure) |

## 22–24. F7 truth-matrix repair, doc truth, contract reconciliation

* **F7 (fabricated evidence) confirmed and repaired**: `9e9c75358e3e673d788e0c3549896016b11b112a`
  is not a git object (`git cat-file` fails); `feature/verification-gap-closure` is not a ref; the
  "5 runs green" claim had no run ids. Real values (live `gh run list` + `git`): PR#71 head =
  `9e9c753082c5…` with runs `36031844422`/`36031809903`; PR#74 = `05d647e9` with `36040556195`/
  `36040549601`; PR#78 head `5f273f08` with green `36060596005`; `b0304429` with `36057676037`/
  `36056456707`. `GATE5_TRUTH_MATRIX.json` is **rewritten** with only live-verified evidence
  (verification commands recorded per row).
* **Doc truth**: JOB_LIFECYCLE's "never destroys a previous artifact" claim is replaced by the
  proven publication contract (and was false before this repair — R1); "all transitions guarded"
  claims now point at `_fence_update` + T1–T15 + M1–M10; GATE5_CLOSURE_2026-09-24.md carries a
  supersession banner; DECISION_LOG gains D-0016/D-0017.
* **Contract changes (never "changed to wrong behavior to green tests")**:
  1. `_mark_pending/_mark_completed/_mark_failed/_mark_verifying` require an `ExecutionToken`
     (stale/absent identity = refused). Test-side updates: crash-monkeypatch signature.
  2. `PROCESSING→PROCESSING` re-claim retired (matrix edge removed; takeover = lease-expired
     `→PENDING` + fresh reservation).
  3. bare `{"success": False}` (missing/empty `error_code`) is now a typed failure
     `typed_failure:untyped_failure` (fail-closed) — NEW LAW 1 item, was COMPLETED with verifiers
     opted out (R4 OLD-RED). `test_failure_semantics` updated with the decision cited.
  4. `resume_pending` takes over only lease-expired rows (fresh-lease rows untouched); legacy rows
     with NULL `started_at` count as expired (no lease recorded is no lease to respect).
  5. `shutdown` releases only its own live generations (other processes' rows untouched).
  6. `ArtifactPublication` gains `recover`/`retire` verbs; `retract` widened to inode-guarded
     rollback (documented in its docstring + D-0017).

## 25. R1–R5: OLD RED → NEW GREEN (same harness, same assertions)

Baseline `6b86633` (2026-09-24T23:08:54Z):

    R1 VIOLATED previous_artifact_destroyed=True published_now=b'NEW GENERATION' durable=failed_retryable (must keep b'OLD GENERATION')
    R2 VIOLATED completed reopened to pending (durable must stay completed)
    R3 VIOLATED durable=pending announced=['completed'] (announced 'completed' without COMMIT_CONFIRMED durable state)
    R4 VIOLATED bare {success:false} reached COMPLETED (must fail closed)
    R5 VIOLATED dual claim on job 56f3b49fe262465b831400c159b5096d: attempt=2 after two _mark_processing calls (both processes own one execution)

After the repair (same scenarios; takeover expressed as lease-expiry, per contract):

    R1 OK previous artifact preserved after refused publication; durable=failed_retryable
    R2 OK completed stayed completed after stale release
    R3 OK no success notification without durable completed (durable=pending, silent)
    R4 OK bare {success:false} failed closed: failed_terminal result={'success': False}
    R5 OK single execution per job (attempt=1, second claim refused=True)

## 26. Regression (this working tree)

* `pytest -m "not slow"`: **2173 passed, 20 skipped** (after the two hygiene fixes found by the
  suite itself: docs-map indexing + board zone rule).
* targeted suites: `test_execution_fencing` 12/12, `test_gate5_closure` + `test_in_process_job_queue`
  + `test_job_lifecycle_queue` + `test_verification_gap_closure` + `test_job_verification_gaps` +
  `test_job_lifecycle` + `test_failure_semantics` 124/124 in the blast-radius batch.
* `ruff check .` + `ruff format --check .` + `mypy src`: see CI gate (section 27) — identical
  commands run locally before push (recorded in the PR body).
* flake: `tests/unit/test_reminder_system.py` status race fixed test-side (poll durable status with
  the file's own `_sleep_until`); production `features/tools.py` send-then-persist ordering is the
  reminder subsystem's documented delivery contract (out of the job-queue golden chain) — disposition:
  ACCEPTED as-is (test now polls; base-repro flake class cannot fire at this assert again).

## 27. CI forensics + gate

Pre-existing failures classified (base-reproduced): runs `36058609988`/`36058604853`/`36056491410`
(`assert 'pending' == 'sent'`, `test_reminder_system.py:94` — the flake class now repaired
test-side). Own red run `36067414925` (765d9f8): lint/lint-fast `ruff format --check` = the audit
.md's embedded code block (fixed); test step = the same flake class. LOCAL GREEN ≠ CI GREEN ≠
MERGED ≠ POST-MERGE VERIFIED: the CI run on the repair head is the gate; its run id is recorded by
the evidence-only follow-up (board + PR body) which carries no code delta.

## 28. Current-SHA invalidation

All pre-merge mutation/test/audit evidence is pinned to `6b86633` (OLD-RED) or to the repair tree
above; the final gate re-runs the full suite + mutation suite on the exact pushed head and records
new SHAs in the board/PR body (LAW 2).

## 29. Final table (mission §29)

| Finding | Root Cause | Fix | Test | Mutation | CI | Docs | Disposition |
|---|---|---|---|---|---|---|---|
| F1 artifact destruction on refused publication | swap without backup/journal; refusal left refused bytes at final name | publish=back up+journal+swap; retract/recover=inode-guarded restore; retire after commit | T8–T11 + R1 | M6, M7, #3 | section 27 gate | JOB_LIFECYCLE §4–§6, D-0017 | **FIXED** |
| F2 stale `_mark_pending` reopened terminal/newer states | `UPDATE … WHERE id=?` blind | `_fence_update` (id+attempt+owner_token+expected states); self-release fenced; takeover lease-gated | T1, T12/T13 + R2 | M2, M1 | gate | JOB_LIFECYCLE §3, D-0016 | **FIXED** |
| F3 success notification without durable completed state | `_mark_completed`/`_mark_failed` discarded rowcount; notify unconditional | `bool` COMMIT_CONFIRMED returns; all fan-out gated | T4/T15 + R3 | M4, M5 | gate | §11–12 contract, D-0016 | **FIXED** |
| F4/R5 dual-claim ownership bug | reservation accepted `status IN (pending,processing)`; docs-only "single-process" | strict CAS PENDING→PROCESSING (rowcount must be 1); PROCESSING→PROCESSING edge retired | T5–T7 + R5 | M3, M10 | gate | matrix update, D-0016 | **RECLASSIFIED → FIXED** |
| F7 fabricated truth-matrix evidence | unverified inheritance ("5 runs green", fake SHA, dead ref) | matrix rewritten with live `git`/`gh` evidence + verification commands per row | docs tests + live re-read | n/a (evidence artifact) | gate | GATE5_TRUTH_MATRIX.json, section 22 | **FIXED** |
| R4 bare `success=False` completed | dialect check required `error_code`; verifiers `{}` opt-out completed it | `is_typed_user_failure` = `success is False` (untyped → `typed_failure:untyped_failure`, TERMINAL) | R4 + `test_failure_semantics` | #2 | gate | D-0016 | **FIXED** |
| Reminder flake (CI noise) | test read durable status before `_deliver`'s persist | test polls (`_sleep_until`); production delivery order documented | `test_reminder_system` | n/a | gate | section 26 | **ACCEPTED delivery contract / test FIXED** |

## 30. Final architecture (mission §30)

    ENQUEUE (idempotency_key UNIQUE)
        │
        ▼
    PENDING + IDENTITY (id, attempt=0, owner_token=NULL)
        │
        ▼
    RESERVE / CAS PENDING→PROCESSING  ←── rowcount must be 1 (never PROCESSING→PROCESSING)
        │   mints EXECUTION GENERATION: attempt+1 (monotone fence) + owner_token (UUID identity)
        │   lease: started_at + TTL
        ▼
    PROCESSING  (fenced zone: every write carries (id, attempt, owner_token))
        │   handler: pure dict→dict, artifact write = STAGED candidate only
        ▼
    STAGE ARTIFACT (attempt-scoped temp → <name>.staged)
        │
        ▼
    VERIFY (independent re-measure; refusal → retract staged → FAILED_RETRYABLE/TERMINAL)
        │
        ├─ NO ──▶ FAILED_* (fenced _mark_failed → notify(failure) if COMMIT_CONFIRMED)
        │
        YES
        ▼
    PUBLISH  =  BACKUP (hardlink <name>.bak) + JOURNAL (_publication{backup, inode}) + os.replace
        │        ← side-effect fencing point: stale generation matches 0 rows at the journal write
        ▼
    RE-PROBE published bytes ── FAIL ──▶ RETRACT (inode-guarded restore of .bak) → FAILED_* → notify(failure)
        │
        YES
        ▼
    DURABLE COMMIT  (fenced _mark_completed → rowcount>0 = COMMIT_CONFIRMED)
        │           ── refused (stale) ──▶ inode-guarded rollback → SILENT (no notification)
        ▼
    COMPLETED  (terminal: no outgoing edge exists — matrix + fence + T12/T13)
        │
        ├─ RETIRE backup (.bak)
        ▼
    NOTIFY success (exactly once, only after COMMIT_CONFIRMED; delivery = best-effort at-most-once)

    ── crash-recovery points ──
    reclaim: resume_pending takes over ONLY lease-expired PROCESSING|VERIFYING rows
             (owner_token invalidated → stale worker dead on arrival: T6/T7)
    publication recovery: journaled swap + uncommitted → restore pre-swap bytes (inode-guarded);
             backup without journal → duplicate dropped (T11)
    stale-worker rejection points: reservation CAS, every _fence_update, journal write,
             fenced commit, notify gate (T1–T5, T8, T15; M1–M5, M9, M10)

## 31. Final status

**MERGE-READY** (subject to section 27's CI gate on the pushed head: LOCAL GREEN ≠ CI GREEN; the
live run is recorded in the PR body/board evidence follow-up). NOT "MERGED + POST-MERGE VERIFIED" —
this branch is not merged.

## 32. Zero-BS 18-item check (mission §32 — ANY NO ⇒ not MERGE-READY)

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | SHA pinned (old ≠ current evidence) | YES | section 0 |
| 2 | ownership verified before mutation | YES | section 1 (`check` exit 0) |
| 3 | F1 independently reproduced + closed | YES | R1 OLD-RED → NEW-GREEN + T9/T10 |
| 4 | F2 independently reproduced + closed | YES | R2 + T1/T12 |
| 5 | F3 independently reproduced + closed | YES | R3 + T4/T15 |
| 6 | R5 reclassified (docs ≠ proof) | YES | section 6 (REAL BUG) |
| 7 | stale-worker fencing proven | YES | T1–T5 + M1/M2/M3/M10 |
| 8 | side-effect fencing proven | YES | T8 + M9 + section 17 |
| 9 | publication semantics proven | YES | T9–T11 + M6/M7/M8 |
| 10 | previous-artifact preservation proven | YES | T9/T10 + R1 |
| 11 | truth matrix verified (no fabricated evidence) | YES | F7 section 22 (rewrite) |
| 12 | mutation RED evidence exists | YES | section 20 (16/16, exit 1) |
| 13 | targeted tests green | YES | section 19/26 |
| 14 | regression green | YES | section 26 (2173/0 fails) |
| 15 | CI green on CURRENT SHA | PENDING-THE-RUN | section 27: local gates green; live CI run recorded in the PR body/board evidence commit (docs-only). If that run is red the status reverts to NEEDS-REPAIR — no merge claim before it. |
| 16 | docs truthful (no unproven superlatives) | YES | section 22–23 + D-0016/D-0017 + supersession banner |
| 17 | no undispositioned finding | YES | section 29 + ACCEPTED RISKS below |
| 18 | no fabricated evidence | YES | every run/SHA/test claim above was live-read or executed |

## Accepted risks / residuals (with evidence)

1. **Handler external side effects are at-least-once** (RAG ingestion, network sends): a steal
   during handler execution can duplicate them. The durable contract never claimed exactly-once
   external effects; the golden chain (artifact → commit → notify) is exactly-once (T1–T15).
2. **Publication convoy edge**: two steals inside one swap window can drop the *oldest committed*
   backup chain after the second swap (each swap backs up the previous candidate). Bounded to the
   steal window (≤ TTL), inode-guarded restores chain back correctly when they run in order;
   no tested contract is violated (T8–T11 green). Recorded, not hidden.
3. **Case-(iii) orphan `.bak`** (crash after commit, before retire): byte-preserving, retired at
   the next publication of the same artifact (T11 (iii)).
4. **Takeover latency**: a crashed owner's rows wait ≤ `lease_ttl_seconds` (default 300s) before
   recovery — the price of T7 (no steal of live work). Configurable per queue.
5. **Reminder delivery order** (send-then-persist): accepted as the reminder subsystem's delivery
   contract (out of the job-queue golden chain); the flake is closed test-side (section 26).
