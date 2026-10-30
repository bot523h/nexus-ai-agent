# PR convergence — M14 defect root-cause and fix (task `pr-repair-189`)

Date: 2026-10-30 · base `main` `dc23968d` · branch `arena/m14-pr-convergence-defects`
Scope: `scripts/pr_convergence.py`, `tests/unit/test_pr_convergence.py`.

The M13 watchdog flagged two defects in `scripts/pr_convergence.py`. Both were
reproduced live before any code changed, then fixed, then re-proved.

## Defect A — merge-readiness judged from the coarse rollup

**Symptom.** PRs #287 and #289 were reported `CI_RED` while every required check
was green on their head.

**Root cause.** `blockers()` read `statusCheckRollup.state`, a *worst-wins*
aggregate over **all** check runs on the head. A re-run *cancels* the superseded
attempt, so the same context appears twice — an older `CANCELLED` next to a newer
`SUCCESS` — and the coarse state stays `FAILURE`. GitHub's own required-check
evaluation uses the **latest** run of each context; the registry did not.

**Evidence (live, 2026-10-30).** On #287 and #289 every one of the three required
contexts appeared twice (old `CANCELLED`, new `SUCCESS`); the registry said
`CI_RED`, GitHub said mergeable. `collect` reported `CI_RED: 4`.

```
#287  lint CANCELLED 01:40:12Z  ->  SUCCESS 01:49:43Z
      test CANCELLED 01:40:27Z  ->  SUCCESS 02:01:44Z
      merge-base-guard CANCELLED 01:40:12Z -> SUCCESS 01:41:50Z   => classified CI_RED
```

**Fix.** `blockers()`/`classify()` judge the **latest run of each required
context** (`_latest_run_state` / `_latest_ci_verdict`), ordered by completion
timestamp and falling back to list position (the REST reader is oldest→newest).
The coarse rollup is consulted only when no required context is present. A
non-terminal/unknown conclusion is never a pass (it stays `CI_PENDING`).

**Proof.** `collect` after the fix: `CI_RED 4 → 2`, `READY 0 → 2`; #287 and #289
are `READY`; the two remaining `CI_RED` PRs (#97, #64) are *genuinely* red — their
latest `test` run is `FAILURE` (the op-matrix blocker, task-280).

## Defect B — an unreadable protection aborted the whole collection

**Symptom.** `collect` exited 2 and wrote **no registry** when branch protection
could not be read.

**Root cause.** `build_registry()` called `_live_protection()` unguarded; any
non-zero `gh` exit (a `403` from a credential that is not an admin) raised, and
`main()` returned 2 before writing anything.

**Evidence (live, 2026-10-30).** With the integration token:
`gh: Resource not accessible by integration (HTTP 403)` → no registry file.

**Fix.** The read degrades instead of aborting. The registry records
`protection_state: "unreadable"` plus `protection_source`/`protection_error`,
derives *candidate* context names from the committed `ci.yml`
(`_required_contexts_from_workflow`, a hint — never proof the gate is on), and
adds a lowest-priority `PROTECTION_UNREADABLE` blocker so **no PR is ever
certified `READY` on an unverified gate**. A real defect (a conflict, a draft)
still outranks it, so an unreadable gate never *hides* a known blocker.

**Proof.** With the integration token the same command now exits 0, writes the
registry, reports `protection_state: unreadable`, and `READY: 0`.

## Tests and mutation

18 new cases in `tests/unit/test_pr_convergence.py` cover: latest-run precedence,
older-failure→newer-success, older-cancelled→newer-success, newer-failure after
older-success (the unsafe direction), in-flight runs, duplicate names, missing
contexts, timestamp/order fallback, registry read-back round-trip; and for defect
B: `403` degradation, workflow fallback, nothing-READY, lowest-priority ordering.

Mutation campaign on an isolated copy (`/tmp`, real suite, no network):

| mutant | result |
|---|---|
| A coarse rollup instead of latest-run | KILLED (9 failed) |
| B ignore `protection_readable` | KILLED (3 failed) |
| C drop `status_contexts` on read-back | KILLED (1 failed) |
| D empty workflow fallback | KILLED (1 failed) |
| E oldest-run-wins ordering | KILLED (6 failed) |
| F unreadable protection reads as `readable` | KILLED (1 failed) |

**6/6 killed**, tree restored green.

## Verification commands

```
GH_TOKEN=<admin>  python3 scripts/pr_convergence.py --repo bot523h/nexus-ai-agent collect --no-wait --out reg.json   # exit 0
GH_TOKEN=<integration> python3 scripts/pr_convergence.py ... collect --no-wait --out reg.json                       # exit 0, protection_state=unreadable
pytest -q tests/unit/test_pr_convergence.py                    # 69 passed
pytest -q -m "not slow" tests/unit tests/architecture          # 5277 passed, 13 skipped
ruff check . && ruff format --check .                          # clean
mypy scripts/pr_convergence.py                                 # clean
```

## Residual limitations

- The workflow fallback is a *hint* (a job without an explicit `name:` is
  skipped); it never asserts the gate exists and never certifies `READY`.
- `collect` still requires a credential that can read the PR list; only the
  branch-protection read degrades.
