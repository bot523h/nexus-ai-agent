# Cross-PR Truth Gate — #67 / #68 / #70 / #71 (task-180, 2026-09-24)

Every row below was measured on 2026-09-24 with `git` + `gh` against
`bot523h/nexus-ai-agent` (no prior agent report was trusted). The four
notions are kept strictly apart:

```
present in branch  ≠  present in PR  ≠  merged to main  ≠  CI verified on main
```

## 0. Ground truth

| Fact | Evidence |
|---|---|
| `main` = `035a896` ("Merge pull request #65") | `git rev-parse origin/main` |
| Last merged PR | **#65** (merged 2026-09-24T09:56:32Z) — P0 stabilization (tasks 165–167, D-0010..0012) |
| CI on main | PR#65 merge commit: `ci` **success**; the `maintenance` (backup) workflow on main: **failure** (known gap, task-164: owner-side R2 secrets) |
| PR #67 | **OPEN**, head `9c3a34f107`, CI 4/4 green, not merged |
| PR #68 | **OPEN**, head `eef827641b`, CI 4/4 green, not merged |
| PR #70 | **OPEN**, head `94505f51e5`, CI 4/4 green, not merged |
| PR #71 | **OPEN**, head `9e9c753082`, CI 4/4 green, not merged |
| Also open, overlapping scope | #69 (Gate-4 truth matrix; touches `in_process_job_queue.py`), #72 (Gate-2 command/capability — **same scope family as #68**), #66, #64, #63, #60, #59, #58, #57, #56 |

> Consequence for this branch: the task-178 lifecycle contract exists **only
> in PR #71's branch**. `arena/01a0d475-nexus-ai-agent` therefore
> fast-forwarded onto `9e9c753` (PR #71 head) — PR #71's two commits are
> preserved commit-for-commit — and task-180 stacks on top. **This branch's
> PR must merge after (or together with) #71.**

## 1. Layer matrix (per PR)

Legend: ● present in the PR head · ○ absent · ✔ merged to main. "CI" = the
PR's own checks at its head commit.

| Layer | #67 (task-177 creative runtime) | #68 (Gate-2 cmd/capability) | #70 (operation matrix) | #71 (task-178 lifecycle) |
|---|---|---|---|---|
| Product (surface/UX) | ● `bot/creative_surface.py` | ○ | ○ | ○ |
| Command | ● studio `bus.py` (session-3 form) | ● `studio/models.py`, `authorization.py` | ○ (documents commands) | ○ |
| Capability | ● packs portrait/scene/runtime | ● `studio/capabilities.py`, `references.py` | ● catalog reconciliation (docs+JSON) | ○ |
| Job | ○ | ● touches `adapters/in_process_job_queue.py` | ○ | ● canonical queue contract |
| Runtime | ● `creative/execution.py`, `rendering/*` (ir/compiler/executor/plan) | ● touches `slideshow/service.py`, `upscale.py` | ○ | ○ (explicitly frozen) |
| Artifact | ● `creative/artifacts.py` (three-identity artifact truth) | ○ | ○ | ● artifact claim/verification dialects |
| Verification | ● `test_artifact_truth.py` (runtime layer) | ● `test_command_capability_boundary.py` | ● `test_operation_matrix_reconciliation.py` | ● `jobs/verification.py` + queue VERIFYING (job layer) |
| Surface docs | ● CREATIVE_STUDIO, PACK_RUNTIME | ● COMMAND_CAPABILITY_CONTRACT | ● OPERATION_CONTRACT_MATRIX, L0_L4_MATURITY | ● JOB_LIFECYCLE.md |
| CI (at head) | ✔ 4/4 green | ✔ 4/4 green | ✔ 4/4 green | ✔ 4/4 green |
| **Merged to main** | **○ NO** | **○ NO** | **○ NO** | **○ NO** |

## 2. What this means (no inflation)

* **Nothing from #67/#68/#70/#71 is in production.** Their green CI proves
  the branch heads pass the gates — it does not put a single line on
  `main`. Any statement like "the canonical lifecycle is delivered" is only
  true *in PR #71's branch* (and now in this branch, which contains it).
* **Two verification layers exist on two different unmerged branches.**
  #67 verifies at the *runtime* layer (`creative/artifacts.py` — physical
  bytes vs render-spec identity, ffprobe-preferred probing); #71 verifies at
  the *job* layer (queue-owned re-measurement, VERIFYING state). They are
  complementary, not duplicates — but if both merge, the seam
  (runtime probe vs `jobs.verification.default_media_probe`) should be
  reconciled deliberately. Task-180 used only the #71 job-layer seam and
  did not import anything from #67.
* **Gate-2 exists twice.** #68 ("Nagar Gate 2: versioned command and
  capability boundary") and #72 ("Gate 2: canonical command + capability
  contract (task-179, D-0013)") implement the same scope family with
  overlapping files (`studio/*`, `COMMAND_CAPABILITY_CONTRACT.md`,
  `test_command_capability_boundary.py` vs `test_command_capability_contract.py`).
  Merging both as-is will conflict; an orchestrator decision (pick one,
  or sequence a reconciliation) is required. Task-180 did not touch either.
* **Merge-order dependencies touching the job queue.** `in_process_job_queue.py`
  is modified by #68, #69 and #71 (and now task-180 on top of #71). Merge
  order matters; after each merge the next branch needs a re-run of
  `tests/unit/test_job_lifecycle.py`, `tests/integration/test_job_lifecycle_queue.py`,
  `tests/architecture/test_verification_registry_ratchet.py`.
* **Truth-matrix ownership.** #69 owns `docs/audits/GATE4_TRUTH_MATRIX.json`,
  #70 owns `OPERATION_MATRIX.json`. Task-180's matrix
  (`docs/audits/VERIFICATION_TRUTH_MATRIX.json`) is deliberately a
  **verification-chain** matrix at distinct paths — no file collision.

## 3. Reproduction

```bash
git fetch origin --prune
git rev-parse origin/main                                  # 035a896…
gh pr view 67 --json state,headRefOid,statusCheckRollup    # OPEN 9c3a34f… 4/4
gh pr view 68 --json state,headRefOid,statusCheckRollup    # OPEN eef8276… 4/4
gh pr view 70 --json state,headRefOid,statusCheckRollup    # OPEN 94505f5… 4/4
gh pr view 71 --json state,headRefOid,statusCheckRollup    # OPEN 9e9c753… 4/4
gh run list --branch main --limit 3                        # PR#65 merge: success
```
