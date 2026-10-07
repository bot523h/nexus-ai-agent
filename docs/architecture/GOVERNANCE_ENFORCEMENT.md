# Governance enforcement plane (law R19)

**Status:** living view · **Authority:** `scripts/governance_guard.py` ·
**Enforcing tests:** `tests/architecture/test_governance_enforcement.py`

---

## 1. Why this page exists

Law R18 closed the #143 wrong-base merge by making `base == main` a fail-closed
*decision* (`scripts/merge_base_guard.py`). It did not make the *enforcement of
that decision* observable. Three planes can disagree, and until R19 nothing in
the repository could tell:

| Plane | What lives there | Failure mode |
|---|---|---|
| Decision | `scripts/merge_base_guard.py` | the guard itself decides wrongly |
| Repository | `.github/workflows/ci.yml` | the required job exists but never runs the guard |
| GitHub | branch protection on `main` | the required check is un-required in settings |

Two of those three were demonstrably unguarded on `main` @ `6122c9b`:

1. **The CI-wiring assertion was vacuous.** `test_merge_base_guard.py` asserted
   only that the *string* `scripts/merge_base_guard.py` appears in `ci.yml` — and
   the workflow's own prose comment contains that string. Deleting the `run:`
   step left **all 12 R18 tests passing** while the required check kept
   reporting green without deciding anything. Reproduced on
   `main` @ `6122c9b` (mutation applied, suite run, tree restored).
2. **Retargeting bypassed R18.** Changing a pull request's base branch fires a
   `pull_request` event of type `edited`. GitHub's default activity types for
   `pull_request` are `opened`/`synchronize`/`reopened` — `edited` is not among
   them. A PR opened against `main`, allowed to go green, then retargeted to
   `arena/*` therefore **reused its previous green check** against the new base.

## 2. The declared policy

`scripts/governance_guard.py` holds the single declaration:

```python
REQUIRED_CONTEXTS = (
    "lint (ruff + mypy + version lockstep)",
    'test (pytest -m "not slow")',
    "merge-base-guard (base == main)",
)
BASE_CHANGING_PR_TYPES = ("opened", "synchronize", "reopened", "edited", "ready_for_review")
```

A required context is only meaningful if it is the **exact `name:`** of a CI job
(or the job key when the job declares no name). That equality is what the guard
pins; a renamed job means GitHub would require a context that can never report.

## 3. What is mechanically checked

`python scripts/governance_guard.py check-offline` (no network, stdlib only):

| Code | Invariant |
|---|---|
| GOV001 | the workflow is readable — otherwise `BLOCKED`, never green |
| GOV002 | the workflow parses to at least one job (the reader cannot silently empty) |
| GOV010 | every declared required context is produced by exactly one job |
| GOV011 | the merge-base context has a producer at all |
| GOV020 | that job actually executes `merge_base_guard.py check-event` |
| GOV021 | it passes both `--event` and `--base` |
| GOV022 | no job producing a required context carries `continue-on-error: true` |
| GOV023 | the job's `if:` does not exclude `pull_request` |
| GOV030 | the workflow re-runs on every base-changing PR activity type |

`python scripts/governance_guard.py check-live --repo bot523h/nexus-ai-agent`:

| Code | Invariant |
|---|---|
| GOV040 | `main` is protected — otherwise `VIOLATION` |
| GOV041 | the live required contexts **equal** the declared ones (missing *or* extra is drift) |
| GOV042 | required checks are enforced for `everyone` |
| GOV050–GOV054 | review requirement, conversation resolution, force-push, deletion, status-check detail: each readable, or recorded `UNKNOWN` |

Exit codes: `0` VERIFIED · `1` VIOLATION · `2` BLOCKED (a source was unreadable).
**An unreadable governance source is never a pass**, mirroring
ADR 0007 (the multisource referee reports unverifiable data as exit 2).

## 4. Verdict vocabulary

Only these words are permitted in a governance report:

`VERIFIED` · `VERIFIED_WITH_LIMITATIONS` · `HARDENED_BUT_NOT_COMPLETE` · `BLOCKED` · `DEFERRED`

"Zero bypass" is never claimed. The repository owner/admin is not readable from
the available token, and GitHub's own semantics let an owner bypass branch
protection; the guard records what it could *not* read instead of assuming it.

## 5. Live state — dated snapshot, not a live claim

The measured GitHub state is committed as
`.agents/evidence/GOVERNANCE_GITHUB_2026-10-07.json` (main @ `6122c9b`).
It is a **historical record**. Re-measure with the command below; do not read
the file as current truth.

```bash
python scripts/governance_guard.py check-live --repo bot523h/nexus-ai-agent --json
```

At capture time, with a token whose only granted permission was `metadata=read`
(`X-OAuth-Scopes:` empty, `X-Accepted-GitHub-Permissions: metadata=read`):

| Invariant | State | Source |
|---|---|---|
| A. `main` protection on | VERIFIED | `GET /branches/main` → `protected=true` |
| F. required checks required | VERIFIED | `protection.required_status_checks.contexts` |
| G. merge-base guard required | VERIFIED | context `merge-base-guard (base == main)` present |
| H. lint required | VERIFIED | context `lint (ruff + mypy + version lockstep)` present |
| I. test required | VERIFIED | context `test (pytest -m "not slow")` present |
| B. PR required before merge | UNKNOWN | `.../required_pull_request_reviews` → 403 |
| C. ≥1 approval | UNKNOWN | same → 403 |
| D. stale approvals dismissed | UNKNOWN | same → 403 |
| E. conversation resolution | UNKNOWN | `.../required_conversation_resolution` → 404 |
| J. strict / up-to-date branch | UNKNOWN | `.../required_status_checks` → 403 |
| K. force push blocked | UNKNOWN | `.../allow_force_pushes` → 404 |
| L. branch deletion blocked | UNKNOWN | `.../allow_deletions` → 404 |
| R. GitHub confirms the policy | VERIFIED_WITH_LIMITATIONS | A/F/G/H/I live; B–E, J–L unreadable |

Write probes at the same moment: `PUT /branches/main/protection` → `403`,
`POST /rulesets` → `403`, and the protection state re-read afterwards was
unchanged. `GET /rulesets` returned `[]` — no rulesets exist on this repository.
**Consequence:** this session could verify but not *change* GitHub's governance
settings. Applying invariants B–E, J–L requires a token with the
`administration` permission and is recorded as `BLOCKED`.

## 6. R18 and freshness are two invariants

R18 is about the **wrong target branch**: `base != main`. Freshness is about a
**stale base**: `base == main` but behind `main`'s head. They are separate:

* `merge_base_guard.py check-pr` reports a stale base SHA as `REBASE_REQUIRED`
  and does **not** fail the gate — a stale base is a rebase signal, not a
  wrong-base attack.
* Freshness is enforced by GitHub, not by a local guard: `strict` required
  status checks (branch must be up to date) and/or a merge queue. Turning the
  local guard into a distributed merge coordinator would duplicate an authority
  GitHub already owns. Invariant J above is the witness for that setting; it is
  currently `UNKNOWN`.

## 7. What this page does *not* claim

* It does not claim that an admin cannot bypass branch protection.
* It does not claim review, freshness, force-push or deletion settings are
  correct — they are `UNKNOWN` from the available token.
* It does not claim the live state of `main` today; §5 is a dated snapshot.
* `check-offline` proves the *binding* between the declared contexts and the CI
  jobs. It cannot prove GitHub still requires them — that is `check-live`, and
  it is only as trustworthy as the token it runs with.

Persian summary: R18 تصمیم «پایه باید main باشد» را fail-closed می‌کند؛ R19
اثبات می‌کند که آن تصمیم واقعاً در CI اجرا می‌شود و واقعاً در GitHub لازم است.
منبع ناخوانا هیچ‌وقت سبز نیست: BLOCKED.
