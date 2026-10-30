# M13 — PR Convergence + Agent-Debt Watchdog (final report)

**Mission:** NEXUS-OPENHANDS-PR-CONVERGENCE-M13
**Anchor main:** `b3e20f93` → **final main:** `2221f0bc`
**Branch:** `arena/m13-operation-matrix` (evidence PR #286), `arena/m13-wrap` (this record)
**Date:** 2026-10-30

## 1. Deployed automation (agent-debt watchdog)

"**NEXUS Engineering-Debt Watchdog**" — an OpenHands prompt-preset automation that, nightly, inspects
the repository and its open PRs and reports/commits every detected weakness or failure (its own and
other agents'), with exact reproduction. Definition committed at
`.agents/automation/debt_watchdog_prompt.md`.

- Schedule: cron `37 2 * * *` (02:37 UTC), timezone UTC, repo `bot523h/nexus-ai-agent@main`, timeout 1800 s.
- The watchdog's detection rules are exactly the ones this shift used by hand (see §3).

## 2. Merges this shift (5) + 1 supersession closure

| PR | What | Result |
|---|---|---|
| #139 | thread destructive verdict across all lifecycle rows | **merged** (main `143cd842`); review thread resolved as false positive (opengrep `dangerous-subprocess-use-audit` on a static pytest argv, no `shell=True`) |
| #244 | cloud object-storage seam (local + Neon), typed capacity claims | **merged** (`616be267`); `DECISION_LOG` D-0032 collision renumbered to D-0035 |
| #59 | effect-consistency outbox dispatcher | **merged** (`08882f52`); `MODULE_MAP.md` union-resolved (R24 + main's R26/R27) |
| #276 | deterministic cognition quality evaluation harness (task-269) | **merged** (`055ad332`) |
| #286 | board reconcile + op-matrix root-cause correction + automation def | **merged** (`2221f0bc`) |
| #88 | PACK-SEC-001 external-pack activation gate | **closed as superseded** by `bd844b66` (real Ed25519 trust root, `packs/trust.py`) |

## 3. Weaknesses found and committed

1. **Operation-matrix contract is architecturally blocked (task-280).** Corrected M12's claim: three
   truth artifacts, not two — `OPERATION_TRUTH.json` + the `.md` projections are **generated**
   (`python -m nexus_ai_agent.nagar --docs`) and gated by `--check`; only `OPERATION_MATRIX.json` +
   `RECONCILIATION.json` are hand-authored. On #97, `--check` reports `runtime 57→77`, `missing 23→3`
   and `--docs` **refuses to write** (`declaration_parity.registered_not_declared` non-empty, 20 ids):
   the new `vision` pack is registered but has no source-level declaration (`sources._PACK_MODULES`
   omits it). Fix = declaration source **plus** a maturity reclassification — a product decision.
   Recorded in `.agents/evidence/OPERATION_MATRIX_ROOTCAUSE_M13.md`; task-280 **deferred** with a
   resume condition.
2. **Board drift: work merged, leases `expired`.** `task-264/265/260/266` were all in `main` but
   showed `expired`. Reconciled to `done`.
3. **Board referee false `UNVERIFIABLE` from a sibling worktree mid-conflict.** Two `test_agent_board.py`
   `check` tests fail locally while a *sibling worktree* sits in a conflicted merge — `check` reads
   every worktree's board and exits 2. Proven environmental (pass in an isolated clone); no code defect.
4. **`check` needs `GITHUB_REPOSITORY`** (not `--repo`) to read open-PR heads; without it the referee
   exits 2. Operational note, not a bug.

## 4. Root causes corrected from M12

- M12 said the operation matrix had "no generator". False: the generator exists and is CI-gated; only
  two of the four artifacts are hand-authored. This changes the fix from "hand-edit numbers" to
  "declare the ops + decide maturity", and correctly classifies #64/#97 as **blocked on a decision**,
  not on a merge conflict (both are `git merge-tree`-clean against `main`).

## 5. Verification (exact commands + observed results)

```
# isolated clone, main @ 2221f0bc
pytest -q -m "not slow" tests/            # 5551 passed, 39 skipped
# the 2 board tests, isolated clone (no sibling worktrees)
pytest -q tests/unit/test_agent_board.py -k "...gates_owner..."   # 2 passed
pytest -q tests/unit/test_agent_board.py  # 75 passed (main)
# referee
GITHUB_REPOSITORY=bot523h/nexus-ai-agent agent_board.py check --files ... --branch ...
                                          # exit 0, 48 sources, no overlap
```

Every merged PR: `lint` / `test` / `merge-base-guard` = success on its head SHA.

## 6. Not done (and why)

- task-280 implementation — blocked on an owner product decision (§3.1).
- #97 / #64 merge — clean against `main` but blocked on the same contract.
- #129 — a second studio transaction model colliding with the merged spine's `EditTransaction`; a
  product decision, deferred.
- The remaining ~30 open PRs are DRAFT, giant, superseded, or stale; none were force-merged. The
  mission's anti-overreach rule takes precedence over a merge count.
