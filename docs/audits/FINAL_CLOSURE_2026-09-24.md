# Final Closure — Repository → CI → Git → Dependency Reconciliation (2026-09-24)

> **This record is not a second verification report.** The verification work it
> closes lives in PR #74's own artifacts
> (`docs/audits/VERIFICATION_GAP_REPORT_2026-09-24.md`,
> `docs/audits/VERIFICATION_TRUTH_MATRIX.json`,
> `docs/audits/CROSS_PR_TRUTH_2026-09-24.md`). This page records only what a
> *separate* agent observed on GitHub and by re-execution, so that "claimed
> done" is replaced by "observable in GitHub". Where a claim could not be
> re-verified independently, it says so.

## 0. Rule applied throughout

`main` = `035a896`. Nothing below is copied from a report; every row names the
command or API call that produced it. A green *local* run is never treated as a
green *GitHub* run — §3 reads the check-runs from the API.

## 1. Repository truth (Phase 1)

| Item | Present | In `main` | CI | Dependency | Status |
|---|---|---|---|---|---|
| `origin/main` | ✔ `035a896` | — | — | — | base of every PR below |
| this branch `arena/01a0d467-…` | ✔ `03a6dc8` | ✘ | ✔ 4/4 | — | **PR #73**, OPEN, MERGEABLE |
| PR #67 | ✔ head `9c3a34f` | ✘ | ✔ 8/8 check-runs | independent | OPEN |
| PR #68 | ✔ head `eef8276` | ✘ | ✔ 8/8 | independent | OPEN |
| PR #70 | ✔ head `94505f5` | ✘ | ✔ 8/8 | independent | OPEN |
| PR #71 | ✔ head `9e9c753` | ✘ | ✔ 8/8; **no workflow runs of its own** | **contained in #74** | OPEN |
| PR #72 | ✔ head `f450df2` | ✘ | ✔ 8/8 | independent | OPEN |
| PR #74 | ✔ head `05d647e` | ✘ | ✔ 8/8, runs `36040556195` (PR) + `36040549601` (push) | **contains #71** | OPEN |
| commit `05d647e` | ✔ **exists on remote** | ✘ | ✔ | — | `refs/heads/arena/01a0d475-…` + `refs/pull/74/head` |
| commit `fd07cfd` | ✘ **does not exist** | ✘ | ✘ | — | API `422 No commit found`; not in any ref, reflog or dangling object |

Merge-bases with `main`: #71/#72/#73/#74 → `035a896` (2/3/4/4 commits ahead);
#67/#68/#70 are single-commit branches.

### `fd07cfd` — resolved, not chased

It is **not** a remote ref tip, **not** an object in any fetched history
(`git log --all`), **not** dangling (`git fsck --lost-found`), and the REST API
returns `422 No commit found for SHA: fd07cfd`. GitHub cannot distinguish
"never existed" from "force-pushed away and unreachable", so the precise status
is: **absent from the repository by every observable route**. No work was
attributed to it and nothing was pushed for it.

## 2. PR dependency graph (Phase 5)

**Measured:** `git merge-base --is-ancestor origin/pr71 origin/pr74` → **true**.

`origin/pr74` = `05d647e ← 59f807d ← 9e9c753 ← 85f4444 ← 035a896`, where
`9e9c753` **is PR #71's head**. PR #74 is a stacked descendant, not a sibling:

```
#74 depends on #71        (verified by ancestry, not by reading a report)
```

Consequences recorded, **no action taken** (no rebase, no force-push):

* #74's diff (24 files) is a strict superset of #71's (13 files);
  `worker.py`, `jobs/*` and `in_process_job_queue.py` overlap with a larger delta.
* **Merge-order hazard, not a merge conflict.** Both PRs target `main` and both
  are `MERGEABLE`. Merging #71 first leaves #74 a smaller diff; merging #74
  first carries all of #71's commits into `main`, after which #71's own PR is
  empty/redundant. GitHub will resolve either order cleanly, so this is a
  **sequencing decision for the owner**, and the identical file set means the
  two must not be merged out of order by different people concurrently.
* PR #71's head has **no workflow runs of its own** — its 8 green check-runs are
  inherited from #74's push/PR runs on the same tree content.

## 3. CI truth (Phase 2, 4) — read from the API, not assumed

| check | commit | result | evidence |
|---|---|---|---|
| `lint (ruff + mypy + version lockstep)` | `05d647e` | ✔ success | `commits/05d647e/check-runs` |
| `lint-fast (lockstep + pinned ruff, no install)` | `05d647e` | ✔ success | same |
| `migrate-postgres` | `05d647e` | ✔ success | same |
| `test (pytest -m "not slow")` | `05d647e` | ✔ success | same, ×2 runs |
| `lint`, `lint-fast`, `migrate-postgres`, `test` | `9e9c753` (#71) | ✔ success | inherited from #74's runs |
| `lint`, `lint-fast`, `migrate-postgres`, `test` | `f450df2` (#72) | ✔ success | `commits/f450df2/check-runs` |

**No CI failure was found on any of these heads, so Phase 4 (failure triage) had
no subject.** One local failure *was* triaged, and it is recorded because it
would otherwise look like a defect:

| Observed | `test_gap_b_pdf_extract_completes_with_verified_text_artifact` and
  `test_pdf_extract_rag_failure_still_fails_job` failed locally |
|---|---|
| Reproduction | re-run in a detached worktree at `origin/pr74` |
| Root cause | `ModuleNotFoundError`-driven: `pypdf` was absent from this
  sandbox's venv — **category D (dependency/install)**, not a code defect |
| Confirmation | `pypdf>=5.1` is declared in `pyproject.toml` (line 67, dev deps,
  with the comment "keep the pdf-extraction test path real, not mocked");
  CI runs `pip install -e ".[dev]"` and is green |
| Minimal fix | **none to the repository** — installed `pypdf` locally |
| Local reproduction after | `14 passed` in `tests/integration/test_verification_gap_closure.py` |

### Import-mode parity (`pytest` ≠ `python -m pytest`)

CI invokes the **bare `pytest` console script**; this sandbox had been using
`python -m pytest`. Both were measured, and both pass for the suite that
`05d647e` was written to make CI-import-safe:

| command | result |
|---|---|
| `pytest -q tests/integration/test_verification_gap_closure.py` (CI mode) | **14 passed** |
| `python -m pytest -q …` (other import mode) | **14 passed** |

That commit's own message ("CI runs the bare `pytest` console script, where
`tests` is not an importable package") is therefore **verified, not taken on
trust**.

## 4. What was independently verified (Phase 8) — and how

Not copied from PR #74's report. Every line is a measurement taken in a detached
worktree at `origin/pr74` (`/tmp/v74`), never in a working tree that anyone owns.

| Claim | Method | Result |
|---|---|---|
| verifier registry exists and is complete | imported it; diffed its keys against `worker.default_job_handlers()` | **4 handlers ↔ 4 verifiers, exact 1:1, zero unverified** |
| both production composition roots install it | read `bot/app.py:175` and `cli.py:897` | both rely on the default (no `artifact_verifiers={}` opt-out) |
| GAP-A slideshow | re-executed the suite | ✔ passed |
| GAP-B pdf | re-executed after the category-D fix | ✔ passed |
| GAP-C story | re-executed | ✔ passed |
| attacks A–H | present as 8 named tests, re-executed | ✔ all passed |
| registry ratchet | re-executed | ✔ passed (also asserts the promised set, not just coverage) |
| ruff / mypy / lockstep | CI on `05d647e` | ✔ (see §3) |
| board referee | `agent_board.py check` against PR #74's file set | see §6 |

**Where only report evidence existed, it was not upgraded.** The table in §5
distinguishes what was re-executed here from what rests on PR #74's own CI.

## 5. GAP status (Phase 6, 7)

| GAP | Status | Evidence | Remaining |
|---|---|---|---|
| **GAP-A** slideshow render | **CLOSED** (independently re-run) | `slideshow_render_verifier` registered by default; `test_gap_a_…` re-executed green; attack E re-executed | none observed |
| **GAP-B** pdf extract | **CLOSED** (independently re-run) | text artifact + `pdf_extract_verifier`; empty text → `verification_failed:empty_artifact`; re-executed green | none observed |
| **GAP-C** story | **CLOSED** (independently re-run) | Pillow structural probe verifier; re-executed green | none observed |
| **GAP-D** legacy `/creative/*` | **OPEN — explicitly accepted, NOT closed** | `_process_video_edit_job` persists `done` with `result.model_dump()` — the handler's own claim — with **no** re-measurement; pinned by `test_legacy_registry_can_record_done_without_any_artifact` | ownership decision (frozen by D-0010, removal sequenced after PR #58). Route intact, API intact, no migration, no new dependency |
| **RAG ingestion** | **OPEN — receipt missing, task defined (§5.1)** | execution probe below | see §5.1 |

### 5.1 RAG — the one genuinely new finding in this pass

PR #74's matrix calls RAG "NOT_APPLICABLE (external side effect)". Re-reading
the code shows the situation is **stronger than that, and worth a task**:

* RAG ingestion is **inside the handler's `try`** — if `add_document` raises, the
  job is persisted `failed`. So RAG *is* a success condition
  (pinned by `test_pdf_extract_rag_failure_still_fails_job`, re-executed green).
* The value that would serve as a receipt — `add_document` returns
  `list[str]` of chunk ids — is **discarded** (`await engine.add_document(...)`,
  no assignment).
* Therefore a `pdf_extract` job can reach `completed` with
  `artifact_verification.status = verified` while the RAG side effect stored
  **nothing at all**.

Measured, with only the RAG engine doubled (returning `[]`, raising nothing):

```
job status            : completed
artifact_verification : 'verified'  reason=None
=> COMPLETED + artifact verified, while RAG stored nothing
=> the job result carries no receipt for the RAG half of the work
```

**No code was changed for this.** Per the mission's rule — a gap is not closed
without acceptance evidence — it is recorded, and the independent task is defined
in §5.2. The product contract does treat RAG as part of success, so this is not
"out of scope"; it is "success asserted beyond what is verified".

### 5.2 Defined task — RAG receipt (not implemented here)

| Field | Value |
|---|---|
| id | `task-181-rag-receipt` |
| priority | P1 |
| scope | `pdf_extract` reports success that includes RAG ingestion, but carries no evidence the ingestion happened |
| acceptance | (1) `add_document`'s chunk ids are captured and persisted on the job result as a receipt; (2) a job whose receipt is empty (`[]`) does **not** complete as success — it fails with a typed reason; (3) a negative test proves a silently-no-op engine cannot yield `completed`; (4) the text-artifact verification of GAP-B is unchanged |
| dependency | PR #74 (GAP-B contract) merged first |
| constraint | no new dependency; no change to `pdf_extract`'s artifact dialect |
| board status | **not inserted into `.agents/board.json` `next_work`** — the protocol pins that list to **exactly ten** forward tasks (`test_next_work_network_is_structured`), and all ten slots are live records belonging to other agents. Adding an eleventh reds the board test; rotating one out deletes another agent's record. Recorded here in the same form the repository already uses for a defined-but-unowned task (cf. GAP-D's remediation entry), so it is actionable without overwriting coordination state. |

## 6. Ownership (Phase 2 of the mission)

`agent_board.py check --branch arena/01a0d475-… --files <PR #74's 24 files>`
run against **`main`'s** board reports:

```
OVERLAP  src/nexus_ai_agent/creative/render_jobs.py  ← task-166-creative-surface-wiring
OVERLAP  src/nexus_ai_agent/worker.py                ← task-166-creative-surface-wiring
```

Run against **PR #74's own** board the same check reports
`no overlap — safe to proceed`.

**Reconciliation:** PR #74 carries the stewardship release of that lease
(`task-166` → `status: done`, "Stewardship-released per orphaned-lease rule
(verified with gh 2026-09-24)"). The overlap is therefore **already resolved
inside #74** — what the referee sees is a **stale active claim on `main`** that
#74 corrects. No file was edited to make this go away.

## 7. Security / integrity (Phase 9)

Probes written for this pass, run directly against `jobs.verification.verify_artifact`
— not PR #74's tests:

| Probe | Result |
|---|---|
| symlink escape (link inside workspace → file outside) | `outside_expected_root` ✔ |
| `..` path escape in the claim | `outside_expected_root` ✔ |
| artifact outside expected root | `outside_expected_root` ✔ |
| wrong artifact type (text claimed as `video`) | `probe_failed` ✔ |
| size mismatch | `size_mismatch` ✔ |
| sha256 mismatch | `sha256_mismatch` ✔ |
| malformed sha claim | `invalid_sha256_claim` ✔ |
| empty artifact | `empty_artifact` ✔ |
| missing artifact | `missing_artifact` ✔ |
| artifact swapped after the claim was taken (stale claim) | `probe_failed` ✔ |
| **positive control:** valid SRT + exact claim | **`ok=True`, `status=verified`** ✔ |

The positive control matters: a guard that rejects everything is not a guard.
The invariant asked for by the mission holds by measurement —
**unverified artifact ≠ successful job**, a typed `verification_failed:<code>`
is never `COMPLETED`, and a partial/missing artifact cannot be a published success.

## 8. Mutation / adversarial honesty (Phase 10)

* No mutation marker (`MUTATED`, `TEST_ONLY`, `bypass`, `monkeypatch`, …) exists
  in production code under `src/` — every keyword hit is unrelated domain prose
  ("the source is never mutated", "bot owner bypasses the rate limit").
* No environment-variable escape hatch exists in the verification path
  (`jobs/*.py`, `adapters/in_process_job_queue.py` contain no `getenv`/`environ`).
* The bounded "unverified" fall-through in the queue
  (`verifier is None → historical semantics`) is **unreachable through the
  default composition roots**, because handler set == verifier set (§4) and
  `test_verification_registry_ratchet.py` fails if that ever diverges.

## 9. Test evidence (Phase 11)

| command | tree | result |
|---|---|---|
| `pytest -q -m "not slow"` (bare, CI mode) | `origin/pr74` | 1965 passed, **7 failed**, 22 skipped |
| `pytest -q -m "not slow"` (bare, CI mode) | this branch (#73) | 1944 passed, **7 failed**, 1 deselected |
| `pytest -q tests/integration/test_verification_gap_closure.py` | `origin/pr74` | 14 passed |
| `ruff check .` / `ruff format --check .` | both | clean (469 files) |

The **same seven** tests fail in both trees, and the same seven were measured
failing at the untouched base commit `035a896` in a detached worktree:
`test_migrate_race_condition.py` ×4, `test_database_url.py`,
`test_litellm_provider.py`, `test_version_command.py`. They need PostgreSQL, a
network engine build and installed-distribution metadata respectively — i.e.
they are **local-environment artefacts (category C/D)**, and CI, which provides
all three, is green (§3). **No test was deleted, skipped or weakened.**

## 10. Git evidence (Phase 13)

| Step | State |
|---|---|
| working tree clean | ✔ `git status --short` empty |
| commit exists | ✔ `03a6dc8` |
| commit pushed | ✔ `git ls-remote origin refs/heads/arena/01a0d467-…` = `03a6dc8…` |
| PR head correct | ✔ PR #73 head = `03a6dc8` |
| CI green | ✔ run `36039264752` for `03a6dc8`; four required checks pass |
| dependency clear | ✔ #73 is independent of #71/#74 (no shared file, different zones) |

## 11. Remaining blockers

1. **#74 depends on #71** (§2) — a sequencing decision, not a conflict.
2. **RAG receipt missing** (§5.1) — `task-181-rag-receipt` defined, not implemented.
3. **GAP-D legacy lane** (§5) — accepted gap, owned elsewhere, deliberately not closed.
4. **Stale `task-166` claim on `main`** (§6) — corrected by #74, not by this branch.
5. Pre-existing local-environment failures (§9) — not regressions; CI carries them.

## 12. Final status

**`PARTIAL`**

PR #74's verification work is real, independently re-executed, and green on
GitHub, and GAP-A/B/C are **CLOSED**; but closure of the mission as a whole is
`PARTIAL` because three items remain open by evidence rather than by opinion —
GAP-D (accepted, still reachable), the RAG receipt gap (`task-181-rag-receipt`
defined, not built), and the #71→#74 merge sequencing that must be decided before
either can land.
