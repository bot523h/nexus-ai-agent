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

## Quarantined candidate (do not re-apply blindly)

The ten-axis production candidate `ac2b7d501a565da16f62bebc0dad0f2a3957fdf8` was removed
from the publish ancestry because PR #116 (W2), PR #113 (W1) and PR #96 (DR) held live
leases over shared files. It is preserved **outside** the branch history:

- tag `preserved-production-ac2b7d5` (local),
- stash `stash@{...} "preserved ac2b7d5 production candidate awaiting W1 W2 DR handoff"`,
- `ci-artifacts/mission-20260928/preserved-ac2b7d5.patch`
  (sha256 `10485146bbebb7601affcf3d68b81e37fece27de35feb081326b4692c3b5535c`).

Re-integration requires, in order: lease release/expiry → semantic reconciliation with the
W1/W2 owners → `scripts/probes/consent_generation_race.py --source-root <union>` must
exit **0** (the probe currently **fails** against the raw candidate: an extraction started
before `forget_user` re-persists the erased profile, source sha256 `8ac6aa7a…`) → fresh
preflight SUCCESS → push.
