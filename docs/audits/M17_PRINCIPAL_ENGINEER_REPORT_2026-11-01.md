# M17 — Principal Engineer Report

**Mission:** `M17-GOVERNANCE-UNBLOCK-P0-EXECUTION-READINESS`
**Repository:** `bot523h/nexus-ai-agent` (public, default branch `main`)
**Session branch:** `arena/f6b34743-nexus-ai-agent` (0 commits ahead / 0 behind `origin/main`)
**Report timestamp:** `2026-11-01T04:58:28Z`
**Head of `main` at time of investigation:** `d52a2434150d4e3a422d71b5f42b354d24bf61eb`
**Final status:** `VERIFIED_WITH_LIMITATIONS` (forensic mission). *The P0 implementation remains unstarted by design.*

---

## A. Executive Finding

The mission premise does not match live reality in **three** load-bearing ways. All three are now
resolved and none of them is a permission problem.

1. **PR #300 was already merged before this session began.** Its state is `MERGED`, merged at
   `2026-11-01T04:36:22Z` (commit `d52a2434…`), 15 minutes before this investigation started. The
   mission therefore cannot "avoid merging PR #300" — that transition is historical fact. Its merge
   brought `1cbe723b8fc957689513d9da83ae6200f41c4381` into `main`, so **the workspace is currently on
   a branch identical to `main`, and `main` already contains the two task registrations.**

2. **The governance blocker is gone.** Both `task-nagar-workspace-redelivery-01` and
   `task-nagar-circleci-pilot-01` are present in the canonical `.agents/board.json` **on `main`**, as
   `queued` claims **and** as registered `zones[]` entries, with `acceptance_criteria` (5 / 4) and
   `evidence_required` (3 / 2) populated. `scripts/check_board_integrity.py` exits **0**. The task is
   claimable today; it has simply never been claimed (`claimed_at: null`, `generation: 0`).

3. **The CircleCI red status is real, permanent, and irrelevant to merge gating.** Every commit
   CircleCI is handed produces `CircleCI Pipeline` = `error`. The cause is proven: **`.circleci/` has
   never existed in this repository — in any commit, on any ref, in any tag** (verified across 1136
   commits of full, unshallowed history). The repository has deliberately cut CircleCI (recorded in
   `docs/DECISION_LOG.md:303`) and runs GitHub Actions. The project has also pre-ruled that
   CircleCI was never a required check. **PR #300 merged with the red status present**, which is the
   strongest available proof that it is not a merge gate.

The actual blocker to P0 is therefore **not governance and not CI**. It is that **nobody has claimed
the task**, and the P0 defect itself is materially worse than the mission brief assumed: the surface
destroys and replaces the *input bytes of an already-admitted durable job*, which the queue's own
conflict protection is structurally unable to detect.

---

## B. Exact Live Baseline

> Note: the sandbox checkout arrived as a **grafted shallow clone** (`rev-list --count HEAD` = 1).
> Full history was restored with a read-only `git fetch --unshallow origin` (1136 commits). No branch
> was changed, nothing was committed or pushed.

| Item | Observed value | Evidence |
|---|---|---|
| `origin/main` SHA | `d52a2434150d4e3a422d71b5f42b354d24bf61eb` | `git rev-parse origin/main` |
| Session branch HEAD | `d52a2434150d4e3a422d71b5f42b354d24bf61eb` | `git rev-parse HEAD` |
| Branch ↔ main divergence | `0  0` (identical) | `git rev-list --left-right --count origin/main...HEAD` |
| PR #300 state | **`MERGED`** | GitHub API `gh pr view 300` |
| PR #300 merge commit | `d52a2434150d4e3a422d71b5f42b354d24bf61eb` | PR API `mergeCommit.oid` |
| PR #300 head SHA | `1cbe723b8fc957689513d9da83ae6200f41c4381` | PR API `headRefOid` |
| PR #300 base ref | `main` (base at open: `48aae28a4ef4a00459530bb177ce4fe4e43c0ad3`) | PR body + API |
| PR #300 created | `2026-10-31T21:43:39Z` | PR API |
| PR #300 merged | `2026-11-01T04:36:22Z` | PR API |
| PR #300 changed files | **1** — `.agents/board.json` (+78 / −0) | `gh pr diff 300 --name-only`, `git show --stat d52a243` |
| PR #300 mergeability | `UNKNOWN` (terminal state; meaningless once merged) | PR API |
| PR #300 review decision | *(empty)* — no human approval; Sourcery `COMMENTED`, CodeRabbit `SKIPPED` | PR API `reviews`, `comments` |
| Worktree | **clean** | `git status --porcelain` → empty |
| Task on `main` (redelivery) | **present**, claim idx 173 + zone idx 77 | `grep -n task-nagar .agents/board.json` |
| Task on `main` (circleci) | **present**, claim idx 174 + zone idx 78 | idem |
| `.circleci/` on any ref | **absent — never existed** | `git log --all -- .circleci` → empty |
| `.agents/board.json` integrity | **OK**, 604 532 bytes, schema 2 | `python3 scripts/check_board_integrity.py` → exit 0 |

**PR #300 remained strictly governance-only.** Exactly one file, `.agents/board.json`, +78 lines. No
unrelated file, no source change, no CI change. The mission's scope-integrity question is answered
affirmatively: **`LIVE_CONFIRMED`**.

---

## C. PR #300 Forensics

**Diff scope:** 78 added lines appended to `.agents/board.json` — two `zones[]` entries
(`nagar-workspace-redelivery-01`, `nagar-circleci-pilot-01`) and two `claims[]` entries in state
`queued`, `generation: 0`, `agent_branch: ""`, `claimed_at: null`, `prerequisites: []`. No active
lease was fabricated. This matches the PR body's claim exactly.

**Checks on the exact PR head `1cbe723`:**

- **GitHub Actions — run `41244937169`** (`.github/workflows/ci.yml`, event `pull_request`,
  `head_sha = 1cbe723…`, `run_attempt 1`): **`conclusion: success`**, created
  `2026-10-31T21:43:50Z`, updated `2026-10-31T22:18:30Z`. **22/22 jobs `success`, 0 failures.**
  Full job list includes `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`,
  `merge-base-guard (base == main)`, `python-parity (3.10/3.11/3.12)`, `continuum-evidence (×3)`,
  `migrate-postgres`, `extras-matrix (core/pdf/speech/translate)`, and five mutation-campaign jobs
  (`governance`, `execution-core`, `api-gate`, `remote-key`, `trust`, `temporal`).
  Evidence: `LIVE_CONFIRMED`.
- **Commit combined status: `failure`** — total 1 status, the sole status being
  `CircleCI Pipeline`. Evidence: `LIVE_CONFIRMED`.
- **Non-success check-runs on that SHA:** 3 × `continuum-evidence` with `conclusion: null` /
  `completed_at: null` (still-in-progress duplicates from the cancelled first attempt); plus one
  cancelled `CodeRabbit` (`skipped`), and a set of `cancelled` duplicate jobs from the early
  superseded attempt at `21:43:43Z`. **No job failed.**
- **Reviews:** no approving review. `reviewDecision` empty. Sourcery `COMMENTED`; CodeRabbit
  `SKIPPED`. Issue comments: two bot review-guide comments only.
- **Mergeability:** `UNKNOWN` / `mergeStateStatus: UNKNOWN` — a terminal post-merge artefact.

**Conclusion:** PR #300 was merged as a governance-only board append, on a passing 22/22 GitHub
Actions run, with **zero** required approvals, **while carrying a red CircleCI status**.

---

## D. CircleCI Root Cause

### D.1 Configuration discovery (decisive)

| Query | Exact ref | Result |
|---|---|---|
| `git log --all --oneline -- .circleci` | all 1136 commits, all refs/tags | **empty** |
| `git ls-tree -r --name-only origin/main \| grep -i circle` | `origin/main` `d52a243` | **empty** |
| `.circleci/` directory on disk | worktree | **absent** |
| Alternative config paths on `main` | `origin/main` | only `.github/workflows/{ci,maintenance,langsmith-smoke,queue-ownership-mutations}.yml` |

`.circleci/config.yml` **has never existed in this repository's history.** Evidence:
`SOURCE_CONFIRMED` + `LIVE_CONFIRMED`.

`docs/DECISION_LOG.md:303` records the deliberate decision:

> `circleci-project-setup` | Irrelevant — CI platform cut | Only adds `.circleci/config.yml`
> (commits `265d6a0`, `2818d9d`). `.circleci/` does not exist on `main`; the project standardizes on
> GitHub Actions (`.github/workflows/ci.yml`, `maintenance.yml`).

Independently verified: `265d6a0` and `2818d9d` are **not present in the repository**
(`fatal: ambiguous argument`); they were never merged. The claim "`.circleci/` does not exist on
`main`" is `LIVE_CONFIRMED`.

### D.2 Status context — exact values

| Field | Value |
|---|---|
| Context | `CircleCI Pipeline` (the **only** status on the SHA) |
| State | `error` |
| PR #300 head (`1cbe723`) | target `https://app.circleci.com/pipelines/gh/bot523h/nexus-ai-agent/9`, created `2026-10-31T21:43:37Z` |
| Merge commit (`d52a243`) | target `https://app.circleci.com/pipelines/gh/bot523h/nexus-ai-agent/15`, created `2026-11-01T04:36:30Z` |
| Predecessor `48aae28` (PR #299 merge) | **no CircleCI status at all** |

**Interpretation.** Three data points, three different outcomes on consecutive commits:
PR #299's merge → *no status*; PR #300's head → pipeline **9** `error`; PR #300's merge commit →
pipeline **15** `error`. The pipeline counter advanced past 9 to 15 while `.circleci/` was never
added, and the same `error` (not `failure`) is emitted ~7 s after each commit. `error` — as distinct
from `failure` — is CircleCI's signal for *the pipeline could not be created/configured*, not *a job
ran and failed*. Combined with the total absence of any configuration file on every ref, the
classification is:

> **The failing stage is Phase C step 3/4 in the mission's own taxonomy — configuration discovery /
> parsing — and it fails at the root: there is no configuration to discover.** Confidence: **HIGH**
> for the config-absence finding (`SOURCE_CONFIRMED` + `LIVE_CONFIRMED`); **HIGH** for the
> error-class inference (`LIVE_CONFIRMED` on state/URL/timing, modelled on CircleCI's documented
> `error` semantics).

### D.3 What could NOT be verified — and why

**CircleCI is unreachable from this sandbox and no CircleCI credential exists.** Every host returned
`HTTP:000` while `api.github.com` returned `HTTP:200` as control:

```
circleci.com        HTTP:000
app.circleci.com    HTTP:000
api.circleci.com    HTTP:000
api.github.com      HTTP:200      <- control
```

No `CIRCLECI*` environment variable is present. Pipeline 9's failure log, its `config_source`, its
trigger source, the project's plan/credit state, and the VCS-integration health are therefore
**`PERMISSION_BLOCKED`** (access-blocked, not permission-denied). See §M.

Notably, this does **not** weaken the conclusion: the config-absence fact is provable entirely from
git and needs no CircleCI API. The API would only confirm *which* of the two consequences fired
(zero-config project vs. broken VCS binding), and **both have the same remediation**.

---

## E. Required-Check Decision

**Verdict: CircleCI is NOT a required merge check. `VERIFIED` for "optional"; `PERMISSION_BLOCKED`
for the live branch-protection read, which does not change the verdict.**

Four independent lines of evidence, strongest last:

1. **Behavioural proof (strongest).** PR #300 was **merged** at `2026-11-01T04:36:22Z` while its head
   SHA carried `state: failure` from `CircleCI Pipeline`. A required-and-red context cannot be
   merged past. Therefore `CircleCI Pipeline` was not required on 2026-11-01. `LIVE_CONFIRMED`.
2. **Documented policy, recorded in-repo.** `CURRENT_TRUTH_AUDIT.md` §0 (correction dated
   2026-10-27, explicitly superseding the earlier body) records the live read of
   `branches/main/protection`:
   ```
   contexts = ["lint (ruff + mypy + version lockstep)",
               "test (pytest -m \"not slow\")",
               "merge-base-guard (base == main)"]
   reviews  = null            # 0 required approvals
   ```
   CircleCI does not appear. `SOURCE_CONFIRMED` (documented live read, not re-read live — see 3).
3. **All three documented required contexts passed on the exact PR head `1cbe723`:**
   `lint (ruff + mypy + version lockstep)` → `success`; `test (pytest -m "not slow")` → `success`;
   `merge-base-guard (base == main)` → `success`. `LIVE_CONFIRMED`.
4. **Rulesets are empty.** `gh api repos/bot523h/nexus-ai-agent/rulesets` → `[]`;
   `?includes_parents=true` → `[]`. There is no ruleset layer to impose a required check.
   `LIVE_CONFIRMED`.

**Permission boundary (recorded exactly as required):**

```
GET /repos/bot523h/nexus-ai-agent/branches/main/protection
→ HTTP 403  {"message":"Resource not accessible by integration",
             "documentation_url":"https://docs.github.com/rest/branches/branch-protection#get-branch-protection"}
GET /repos/bot523h/nexus-ai-agent/rulesets          → 200  []
GraphQL repository.branchProtectionRules            → FORBIDDEN, "Resource not accessible by integration"
```

The token reports `permissions: {admin:true, maintain:true, push:true, pull:true, triage:true}` on the
repository object yet is refused the *protection* endpoint — i.e. this is a **GitHub App /
installation-token** limitation, not an absence of repository role. The same 403 is exactly why
`scripts/pr_convergence.py` treats an unreadable protection as a hard failure (exit 2) rather than
"no gate". **No policy was altered and no privileged identity was substituted.**

---

## F. Governance Findings

### F.1 Canonicality — resolved

The two tasks **are** canonically registered on `main`:

| Task | Status | Zone registered | `exclusive_paths` == zone `paths` | AC | Evidence | Prereqs | Lease |
|---|---|---|---|---|---|---|---|
| `task-nagar-workspace-redelivery-01` | `queued` | ✅ idx 77 | ✅ identical | 5 | 3 | `[]` | `generation 0`, `claimed_at null`, `agent_branch ""` |
| `task-nagar-circleci-pilot-01` | `queued` | ✅ idx 78 | ✅ identical | 4 | 2 | `[]` | `generation 0`, `claimed_at null`, `agent_branch ""` |

Both satisfy the AGENTS.md rules that a claimable task carries `acceptance_criteria` **and**
`evidence_required` (the two properties `tests/unit/test_agent_board.py` enforces). Neither is an
orphan claim; neither has a dangling zone. `check_board_integrity.py` → exit 0.

The mission's warning *"do not mistake branch presence for canonical registration"* no longer
applies: registration is on `main` itself, at `d52a243`.

### F.2 Structural soundness of the proposed definitions

- **Redelivery task.** `exclusive_paths` are three exact file paths, two of which exist
  (`creative_surface.py`, `test_creative_surface.py`) and one of which **does not**
  (`tests/integration/test_creative_surface_redelivery.py`). This is **correct and necessary**: a
  test file that must be *created* requires a fence in advance, otherwise the creating agent has no
  ownership of the file it creates. **File absence ≠ ownership absence** — the path is fenced even
  though the file is absent. `VERIFIED`.
- **CircleCI task.** `exclusive_paths` are `.circleci/config.yml` and
  `docs/engineering/NAGAR_CIRCLECI_PILOT.md`. Both files are absent; the directory
  `docs/engineering/` is also absent. Fencing non-existent paths in a non-existent directory is
  legal under the schema (`_path_matches` is pure string comparison) and correct for the same reason.
  `VERIFIED`.

### F.3 The one real ownership finding — a *prospective* competing fence

`pr-repair-69` (claim for open PR #69) lists in its `exclusive_paths` **both**
`src/nexus_ai_agent/bot/creative_surface.py` **and** `tests/unit/test_creative_surface.py` — i.e.
**2 of the 3 P0 exclusive paths**.

But it is **not** a live conflict:

- `status: queued`, `claimed_at: null`, `agent_branch: ""`, `generation: 0`.
- `_claim_live()` returns **False** because `claimed_at is None` (`scripts/agent_board.py:222-229`).
- Its own `note` reads: *"Registration only. Existing records and leases remain unchanged.
  Overlapping queued entries must never be claimed concurrently."*

**Consequence:** a P0 claim taken **today** is conflict-free (`check` exit 0, verified both
`--no-remote` and full remote with **45 sources read**). A `pr-repair-69` claim taken later would
**not** be — the two would then genuinely collide. This is a real, disclosed, ordered risk, not a
current blocker.

### F.4 Why the referee is silent about PRs #66 / #69 touching P0 files

`praudit` on the canonical board (43 open PRs) reports **neither #66 nor #69 as a `foreign_fence`**
against the new zone, despite each modifying `creative_surface.py` and `test_creative_surface.py`:

```
#69  own_claims: []   foreign_fences: []   classification: BLOCKED  invisible: True
     reasons: ['no live board claim for head branch', '98 file(s) outside every exclusive_paths']
#66  own_claims: []   foreign_fences: []   classification: BLOCKED  invisible: True
     reasons: ['no live board claim for head branch', '37 file(s) outside every exclusive_paths']
```

The mechan­ism is `_conflicting_paths()` (`scripts/agent_board.py:275-290`): it only iterates claims
where `_claim_live()` is true. PR #69's board record is `queued` with no lease → not live → skipped.
Separately, the new zone cannot report `foreign_fences` because its own claim is `queued` and it has
no branch to compare against. **The referee is behaving correctly; it is answering a narrower
question than "which open PR touches this file".** Both PRs are nevertheless flagged
`BLOCKED/invisible` for the unrelated reason that their head branch has no live board claim.

**Independent check — actual merge risk is low-to-nil.** The diffs of both PRs are anchored to
**stale ancestral blobs**: PR #69's `creative_surface.py` hunk header is
`index 118f946c..40f8ff7e` and PR #66's is `index 02c8e978..e8be5bb8`, whereas the current `main`
blob is `e3e8aff8`. Neither `118f946c` nor `02c8e978` is present in the fetched object graph. Merge
bases are ancient: **PR #66 is 777 commits behind `main`, PR #69 is 768 behind.** A P0 implementation
must not rebase onto either; doing so would replay 700+ commits of drift. `LIVE_CONFIRMED`.

### F.5 No claim was taken

Per instruction 8 and 9, **no claim was executed and no referee was used as a claim**. `claim` is
mutating (`save_board` + `git push`), so it was not run even speculatively. The exact official
command, confirmed from the script's own `--help` and code path, is deferred to §N.

---

## G. P0 Code Trace

All line numbers are on `main @ d52a2434150d4e3a422d71b5f42b354d24bf61eb`.
Primary file: `src/nexus_ai_agent/bot/creative_surface.py` (394 lines).

### G.1 Call graph — actual, with authority and side effects

| # | Transition | Location | Input | Returned / produced | Side effect | Authority | Error semantics |
|---|---|---|---|---|---|---|---|
| 1 | Telegram `Update` arrives | PTB, outside this module | `Update` | — | none | PTB dispatcher | — |
| 2 | Command → handler | `build_creative_handlers` `383-394`; map `390-394` | `command` | `handler` | none | surface | map is **exactly** `edit`/`caption`/`grade` |
| 3 | Enter dispatch | `_dispatch` `272` | `update`, `context`, `command` | — | none | surface | returns silently if `message is None` (`274-275`) |
| 4 | Message identity | `272-279` (`message`, `edited_message` fallback at `245`/`273`) | `update` | `message`, `user_id`, `chat_id`, `message_id` | none | **PTB / Telegram** | ids default to `0` if the object lacks `.id` |
| 5 | Request mapping | `306-318` → `CreativeSurfaceMapper.map` `116-142` | `CreativeRequest` | `CreativeRequest` or `CreativeFailure` | none | **surface** (pure) | typed failure → i18n reply → `return`; **no queue contact** |
| 6 | **Idempotency key** | `idempotency_key()` `204-218`, called `320` | `user_id`, `chat_id`, `message_id` | `creative:{u}:{c}:{m}` | none | **surface** (sole minter) | `message_id is None` → `creative:{u}:{c}:ad-hoc:{uuid4}` (**`213-216`**) |
| 7 | Execution trace mint | `execution_trace()` `188-201`, called `323-325` | same triple | `ExecutionTrace` | none | surface; **worker re-derives and verifies**, never mints | identity derived from the same key |
| 8 | **Workspace name** | `_workspace_for()` `221-227`, called `335` | `key`, `creative_temp_dir` | `{root}/creative_{sha256(key)[:12]}` | none | surface | **collision-free by construction** — the name is a function of the key alone |
| 9 | **DESTRUCTIVE DELETE** | **`339`** `shutil.rmtree(workspace, ignore_errors=True)` | `workspace` | — | **removes the entire previous workspace for this key, including `input.*`** | **surface** | `ignore_errors=True` — *silent* |
| 10 | Workspace create | `340` `workspace.mkdir(parents=True, exist_ok=True)` | `workspace` | — | creates dir | surface | propagates (caught at `342`) |
| 11 | Media acquisition / download | `_stage_media()` `261-270`; `bot.get_file` `266`; `download_to_drive` `269` | `media_file_id` | `str(target)` | **writes `input{suffix}` directly into the live workspace path** | **surface / Telegram** | exception → `342-346`; `None` → `347-349` |
| 12 | **Nested destructive delete** | **`344`** `shutil.rmtree(workspace, ignore_errors=True)` inside `except` | `workspace` | — | **removes the workspace it just failed to populate** | surface | swallows both the original error and any cleanup error |
| 13 | Payload freeze | `job_payload()` `144-177`, called `353-362` | mapped req + `workspace_dir` + `input_path` + `trace_id` | `dict` | none | surface | payload carries **no media digest** — only `media_duration_us` |
| 14 | **Queue admission** | `364-368` `job_queue.enqueue(...)` | `job_type="creative_render"`, `key`, `payload` | `job_id` | **durable SQLite row** | **JobQueuePort (authoritative)** | exception → `369-372` i18n reply → `return`, **workspace left behind** |
| 15 | Duplicate resolution | `in_process_job_queue.enqueue` `845-927` → `_insert_or_get` `2442-2569` | `job_type`, `key`, frozen `payload_json` | `_EnqueueOutcome` | `INSERT … ON CONFLICT(idempotency_key) DO NOTHING` (`2464-2483`) | **queue** | conflict → `CreativeRequestConflictError` **only** when `job_type` is `creative_render` (`2536-2539`, `922-925`) |
| 16 | Payload-fingerprint compare | `2459`, `2511-2534`; `request_identity` in `jobs/creative_passport.py:146-162` | payload dict | `request_fingerprint` | none | queue | compares **payload fields only** — never file bytes |
| 17 | Worker claim / fencing | `in_process_job_queue` claim path; `attempt_id()` `creative_passport.py:165-169` | `job_id`, `attempt` | attempt id | row → in-flight | **queue (single owner)** | stale generation fenced out |
| 18 | Trim-source preflight | `1650-1700`; `capture_trim_source` `creative_passport.py:186+` | payload | `FileSnapshot` \| `None` | none | **worker / queue** | `None` → `FAILED_TERMINAL` `creative_passport_source_unavailable:missing_snapshot`; typed render error preserved |
| 19 | Workspace guard | `_guarded_workspace()` `render_jobs.py:203-215` | `workspace_dir` | `Path` | none | worker | refuses outside `creative_temp_dir`; refuses name lacking `creative_` prefix |
| 20 | Input guard | `_guarded_input()` `render_jobs.py:218-231` | payload, workspace | `Path` | none | worker | `input_path is None` → `media_missing`; **escapes workspace** → `invalid_request`; **`not is_file()` → `media_missing`** |
| 21 | **Bytes read at render time** | `render_jobs.py:704` `sha256_file(input_path)`; `699` `probe_video(input_path)`; `724` `media_paths = {SOURCE_ASSET_ID: str(input_path)}` | path | digest, probe, lane input map | none | **worker** | reads **whatever bytes are at the path now** |
| 22 | Execution → verification → publication | `render_jobs.py:1106-1315`; `AttemptStaging` `execution/staging.py`; `_creative_render_staging` `964-988` | result | artifact + passport | staging tree per `(job_id, attempt_id)` | **queue-owned** | ownership sidecar; fails closed without provable ownership |
| 23 | Completion notify + **cleanup** | `bot/app.py:139` `cleanup_workspace({**payload, **result})` in `finally`; `render_jobs.py:921-929` `shutil.rmtree(directory)` | merged record | — | **deletes `workspace_dir`** | **notifier** | best-effort, never raises |

**Two independent destructive authorities.** Transition 9 (surface, *pre-admission*) and transition
23 (notifier, *post-publication*) both `rmtree` a path derived from the same key. Nothing in the
current code arbitrates between them.

### G.2 Critical investigations — A: idempotency identity

Key = `creative:{user_id}:{chat_id}:{message_id}`, minted only in `idempotency_key()`.

- **Includes user + chat + message identity:** yes, all three.
- **Synthetic message without an ID:** `213-216` falls back to
  `creative:{u}:{c}:ad-hoc:{uuid4().hex}` — a **fresh random key every call**. Correct for real
  traffic (nothing to dedupe against) but it means any test or integration that constructs a
  message without `message_id` silently loses all duplicate protection, and every delivery creates a
  distinct workspace. *Untested behaviour; not a production bug.*
- **Edited message:** **this is the first defect.** The handler reads
  `getattr(update, "message", None) or getattr(update, "edited_message", None)` (`273`), so an
  `edited_message` **is processed**, and Telegram preserves `message_id` across edits. An edited
  creative command therefore reuses the **identical** key → **identical workspace** → step 9 wipes
  the input of a job admitted from the pre-edit content. Two distinct sub-cases: (i) the media is
  unchanged and only the operation changed → *changed payload*, refused loudly by `2536-2539`
  (safe, but only by accident of `job_type`); (ii) the replied media changed → *identical payload*
  (same `workspace_dir`, same `input_path`, same `args`) → **silently accepted as a duplicate while
  different bytes are rendered.**
- **Different commands colliding:** no. Two commands from one user are two messages, hence two keys
  (pinned by `test_distinct_messages_map_to_distinct_keys`).
- **File identity / content in the key:** **no.** `media_file_id` is *not* in the key. `file_id` is
  a stable Telegram handle, and a *replaced* reply would carry a different `file_id` — but nothing
  compares it, and `file_id` is not in the payload either. **No content identity exists anywhere in
  the admission path.**

### G.3 Critical investigations — B: duplicate resolution

`_insert_or_get()` (`2442-2569`), inside one `self._db_lock` + `with self._connection() as
connection` — i.e. a **single atomic transaction**:

- Checks uniqueness **before** deciding: no — it *attempts* the insert and reads `cursor.rowcount`
  (`2464-2484`). This is correct: the `UNIQUE` constraint is the arbiter, not a read-then-write.
- Returns the **existing** job id on duplicate (`2561-2569`, `job_id=existing_id, created=False`).
- **Compares payload vs incoming payload:** yes — via `build_request_identity(...)` fingerprints
  (`2459`, `2511-2534`).
- **Rejects conflicting payloads:** yes, but **only** when `job_type == "creative_render"` or the
  existing row is `creative_render` (`2536-2539`), raising `CreativeRequestConflictError` (`922-925`)
  *after* recording a `JOB_ENQUEUE_DUPLICATE` causal event with `resolution: "creative_request_rejected"`.
  Other job types retain first-payload-wins (`2546-2552`).
- **Silently retains original:** for non-creative types, yes.
- **Behaviour across restart:** durable — the SQLite row survives; identity is recomputed from the
  persisted payload and cross-checked against the persisted `request_id`/`request_fingerprint`/
  `transaction_id`, and any mismatch raises
  `RuntimeError("existing request identity is corrupt…")` (`2514-2529`). Fail-closed. Good.

> **The decisive structural point.** The fingerprint is computed over the *payload dict*
> (`creative_passport.py:150-152`), and the payload carries `workspace_dir` + `input_path` + `args`
> + metadata — **but never the bytes**. Therefore a redelivery that replaces the *file contents*
> produces a **byte-identical payload**, so `request_fingerprint` matches, `conflict` is `False`, and
> no `CreativeRequestConflictError` is raised. **A `UNIQUE` idempotency index plus a payload
> fingerprint does not, and cannot, provide payload *consistency* — it provides payload *equality
> modulo content*.** This is exactly the trap the mission brief warned about, and it is live.

### G.4 Critical investigations — C: workspace lifecycle

- **Exact destructive `rmtree`:** line **339** (pre-admission, unconditional) and line **344**
  (inside the download `except`), plus the independent post-publication `rmtree` at
  `render_jobs.py:927` via `bot/app.py:139`.
- **Before queue admission?** **Yes.** `339` runs before `364`. This is the ordering defect.
- **Has duplicate detection already happened?** **No.** The queue has not been consulted at all —
  which is precisely why the queue's protection is inoperative.
- **What the existing path contains:** for an active job, the live `input{suffix}` that the worker
  will read at `render_jobs.py:704/724`. Deleting it is deleting the job's only input.
- **Shared across attempts?** The **workspace** is shared across every delivery of the same message
  identity; the **staging tree** is isolated per `(job_id, attempt_id)` (`964-988`). Fencing protects
  publication, not input bytes.
- **Can the input be replaced by a concurrent delivery?** **Yes** — `_workspace_for` is a pure
  function of the key, so two concurrent deliveries for one message compute the *same* path. This is
  a genuine TOCTOU window (`339` → `341` → `364`).
- **Does the worker read the path after enqueue?** **Yes, and only then** — `input_path` is
  dereferenced at render time, long after admission.

### G.5 Critical investigations — D: download lifecycle

`_stage_media()` `261-270`:

- `bot.get_file(req.media_file_id)` (`266`) — network call, no timeout/retry policy here.
- Suffix from `tg_file.file_path` (`267`), defaulting to `.bin`.
- `target = workspace / f"input{suffix}"` (`268`) — **derived from the file extension**, so a
  changed media type changes the filename and leaves the previous file in place; a same-type
  replacement overwrites in place.
- `await tg_file.download_to_drive(target)` (`269`) — PTB writes **directly to the final path**.
  There is **no temp-then-rename**, so a partial write is a *truncated file at the live path*.
- **Partial write:** the truncated bytes are visible at `input{suffix}` between the moment PTB opens
  the file and the moment it finishes. Concurrently, a worker could read them.
- **Exception behaviour:** propagates to `342`; `343` logs `creative_media_stage_failed`;
  `344` **wipes the whole workspace**, deleting the previous *valid* input; `345` replies with the
  typed, translated `creative.media_download_failed`; `346` returns.
- **Existing-target overwrite:** unconditional (PTB truncates).
- **Incomplete-download cleanup:** the cleanup at `344` is **over-broad** — it is scoped to the
  workspace, not to the partial file.
- **Symlinks:** not checked anywhere on the surface side. `Path` operations follow symlinks.
  (Worker-side `_guarded_input` calls `.resolve(strict=False)` and `relative_to(workspace)`, which
  would catch an escaped input — but only at render time, after the write has already happened
  outside the root.)

> **An interrupted download can and does destroy an existing valid input.** Proven in §H.

### G.6 Critical investigations — E: queue and worker lifecycle (PR #299 guarantees — intact)

Verified present and untouched:

- **Single process owner**; startup recovery ordering with a publication barrier.
- **Fencing token** = monotonic `claim.attempt`; `attempt_id(job_id, attempt)` is a stable content id
  (`creative_passport.py:165-169`), and the queue threads `_queue_job_id` / `_queue_attempt` into
  every publication callback so ownership evidence names **job + attempt**, not an attempt number
  alone (`render_jobs.py:943-949`).
- **Heartbeat / staleness**: `_claim_live`-style lease logic in the queue; stale generation refused.
- **Attempt-isolated artifact publication** via `AttemptStaging` under
  `<artifact_root>/staging`, publishing into `<artifact_root>/published`; ownership sidecar
  (`_write_sidecar` / `_read_sidecar`, `1030-1105`) makes mutation of published output fail closed
  when ownership cannot be proved.
- **Terminal states**: `PENDING` / in-flight / `COMPLETED` / `FAILED_TERMINAL`; failure semantics
  typed via `jobs/failure_semantics.py`.
- **Cleanup**: `cleanup_workspace` (`921-929`) is *post-notification* and best-effort.

**Nothing above needs rebuilding, and the P0 fix must not touch it.**

---

## H. Root Cause

### H.1 The unsafe ordering

```
_dispatch()
  320  key = "creative:{u}:{c}:{m}"            <- identity fixed here
  335  workspace = creative_{sha256(key)[:12]}  <- ONE path per identity, forever
  339  rmtree(workspace)                        <- DESTROYS ADMITTED JOB'S INPUT
  340  mkdir(workspace)
  341  download -> workspace/input.ext          <- writes live path directly
  344  rmtree(workspace)  (on ANY download error) <- DESTROYS IT AGAIN
  353  payload = {... workspace_dir, input_path ...}   <- no content digest
  364  job_queue.enqueue(key, payload)           <- admission happens LAST
```

The surface is performing **mutation of durable-job-owned state before the authority that owns
durable jobs has been consulted.** Authority order is inverted: `JobQueuePort` is authoritative for
job identity and lifecycle, yet the filesystem — which is part of the job's input contract — is
mutated first, unilaterally, by the surface.

### H.2 The failure mechanism, in three variants

**Variant 1 — silent content substitution (integrity).** A redelivery or edit of message *m* with
different media bytes executes `339` (delete input A), `341` (write input B), then `364`. The payload
is byte-identical to the original, so `INSERT … ON CONFLICT DO NOTHING` fires, `request_fingerprint`
matches, `conflict = False`, and `enqueue` returns the **original** `job_id` as
`existing_request_reused`. The user is told "Queued — job `79197…`" *the same job id, twice*. The
worker then renders **media B** under a job that was admitted with **media A**, and the passport
records a digest of B against A's request identity. No error is raised at any layer.

**Variant 2 — input destruction / data loss (availability).** A redelivery whose download fails
mid-transfer truncates then aborts: `342` fires, `344` wipes the workspace, `346` returns. The job
stays `pending`, its payload still points at `…/input.mp4`, and that file **no longer exists**. When
the worker claims it, `_guarded_input` (`render_jobs.py:229-230`) raises
`CreativeRenderError("media_missing")` → the job is terminally failed. The user was told the download
failed *this time*; the job they were previously told was queued is now unrecoverable and they were
never told.

**Variant 3 — the queue's protection is structurally inapplicable.** `CreativeRequestConflictError`
protects against *changed payload fields*. It cannot protect against *changed file contents* because
`request_identity()` (`creative_passport.py:150-152`) hashes the payload dict, and the payload dict
contains no media identity. The guard is real, tested, and **orthogonal to the actual attack surface.**

### H.3 Empirical confirmation

All three variants were **reproduced against `main @ d52a243`** using the real
`InProcessJobQueue` + real SQLite + real filesystem. Scratch scripts lived in `/tmp` and were **not**
added to the repository.

**Variant 1 (real durable queue, `EXECUTED_TEST_CONFIRMED`):**
```
delivery 1 reply: ['⏳ Queued /edit trim — job 7919785f5e064903a5fe0edce4cf4658']
admitted row id      : 7919785f5e064903a5fe0edce4cf4658
admitted status      : pending
admitted input_path  : /tmp/scratch_rq/creative/creative_2d2631d18def/input.mp4
admitted sha256      : 1c3fa498…  (bytes=b'MEDIA-A-ORIGINAL-MED'...)

delivery 2 reply: ['⏳ Queued /edit trim — job 7919785f5e064903a5fe0edce4cf4658']   <-- SAME job id
input file sha256 now: c52cc627…  (bytes=b'MEDIA-B-REPLACEMENT-'...)
queue rows for key   : 1

queue refused the changed request (CreativeRequestConflictError): False
admitted input bytes changed underneath the durable row: True
```

**Variant 2 (data loss, `EXECUTED_TEST_CONFIRMED`):**
```
admitted job id     : aa28a2eb30df444384322bb1b09b6eb4
admitted status     : pending
admitted input      : /tmp/scratch_rq3/creative/creative_2d2631d18def/input.mp4
input exists        : True

delivery 2 replies  : ['❌ Downloading the media from Telegram failed. Please try again.']

--- after the failed redelivery ---
input exists        : False
workspace exists    : False
job status (durable): pending  <-- still admitted, still aims at that path
```

**Variant 1, isolated (surface only, `EXECUTED_TEST_CONFIRMED`):** the admitted job's `input_path`
string is unchanged while its content flips from `ORIGINAL-MEDIA-BYTES…` (sha256 `f5879a57…`) to
`REPLACED-MEDIA-BYTES…` (sha256 `46b3c879…`).

**Symlink escape (`EXECUTED_TEST_CONFIRMED`):** with a directory symlink planted at the
deterministic workspace path — no elevated privilege required, since the path is derivable from the
public key formula — `download_to_drive` wrote **outside** `creative_temp_dir`:
```
planted symlink: /tmp/scratch_sym/creative/creative_2d2631d18def -> /tmp/scratch_OUTSIDE
reply: ['⏳ Queued /edit trim — job j1']
files written OUTSIDE the creative temp root: ['input.mp4']
```
Note the user was told the job was queued. The worker's guard would later reject it as
`input path escapes its workspace` — **the write has already happened by then.**

---

## I. Existing Architecture Guarantees

What is **already correct** and must be preserved verbatim:

| Mechanism | Guarantee it actually provides | Where |
|---|---|---|
| `UNIQUE(idempotency_key)` + `ON CONFLICT DO NOTHING` | At most one durable row per logical key | `in_process_job_queue.py:2228`, `2464-2483` |
| `request_fingerprint` | **Payload-field** identity; detects changed *fields*, not changed *bytes* | `creative_passport.py:146-162` |
| `CreativeRequestConflictError` | Fail-closed refusal of a changed `creative_render` request | `in_process_job_queue.py:2536-2539`, `922-925` |
| Persisted-identity cross-check | Detects a corrupt durable row across restart | `2514-2529` |
| `_guarded_workspace` | Workspace must resolve inside `creative_temp_dir` and carry `creative_` | `render_jobs.py:203-215` |
| `_guarded_input` | Input must resolve **inside its workspace**, and must be a file | `render_jobs.py:218-231` |
| `capture_trim_source` preflight | Trim jobs fail closed if the source snapshot is unavailable | `1650-1700` |
| Attempt fencing (`attempt_id`) | Stale attempts cannot publish | `creative_passport.py:165-169` |
| `AttemptStaging` per `(job_id, attempt_id)` | Publication isolation; ownership sidecar; fail-closed mutation | `render_jobs.py:964-988`, `1030-1105` |
| Single process owner + startup barrier | One execution authority; ordered recovery | PR #299 |
| Worker re-derives trace id | Surface cannot forge causal identity | `render_jobs.py:313-340` |

**Gap statement (explicit, per §10):** *the current APIs do **not** provide any guarantee that the
bytes at `input_path` are the bytes that were admitted.* No read-by-key primitive exists on
`JobQueuePort` (`enqueue`/`get_status`/`get_result` only), no immutable input store exists, and no
content identity exists in the admission contract. Any design that claims exactly-once *content*
semantics without adding that identity is unsound.

---

## J. P0 Design — smallest complete fix

### J.1 Candidate evaluation

| Concern | A: delivery-scoped unique staging | B: content-addressed input | C: atomic admission + reservation | D: **chosen** — immutable attempt-input + digest admission |
|---|---|---|---|---|
| Identity | new name per delivery → dedupe lost | content → identity semantically wrong for "one logical request" | key + reservation | key stays the identity; content becomes a *verified attribute* |
| Input ownership | surface | nobody (content store) | surface | **surface**, under a name the queue cannot confuse |
| Atomicity | weak | strong | strong | strong (`os.replace` + `ON CONFLICT`) |
| Duplicate behaviour | safe | safe | safe | **reuse existing job; never rewrite its input** |
| Conflict behaviour | none | none | partial | **typed fail-closed (`CreativeRequestConflictError`)** |
| Crash safety | orphan dirs | orphans | orphan reservation | one bounded orphan directory |
| Recovery | none | none | reaper | existing `capture_trim_source` + new digest re-verify |
| Cleanup | ambiguous | ambiguous | own reaper | `bot/app.py` after publish (single owner) |
| Security | traversal/symlink unsolved | good | good | `lstat` + `O_NOFOLLOW` + resolve-containment |
| Queue compatibility | none needed | none needed | needs port change | **queue remains sole authority** |

Rejected: **A** loses duplicate collapsing; **B** makes content the request identity, violating "one
logical request identity"; **C** needs a new reservation primitive on `JobQueuePort` — a *second*
durable authority, explicitly forbidden.

### J.2 Chosen design — five minimal moves

**J2.1 — Immutable, attempt-unique input path.** Replace the fixed `input{suffix}` with a name that
includes an opaque, non-reusable component derived from the *delivery*, e.g.
`input-{sha256(key + uuid4).hexdigest()[:16]}{suffix}`. The workspace keeps its deterministic
`creative_{sha256(key)[:12]}` name (so cleanup and the worker's prefix guard are unchanged), but the
**input file inside it is never overwritten**. Property: *no delivery can ever mutate the bytes named
by a previously admitted payload.*

**J2.2 — Stage-then-publish, never write the live path.** Download into a sibling temp name in the
**same directory** (same filesystem, so `os.replace` is atomic), then `os.replace(tmp, final)` only
after the transfer completes. A partial transfer leaves a temp file, never a truncated live input.
Property: *no partial file is ever visible under an admitted name.*

**J2.3 — Content digest in the admission contract.** After the atomic publish, compute
`sha256` of the published input and pass it through. **This is a frozen-contract change:
`CreativeRenderPayload` is `model_config = ConfigDict(extra="forbid")` (`render_jobs.py:130`), so an
unknown key is rejected and a P0 claim will leak a validation error from a concurrently-open PR.**
The merge order must therefore be either (a) P0 adds the field *and* the current open PRs are
rebased after, or (b) the digest rides in an existing free-form field. The honest choice is (a),
declared as an ordering dependency. Property: *the admitted payload names the bytes, not just a path.*

**J2.4 — Digest re-verification at claim.** In the worker preflight — beside the existing
`capture_trim_source` gate, not replacing it — re-hash `_guarded_input(...)` and compare against the
admitted digest. Mismatch → `FAILED_TERMINAL` (typed). Property: *a substituted input is detected
before any render work, for every operation, not only trim.*

**J2.5 — Delete `339` and narrow `344`.**
- **`339` (`rmtree` before admission) is removed outright.** Its stated purpose — "stage into
  byte-identical replacements of its own files instead of multiplying directories" — is served by
  J2.1 plus a *bounded* orphan reaper, not by destroying live state.
- **`344` is narrowed** from `rmtree(workspace)` to unlinking **only** the temp/final file this
  delivery itself created. It must never delete a file it did not create in this call.
- **Cleanup ownership stays where it already is:** `bot/app.py:139` after publication. The surface
  gains **no** new deletion authority.
- **Symlink/traversal:** before `mkdir`/`replace`, `lstat` the target and refuse a symlink; after
  resolution, assert containment in `creative_temp_dir` (mirroring `_guarded_workspace` so the
  surface and the worker agree). Property: *no write through a pre-planted symlink; no write outside
  the temp root.*

### J.3 Crash model

| Crash point | Durable state | Recovery |
|---|---|---|
| before download completes | temp file only, no payload | temp is swept by the bounded reaper; **no admitted job affected** |
| after publish, before `enqueue` | orphan immutable input; **no job row** | reaper (age-bounded) reclaims; user sees an error reply |
| after `enqueue`, before reply | **job row `pending`**, `input_path` immutable | worker proceeds normally; user may re-deliver → new attempt name, **existing input preserved** |
| mid-render, process dies | row in-flight + fencing token | existing PR #299 recovery; `_guarded_input` + digest check re-validate |
| after publish artifact, before cleanup | published artifact + workspace | existing `finally: cleanup_workspace` |

### J.4 Cleanup ownership rules

1. **Surface** may create and may delete **only** files it created in the current call (temp → final).
2. **Surface** may never delete a file named by an admitted payload.
3. **Notifier** (`bot/app.py`) remains the sole post-publication workspace reaper.
4. **Reaper** reclaims only directories with **no queue row** and age > TTL.

### J.5 What the design deliberately does not do

No second durable queue. No new worker path. No second Passport. No new filesystem authority. No
change to fencing, heartbeat, publication, or terminal states. No bypass of `CommandBus`. No schema
change beyond the single digest field, and that field is declared as a **contract-ordering
dependency**, not smuggled in.

---

## K. Test Matrix

Target file (must be created under the already-fenced path):
`tests/integration/test_creative_surface_redelivery.py`.
Invariant source of truth: §H.2. "Real" = real `InProcessJobQueue`, real SQLite, real filesystem, no
queue mock. Current baseline for the neighbouring unit file is **24 passed** (`EXECUTED_TEST_CONFIRMED`).

| # | Scenario | Given | When | Then — exact observable assertion | Expected on unsafe baseline |
|---|---|---|---|---|---|
| 1 | Same-message duplicate, same content | job admitted for msg *m* | redeliver identical bytes | `enqueue` returns the **same** `job_id`; `input_path` bytes **and** sha256 unchanged; exactly 1 row | passes already (guard: regression lock) |
| 2 | Same identity, conflicting content | job admitted with bytes A | redeliver msg *m* with bytes B | `CreativeRequestConflictError` **or** a new attempt path; **A's bytes still readable at A's path** | **FAILS** — silent substitution |
| 3 | Distinct messages, identical media | two messages *m1* ≠ *m2*, same bytes | deliver both | 2 rows, 2 distinct `job_id`, 2 distinct workspaces; both inputs intact | passes (regression lock) |
| 4 | Concurrent duplicate delivery | one key | `asyncio.gather` of 2 identical deliveries | exactly 1 row; 1 input; no truncated/lost bytes; no exception escapes | **FAILS** — TOCTOU on `339` |
| 5 | Concurrent different-content conflict | one key | gather 2 deliveries, bytes A vs B | one admitted digest; the other fails **typed**; **no torn file** (every readable input hashes to A or B, never a mix) | **FAILS** |
| 6 | Partial download | job admitted with A | redeliver whose transfer truncates | A intact; no file under an admitted name contains truncated bytes; typed failure reply | **FAILS** — A deleted at `344` |
| 7 | Download exception | job admitted with A | redeliver raises `ConnectionError` | **A exists**, sha256 unchanged, job row still `pending` | **FAILS** — proven in §H.3 |
| 8 | Enqueue exception | fresh key | `enqueue` raises | typed `queue_failed` reply; **no** file named by any admitted payload is deleted | passes (but assert it) |
| 9 | Process death before enqueue | fresh key | kill after publish, before `enqueue` | no queue row; only orphan immutable input; reaper-eligible | new |
| 10 | Process death after enqueue | admitted job | kill before worker claim | row `pending`; `input_path` exists; digest matches admitted | **FAILS** if `339` ran again |
| 11 | Restart and recovery | admitted job | close + reopen queue | row recovered; input intact; digest re-verified; `_guarded_input` succeeds | new |
| 12 | Active-job input preservation | in-flight job with A | 3 redeliveries with B, C, D | A's path **always** hashes to A for the whole window | **FAILS** |
| 13 | Path traversal | crafted `workspace_dir` = `../../etc` | worker claim | `CreativeRenderError("invalid_request")`; nothing written outside root | passes (worker guard) — assert surface side too |
| 14 | Symlink escape | symlink planted at deterministic workspace path | deliver | refusal; **zero** files outside `creative_temp_dir` | **FAILS** — proven in §H.3 |
| 15 | Exact input digest preservation | admitted digest recorded | redeliver ×2 | worker-observed sha256 == admitted sha256 on every attempt | **FAILS** (no such field today) |
| 16 | Retry after failure | job `FAILED_TERMINAL` | re-deliver same message | new attempt, **new** input path; old input never reused; one terminal outcome | new |
| 17 | Edited message identity | msg *m* with A, op `trim` | edited to op `reverse`, bytes B | **must fail closed**; A preserved; explicit conflict signal | **FAILS** — passes *only* by payload-field accident |
| 18 | Stale worker publication refusal | attempt 1 fenced out | attempt 2 publishes | attempt 1's publication refused; published artifact belongs to attempt 2 | passes (PR #299) — regression lock |

**Honesty note.** Scenarios 1, 3, 8, 13, 18 are **regression locks** on guarantees that already hold;
they are not evidence of a fix. Scenarios 2, 4, 5, 6, 7, 12, 14, 15, 17 are the ones that must go
RED first. Scenarios 2, 7 and 14 have already been shown to go RED by this investigation; the other
six are **predicted, not proven** — a test *design* is not a RED result, and this report does not
claim one.

---

## L. Independent Review

**Architecture.** The design touches exactly one file of production code plus one test file, both
already fenced. It removes an authority (the surface's pre-admission delete) rather than adding one —
correct direction. Risk: J2.3's digest field collides with the open PRs (#66/#69 also edit payload
shape), so the contract change is an **ordering dependency**, not a local edit. A P0 claim taken
before those PRs are resolved will leak `ValidationError` from a concurrently open PR. Declared, not
hidden.

**Security.** Two genuine findings beyond the brief: (i) the deterministic workspace path is derived
from a *public* formula (`sha256` of `user:chat:message`), so any party able to create a directory
under `creative_temp_dir` can pre-plant a symlink at a predictable path and redirect a verified-Telegram
download — proven. (ii) No `O_NOFOLLOW`/`lstat` check exists on the surface write path. Both are fixed
by J2.2 + J2.5. The worker's `_guarded_input` guard does **not** mitigate either, because it runs
after the write.

**Test design.** Mocked queues cannot prove scenarios 4, 5, 9, 10, 11, 16 — they require real SQLite
and real process boundaries. Scenario 15 is untestable until J2.3 lands; writing it first would only
assert the absence of a field. Scenario 17 is the subtlest: on today's baseline it *appears* safe,
but only because `args` differed; with identical `args` and different media it fails silently. The
test must therefore vary the **media**, not the operation.

**Objections I could not dismiss.** (a) Removing `339` means duplicate redeliveries accumulate
orphan inputs for the life of a job; a bounded reaper is therefore *required*, not optional, and the
reaper must never delete a path named by a non-terminal row. (b) Reading `edited_message` is
arguably wrong at the surface level — a stricter fix would refuse `edited_message` entirely. I
recommend **both**: refuse edits **and** make the input immutable, because immutability is the only
one that survives a future caller that does not refuse.

---

## M. Owner Action Matrix

| Blocker | Exact missing evidence | Owner action | What becomes possible | Cost |
|---|---|---|---|---|
| **P0 task unclaimed** | an official `claim` (mutating) | authorize the claim in §N on this branch | P0 implementation begins | 1 command |
| **CircleCI pipeline 9 root cause not *directly* logged** | `error` stage detail, `config_source`, VCS-binding health, plan/credit state | **read-only** CircleCI project diagnostics: project *Settings → Advanced/Setup*, `GET /api/v2/pipeline/{id}`, and the pipeline's failed-workflow view for pipeline 9 and 15 | closes the residual ambiguity between "zero-config project" and "unhealthy VCS binding" | no credits, no build triggered |
| **CircleCI unreachable from sandbox** | network egress to `*.circleci.com` + a read-only token | either (a) provide a **read-only** CircleCI token + egress, or (b) paste the pipeline-9 error text | `PERMISSION_BLOCKED` → `LIVE_CONFIRMED` on D.2 | minimal; no admin scope |
| **Branch-protection unreadable (403)** | `required_status_checks.contexts` re-read live | no action needed for the verdict (§E is already proven behaviourally). If a re-read is wanted: a token that is **not** a GitHub App installation token, read-only | re-confirms §E from the live payload instead of the documented one | optional |
| **`pr-repair-69` fences 2 of 3 P0 paths** | n/a — currently **not live** | none now. Owner must **not** claim `pr-repair-69` before the P0 claim, and if it is ever claimed first the P0 claim must be re-refereed | keeps P0 ownership conflict-free | 0 |
| **CircleCI config genuinely absent** | `.circleci/config.yml` | **do not** add it to a P0 PR. `task-nagar-circleci-pilot-01` exists for exactly this, `queued`, fenced | the pilot becomes its own authorised task | 0 |
| **PRs #66/#69 are 777/768 commits behind** | n/a | no action; do **not** rebase P0 onto them | avoids replaying 700+ commits of drift | 0 |

**Nothing in this matrix requires a broad credential, a secret, or a write to any policy.** The
smallest missing action is a single `claim`.

---

## N. Next Authorized Operation

**One operation, not a list:**

```bash
python3 scripts/agent_board.py claim task-nagar-workspace-redelivery-01 \
  --branch arena/f6b34743-nexus-ai-agent \
  --expected-generation 0 \
  --ttl 24
```

**Preconditions, all already verified in this session:**

- Task is canonical on `main` (`queued`, idx 173) — §F.1.
- Zone is registered (idx 77) and its `paths` equal the claim's `exclusive_paths` — §F.1.
- `generation == 0` → `_claim_locked` takes the **fresh-task** branch
  (`agent_board.py:423-427`: *"A truly fresh task (no record, or generation 0) needs no token"*);
  passing `--expected-generation 0` is explicit and harmless.
- It will set `status="active"`, `agent_branch`, `claimed_at`, `gates_owner=False`,
  `generation=1` (`453-455`).
- Referee is clean on all three P0 paths — **45 sources read, exit 0**, reported verbatim as
  *"no overlap — safe to proceed (every consulted source was readable)"*.

**Immediately after, per the board's own printed instruction** (`458-459`):
`git add .agents/board.json && git commit && git push` — *an unpushed claim does not exist.*

**This command was deliberately NOT executed.** `claim` is mutating (`save_board` + push) and
instruction 8 forbids claiming during this mission. It is recorded here verbatim so the next
authorized engineer does not have to re-derive the interface.

*(Non-blocking, recommended before it: re-run*
`python3 scripts/agent_board.py praudit --repo bot523h/nexus-ai-agent` *to confirm no new claim
appeared on the P0 paths since `2026-11-01T04:58Z`.)*

---

## O. Remaining Limitations

| # | Not verified | Evidence class | Why |
|---|---|---|---|
| 1 | CircleCI pipeline 9 **failure log**, `config_source`, trigger, VCS-binding health | `PERMISSION_BLOCKED` | `*.circleci.com` → `HTTP:000`; no CircleCI token |
| 2 | CircleCI **plan, credit balance, runner availability** | `UNVERIFIED` | same; and deliberately not probed (would consume credits) |
| 3 | Live `branches/main/protection` payload | `PERMISSION_BLOCKED` | **403** on REST and GraphQL (`Resource not accessible by integration`); §E stands on the merge event instead |
| 4 | Which of *zero-config* vs *broken-VCS-binding* caused the `error` | `UNVERIFIED` (HIGH-confidence inference) | both require the CircleCI API; **same remediation either way** |
| 5 | Scenarios 4, 5, 6, 9, 10, 11, 16, 18 of §K | **predicted only** | designing a test is not running it; no test was written, per instruction 7 |
| 6 | Behaviour under a *real* Telegram `edited_message` object | `SOURCE_CONFIRMED` only | traced statically; not exercised with live PTB objects |
| 7 | `265d6a0` / `2818d9d` content | `HISTORICAL_REPORT_ONLY` | objects absent from the repository; never merged |
| 8 | Any guarantee of exactly-once *content* semantics | **absent from current APIs** | stated explicitly in §I; do not assume it |
| 9 | Full-suite pytest / lint / mypy on this worktree | not run | out of scope; no production code changed. Only `tests/unit/test_creative_surface.py` was executed: **24 passed** |
| 10 | The P0 fix itself | **not started** | prohibited by instructions 6 and 7 |

**Scope of verification.** This report is `VERIFIED_WITH_LIMITATIONS` **for the forensic mission**: the
live baseline, PR #300's history, the CircleCI root cause (config absence), the required-check
decision, canonical board registration, the P0 root cause, and three RED reproductions are all
established on evidence. The **P0 implementation is 0 % complete** and correctly so. No source file,
no test, no workflow, no board entry and no PR state was modified; `git status` is clean and the
session branch is identical to `origin/main`.

---

### Appendix — Evidence ledger

| Field | Value |
|---|---|
| Investigation UTC window | `2026-11-01T04:51:45Z` → `2026-11-01T04:58:28Z` |
| `main` / PR #300 merge | `d52a2434150d4e3a422d71b5f42b354d24bf61eb` |
| PR #300 head | `1cbe723b8fc957689513d9da83ae6200f41c4381` |
| PR #300 base at open | `48aae28a4ef4a00459530bb177ce4fe4e43c0ad3` |
| PR #299 merge (no CircleCI status) | `48aae28a4ef4a00459530bb177ce4fe4e43c0ad3` |
| Actions run | `41244937169` — `success`, 22/22 jobs |
| CircleCI status (PR head) | `CircleCI Pipeline` = `error`, pipeline **9**, `2026-10-31T21:43:37Z` |
| CircleCI status (merge commit) | `CircleCI Pipeline` = `error`, pipeline **15**, `2026-11-01T04:36:30Z` |
| CircleCI status (PR #299 merge) | **none** |
| Required contexts on PR head | all 3 `success` |
| Rulesets | `[]` (direct, and `includes_parents=true`) |
| Board integrity | `OK`, 604 532 bytes, schema 2 |
| Referee (P0 paths, 3 files) | exit **0**, 45 sources, *"no overlap — safe to proceed"* |
| Referee (CircleCI task paths, 2 files) | exit **0**, 45 sources |
| Unit baseline | `tests/unit/test_creative_surface.py` → **24 passed** |
| RED reproductions | 3 (content substitution, download data-loss, symlink escape) + 1 isolated surface variant |
| Repository writes by this session | **none** (`git status` clean; scratch scripts confined to `/tmp`) |
