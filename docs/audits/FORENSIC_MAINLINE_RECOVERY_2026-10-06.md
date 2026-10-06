# Forensic Mainline Recovery — 2026-10-06

**Status:** `HARDENED_BUT_NOT_COMPLETE`

**Scope:** Live-truth audit of `bot523h/nexus-ai-agent`, current `main`, open pull requests, governance board, architecture boundaries, release metadata, and reproducible local evidence. No PR was merged, closed, deleted, rebased, or force-pushed by this audit.

## 1. Live truth

| Item | Exact evidence |
|---|---|
| Repository | `bot523h/nexus-ai-agent` |
| Current main at audit start | `5a228ea9a114363f6b77a4becd0681eeab3527a2` |
| Audit branch | `arena/forensic-mainline-20261006` |
| Delivered audit head | `28ca82f4daf92dc3b73c300f381404f645c19630` |
| Open PR count | `78` via GitHub REST `/pulls?state=open&per_page=100` |
| Working tree after delivery | clean; branch tracks and equals remote head |
| Board | schema 2, `updated_at=2026-10-05T07:43:54Z`; many historical/expired entries remain; the board is not a current PR disposition ledger |
| Gates owner | the board records the former `ci-gates-steward` as `completed_released`; no current live gates owner was established by this audit |

## 2. Open PR forensic triage

The following classifications are evidence-backed for the mission’s specifically named PRs. They are dispositions, not merge/close commands.

| PR | Live truth | Classification | Disposition | Evidence / next action |
|---:|---|---|---|---|
| #160 | base `5a228ea9a114363f6b77a4becd0681eeab3527a2`, head `a870126b404d1e45443842443733fa416956815c`, open, non-draft, `mergeable_state=clean` | `CANONICAL_NOW` for repository-side preflight only; `BLOCKED_EXTERNAL` for production proof | `ADAPT` | Scope is `.github/workflows/maintenance.yml`, `.agents/board.json`, and a 309-line contract test. It does not establish real production backup credentials, object identity, restore, or measured RPO/RTO. Keep the production claim explicitly limited until external evidence exists. |
| #161 | base `5a228ea9a114363f6b77a4becd0681eeab3527a2`, head `c90450bf5ac5b9785d906774ec4a8ead4fda037c`, open, non-draft, `mergeable_state=unstable` | `HUMAN_DECISION_REQUIRED` | `HUMAN_DECISION` | Adds ADR-0008 plus 279-line executable contract and board/docs changes. Review against the already canonical identity model before adoption; do not create a second identity authority. |
| #162 | base `5a228ea9a114363f6b77a4becd0681eeab3527a2`, head `918c51eb7135de6f703d0c8e945ea00001a69101`, open draft, `mergeable_state=clean` | `SUCCESSOR_NEEDED` | `DEFER` | Adds 47 changed paths / 9,325 insertions, including a new `src/nexus_ai_agent/nagar/` cognition system. This is the kind of platform-wide abstraction the mission explicitly says not to introduce without demonstrated failure. It is not in main and must not be treated as canonical implementation. |
| #163 | base `5a228ea9a114363f6b77a4becd0681eeab3527a2`, head `2c0b5ec58c49f190fd9b2fc86ad57cd5e53bd9bf`, open draft, `mergeable_state=clean` | `HISTORICAL_REFERENCE` | `HISTORICAL` | Adds an audit/sentinel report plus the same cognition slice as #162 (48 changed paths / 9,538 insertions). Its evidence can inform a human decision, but it cannot establish that #162 is implemented, integrated, verified, or production-proven. |

The repository also contains a very large historical branch/PR topology. No mass action was taken because branch existence is not evidence of canonicality and deletion/merge/close decisions remain human governance actions.

## 3. Already solved on current main

- Canonical command/capability contract and `CommandBus` boundary are enforced by architecture and unit tests.
- Durable in-process job queue owns job state; provenance is explicitly observer-only and fail-closed passport logic exists.
- Pack purity, render-lane process ownership, import boundaries, surface isolation, and data-only manifests have executable guards.
- Current source passes `ruff` and `mypy` on this checkout.
- Current architecture/governance/docs suite passed: `213 passed` including `tests/architecture`, board tests, docs integrity, and release lockstep.

These are repository-side proofs, not production proofs.

## 4. Changes delivered by this audit

### Checkpoint 1 — ownership

- Task claimed: `task-135-lockstep-residue`
- Commit: `0ca529af55ce84b8e20ede3ea39525acbe776394`
- Pushed: YES
- Remote verified: YES
- Change: board claim committed and pushed before implementation.

### Checkpoint 2 — README truth

- Commit: `aa097ac3cf5a1f835f810cc645c5e90b2f4da938`
- Pushed: YES
- Remote verified: YES
- Change: corrected the two current user-facing `v3.12.0` labels to `v3.13.0`.

### Checkpoint 3 — executable release guard

- Commit: `c7d1ae26f6fccef7cc7b042e409ed81b2467b229`
- Pushed: YES
- Remote verified: YES
- Change: `scripts/check_version_lockstep.py` and its unit test now enforce the README canonical Version label in addition to `VERSION`, `pyproject.toml`, and the newest released `CHANGELOG` heading. An adversarial stale-README fixture proves the guard fails when the label drifts.

### Checkpoint 4 — Continuum refresh

- Commit: `28ca82f4daf92dc3b73c300f381404f645c19630`
- Pushed: YES
- Remote verified: YES
- Change: `.nexus/continuum.json` refreshed from the clean checkout. Snapshot now records exact step `c7d1ae26f6fccef7cc7b042e409ed81b2467b229`, `3032` measured test items, and Python `3.12.3`. `python -m nexus_ai_agent.cli continuum verify` passes after commit.

## 5. Test proof

| Command | Result |
|---|---|
| `python -m pytest -q tests/architecture tests/unit/test_agent_board.py tests/unit/test_docs_integrity.py` | `207 passed` before release-guard extension |
| `python -m pytest -q tests/architecture tests/unit/test_agent_board.py tests/unit/test_docs_integrity.py tests/unit/test_version_lockstep.py` | `213 passed` after changes |
| `python -m ruff check src tests` | passed |
| `python -m mypy src/nexus_ai_agent --no-incremental` | `Success: no issues found in 255 source files` |
| `python scripts/check_version_lockstep.py` | passed; all release declarations `3.13.0` |
| `python -m nexus_ai_agent.cli continuum verify` | passed after snapshot commit |

## 6. Mutation/adversarial proof

- Release guard fixture with mismatched `pyproject.toml` and `CHANGELOG` remains red.
- New stale-README fixture is red until the README value is corrected.
- Existing architecture suite includes negative boundary tests for authorization denial, duplicate/idempotency paths, malformed input, provenance tampering, pack impurity, render process escape, and import bypasses.
- No claim is made that this audit proves production backup/restore, external providers, or deployment readiness.

## 7. Current main gaps / real backlog

### NOW

1. Establish a current human-owned gates owner and reconcile the board’s historical claims with the 78 live PRs.
2. Produce a machine-generated all-PR disposition matrix from exact base/head SHAs, changed files, checks, reviews, and board ownership.
3. Review #160 for repository-side correctness while keeping production backup status `BLOCKED_EXTERNAL` until restore evidence exists.
4. Resolve the #161 identity-arbitration question against the existing canonical identity model before any adoption.
5. Mark #162/#163 as non-canonical draft cognition work unless a reproducible current-main failure requires a bounded successor.

### NEXT

1. Refresh living architecture documents whose `Verified against` SHA is older than current main; do not rewrite dated audit records.
2. Add a reproducible PR-to-board overlap report to the current governance workflow.
3. Add exact-head check evidence and review-state capture to the final PR disposition ledger.
4. Run the full project gate only under the designated gates owner, not concurrently from this audit.

### LATER

1. Any broad unified cognitive fabric / NIK abstraction.
2. Real production backup/restore evidence requiring credentials and external infrastructure.
3. Branch deletion, PR close/merge, history rewrite, or force-push decisions.

## 8. Production limitations

Repository-side tests, local artifact checks, and CI metadata do not prove production. Missing evidence includes production credentials, external object-store identity/retention, isolated restore, deployment smoke against the exact deployed SHA, and measured RPO/RTO. Status is therefore not `VERIFIED`.

## 9. Final verdict

`HARDENED_BUT_NOT_COMPLETE`

The mainline was not mass-merged or mass-closed. Safe, evidence-backed release metadata defects were corrected and each completed unit was committed, pushed, remote-verified, and left clean. Remaining architectural and production decisions are explicitly classified rather than fabricated as complete.
