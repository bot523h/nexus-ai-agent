# NEXUS Engineering Contract

Permanent project-root governance for how work is designed, changed, and verified.
This file is not a session note. It survives the agent that wrote it.

Coordination (who may edit which path, leases, gates ownership, branch identity)
lives in [`AGENTS.md`](AGENTS.md) and `.agents/board.json`. If the two documents
appear to disagree:

- **file ownership, leases, and the gates owner** are decided by `AGENTS.md`;
- **how owned work is designed and verified** is decided by this contract.

Neither document authorises skipping the other.

## 1. Repository-first source of truth

Evidence priority, highest first:

1. the working tree and the tests that execute against it
2. `git status`, `git diff`, `git log`
3. CI artifacts and command output captured in the same change
4. this contract, `AGENTS.md`, and `.agents/board.json`
5. chat reports from any agent, including the author of the change

A report is not proof. A green sentence without the command and the observed
result is not evidence. Do not fabricate commits, CI, coverage, or GitHub state.

## 2. Research before implementation

Before changing behaviour, read the code that already owns the behaviour, the
tests that pin it, and the board fences that bound it. Measure the current
failure (a stub reply, a swallowed exception, a leaked engine) instead of
redesigning from the task title.

## 3. Five-solution rule

For any non-trivial design choice, name at least five options, reject the ones
that violate a fence, a security invariant, or an existing contract, and record
why the chosen option is the smallest change that meets the evidence. Do not
ship the first idea that compiles.

## 4. Evidence-based architecture selection

Prefer the pattern the repository already proved (shared engine, bind at
startup, sync database work off the event loop, handlers as testable closures).
A new abstraction must pay for itself with a failing test the old shape cannot
express. Do not invent a distributed protocol when the process model is one bot.

## 5. Security-first design

Owner-only and destructive commands fail closed. Untrusted input is validated
before any write or network call. Callbacks do not grant privilege. Errors
returned to users do not echo secrets, raw exception chains, or other users'
data. Authorization is checked in the handler that faces the user, not only in
a comment next to a stub.

## 6. Concurrency and data

State the consistency goal before writing a scheduler: for advertisements and
scheduled posts the goal in this codebase is **at most once per process**, not
distributed exactly-once. Claim the row before sending. Normalise naive SQLite
datetimes to UTC before comparing them. Bound loops, caps, and text length.
Cancel tasks on shutdown. A restart must restore persisted pending work without
double-sending work already claimed.

## 7. Contract-driven tests

Every behaviour change lands with a test that fails if the behaviour is
reverted. Tests assert the contract (persisted row, denied caller, restored
schedule), not a private implementation detail that can be gamed. Architecture
tests that claim reachability must observe the composition root, not a comment.

## 8. No test cheating

Do not weaken an assertion, skip a failing test, or special-case the suite to
go green. Do not delete a regression test to make a stub look wired. If a test
cannot run, say so and name the missing dependency.

## 9. Minimum safe architectural change

Touch the fewest files that make the behaviour true. Do not refactor neighbours.
Do not edit a path another live lease owns — register alongside it, or defer,
rather than rewriting the leased file. A workaround that exists only because of
a fence must name the fence and the deletion condition.

## 10. No blind refactoring

A drive-by rename, a new framework, or a "while I was here" cleanup is out of
scope unless a test requires it. Preserve public behaviour that existing tests
already pin.

## 11. Multi-agent ownership

Claim before coding. Push the claim. Run
`python scripts/agent_board.py check --files … --branch <you>` before pushing
code. One owner per exclusive path. Do not release, retitle, or shrink another
branch's live lease. Shared coordination files (`.agents/board.json`) are edited
by appending state, not by dropping other agents' cards.

## 12. Evidence-based verification

The change description lists the commands that were run and the counts they
printed. Diagnostics (`ruff check` on touched files, targeted `pytest`) are
always allowed. The full gate (`make lint`, `make types`, `make test`) belongs
to the active gates owner. If that role is vacant or fenced, say "deferred"
instead of implying the full gate ran.

## 13. Production readiness

A command that tells the user it did something must have done that thing.
Simulated success is a defect. Startup and shutdown are part of the feature,
not a follow-up. Persistence that does not survive a restart is not persistence.

## 14. Adversarial self-review

Before calling work done, try to break it: non-owner, empty input, past and
far-future times, double delivery, restart, missing `language_code`, callback
with unexpected data, unbound bot, and the existing regression tests for
neighbouring commands. Record what was tried and what remains a risk.

## 15. No fabricated verification

If GitHub, CI logs, or a dependency is unavailable, the local evidence stands
and the remote gap is named. Do not invent a pull-request number, a green
check, or a commit that `git log` does not show.
