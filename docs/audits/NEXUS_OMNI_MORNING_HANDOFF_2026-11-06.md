# NEXUS-OMNI morning handoff — 2026-11-06

Dated record. Every number below is reproducible from the tree and the GitHub
API with the commands shown; where something is not proven it says so.

## Executive summary

This session added one **P1 CI/security remediation** and re-verified the live
ground truth by running commands, not by trusting earlier reports.

- **Delivered (PR #318):** `pip-audit` is now an enforced, **self-expiring**
  dependency gate — `scripts/dependency_audit.py` + a reviewed allow-list
  (`.github/dependency-audit.json`) + a `dependency-audit` CI job that also
  proves the gate fails closed on an empty allow-list.
- **Corrected:** the earlier "raise `filelock` to ≥ 4.2.1" recommendation is
  **not achievable** — `litellm` pins `filelock<4.0` on every release, so 4.2.1
  is unresolvable. Encoded as a `transitive-cap` acceptance that reds the build
  the moment `litellm` relaxes the pin.
- **Re-verified ancestry** of the two board PRs by git + the API: **both are
  unmerged** (`ahead_by` 6 and 1); neither is "already in main".
- **Verified main is healthy:** push CI on `main @ aadaf531` = **success**.

## Live state (verified 2026-11-06)

| Fact | Value | How verified |
|---|---|---|
| `main` HEAD | `aadaf53157670b64af6978fcf41b592e108ac9ae` | `git rev-parse origin/main` |
| `main` push CI | **success** | Actions API, latest run on `main` |
| PR #318 head | `b7fcd524` | Actions API |
| PR #318 CI | 17+ success, **0 failures** | Actions API; `dependency-audit` job green, incl. its anti-vacuity step |
| PR #316 | open, `ahead_by=6`, `mergeable=true`, `behind_by=0` | compare API `main...938de001` |
| PR #317 | open, `ahead_by=1`, `mergeable=true`, `behind_by=0` | compare API `main...977390e1` |

## Delivered — PR #318 (dependency gate)

`pip-audit` was **not** an enforced gate. Now it is, and the acceptance is
self-expiring. Evidence:

```
$ python scripts/dependency_audit.py                 # real allow-list
18 advisories: 18 accepted, 0 blocking               EXIT=0
$ python scripts/dependency_audit.py --allowlist /tmp/empty_allow.json
18 advisories: 0 accepted, 18 blocking               EXIT=1
$ python scripts/dependency_audit.py --allowlist /tmp/stale_allow.json
STALE CVE-2026-112940 (filelock) — no installed dependency caps filelock below 4.2.1 any more
18 advisories: 17 accepted, 1 blocking               EXIT=1
$ pytest tests/unit/test_dependency_audit_gate.py -q
8 passed
```

CI run `42060153011`: `dependency-audit` job → **success** (both the audit step
and the "must fail closed" step).

## Open PR disposition (this session's scope)

- **#316** — `openhands/omni-recovery-integration-20261105` — board machinery:
  claim-prerequisite enforcement (`scripts/agent_board.py`), the mutation
  harness anchor fix (`scripts/board_referee_mutations.py` +
  `tests/unit/test_mutation_harness_anchors.py`), CI job-name lockstep, the
  2026-11-05 delta. `ahead_by=6`, mergeable, no conflict with `main`. **Owner:
  this session.** Merge is a controlled-owner step.
- **#317** — `board-reconcile-282-stale-lease` — models `task-282` as an epic
  (release, not done) + a typed PR #294 gate in `.agents/board.json`. `ahead_by=1`.
  **Overlaps #316 only on `tests/unit/test_agent_board.py`** (a test union; no
  semantic conflict). Land **after** #316 or rebase onto it.
- **#318** — this session's dependency gate. Additive, self-checking, **0 CI
  failures**. Recommended merge order: **#318 → #316 → #317**.

## Corrections to prior records

1. **`filelock` bump is impossible now** (see above); recorded in
   `NEXUS_OMNI_DELTA_2026-11-06.md`.
2. **#316/#317 are not "already contained in main"** — a `main-ahead=0,
   branch-ahead=N` reading means the *branch* is N commits ahead (unmerged), the
   opposite of containment. Verified via `git merge-base --is-ancestor` (NO) and
   the compare API (`ahead_by` 6/1).

## What is NOT proven

- The `test` (full pytest), `continuum-evidence`, and `python-parity` jobs on
  #318 were still **pending** at the time of writing (long jobs). No failure was
  observed; the dependency-audit job and 17 others were green.
- Production deploy / restore is out of scope here (no production access);
  recorded as **NOT_PROVEN**.
- The 42 other unmerged PRs were **not** re-triaged in this session; the
  authoritative classification remains
  `docs/audits/NEXUS_OMNI_PR_CONVERGENCE_LEDGER_2026-11-03.md`.

## Now / Next / Later

- **Now:** review + merge **#318** (additive, green), then **#316**, then **#317**.
- **Next:** when `litellm` drops `filelock<4.0`, the gate reds automatically —
  bump `filelock`, delete the 13 allow-list entries.
- **Later:** fold the dependency gate into the optional-extras install legs if a
  future extra pulls a materially different closure.
