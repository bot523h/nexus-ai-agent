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

---

## M15 addendum (2026-10-30) — a missing required context certified READY

**Sourcery finding (PR #291, human-review request).** `_latest_ci_verdict()`
judged only the required contexts that happened to be *present* in the data.
With live per-context data, a required context that had **never reported on the
head** was silently ignored, so a head with 2-of-3 green checks and the third
absent classified `READY`.

**Reproduced (before the fix).**
```
REQ = [lint, test, merge-base-guard]
contexts = [lint SUCCESS, test SUCCESS]      # merge-base-guard MISSING
_latest_ci_verdict -> "SUCCESS"; classify -> "READY"     # DEFECT
contexts = []  rollup=SUCCESS                # ALL required missing
classify -> "READY"                                       # DEFECT
```

**Fix.** Strict (live) mode requires **every** required context to be present
**and** its latest run green; a missing / running / unknown context is
`CI_PENDING`, never `READY`. `_has_context_data()` distinguishes a live read
(per-context list present) from a genuinely old snapshot (no list), which alone
uses the coarse rollup — the explicit legacy contract. `require_all_present`
ties the strict rule to a **readable** protection (the only state in which the
required set is authoritative); when protection is unreadable the context list is
a hint and `PROTECTION_UNREADABLE` fail-closes separately.

**Proof.** `collect` on the corrected collector against live `main` `8d1e7167`:
`READY 0` (was 2 under the permissive rule), `protection_state: readable`.
#287/#289 are no longer false `CI_RED`; the remaining reds are genuine.

**Tests.** The outdated `test_classify_missing_required_context_is_not_a_pass`
was corrected, and the ten mandatory cases added (one/two/all missing; older
fail→newer success; older success→newer fail; still running; duplicate ambiguous
timestamps; no data; explicit zero required; unreadable protection). 82 pass.

**Mutation.** 3/3 killed on the new logic (drop the `require_all_present` gate;
treat a missing context as green; drop the coarse-rollup fallback); tree restored
byte-identically.
