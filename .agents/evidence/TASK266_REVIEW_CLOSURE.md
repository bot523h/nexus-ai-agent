# task-266 — review closure on PR #202

Two review findings on `scripts/agent_board.py` (the referee), both about the
open-PR source. Both were treated as valid, reproduced RED first through
`cmd_check` with every external source faked (hermetic: no network, no real
git — see `TASK266_REVIEW_CLOSURE_red_probe.py`), then fixed, then pinned by
tests **and** by the permanent mutation campaign.

## Finding-by-finding record

| # | Finding (reviewer) | Commit | Reproducing test | RED before | Result after | Thread |
|---|---|---|---|---|---|---|
| A | Open-PR listing read page 1 only (`agent_board.py:636`, CodeRabbit, Major): PR #101+ leases invisible while `check` still exited `0`. | `569c413` | `test_open_pr_heads_walks_every_page`; `test_open_pr_heads_fails_closed_when_a_later_page_is_unreachable`; `test_open_pr_heads_fails_closed_on_a_malformed_page`; `test_open_pr_heads_refuses_a_listing_beyond_the_page_bound`; `test_open_pr_heads_refuses_a_head_without_repository_identity` | Conflict on PR #101 → exit `0` (page 2 never requested); unreachable page 2 → exit `0` | exit `1`; exit `2` (unverifiable, never partial) | Addressed in code + tests; the thread is not resolved or hidden by us (no merge/close is performed from this branch) |
| B | PR head kept only `head.ref` (`agent_board.py:688`, inline, Major): a fork PR whose branch is `main` is resolved through *origin's* `main` — a different repository, commit and board — and can exit `0` despite a proven conflict on the real head. | `569c413` | `test_remote_branch_boards_reads_a_fork_from_its_own_repository`; `test_head_fetch_url_ignores_a_payload_clone_url`; `test_every_derived_fetch_url_stays_on_github`; `test_a_fork_head_that_cannot_be_fetched_is_unverifiable`; `test_two_forks_sharing_a_ref_name_are_two_sources`; `test_self_exclusion_is_by_identity_not_by_branch_name` | Fork PR with ref `main` → `+refs/heads/main:refs/remotes/origin/main` issued as the PR's source, exit `0`; two forks sharing a ref name → deduped to zero sources | exit `1` from the fork's own fetched ref; two forks fetched as two sources | Addressed in code + tests; thread untouched (no resolve/close) |

Nothing was deleted, weakened or skipped: the pre-existing referee tests were
re-pointed at the identity contract, and the campaign still enforces its 15 older
mutations alongside the 7 new ones.

## Adversarial proof (both findings)

* **RED → GREEN driver** — `TASK266_REVIEW_CLOSURE_red_probe.py`, 5 probes:
  pagination, page-2 unavailable, fork head via its own repository, no
  origin refspec for a foreign head, two forks sharing a ref name. All five
  were red before the fix and are green after it. The local board is deliberately
  clean w.r.t. the checked file so the exit code is decided by the PR-source
  path under test.
* **Mutation campaign** — `scripts/board_referee_mutations.py`, output in
  `TASK266_REVIEW_CLOSURE_mutation_campaign.txt`: **26/26 killed, exit 0, no
  survivor, no drifted mutant**, byte-identical restore.
  * The first run had 4 mutants reported `DOES NOT APPLY (source drifted)`
    (stale anchors from the refactor) — re-pointed at the current source; a
    drifted mutant proves nothing.
  * The first run also had a **survivor**:
    `fork_head_resolved_through_origin` fetched the fork and then read origin's
    same-named branch. The test asserted the fetch but not the read. The
    assertion now pins the `git show` ref as well, and the mutant dies.
* **Reversal** — every new guard was checked by removing it in isolation: the
  failure is an assertion naming the invariant, never an import or syntax error.

## Regression of my own, caught before pushing

The first version of the head type was a `@dataclass`. Two sibling test files
(`test_agent_board_active_in_review.py`, `test_agent_board_pr_visibility.py`)
load this CLI *by path* without registering it in `sys.modules`; `dataclasses`
introspects `sys.modules[cls.__module__]` for `KW_ONLY` annotations and raises
`AttributeError`, so **both files failed to collect**. It is a `NamedTuple` now,
which carries no such environment requirement. Three referee suites: 91 passed.

## Scope of the verification (gate ownership respected)

The single `gates_owner` is held by another agent (`ci-gates-steward-closure`,
branch `phase1-pr196-guard-20261009`, fencing `Makefile` and `.github/`), so this
branch did **not** run the full gate. `check` on every changed path returned 0
across 55 readable sources first. Locally: ruff clean (665 files), mypy clean
(289 files), 200 tests across referee, truth-doctor, docs-integrity and
scripts-boundary suites. CI on the pushed SHA is the authoritative full run —
recorded in the PR, per SHA.
