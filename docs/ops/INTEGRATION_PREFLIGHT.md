# Integration & Publication Preflight

*Living runbook — update in the same PR that changes the guard. ADR-0007 records the
decision; this file records how to run it and what was actually observed.*

## What publication approval means now

`git push` publishes the **entire outgoing commit range**, including commits whose changes
are later reverted, and including merge commits. Approval is therefore computed from:

1. the **observed remote frontier** — `git ls-remote --heads origin 'main' 'arena/*'`,
2. the **full outgoing range** — every commit reachable from local HEAD but not the
   frontier's tip of this branch, with the union of all changed paths (`diff-tree -m`),
3. **owner-head lease precedence** — a lease is believed only from its owner's current
   head; inherited copies on other branches cannot release or move it.

## Commands

```bash
# before every push (and before proposing integration):
python scripts/agent_board.py preflight --branch "$(git branch --show-current)"

# exit codes: 0 = SUCCESS (observed range is publishable),
#             1 = REJECTED (a foreign live lease covers an outgoing file),
#             2 = NOT_VERIFIED (a premise is missing — fix it, do not retry blindly)
```

`check --files ...` stays useful while *working*, but it is advisory: it knows nothing
about the remote frontier or history depth.

## Failure classes and the required response

| Signal | Meaning | Response |
|---|---|---|
| `shallow_history` / `grafted_history` | outgoing range cannot be enumerated completely | `git fetch --unshallow origin` (or remove grafts) and re-run |
| `fetch_push_destination_mismatch` | push would land somewhere other than the observed fetch URL | fix `remote.*.url` / `remote.*.pushurl` |
| `remote_moved_during_fetch` / `remote_frontier_changed` | the frontier is a moving target | re-run; if persistent, coordinate on the board |
| `owner_head_missing` | a live inherited claim whose owner head is absent | explicit owner reconciliation on the board; never treat as free |
| `invalid_lease_time` / `invalid_exclusive_paths` / `invalid_active_identity` | remote board data is ambiguous | fail closed; calibrate only with reviewed status rules |
| `REJECTED` + conflict rows | foreign live lease covers an outgoing file | do not push; request handoff or wait for verified expiry, then re-run |

## Observed history (append-only)

- **2026-09-28 (branch `arena/01a0e742-nexus-ai-agent`)** — first live runs:
  1. fresh sandbox clone → `NOT_VERIFIED ['shallow_history']` (guard refused to judge a
     truncated history; fixed by `git fetch --unshallow`, no code bypass);
  2. after unshallow → `NOT_VERIFIED` with five legacy metadata records
     (`active_in_review_PR33`, `done_partial_residue_queued`, `queued_reserved_for_B`,
     two live leases with empty `exclusive_paths` lists); calibrated explicitly in
     `scripts/agent_board_remote.py` instead of permissive parsing;
  3. after calibration → `SUCCESS` with the observed frontier bound to `frontier_sha256`.
  Evidence: `ci-artifacts/mission-20260928/preflight-initial.json` (workspace, git-ignored).
- **2026-09-28 later (sandbox restore incident)** — the sandbox was rebuilt as a fresh
  shallow clone of `main` with the previous worktree files laid on top. Consequences,
  verified live: the local branch was re-synced to the remote tip with **zero** worktree
  drift; the quarantine artifacts of the ten-axis candidate (local tag
  `preserved-production-ac2b7d5`, named stash, and the git-ignored patch) were **lost**,
  and the candidate commit object exists on **no** ref anywhere (`git cat-file` fails
  after a full unshallow fetch). The candidate is unrecoverable from git; see
  `docs/audits/2026-09-28-hardening-proof.fa.md` §A/§E.
- **2026-09-28 (guard CI repair)** — the guard's own CI job failed on pytest 9.1.1 with
  exit 4: it installed only `pytest`, while `tests/conftest.py` imports the application
  package and the repo's pytest config sets `asyncio_mode` (needs `pytest-asyncio`).
  Fixed by installing `pytest-asyncio` and running the two self-contained board modules
  with `--noconftest`; reproduced locally in a clean venv before publishing
  (`40 passed` → `49 passed` with the adversarial round).

## Adversarial coverage (hardening-proof round, all executable)

`tests/unit/test_agent_board_remote.py` now pins, on disposable real Git repositories:

| Scenario | Verdict pinned |
|---|---|
| file changed then reverted inside the outgoing range | REJECTED |
| old commit touched a forbidden file (kept) | REJECTED |
| local board declares the peer released; remote owner still fences | REJECTED |
| local remote-tracking refs deleted; lease lives remotely | REJECTED |
| remote unreachable / fetch fails | NOT_VERIFIED |
| shallow (incomplete) history | NOT_VERIFIED |
| rewritten local history against the published tip (force-push shape) | NOT_VERIFIED |
| file introduced through a merge commit | REJECTED |
| cherry-picked forbidden change | REJECTED |
| local quarantine tag on an unpushed commit | SUCCESS (tags are never published by a branch push) |
| post-push state (empty outgoing range) | SUCCESS |
| remote frontier moves during observation | NOT_VERIFIED |
| fetch URL ≠ push URL, detached HEAD, foreign/absent branch | NOT_VERIFIED |

## Quarantined candidate — SUPERSEDED RECORD

The earlier claim that `ac2b7d5…` was preserved as a local tag, a named stash and a
hashed patch **no longer holds** (sandbox restore; artifacts lost; object never pushed).
The candidate cannot be merged because it no longer exists. If the ten-axis work is
redone, it must be rebuilt by its owners after the W1/W2/DR handoffs, must incorporate
the privacy-invalidation fix (see the consent probe), and must pass
`scripts/probes/consent_generation_race.py --source-root <union>` with exit 0 plus a
fresh preflight SUCCESS before publication.
