# M17 — Open-PR backlog repair ledger (2026-10-31)

Mission: **NEXUS-M17-OPEN-PR-BACKLOG-REPAIR-01** (repo `bot523h/nexus-ai-agent`).
Goal: census every open PR, dispose of each one with evidence, safely repair the
repairable ones, and leave the rest with an honest, independently-verifiable
status. No merge, no force-push, no protection change, no approval fabrication.

Machine-readable companion: `M17_PR_REGISTRY_2026-10-31.json` (one record per PR).

## 1. Baseline

| Fact | Value |
|---|---|
| `main` at mission start | `87744dda90c7b6ea38100317afbd1f4c797768e6` |
| Open PRs | 44 (12 drafts) |
| Required contexts | `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)` |
| Protection | `enforce_admins: true`, `strict: true` |

`merge-base-guard` requires every PR's base to equal current `main`, so a PR must
be **synced** before it can be green; `strict` requires it to be **up to date** at
merge. That is why most PRs read `BLOCKED`/`BEHIND`/`DIRTY`.

## 2. Dispositions (all 44 PRs)

| Disposition | Count | Meaning |
|---|---|---|
| `REPAIRED_PUSHED` | 5 | safe additive repair pushed to the PR head; awaiting CI + human review |
| `CONFLICTING_STALE_REBASE_REQUIRED` | 24 | conflicts with main; needs the owner to rebase |
| `DRAFT_NOT_READY` | 11 | draft; author has not requested review |
| `SYNC_REQUIRED` | 2 | mergeable but behind main; needs a sync |
| `DRAFT_ACTIVE_OWNER_PROTECTED` | 1 | #298 (protected scopes, live lease) |
| `ACTIVE_FRESH_CI_RUNNING` | 1 | #299 (created this session) |

The two mergeable-but-blocked PRs (#296, #297) are counted under `REPAIRED_PUSHED`
because their only blocker was the defect/gap fixed here.

## 3. Repairs landed this session

All five were pushed to the existing PR head branch (no new PR, no force-push);
each ran the ownership referee (`agent_board.py check` → exit 0) immediately
before the push.

### #296 — `automation/debt-watchdog` → `05834fa3`
- **Defect (required gate):** `lint` and `lint-fast` failed. `ruff format --check`
  formats the Python fence inside `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-10-31.md`;
  one `return` line carried trailing-alignment padding ruff would strip.
- **Fix:** whitespace-only reformat of the quoted snippet (semantics unchanged).
- **Verified:** `ruff check .` → all passed; `ruff format --check .` → 866 files formatted.

### #294 — `arena/m16-pr189-repair` → `9b60307a`
- **Defect:** `CreativeGraph.edges()` chose its SQL endpoint column with
  `"src" if direction == "out" else "dst"`, so any malformed `direction`
  silently read the wrong endpoint (and a static analyzer flagged the f-string
  interpolation as a possible injection).
- **Fix:** map `direction` through a closed `{in,out} → {src,dst}` table and
  refuse anything else with a typed `ValueError` **before** the statement is
  built. Endpoint and relation values stay parameter-bound.
- **Disposition of the injection report:** false positive, now *proven* — only the
  closed literal set is ever interpolated as a column name; a refused direction
  never reaches a statement.
- **Verified:** 50 passed `tests/unit/test_creative_graph.py` (3 new adversarial
  tests), 14 passed boundary/identity tests, `mypy` clean.

### #295 — `arena/m16-pr218-extract` → `c641154d`
- **Defect (security):** `O_NOFOLLOW` guards only the *final* path component. A
  symlinked intermediate shard directory (`cas/sha256` → elsewhere, or the `cas`
  root itself) redirected the atomic `os.link` so `admit()` published bytes
  **outside** the artifact root while still reporting success — contradicting the
  module's own `AssetBoundaryError` contract.
- **Fix:** reject any symlinked component between the root and the target, both
  before `mkdir` and immediately before the publish link.
- **Verified:** 42 passed `tests/unit/test_asset_library.py` (4 new regressions:
  three escape paths refused, one legitimate nested admit not a false positive),
  `mypy` clean.

### #297 — `arena/m16-closeout-evidence` → `f142f2e9`
- **Defect:** none in code; the PR was simply `BEHIND` main (evidence-only).
- **Fix:** merge `origin/main` (clean, no conflicts).
- **Verified:** board schema suite 75 passed.

### #290 — `arena/nexus-durable-project-state-board-20261030-1028` → `d6437217`
- **Defect:** conflicted **only** in `.agents/board.json` (`CONFLICTING/DIRTY`), so
  the whole PR was unmergeable because of one coordination file.
- **Fix:** rebuild the board from current `main` and re-append this PR's own
  additions (`zone durable-project-state-lifecycle` + claim
  `task-282-durable-project-state-lifecycle`) — a pure addition with no lost
  records — then merge `main`. `git merge-tree` now reports no conflict.
- **Verified:** board schema suite 75 passed.
- **Note:** the registered id `task-282-durable-project-state-lifecycle` collides
  by number with the main-board `task-282`; the owner should renumber before merge.

## 4. Board coordination

- Registered and claimed `task-284-m17-open-pr-convergence` on
  `arena/m17-open-pr-repair` (generation 1) and pushed the claim immediately.
- **Task-id collision found and fixed:** the first registration used
  `task-282`/`task-283`, which are already taken by
  `task-282-durable-project-state-lifecycle` (PR #290) and
  `task-283-queue-ownership-publication-closure` (#299). Renumbered the campaign
  to `task-284/285/286`.
- Protected scopes never touched: `docs/README.md`, `OPERATION_TRUTH.json`,
  `.github/workflows/ci.yml`, `src/nexus_ai_agent/nagar/ibpc/` (owner #298).

## 5. Honest status

- Every repaired PR is **left open** for independent human review; none is claimed
  merge-ready.
- The 24 conflicting PRs are **not** blind-rebased: a mass rebase of stale branches
  would rewrite history and is an owner decision, not a repair campaign action.
- CI on the five repaired heads was **queued** at the time of writing; the local
  gates above are the reproducible evidence.

## 6. External observability (LangSmith)

The deployed `LANGSMITH_API_KEY` is a **tracing** key: it ingests runs
(`POST /runs` → 202) but cannot read projects (`GET /sessions` → 403
`Permission projects:read is not allowed for tracing keys`). The M17 campaign and
each repair were emitted as runs to the `nexus-m17-open-pr-repair` project via a
standalone HTTP helper outside the repository, adding no dependency to the tree.

## 7. Reproduction

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
make lint && make types && make test
```
