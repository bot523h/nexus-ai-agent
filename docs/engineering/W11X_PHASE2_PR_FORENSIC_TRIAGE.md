# W11X Phase 2 — forensic triage of the open pull requests

Status: measured on 2026-11-25 from a fresh clone. Base: `origin/main` @ `bc089847`.
Every number below is recomputed from git, not copied from a PR description.

## Method

```bash
git fetch origin --prune
git fetch origin "+refs/pull/*/head:refs/remotes/origin/pr/*"
gh api repos/bot523h/nexus-ai-agent/pulls?state=open   # 46 open, 45 with a pull ref
git diff --numstat origin/main...origin/pr/<N>          # what the PR adds vs its merge-base
git rev-parse origin/pr/<N>:<path>  vs  origin/main:<path>   # byte identity per file
```

A file that exists on `main` **and** has the same blob is already shipped. A file
that exists but differs means main has moved on (usually a later PR rewrote it). A
path absent on main is the only place unique content can hide — and even then the
capability may exist on main under a different module name, which is why each
verdict below names the module that supersedes it.

Verdicts: `SUPERSEDED_ON_MAIN`, `PARTIAL_SUPERSEDED`, `HARVESTED_IN_THIS_PR`,
`UNIQUE_CONTENT_PENDING`, `TEST_OR_DOCS_ONLY`.

## All open PRs

| PR | HEAD | merge-base | + | − | files | behind/ahead | src absent | src differ | verdict |
|---|---|---|---|---|---|---|---|---|---|
| #33 | `99429397` | `48f280c5` | 2353 | 158 | 40 | 1225/15 | 2 | 15 | `UNIQUE_CONTENT_PENDING` |
| #57 | `2f591618` | `a997aab5` | 2375 | 9 | 14 | 1402/2 | 5 | 1 | `UNIQUE_CONTENT_PENDING` |
| #58 | `bb06da1c` | `a997aab5` | 331 | 27 | 12 | 1402/2 | 0 | 10 | `SUPERSEDED_ON_MAIN` |
| #60 | `efea9bae` | `a997aab5` | 5837 | 19 | 29 | 1402/6 | 9 | 4 | `PARTIAL_SUPERSEDED` |
| #63 | `3e376bea` | `a997aab5` | 3710 | 40 | 35 | 1402/1 | 4 | 5 | `UNIQUE_CONTENT_PENDING` |
| #64 | `ab564d7c` | `85c502ed` | 8329 | 209 | 38 | 846/24 | 7 | 13 | `UNIQUE_CONTENT_PENDING` |
| #66 | `db18cf66` | `2cf22137` | 4342 | 286 | 37 | 1393/3 | 2 | 20 | `UNIQUE_CONTENT_PENDING` |
| #69 | `74980748` | `035a896d` | 24028 | 503 | 99 | 1384/18 | 13 | 25 | `UNIQUE_CONTENT_PENDING` |
| #73 | `1a35ec06` | `035a896d` | 7755 | 2 | 20 | 1384/5 | 0 | 4 | `SUPERSEDED_ON_MAIN` |
| #84 | `89e04c6a` | `2351f099` | 2422 | 8 | 15 | 1344/9 | 0 | 0 | `TEST_OR_DOCS_ONLY` |
| #89 | `2f0df4f1` | `2351f099` | 1677 | 80 | 7 | 1344/4 | 0 | 2 | `SUPERSEDED_ON_MAIN` |
| #92 | `40c11633` | `d52a2434` | 143 | 2 | 4 | 616/11 | 0 | 1 | `TEST_OR_DOCS_ONLY` |
| #97 | `3307d397` | `9145c5f2` | 997 | 1 | 17 | 760/4 | 3 | 9 | `PARTIAL_SUPERSEDED` |
| #99 | `9a862e26` | `6122c9bf` | 3191 | 212 | 34 | 1230/1 | 2 | 20 | `UNIQUE_CONTENT_PENDING` |
| #113 | `6a867a77` | `e5b326b2` | 2003 | 78 | 14 | 1294/8 | 1 | 7 | `UNIQUE_CONTENT_PENDING` |
| #114 | `6ca1b13b` | `cf8b7f66` | 7792 | 0 | 16 | 1077/4 | 12 | 0 | `UNIQUE_CONTENT_PENDING` |
| #115 | `f1987348` | `6122c9bf` | 1320 | 121 | 14 | 1230/5 | 1 | 10 | `UNIQUE_CONTENT_PENDING` |
| #117 | `4a4b30c2` | `e5b326b2` | 5245 | 193 | 41 | 1294/30 | 2 | 6 | `UNIQUE_CONTENT_PENDING` |
| #118 | `d5a205f4` | `e5b326b2` | 23786 | 353 | 52 | 1294/26 | 10 | 11 | `UNIQUE_CONTENT_PENDING` |
| #121 | `b4a59653` | `e5b326b2` | 4658 | 733 | 20 | 1294/2 | 4 | 9 | `UNIQUE_CONTENT_PENDING` |
| #122 | `8b8ba31d` | `cf8b7f66` | 3464 | 5 | 11 | 1077/17 | 0 | 1 | `TEST_OR_DOCS_ONLY` |
| #129 | `75f646a5` | `e5b326b2` | 713 | 12 | 6 | 1294/6 | 0 | 4 | `SUPERSEDED_ON_MAIN` |
| #135 | `f83b1283` | `e5b326b2` | 1207 | 11 | 8 | 1294/7 | 0 | 3 | `SUPERSEDED_ON_MAIN` |
| #137 | `e2a46144` | `e5b326b2` | 1554 | 10 | 9 | 1294/9 | 0 | 0 | `TEST_OR_DOCS_ONLY` |
| #140 | `25da5629` | `e5b326b2` | 1216 | 13 | 9 | 1294/5 | 0 | 3 | `SUPERSEDED_ON_MAIN` |
| #149 | `e865c146` | `e5b326b2` | 2322 | 467 | 51 | 1294/1 | 0 | 10 | `SUPERSEDED_ON_MAIN` |
| #150 | `5f84be4d` | `e5b326b2` | 6807 | 76 | 29 | 1294/2 | 7 | 14 | `UNIQUE_CONTENT_PENDING` |
| #157 | `00ccf903` | `5a228ea9` | 2977 | 106 | 24 | 1261/16 | 0 | 6 | `SUPERSEDED_ON_MAIN` |
| #193 | `d505d80a` | `48f280c5` | 8626 | 52 | 15 | 1225/6 | 0 | 0 | `TEST_OR_DOCS_ONLY` |
| #204 | `13078d79` | `d52a2434` | 486 | 54 | 4 | 616/9 | 0 | 1 | `SUPERSEDED_ON_MAIN` |
| #205 | `ff2792b7` | `df3c9d16` | 12330 | 29 | 43 | 1182/15 | 0 | 11 | `SUPERSEDED_ON_MAIN` |
| #230 | `9f572aae` | `cf8b7f66` | 5860 | 39 | 52 | 1077/8 | 0 | 0 | `TEST_OR_DOCS_ONLY` |
| #247 | `d9176f9b` | `9145c5f2` | 3817 | 49 | 33 | 760/18 | 5 | 7 | `HARVESTED_IN_THIS_PR` |
| #257 | `b057ef5f` | `cf16beaa` | 109 | 7 | 4 | 1027/2 | 0 | 2 | `TEST_OR_DOCS_ONLY` |
| #273 | `9d29722e` | `cd7e2e65` | 1898 | 1 | 19 | 753/12 | 5 | 3 | `SUPERSEDED_ON_MAIN` |
| #284 | `82d9be01` | `ecb07312` | 2733 | 5 | 13 | 723/5 | 4 | 2 | `SUPERSEDED_ON_MAIN` |
| #285 | `ce1ede4e` | `b3e20f93` | 14 | 6 | 1 | 720/2 | 0 | 0 | `TEST_OR_DOCS_ONLY` |
| #295 | `c641154d` | `87744dda` | 843 | 0 | 2 | 636/3 | 1 | 0 | `UNIQUE_CONTENT_PENDING` |
| #297 | `f142f2e9` | `87744dda` | 8375 | 4 | 5 | 636/3 | 0 | 0 | `TEST_OR_DOCS_ONLY` |
| #301 | `9cef96a7` | `d52a2434` | 3983 | 0 | 11 | 616/14 | 0 | 0 | `TEST_OR_DOCS_ONLY` |
| #313 | `ca7f9ea8` | `cf427874` | 59 | 1 | 1 | 535/1 | 0 | 0 | `TEST_OR_DOCS_ONLY` |
| #327 | `d5d5ead3` | `5ed6d4e1` | 14869 | 58 | 73 | 453/25 | 9 | 15 | `PARTIAL_SUPERSEDED` |
| #328 | `4f6ca1d1` | `5fe94512` | 2339 | 10 | 26 | 448/5 | 0 | 4 | `SUPERSEDED_ON_MAIN` |
| #329 | `56f34094` | `5fe94512` | 196 | 1 | 1 | 448/2 | 0 | 0 | `TEST_OR_DOCS_ONLY` |
| #362 | `3760a3d5` | `3ee52c4f` | 3189 | 131 | 18 | 2/3 | 1 | 6 | `UNIQUE_CONTENT_PENDING` |

## The sixteen PRs named in the mission

### #327 — feat(composition): integrate the NAGAR Creative Capability Composition Runtime into the canonical path

- **verdict**: `PARTIAL_SUPERSEDED`
- **why**: engine/contracts/verifier/license_lattice landed by #360 waves 10-11 under a typed API; executor/job/proof/packs-media target a superseded `MediaEngine` API and are not drop-in portable
- **measured**: `+14869/−58` over 73 files, 453 commits behind main, head `d5d5ead3`, merge-base `5ed6d4e1`; 9 added `src/` paths absent on main, 15 present-but-different.

### #247 — feat(creative): perception lane — measured silence/scene/loudness evidence (free offline FFmpeg)

- **verdict**: `HARVESTED_IN_THIS_PR`
- **why**: pack + parsers absent on main and now shipped; the PR's own 80 tests pass on main
- **measured**: `+3817/−49` over 33 files, 760 commits behind main, head `d9176f9b`, merge-base `9145c5f2`; 5 added `src/` paths absent on main, 7 present-but-different.

### #284 — feat(nagar): add read-only Causal Memory and bounded Director preview (task-278)

- **verdict**: `SUPERSEDED_ON_MAIN`
- **why**: `nagar/causal_memory/do_calculus.py` and `nagar/director/bounded_director.py` (DirectorBudget/DirectorPlan) landed by #360 wave 7
- **measured**: `+2733/−5` over 13 files, 723 commits behind main, head `82d9be01`, merge-base `ecb07312`; 4 added `src/` paths absent on main, 2 present-but-different.

### #273 — feat(nagar): P0 intent NEL semantic time causal contracts

- **verdict**: `SUPERSEDED_ON_MAIN`
- **why**: `nagar/p0/nel_algebra.py` (exact rational time + Allen algebra) landed by #360 wave 6
- **measured**: `+1898/−1` over 19 files, 753 commits behind main, head `9d29722e`, merge-base `cd7e2e65`; 5 added `src/` paths absent on main, 3 present-but-different.

### #328 — feat(studio): NAGAR Studio PWA over the verified read API (Draft)

- **verdict**: `SUPERSEDED_ON_MAIN`
- **why**: 15 of 26 changed files are byte-identical on main; the PWA + studio_web surface is already shipped
- **measured**: `+2339/−10` over 26 files, 448 commits behind main, head `4f6ca1d1`, merge-base `5fe94512`; 0 added `src/` paths absent on main, 4 present-but-different.

### #114 — feat(cockpit): Persian capability explorer and input-only command lab

- **verdict**: `UNIQUE_CONTENT_PENDING`
- **why**: Persian capability-explorer cockpit: all 16 files absent on main
- **measured**: `+7792/−0` over 16 files, 1077 commits behind main, head `6ca1b13b`, merge-base `cf8b7f66`; 12 added `src/` paths absent on main, 0 present-but-different.

### #63 — task-163: board-Git truth gate + scale-to-zero state tiers (#1 + #3)

- **verdict**: `UNIQUE_CONTENT_PENDING`
- **why**: opt-in Postgres/Redis state tiers (`stateful/*`) absent on main; SQLite WAL monolith remains the default
- **measured**: `+3710/−40` over 35 files, 1402 commits behind main, head `3e376bea`, merge-base `a997aab5`; 4 added `src/` paths absent on main, 5 present-but-different.

### #121 — feat(memory): retrieval truth — noise-ranked recall (hit@3 0.167) → guaranteed lexical+dense floor (1.000); fail-closed moderation; one context-window policy (task-203)

- **verdict**: `UNIQUE_CONTENT_PENDING`
- **why**: `memory/recall.py` + `retrieval/core.py` absent on main
- **measured**: `+4658/−733` over 20 files, 1294 commits behind main, head `b4a59653`, merge-base `e5b326b2`; 4 added `src/` paths absent on main, 9 present-but-different.

### #122 — fix(orchestration): H1 — route chat through the memory reader (task-206) + reachability/isolation contract (task-205)

- **verdict**: `TEST_OR_DOCS_ONLY`
- **why**: no new source module; only scripts/tests plus one edit to `orchestration/graph.py`
- **measured**: `+3464/−5` over 11 files, 1077 commits behind main, head `8b8ba31d`, merge-base `cf8b7f66`; 0 added `src/` paths absent on main, 1 present-but-different.

### #118 — W2 golden hardening: authority races, probe permits and independent proof (NOT VERIFIED)

- **verdict**: `UNIQUE_CONTENT_PENDING`
- **why**: 10-file `llm/gateway/` stack absent; main ships only a 23-line `__init__.py` + control-theory governor
- **measured**: `+23786/−353` over 52 files, 1294 commits behind main, head `d5a205f4`, merge-base `e5b326b2`; 10 added `src/` paths absent on main, 11 present-but-different.

### #97 — feat(vision): complete portrait and scene Vision packs

- **verdict**: `PARTIAL_SUPERSEDED`
- **why**: portrait/scene packs are on main (evolved by later PRs); only `packs/vision/*` is absent
- **measured**: `+997/−1` over 17 files, 760 commits behind main, head `3307d397`, merge-base `9145c5f2`; 3 added `src/` paths absent on main, 9 present-but-different.

### #57 — feat(research-v2): P1 CLAIM+LEASE + P2 INBOX/RECEIPT (task-160)

- **verdict**: `UNIQUE_CONTENT_PENDING`
- **why**: `domain/{inbox,lease}.py` + claim/lease adapters absent on main
- **measured**: `+2375/−9` over 14 files, 1402 commits behind main, head `2f591618`, merge-base `a997aab5`; 5 added `src/` paths absent on main, 1 present-but-different.

### #60 — M0 runtime integration: InstrumentedJobQueue + readyz factory + Q1–Q5 proofs (task-172)

- **verdict**: `PARTIAL_SUPERSEDED`
- **why**: `observability/slo_burn_calculus.py` landed by W10X; the M0 metrics/readiness instrumentation is absent
- **measured**: `+5837/−19` over 29 files, 1402 commits behind main, head `efea9bae`, merge-base `a997aab5`; 9 added `src/` paths absent on main, 4 present-but-different.

### #92 — fix(storage): honor SQLite defaults; preserve inherited release audit scope

- **verdict**: `TEST_OR_DOCS_ONLY`
- **why**: no added source module; a storage-defaults fix plus tests
- **measured**: `+143/−2` over 4 files, 616 commits behind main, head `40c11633`, merge-base `d52a2434`; 0 added `src/` paths absent on main, 1 present-but-different.

### #204 — fix(memory): resolve sqlite-vec extension fallback and add forensic checkup audit

- **verdict**: `SUPERSEDED_ON_MAIN`
- **why**: main's `memory/long_term.py` already carries the sqlite-vec capability probe
- **measured**: `+486/−54` over 4 files, 616 commits behind main, head `13078d79`, merge-base `d52a2434`; 0 added `src/` paths absent on main, 1 present-but-different.

### #257 — 🎨 Palette: Improve Telegram inline callback UX for language and agent selection

- **verdict**: `TEST_OR_DOCS_ONLY`
- **why**: bot handler UX edit plus a palette note; no new capability module
- **measured**: `+109/−7` over 4 files, 1027 commits behind main, head `b057ef5f`, merge-base `cf16beaa`; 0 added `src/` paths absent on main, 2 present-but-different.


## What this phase actually harvested

`#247` — the perception pack and its parsers, verified by **the PR's own 80 tests**
running against main:

```text
tests/unit/test_perception_parsers.py ....  60 passed
tests/unit/test_perception_pack.py    ....  20 passed
```

Shipped: `creative/packs/perception/{models,operations,__init__,pack.manifest.json}`,
`creative/perception/parsers.py`, plus registration in `packs/runtime.py`
(`COMPOSITION`), `studio/lifecycle.py` (`PACK_LIFECYCLE` / `PACK_EVIDENCE`) and
`nagar/sources.py` (declaration scan). `OPERATION_TRUTH.json` and the generated
docs were re-rendered: the runtime registry grows from 88 to 91 operations.

The PR's own `tests/architecture/test_perception_boundary.py` also ships, with
`enum` added to its stdlib allow-list because the merged W10X
`radiometric_scene_cut.py` classifies transitions with an `Enum`.

## What is *not* harvested, stated plainly

* **`#327` composition executor/job/proof** — 2 483 lines that import
  `MediaEngine`/`MediaOutcome`/`MediaEngineError` from `composition.engine`.
  Main's engine exports `CompositionEngine`/`CompositionExecution`/
  `CompositionExecutionError` (typed contracts, #360 wave 10). Porting it is a
  rewrite, not a harvest, so it stays open with this evidence rather than being
  claimed as done.
* **`#114` cockpit, `#63` state tiers, `#118` gateway stack, `#121` retrieval,
  `#57` inbox/lease, `#60` M0 instrumentation** — genuinely absent on main, each a
  large surface. They remain open and are listed as `UNIQUE_CONTENT_PENDING`.
* **`#328` PWA** — 15 of its 26 files are byte-identical on main; the remainder
  differ because main moved on. Nothing to harvest.

## Reproduction

```bash
python - <<'EOF'
import json, subprocess
prs = json.load(open("open_prs.json"))
def git(*a): return subprocess.run(["git", *a], capture_output=True, text=True).stdout
for n in (327, 247, 284, 273, 328, 114, 63, 121, 122, 118, 97, 57, 60, 92, 204, 257):
    ref = f"origin/pr/{n}"
    print(n, git("diff", "--numstat", f"origin/main...{ref}").count("\n"))
EOF
```
