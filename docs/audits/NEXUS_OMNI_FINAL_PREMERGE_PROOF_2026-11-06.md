# NEXUS-OMNI — final pre-merge hardening & integration proof (2026-11-06)

Final-responsible-engineer pass over the three open PRs — **#316** (board
PR-merge evidence), **#317** (released-status / epic model), **#318**
(dependency-audit gate). The mandate is not "make CI green" but **prove the three
changes are correct and correct *together*, with no security boundary breached**.

Method for every change: **PROBLEM → REPRODUCE → ROOT CAUSE → FIX → TEST →
INTEGRATION → INDEPENDENT VERIFICATION → PROOF**. Evidence is a command and its
observed result against a named SHA; where a claim cannot be proven in this
environment it is recorded **NOT_PROVEN**, never asserted.

Ground truth: `main` untouched at `aadaf53157670b64af6978fcf41b592e108ac9ae`.
All work on branches; no merge performed.

---

## 1. P0 — forgeable PR-merge evidence (PR #316)

**Problem.** `.agents/board.json` is the shared coordination medium. A typed
`pull_request` prerequisite was satisfied by *stored text*: a completion record
with `evidence.source == "github_api"` and any 40-hex `merge_commit_sha`.

**Reproduce (RED, before the fix).** A hand-forged record — the right repo,
number, state, source, and a made-up `"a" * 40` SHA — opened the dependency and
granted a lease (`claim` exit **0**). Anyone able to edit the board could unblock
their own work.

**Root cause.** `_pr_satisfied()` in `scripts/agent_board.py` trusted the record;
it never re-derived the merge from GitHub.

**Fix.** `_pr_satisfied()` now calls `_merge_still_verified(record, token=…)`,
which re-derives the fact live: the PR must really be merged, **into `main`**,
the API `merge_commit_sha` must **equal** the recorded SHA, and that commit must
be an **ancestor of `origin/main`**. Every failure path — non-merged, foreign
base, SHA mismatch, missing token, unreachable API, unprovable ancestry —
leaves the dependency unmet (**fail-closed**). Verification is cached for
`PR_MERGE_CACHE_TTL` (10 min) keyed by `(repo, number, sha, token-fingerprint)`;
the cache never stores a token, and the fingerprint is `sha256(token)[:16]`.

**Test.** The old test that *encoded the forgery as success* is replaced by an
adversarial suite: a perfect forgery is refused; every unprovable merge is
refused (parametrised); a genuinely merged, ancestor-of-main PR is accepted; a
wrong-`source` record is refused even when the merge would verify; a record for a
different PR does not satisfy the dependency; the cache honours its TTL and does
not cross token identities; a hand-typed fingerprint is non-reversible.

**Mutation.** The referee campaign grew 37 → 38
(`consumer_trusts_stored_merge_text`); `38/38 killed`. If the live re-check is
removed, the adversarial suite turns RED — the guard is load-bearing.

---

## 2. P0 — `completed_released` satisfied prerequisites (PR #317)

**Problem.** A non-terminal "released" epic/slice (`completed_released`) was in
`SATISFIED_STATUSES`, so it satisfied a prerequisite even though the work it
represents is not finished — letting a dependent start on an unfinished base.

**Fix.** `SATISFIED_STATUSES = {"done", "completed_merged"}` only; a released
epic keeps blocking its dependents. Task-282 is modelled as an epic
(`completed_released`, never `done`) with Slice 2 gated on the **merged** PR #315
(a typed dependency) and Slice 3 on Slice 2 plus the external PR #294.

**Test / mutation.** `test_task_282_epic_is_not_done_while_slices_are_open` +
`test_typed_prerequisites_are_well_formed`; the mutation
`released_status_satisfies_a_prerequisite` is killed.

---

## 3. P1 — dependency-audit gate hardening (PR #318)

**Problem (env markers).** An independent audit asked whether a dependency cap is
really active in the *target* environment or only in metadata. It was not:
`_pin_still_holds` read a requirement's specifier without evaluating its
environment marker, so a cap guarded by `sys_platform == "win32"` was treated as
blocking on Linux — keeping an acceptance alive for a platform the project does
not ship on and hiding a reachable upgrade.

**Fix.** `_marker_applies()` gates each cap on `marker.evaluate()` (absent marker
applies; an unevaluable marker stays conservative and keeps the cap).

**Fail-closed hardening.** `_load_allowlist` rejects a
missing/unreadable/non-object/entry-malformed allow-list (missing package,
unknown reason, `transitive-cap` without `fixed`/`capped_by`) instead of
degrading to "no acceptances"; `_parse_pip_audit` rejects empty, truncated, or
wrong-shaped `pip-audit` output; `_run_pip_audit_stdout` keeps a fixed argv with
no shell.

**Keying (audit question).** Findings are keyed by the pair `(package, id)`. A
shared advisory id is decided once per package — the accepted package passes, any
other package is a **MISMATCH** failure, never dropped or masked.

**Evidence.** `25` gate tests pass; against the real environment the gate reports
`18 advisories: 18 accepted, 0 blocking`; an empty allow-list yields `18
UNACCEPTED` (exit 1) with no `STALE`/`MISMATCH`, which is exactly what the CI
anti-vacuity step asserts.

---

## 4. Integration proof — the three changes combined

The three PRs were merged in a scratch branch (main untouched) to prove they are
correct *together*:

```
git checkout -b integ-proof-316-317-318 origin/main
git merge --no-ff ci-dependency-audit-gate   # #318  -> clean
git merge --no-ff 48e02e24                   # #316  -> clean (auto-merged ci.yml, docs/README.md)
git merge --no-ff 47e0a80e                   # #317  -> clean (auto-merged tests/unit/test_agent_board.py)
```

**Result: zero conflicts.** The two independent `tests/unit/test_agent_board.py`
edits (import block + the ~line-272 region) merged cleanly, and `ci.yml` carries
**both** #316's `governance-mutations (board referee 38 + Gate-5 fencing 17)` and
#318's `dependency-audit` job — non-overlapping regions, combined coherently
(`yaml.safe_load` parses the file).

Combined gates on the integrated tree (SHA `dad1dd9d`):

| Gate | Command | Result |
|---|---|---|
| Lint | `ruff check .` | **All checks passed** |
| Format | `ruff format --check .` | **952 files already formatted** |
| Types | `mypy src` | **Success: no issues found in 361 source files** |
| Board + gate + docs tests | `pytest test_agent_board.py test_dependency_audit_gate.py test_mutation_harness_anchors.py test_board_authority_option_b.py test_docs_integrity.py` | **273 passed** |
| Mutation campaign | `python scripts/board_referee_mutations.py` | **38/38 killed** |
| Dependency gate | `python scripts/dependency_audit.py` | **18 accepted, 0 blocking** |
| Full suite | `pytest -q` | **6389 passed, 39 skipped** |

No security boundary is breached by the combination: the board's live-verify
seam (`_gh_get` / `_git`) is untouched by #317/#318, and the dependency gate does
not import or mutate board state.

---

## 5. CI on the final heads

Required status checks on `main` (`branches/main/protection`):
`lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`,
`merge-base-guard (base == main)`; `strict: true`; no required reviews;
conversation-resolution disabled; admins enforced.

At the code heads recorded here — **#316 `48e02e24`**, **#318 `3cfaed2f`**,
**#317 `47e0a80e`** — all three required checks are **green** (verified via the
commit status API). Every other job shown is **advisory** (not required).

> The documentation commit that carries this file moves #316's head; the required
> checks must be re-verified at that final head (see §6).

---

## 6. What is proven, what is not

**Proven (in this environment).**
- The forged-record exploit is closed and the guard is load-bearing (38/38).
- `completed_released` no longer satisfies a prerequisite.
- The dependency gate is marker-aware and fails closed on bad input.
- The three changes integrate with **zero conflicts** and every combined gate is
  green, including the full suite.

**NOT_PROVEN / needs independent verification.**
- CI on the *exact* final head of each PR after its documentation commit (the
  required checks were green at the code heads above; re-confirm after the doc
  commit).
- Any end-to-end run against a live GitHub PR that is *actually* merged (the
  live-verify path is unit-tested against a stubbed seam and exercised against a
  stubbed `_git`; a real merged-PR round-trip was not run here).
- Merging the three PRs into `main` is **not** performed here; it is the
  owner's controlled final step, in the order #318 → #316 → #317 (all merge
  cleanly in that order as shown above).
