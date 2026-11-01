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

## Round 2 — three further findings, on this evidence artifact

The reviewer's next pass (after the closure commit) raised three findings, all on
`.agents/evidence/TASK266_REVIEW_CLOSURE_red_probe.py` — i.e. on the proof, not on
the fix. All three were valid and are fixed in `69e4b8fe961b7d9e5f1be52850c387c1ad5e3122`.

| # | Finding | Fix | Result |
|---|---|---|---|
| A2 | `REPO` hardcoded to `/home/user/nexus-ai-agent` — the evidence cannot be reproduced in another checkout | `REPO = Path(__file__).resolve().parents[2]` | the probe loads `scripts/agent_board.py` from wherever the repository is checked out |
| B2 | fixed `/tmp/probe` fixture path — a symlink planted there would be followed by `write_text` (CWE-59/377) | `tempfile.TemporaryDirectory(prefix="task266-red-probe-")` (random name, mode 0700, removed on exit) | no shared, predictable write target anywhere in the probe |
| C2 | the two-fork probe passed on *two fetch attempts* regardless of the exit code — it could have accepted a referee that treats an unreadable fork as clean | split into three outcome-asserting fixtures (below) + two permanent `cmd_check`-level tests + a new mutant `failed_fork_fetch_counted_as_readable` | the probe now fails if the verdict is wrong, not just if the fetch loop is short |

The third finding is the important one: it is the same defect class as the original
findings — *asserting the mechanism instead of the outcome*. The rewritten fixtures:

* **B2a** — both forks readable → exit 1, **both** fork boards read (asserted on the
  `git show` refs, not just on the fetch refspecs).
* **B2b** — nothing conflicts, one fork unreachable → **exit 2**, the unreadable head
  named in the output, and no fallback to `origin`'s same-named branch.
* **B2c** — a proven conflict in a readable fork *and* an unreadable fork → **exit 1**
  by deliberate design (a confirmed conflict outranks an unreadable source; hiding it
  behind exit 2 would be worse), with the unreadable source still surfaced.

Writing B2b first asserted exit 2 for the *conflicting* fixture and reported RED —
the probe was wrong, not the code: `cmd_check` documents and prints this precedence
("a *proven* conflict is exit 1 even when another source is unreadable"). Both
behaviours are now pinned, as decisions rather than accidents.

Permanent rails added for the same class (`tests/unit/test_agent_board.py`):
`test_an_unreadable_fork_head_makes_the_verdict_unverifiable` and
`test_a_proven_conflict_survives_an_unreadable_fork_head`. Both were checked against
the real file: with the "failed fetch still counts as readable" mutation applied they
fail with assertion errors naming the violated invariant; with the file restored they
pass. The mutation campaign is **27/27 killed, exit 0, no survivor, no drift**.

## Round 3 — one finding, the same class a third time

| # | Finding | Fix (commit `0953b1c2a6858af6d480b3367da8d80d9ec47ecf` on this branch) | Result |
|---|---|---|---|
| D3 | `len(foreign) >= 2` is satisfied by two fetches of the *same* fork, so B2a could report success while `forkb` was never fetched | the assertion now requires each fork's own repository URL among the fetch targets and names the missing ones | probe reports `1 URL(s) — missing https://github.com/forkb/...` when the code dedupes by ref name, and two URLs when it is correct |

Falsified against the real file before being trusted: with `key = head.ref` applied
to `scripts/agent_board.py`, the probe goes RED (five findings, the missing URL named);
with the file restored it is green. A count is not evidence — each source has to be
named. This is the third finding of the same shape (`A`, `B2`, `D3`), which is worth
saying plainly rather than reporting as three unrelated incidents.
