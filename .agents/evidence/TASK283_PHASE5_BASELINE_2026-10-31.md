# task-283 — Phase 5 baseline proof (full suite, independent checkout)

Date: 2026-10-31
Repo: `bot523h/nexus-ai-agent`
PR: #299 (`arena/ac3f046e-nexus-ai-agent`)

## Why this file exists

The closure adds guards to the queue-ownership / publication path, and the local
full suite is red for reasons that have nothing to do with this branch (optional
integrations and distribution metadata are absent from this sandbox). A claim
that "these failures are pre-existing" must be *demonstrated*, not asserted — and
reverting only `src/` is not sufficient proof, because the branch also changes
tests, scripts and a workflow.

## Method

Two independent checkouts, identical command, identical environment:

| | checkout | `PYTHONPATH` | command |
|---|---|---|---|
| A | this branch (working tree with all closure changes) | `/home/user/nexus-ai-agent/src` | `pytest -m "not slow" -q -p no:randomly --tb=no -rf` |
| B | `git worktree add --detach /tmp/base-tree e2080db46d562da5cb9d2fc90097da7b65b2fa86` (the real merge base of `main`) | `/tmp/base-tree/src` | same |

Both runs: `PYTHONHASHSEED=0`, sequential (no CPU contention), same interpreter
(`.venv/bin/python`, Python 3.11), same installed dependencies.

Both trees were confirmed to import their own sources:
`nexus_ai_agent.__file__ == <checkout>/src/nexus_ai_agent/__init__.py`.

## Result

| | checkout A (branch) | checkout B (`e2080db`) |
|---|---|---|
| passed | 5868 | 5775 |
| failed | 42 | 42 |
| skipped | 41 | 41 |
| duration | 452 s | 447 s |

**The set of failing node ids is identical** (`diff` of the sorted `-rf` lists
returns no differences):

* no failure exists in checkout A that does not also exist in checkout B — i.e.
  **no regression** from this branch;
* no failure is fixed by the branch, so no claim is made that it repairs
  unrelated red tests;
* the 93 extra passing tests are the closure's new coverage.

## Scope of what this proves

It proves the full-suite failure set is pre-existing at the real base SHA. It
does **not** claim those 42 are environmentally impossible — they are local
sandbox gaps (optional integrations and installed-distribution metadata absent),
and the CI jobs for this branch run the same selection green.
