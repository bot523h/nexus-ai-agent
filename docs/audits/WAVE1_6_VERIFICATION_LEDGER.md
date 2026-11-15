# Waves 1–7 verification ledger — branch `arena/fd837e50-nexus-ai-agent`

Every number below was measured on this branch, at the revision named next to it.
Nothing is projected from a plan. The things this environment **could not**
measure are listed as such in §8 instead of being filled in.

Branch: `arena/fd837e50-nexus-ai-agent` · base: `main` at `deab2b3e`

---

## 1. What this branch is — and what PR #343 is not

**Corrected relationship.** An earlier revision of this file was titled
"Waves 1–6 verification ledger (PR #343)". That title was wrong and is retracted:
this branch is *not* PR #343, and the waves below are *not* what PR #343 contains.

| Thing | Measured value |
|-------|----------------|
| This session's branch | `arena/fd837e50-nexus-ai-agent` (the only branch this session can push to) |
| Base | `main` at `deab2b3e` |
| PR #343 | `#343`, state `OPEN`, `mergeable: MERGEABLE`, base `main` |
| PR #343 head branch | `arena/750b78f5-nexus-ai-agent` at `7189a8a` — **a different branch** |
| PR #343 title | "Waves 2–3 integration — VERITAS/CHRONOS/HYPERION/MORPHEUS + ATLAS lab, AETHEL builtin packs, P0 evidence" |

Two separate inaccuracies, both stated here rather than smoothed over:

1. The PR title advertises **Waves 2–3**. The commits on its branch are Waves 4–6,
   the I1/I2 hardening checkpoint and this closeout's Wave 7 (§3). The title
   describes neither its own branch nor this one.
2. Work done in this session cannot reach PR #343: pushing there would need a push
   to `arena/750b78f5-nexus-ai-agent`, which this session is not allowed to do. The
   three *commits* of that branch that matter here were cherry-picked instead (§2.2).

## 2. The full history chain

### 2.1 Size, measured (not estimated)

```bash
git rev-list --count deab2b3e..HEAD                  # 1507
git rev-list --count --first-parent deab2b3e..HEAD   # 54
git rev-list --count da3e5ed7..HEAD                  # 31
```

(Measured at the commit that carries this sentence: every later documentation commit
adds one to the total and to the window, and none adds behaviour. 19 of the 31 are the
pre-closeout waves, 12 are this closeout.)

The **diff** is smaller than the history and easier to review:
`git diff --stat deab2b3e..HEAD` reports **512 files, +124 220 / −2 841**. §2.4 is a
reading guide for exactly those files.

The directive issued for this closeout expected **161 commits**. No measurement
reproduces that figure: not the total (1503), not the first-parent count (50), not
the wave window (27). The three real counts are given above; 161 is not quoted
anywhere else in this file. The 1503 is dominated by 1453 commits that a previous
session merged in from parallel branches — they are part of the branch's history and
are not re-litigated here.

### 2.2 The three commits taken from the PR branch

`arena/750b78f5-nexus-ai-agent` carried three commits that did not exist on this
branch. They were **cherry-picked** (no merge, no history rewrite), so their
content is present here under new SHAs:

| Original (PR #343 branch) | Cherry-picked here | Subject |
|---|---|---|
| `10c14e4` | `df1ea42` | `ci(test): raise the pytest timeout 30m -> 45m on the suite itself` |
| `5a80116` | `f2c4db4` | `test(morpheus): make P53 nodal-equilibrium assert LAPACK-build stable` |
| `7189a8a` | `64e1392` | `ci(migrate-postgres): pin runner to ubuntu-24.04 (PG16 client) + annotate restore-atomicity failures` |

Three commits that already existed only here were preserved unchanged — nothing was
reset, rebased or force-pushed to make room: `d1fb569`, `4bcee33`, `40cf83c`.

### 2.3 The wave window, commit by commit

`483403d..HEAD` — 31 commits. Everything in §3–§7 is one of these.

| # | SHAs (oldest first) | Wave | What it is |
|---|---|---|---|
| 1 | `483403d` | pre | `fix(morpheus)`: annotate `arr` so `frombuffer`/`reshape` type-check everywhere |
| 2 | `299e931` | 1–3 | bounded subprocess budgets, fail-closed media verification, reconciler CLI |
| 3 | `0e6fabc` | 4 | non-destructive port of the B1–B4 audio kernels (cinema kernels untouched) |
| 4 | `fa4d69e` | 5 | the six NAGAR engines registered as capabilities |
| 5 | `6c62064` | 6 | authenticated Studio render submission over the canonical chain |
| 6 | `3b3b2f6` | 6 | pin the engine-capability axis in the runtime composition invariant |
| 7 | `fb6eae7` | 6 | pin the Studio surface as one read + one submission route |
| 8 | `7e78726` | — | the (then misleading) ledger this file replaces |
| 9 | `b8d3a69` | — | legacy background-task flow in one event loop |
| 10 | `d1fb569` | — | realign the local suite with the landed migration head and operation truth (preserved) |
| 11 | `4bcee33` | — | enrol the Studio web and session lanes in the route inventory (preserved, and the revision the ATLAS evidence in §8.1 is bound to) |
| 12 | `40cf83c` | — | resolve the mux encoder through the one FFmpeg ladder (preserved) |
| 13 | `df1ea42` | 1 | cherry-pick `10c14e4` |
| 14 | `f2c4db4` | 1 | cherry-pick `5a80116` |
| 15 | `64e1392` | 1 | cherry-pick `7189a8a` |
| 16 | `51242b1` | 2 | morpheus: validate mux inputs before resolving the encoder |
| 17 | `3ff7e71` | 2 | chunked CI-parity runner with a proven collection equivalence |
| 18 | `c55a8a0` | 3 | scene flow block matching evaluated per shift, not per pixel |
| 19 | `ce46a62` | 3 | pack benchmarks: warm-up, N=5, median + MAD |
| 20 | `9da4619` | 4 | whole-tree `deptry` made honest about `scripts/` |
| 21 | `946e48e` | 5 | this ledger (rewritten) + `docs/evidence/p0/ci_parity_addendum.json` |
| 22 | `85b3101` | 5 | operation truth regenerated at the docs commit |
| 23 | `1e1201d` | 8 | the four red CI jobs of the first run, fixed (§6.3) |
| 24 | `a0eb95b` | 8 | the ledger records that run |
| 25 | `aaf3ff8` | 8 | the fencing-token mutant now removes both enforcement points (§6.3.2) |
| 26 | `303129c` | 8 | the reconciler CLI test off the process-global engine (§6.3.2) |
| 27 | `6793bf2` | 8 | the green run recorded (§6.3.4) |
| 28 | `cff0e69` | 8 | chain counts corrected to the final tree (docs only) |
| 29 | `80c8ea6` | 8 | the chain-count sentence made self-referential and true (docs only) |
| 30 | `8fa032e` | 8 | ATLAS split into six proven sub-chunks; PG concurrency module made order-independent |
| 31 | `22e761a` | 8 | the P0 bundle regenerated on a real PostgreSQL 16.2 |

Commits 28–29 are documentation only; 30–31 are behaviour and evidence, and §4 is
measured at 31.


Integrity: no `rebase`, no `reset --hard` on an existing commit, no force-push was
used at any point. The only history edit was `git commit --amend` on `ce46a62`
seconds after it was created and **before any push**, to replace a commit message
that a shell heredoc had truncated to a single `Co-authored-by:` line; its tree was
not touched.

### 2.4 Human review guide — 512 files, in the order that costs least

A 512-file diff is not reviewed alphabetically. This is the map: what changed, where it
is, and what a reviewer can settle without reading it. Counts are
`git diff --name-status deab2b3e..HEAD` grouped by area (`A` added, `M` modified,
`D` deleted).

| Area | Files | A / M / D | What it is | Review weight |
|---|---:|---|---|---|
| `src/nexus_ai_agent/creative/**` | 170 | 163 / 7 / 0 | the six engines (VERITAS, CHRONOS, HYPERION, MORPHEUS, ATLAS, AETHEL) and their studio/product/web surfaces | **read** — this is the behaviour |
| `tests/creative/**` | 133 | 133 / 0 / 0 | their falsification, mutation and law suites (the only proof of most of it) | **read** the mutation/falsification files |
| `tests/unit/**` | 55 | 37 / 18 / 0 | unit coverage for the new seams and the chunked runner | skim |
| `tests/integration/**` | 13 | 3 / 10 / 0 | PostgreSQL durability, Studio submission, spaced-repetition of the PG legs | skim; §4.2 has the counts |
| `tests/architecture/**` | 10 | 6 / 4 / 0 | the invariants that stop the surface drifting (route inventory, capability axis) | **read** — they are contracts |
| `tests/storage/**` | 3 | 3 / 0 / 0 | schema-golden and backup legs | skim |
| `src/nexus_ai_agent/` — everything else | 70 | 41 / 29 / 0 | `storage` 12, `nagar` 10, `maintenance` 7, `connect` 6, `api` 4, `continuum`/`execution`/`provenance` 3 each, `adapters` 2, `bot`/`config`/`jobs`/`cli` 4 | skim; `src/storage` and `src/nagar` first |
| `scripts/**` | 14 | 8 / 6 / 0 | `ci_parity.py` (chunked runner), `pack_coverage.py`, `p0_*`, mutation drivers | **read** `ci_parity.py` |
| `.github/workflows/**` | 2 | 0 / 2 / 0 | `ci.yml` (timeout 45 m, annotations, FFmpeg, PyAV) and `migrate-postgres.yml` | **read** §6.2 first — few lines change |
| `migrations/**` | 1 | 1 / 0 / 0 | the `20261112connect` migration head | skim |
| repo root (15) | 15 | 1 / 14 / 0 | `pyproject.toml` (deps + deptry config), `CONTRIBUTING.md`, `Makefile`, `README`, `CHANGELOG` | skim |
| `docs/**` | 13 | 6 / 7 / 0 | this ledger, the P0 evidence bundle, the addendum, `OPERATION_TRUTH.json`, `DECISION_LOG`, `README` index | **read** this file first |
| `.openhands/memory/**` (10, all `D`) | 10 | 0 / 0 / 10 | **deletions only** — a mission-zone reset that restores the branch baseline; no product file is removed | skip |
| `.agents/**`, `.github/DEPENDENCY_MATRIX.md`, `tests/fixtures/**` | 3 | 0 / 3 / 0 | one agent policy line, one matrix row, one fixture pointer | skip |
| **total** | **512** | **402 / 100 / 10** | | |

Three properties make the review cheaper than its size:

1. **402 of the 512 are additions.** The 100 modified files are, in order of risk:
   `.github/workflows/ci.yml`, `pyproject.toml`, `src/storage/*`, `src/nagar/*`, the
   truth tooling, and the ten `docs/**` files. The ten deletions are all
   `.openhands/memory/**` plus one fixture — nothing shipped is removed.
2. **The behaviour is concentrated.** 170 of 512 files are one package tree
   (`creative/`); every one of its engines carries its own falsification suite in
   `tests/creative/**`, so a reviewer who reads a source file can jump straight to the
   test that is meant to break it.
3. **Nothing here is unreviewed-by-assertion.** Each claim in §3–§7 names the command
   that produced it, and §9 reproduces them. Where a claim could not be measured, §8
   says so instead of hedging.

---

## 3. Waves 2–7: what was built and what proves it

### 3.1 Wave 2 — bounded, validated, and honestly chunked (`51242b1`, `3ff7e71`)

* **Morpheus mux order** (`51242b1`). `mux_mp4_ffmpeg` resolved its encoder first,
  so a non-existent audio path or a frame pattern matching nothing was reported as
  "no FFmpeg binary found". Resolution is now last: audio file → frame expansion →
  `resolve_ffmpeg_bin`. The two input checks keep their historical order (audio,
  then frames) so no existing caller's error contract moves.
  `tests/creative/morpheus/test_morpheus_mux_resolution_and_validation.py` proves
  both halves hermetically: the resolver is patched to show the mux runs the binary
  it returns, and replaced with one that fails the test if reached too early.
* **Chunked CI parity** (`3ff7e71`). CI runs the whole suite in one pytest process;
  that selection does not fit this machine's `/tmp` (the ATLAS campaign alone writes
  >15 GiB), and pytest reports a full disk as *hundreds* of phantom failures.
  `scripts/ci_parity.py` reproduces the selection **and proves the reproduction**:
  the marker expression is read from `.github/workflows/ci.yml` at run time,
  `--collect-only` under that marker runs once over `tests` and once per chunk, and
  the union must equal the CI selection exactly — missing 0, duplicated 0,
  unexpected 0 — *before* a single test executes. Each chunk gets its own
  `mkdtemp` directory (`TMPDIR`/`TEMP`/`TMP` + `--basetemp`) and only that directory
  is removed; nothing outside it is ever deleted.
* **Verdict label.** The record calls itself
  `CHUNKED_SUITE_VERIFICATION_WITH_COLLECTION_EQUIVALENCE` and lists what it does
  not reproduce (runner image, service containers, triggers, other jobs). It is
  **not** a GitHub Actions run (§8.3).

### 3.2 Wave 3 — median acceptance, and the kernel that made it pass (`c55a8a0`, `ce46a62`)

`min(times)` was rejected as the acceptance statistic: a minimum can hide a
systematic regression behind one lucky round. The pack benchmarks now take **two
discarded warm-up rounds**, `gc.collect()` before every measured round, **five**
measured rounds, and accept on the **median**. The failure message carries the raw
samples, the median, the MAD (and its share of the median), the minimum and the peak
allocation, so a noisy machine is visible instead of being averaged away.

Measurement discipline alone would not have saved the scene flow benchmark: the old
kernel was 440 ms per call and the budget is 3000 ms, so one loaded sample reddened
it (3600 ms observed). `c55a8a0` evaluates the exhaustive block-matching search
**per shift over the whole region** (integral-image window sums) instead of per pixel
in Python — 440 ms → ~4.4 ms, and **bit-identical** to a literal transcription of
the old loop across seven geometries, three window sizes, five shift limits and
identical/rolled inputs.

### 3.3 Wave 4 — gates at the final tree (§4)
### 3.4 Wave 5 — truth and this ledger (§5)
### 3.5 Wave 6 — the two CI questions (§6)
### 3.6 Wave 7 — the closeout itself (`51242b1` … this commit, §3.1–§3.2)

---

## 4. Gates at the final tree

**Correction.** An earlier revision of this section said that between the measured
revision and the ledger commit "the only delta was `pyproject.toml` and documentation".
That sentence was false and is retracted: the diff against `deab2b3e` is **512 files,
240 of them under `src/`** (§2.4). Two numbers were carried forward on the strength of
it. Everything below was therefore **re-measured at `f574e48`**, the last commit whose
tree carries the suites, and every row names the revision it belongs to when it is not
that one. Two rows are still tied to earlier revisions and say so: the mutation count
(`aaf3ff8`/§6.3.2) and the pack-coverage verdict (`9da4619`) — neither tree component
changed since.

| Gate | Command | Result |
|------|---------|--------|
| Lint | `ruff check .` | `All checks passed!` (ruff 0.16.8 — the CI pin, `.github/workflows/ci.yml:880`) |
| Format | `ruff format --check .` | 1395 files already formatted |
| Types | `mypy src` | `Success: no issues found in 552 source files` |
| Dependencies | `deptry .` | `Success! No dependency issues found.` (whole tree, `9da4619`) |
| Dependencies (gate scope) | `deptry src` | `Success! No dependency issues found.` |
| Operation truth | `python -m nexus_ai_agent.diagnostics.truth --root .` | `OK — no findings` (version-lockstep, docs-index, law-test-resolution, law-numbering, claim-witnesses, fail-open-defaults, board-truth) |
| Dependency audit | `python scripts/dependency_audit.py` | **exit 1** — 21 advisories: 18 accepted, **3 blocking (`soupsieve`)** — §8.4; CI's own `dependency-audit` job is green (§6.3.2) |
| Pack coverage | `python scripts/pack_coverage.py` | **ACCEPTED**, `9da4619`, 9/9 packs ≥ 95%, source `sha256:8289dab0…` |
| Chunked parity | `python scripts/ci_parity.py` | **exit 0**, **20/20 chunks green, none skipped**, collection equivalence proven at **11 842 = 11 842** |
| Mutation harnesses | `pytest -k "mutation or mutant or kill"` + the ATLAS campaigns | **486 killed** repo-wide (§6.3.2) **and 120/120 inside `atlas#mutation-120`** (§8.1); 0 survived |
| Benchmarks | `pytest tests/creative/packs` | 155 passed; medians 1.6–65.5 ms against 2000–3000 ms budgets |

### 4.1 Pack coverage (ACCEPTED at `9da4619`)

| pack | modules | executed | executable | cover |
|---|---:|---:|---:|---:|
| audio | 3 | 939 | 943 | **99.58 %** |
| caption | 5 | 1031 | 1072 | **96.18 %** |
| core | 7 | 1000 | 1042 | **95.97 %** |
| delivery | 4 | 624 | 633 | **98.58 %** |
| edit | 3 | 490 | 508 | **96.46 %** |
| motion | 3 | 679 | 689 | **98.55 %** |
| portrait | 4 | 173 | 174 | **99.43 %** |
| scene | 4 | 213 | 214 | **99.53 %** |
| slideshow | 5 | 880 | 899 | **97.89 %** |
| **TOTAL** | **38** | **6029** | **6174** | **97.65 %** |

43 test modules, pytest exit 0, contract threshold 95 %, `verdict: ACCEPTED`.
`COVERAGE_FILE=/tmp/.coverage_arena` was set for every coverage run; the repository
holds no `.coverage*` file.

### 4.2 Chunked parity — per-chunk results

Collection proof (before execution):

```
collection: CI 11842 ids, union 11842 ids, missing 0, duplicated 0, unexpected 0
collection equivalence proven: the chunks are exactly the CI selection
```

Twenty chunks, every one green, **nothing skipped for space**:

| Chunk | Result | Collected | Wall |
|---|---|---:|---:|
| `tests/architecture` | 406 passed | 406 | 64.9 s |
| `tests/bench` | 3 passed | 3 | 0.8 s |
| `tests/creative/atlas#mutation-120` | **124 passed** (the 120-mutant receipt) | 124 | 711.9 s |
| `tests/creative/atlas#falsification-160` | 190 passed | 190 | 10.2 s |
| `tests/creative/atlas#titan-mutations` | 97 passed, 3 skipped | 100 | 197.1 s |
| `tests/creative/atlas#prometheus-governance` | 164 passed, 9 skipped | 173 | 13.3 s |
| `tests/creative/atlas#waves-13-22` | 406 passed, 1 skipped | 407 | 10.9 s |
| `tests/creative/atlas#waves-23-30-and-solvers` | 510 passed, 8 skipped | 518 | 13.2 s |
| `tests/creative/audio_engine` | 163 passed | 163 | 4.4 s |
| `tests/creative/chronos` | 240 passed | 240 | 13.1 s |
| `tests/creative/composition` | 306 passed | 306 | 85.7 s |
| `tests/creative/director` | 68 passed | 68 | 0.9 s |
| `tests/creative/episode_causality_lab` | 183 passed | 183 | 7.6 s |
| `tests/creative/morpheus` | 281 passed | 281 | 4.4 s |
| `tests/creative/packs` | 155 passed | 155 | 7.9 s |
| `tests/creative/speech_cinema` | 284 passed, 4 deselected | 284 | 32.2 s |
| `tests/creative/veritas` | 648 passed | 648 | 18.2 s |
| `tests/integration` | 661 passed, 36 skipped | 697 | 260.2 s |
| `tests/storage` | 43 passed | 43 | 0.8 s |
| `tests/unit` | 6838 passed, 15 skipped | 6853 | 673.5 s |

**Totals: 11 770 passed, 72 skipped, 4 deselected (outside the CI marker selection),
0 failed, 0 errors — 11 842 executed = 11 842 in the CI selection, ~36 min of wall
time.** The skips are environmental and self-describing: FFmpeg/ffprobe absent on this
machine (the ATLAS media mutants say so in their own skip reason), the core-only
optional-extras legs, and the CI-leg validation that is inert off CI.

### 4.3 Benchmarks — raw samples, median, MAD

Two warm-up rounds (discarded), `gc.collect()` before each of five measured rounds,
`tracemalloc` peak. Medians are the acceptance statistic; budgets are the pack
contracts.

| kernel | median | MAD (% of median) | min | raw measured samples (ms) | warm-up (ms) | peak | budget |
|---|---:|---:|---:|---|---|---:|---:|
| homography | 27.837 ms | 0.561 (2.0 %) | 17.268 | 27.837, 28.398, 28.350, 17.268, 17.461 | 28.360, 26.939 | 11.1 MB | < 2000 ms |
| blur 64² | 1.637 ms | 0.074 (4.5 %) | 1.552 | 1.637, 1.711, 1.633, 1.552, 1.713 | 0.945, 0.863 | 0.2 MB | — |
| blur 512² | 45.412 ms | 0.629 (1.4 %) | 43.384 | 45.353, 46.041, 48.355, 45.412, 43.384 | 41.716, 41.440 | 12.1 MB | — |
| inpaint (30 it) | 65.546 ms | 7.661 (11.7 %) | 55.596 | 55.596, 65.678, 65.546, 57.885, 75.050 | 48.413, 41.072 | 2.7 MB | — |
| **flow (32², w5, ±4)** | **15.601 ms** | 1.035 (6.6 %) | 14.141 | 14.141, 16.636, 15.601, 16.672, 14.667 | 4.002, 4.034 | 0.1 MB | **< 3000 ms** |
| skintone 256² | 13.396 ms | 0.687 (5.1 %) | 12.327 | 14.083, 12.327, 13.396, 12.385, 13.985 | 13.139, 11.408 | 7.3 MB | < 3000 ms / < 300 MB |
| lab round-trip | 9.499 ms | 0.718 (7.6 %) | 8.403 | 9.943, 10.217, 8.685, 8.403, 9.499 | 9.951, 9.647 | 6.2 MB | — |
| bokeh | 7.977 ms | 0.558 (7.0 %) | 6.507 | 7.977, 8.485, 7.419, 6.507, 9.314 | 4.143, 5.470 | 0.8 MB | — |
| relight | 1.876 ms | 0.029 (1.6 %) | 1.811 | 2.487, 1.906, 1.855, 1.811, 1.876 | 1.738, 1.299 | 1.4 MB | — |

The flow row is the measured-in-benchmark number (it carries `tracemalloc`
overhead); the bare kernel measures 3.4–4.6 ms/call. Either way it is two orders of
magnitude inside its 3000 ms budget, which is the point of `c55a8a0`.

---

## 5. Wave 5 — operation truth (regenerated, not edited)

`python -m nexus_ai_agent.nagar --docs` regenerates `OPERATION_TRUTH.json`,
`OPERATION_CONTRACT_MATRIX.md`, `L0_L4_MATURITY.md`, `RECONCILIATION.md` and
`OPERATION_WAVE_PLAN.md` from their declaration sources. Measured content of the
regenerated truth:

| Fact | Value |
|---|---|
| `source_revision` | `f574e48` (recorded in the file itself; the two non-deterministic fields are `generated_at` and this one) |
| catalog operations | 70 |
| runtime registry | 88 |
| overlap | 47 · gaps 23 · extras 41 |
| universe | 111 |
| executable surface | **12** operations (rose from 9 with the Wave-5/6 Studio lanes) |
| suite-executed operations | **82** of the 111 (the other 29 are referenced-only or missing; §8 has no hidden ones) |
| migration head | `20261112connect` |
| truth doctor | `OK — no findings` (version-lockstep, docs-index, law-test-resolution, law-numbering, claim-witnesses, fail-open-defaults, board-truth) |

Two counts measure different things and are both given deliberately: **82** is the
number of *operations* the truth engine proves are executed by the suite (from the same
AST resolution pass the doctor re-checks, so it cannot drift from the tree); **11 770**
is the number of *tests* the chunked parity run executed (§4.2). The regeneration moved
only `source_revision` 946e48e → f574e48 and `generated_at`: every measured field is
unchanged, which is what a tree without registry changes should produce.

`docs/evidence/p0/ci_parity_addendum.json` records the closeout's commands, exit
codes, timings, the SHA-256 of the parity record and of the coverage artifact, and the
limitations that survived §8. It contains no invented output: every field was captured
from a run. It is not a substitute for the P0 bundle — that file is now the script's own
output on a live PostgreSQL (§8.2), and the addendum says so instead of standing in for
it.

---

## 6. Wave 6 — the two CI questions

### 6.1 Why CircleCI reports `error` on this branch's commits

Measured on `40cf83c` (a commit of this branch that was pushed):

```bash
gh api repos/bot523h/nexus-ai-agent/commits/40cf83c/status \
  --jq '{state, n: (.statuses|length), items: [.statuses[]|{context,state}]}'
# {"items":[{"context":"CircleCI Pipeline","state":"error"}],"n":1,"state":"failure"}
gh api repos/bot523h/nexus-ai-agent/commits/40cf83c/check-runs --jq '.total_count'
# 0
git log --all --oneline -- .circleci | wc -l
# 0
```

What that means:

* **One** commit status, context `CircleCI Pipeline`, state `error` — and **zero**
  check-runs. Nothing in this repository *runs* on GitHub Actions for a push to a
  working branch (§6.2), so the only signal a reviewer sees here is CircleCI's.
* `.circleci/` has **never** existed in any ref of this repository (the `git log
  --all` above is empty over the entire history). There is no CircleCI
  configuration to build: the status comes from an external GitHub App whose webhook
  is still wired to this repository, and it fails because it has nothing to run. It
  is therefore not evidence about this branch's code, and it must not be read as a
  red test run.
* Making it green is a repository-settings action (disconnect the CircleCI app /
  remove the stale webhook), not a code change. It is **out of scope** for this
  closeout — the directive forbids changing required-check policy — so the condition
  is documented instead of silenced.

### 6.2 Why the `ci.yml` jobs do not appear on a push to a working branch

`.github/workflows/ci.yml` declares:

```yaml
on:
  push:
    branches: [main]
    tags: ["v*"]
  pull_request:
    types: [opened, synchronize, reopened, edited]
```

A push to `arena/fd837e50-nexus-ai-agent` matches neither `push.branches` (only
`main`) nor `push.tags` (only `v*`). The workflow therefore runs for this branch
**only through the `pull_request` event** of whatever PR points at it — which is
exactly why §7 opens one. This is a deliberate cost decision recorded in the file
itself ("Audit 2026-10-15 (A-08): an unfiltered `push:` ran the whole matrix twice
for every PR commit"); it is not a misconfiguration and it is not changed here.

Consequence for a reviewer: `main` HEAD carries 28 check-runs, a working-branch
commit carries 0 check-runs and 1 CircleCI status (§6.1). Absence of check-runs on
this branch is expected and is **not** evidence of a skipped test job — §4 replaces
it with a local, collection-equivalence-proven run.

---

### 6.3 The first CI run on this branch: four red jobs, root-caused and fixed

PR #344's first run (`43634497488`, head `85b3101`) opened the machine-checked half of
this ledger: `main` (`deab2b3e`) and PR #343's head (`7189a8a`, run `43618529837`) were
both fully green, so the four red jobs were **this branch's**, not pre-existing. Each
was reproduced locally before being touched.

| Job | Failing step | Root cause | Fix |
|---|---|---|---|
| `test (pytest -m "not slow")` | `dependency audit` | `cryptography` is a new dependency of this branch, capped `<48`; 47.0.0 carries four unaccepted advisories whose fixes are 48.0.1/49.0.0/50.0.0, so the cap could never resolve to a clean release | cap widened to `<51` (`pyproject.toml`), installed 50.0.2, 70 connect/crypto/studio tests green |
| `dependency-audit` | same audit | same | same |
| `trust-mutations` | `python scripts/ibpc_security_mutations.py` exit 2 | the job installs a hand-curated *minimal* set; `299e931` added `nexus_ai_agent.connect`, whose package `__init__` imports `connect.crypto` (`cryptography`) and whose provenance path canonicalises with `rfc8785`. Reproduced in a clean venv from that exact install line: `ModuleNotFoundError: No module named 'cryptography'` → collection error → pytest exit 2 → the harness correctly refused to read it as a kill | the two packages added to that install line |
| `migrate-postgres` | `pytest tests/integration/test_checkpoint_adapter_contract.py` | `299e931` added migration `20261112connect`, which moved the Alembic head; `postgres.langgraph.json` embeds the head, so the committed golden was stale (`expected=71176645… actual=61188263…`). The same job's last step also still grepped for the old head | golden regenerated with the **human-only** CLI against a real PostgreSQL 16.2 at `20261112connect`; the stale head reference and grep in `ci.yml` updated; `docs/architecture/DATA_AND_STORAGE.md` now names the head the golden was regenerated at |

**6.3.1 A real PostgreSQL was used, so one limitation shrank.** The regeneration was
performed with `nexus checkpoints golden update --backend postgres --yes` against a
PostgreSQL 16.2 instance (the same major version as CI's `pgvector/pgvector:pg16`
service), created with the `pgserver` wheel because this sandbox has no Docker. The
result is not a guessed hash: on a **fresh** database, `nexus migrate` to
`20261112connect` followed by the contract module gave **26 passed**, including
`test_committed_postgres_golden_matches_pg16_in_ci`; the four other PostgreSQL legs of
the job (`test_postgres_create_all_race`, `test_postgres_single_engine`,
`test_lifecycle_store_contract`, `test_postgres_persistence`,
`test_knowledge_retrieval_postgres`, `test_postgres_restore_atomicity`) were run
against it too: 44 + 11 passed. One module
(`test_artifact_binding_postgres_concurrency`) fails when run **after** the others on a
shared database and passes alone (5 passed) — order-sensitivity of a shared-database
test, recorded here rather than hidden.

**6.3.2 Two discrepancies this closeout does not explain away.**

*Soupsieve.* Locally, after the cryptography fix, `scripts/dependency_audit.py`
reports 3 blocking advisories in `soupsieve 2.10` (a transitive of
`beautifulsoup4`): `CVE-2026-126190/126191/126193`. They cannot be honestly accepted —
the gate's `no-fix-released` path requires an empty `fix_versions` list and pip-audit
reports 3.0.2/4.0.0, while the index tops out at 2.10 (`pip index versions soupsieve`).
**The CI job nevertheless went green** on `a0eb95b` with the same `pyproject.toml`, so
CI's advisory view and this sandbox's `pip-audit 2.10.1` disagree about those three
entries; the closure is identical (no `soupsieve` pin changed on this branch), so
neither run is evidence about this branch's code. Recorded as an unresolved
discrepancy — the local result is *not* presented as a fix, and no allow-list entry was
added on the strength of either run.

*The `test` job.* It failed on `aaf3ff8` at
`tests/unit/test_reconciler_cli_and_verified_registration.py::test_cli_is_a_dry_run_by_default`
with `ResourceClosedError: This Connection is closed` and "Event loop is closed".
The test drove `reconciler._run` with `--database-path` absent, so the CLI used the
**process-global** `storage.db.get_session` engine — which belongs to whichever event
loop created it first. In a single-process suite that engine can be retired or bound
to a closed loop by an earlier module, which is exactly what happened; run alone, the
module is green. The test now substitutes that one function with a session factory over
its own engine: the CLI keeps its real path, the engine is never global, and the
reconciliation now runs against the rows the test itself wrote (a stronger assertion
than "whatever `data/app.sqlite` held").

*The fifth harness job.* The same run turned `trust-mutations` red at a step the first
run never reached: `scripts/durable_project_state_mutations.py` reported
`33/34 killed`, with `retraction_not_token_scoped` surviving. Reproduced locally, and
the cause is `299e931` adding a **second** enforcement point for the fencing token (a
pre-read guard next to the delete predicate). Either point alone stops the attack, so
the single-point mutant became *equivalent* to the original — no honest test can kill
it. The mutant now removes both points (the harness grew multi-edit support for
exactly this case, and says why), and the campaign is **34/34 killed** with the
baseline green before and after.

**6.3.3 The CircleCI status is unchanged by all this** (§6.1): the fixes above affect
GitHub Actions only.

**6.3.4 The green run.** After the four fixes above, run `43674349207` (head
`303129c`) completed **success with every job green** — `test (pytest -m "not slow")`,
`migrate-postgres`, `trust-mutations`, `dependency-audit`, `lint`, `lint-fast`,
`merge-base-guard`, `release-lineage`, the four mutation jobs, `python-parity` on
3.10/3.11/3.12, `continuum-evidence` on 3.10/3.11/3.12 and the `extras-matrix` legs.
That is the real GitHub Actions verification this ledger's local numbers could not
provide, and it is on the same content as §4 (the only commits after `ce46a62` are the
fixes in §6.3, `1e1201d`, `aaf3ff8`, `303129c` and documentation).

Locally on that tree: `tests/unit` + `tests/architecture` 7239 passed (15 skipped),
ruff and `ruff format --check` clean, `mypy src` 552 files clean, `deptry .` clean,
truth doctor `OK — no findings`, and `scripts/durable_project_state_mutations.py`
34/34 killed.

## 7. Where this work is published

| Thing | Value |
|---|---|
| Branch pushed | `origin/arena/fd837e50-nexus-ai-agent` (the only branch this session may push to; no force-push, no rebase) |
| Pull request | **#344** — https://github.com/bot523h/nexus-ai-agent/pull/344, base `main`, `OPEN` / `MERGEABLE` |
| PR #344 title | "Waves 1–7 closeout: bounded CI parity, median benchmarks, traced history (not PR #343)" |
| PR #343 | **superseded and closed** with a comment pointing at #344, its three unique commits cherry-picked (§2.2); its branch `arena/750b78f5-nexus-ai-agent` is preserved, nothing was force-pushed or deleted |
| CI on the final SHA | recorded in §6.3.5 once the run on the pushed head finishes; §6.3.4 has the earlier green run (24/24) |

PR #343's title advertises "Waves 2–3 integration" and its branch carries Waves 4–6 plus
the I1/I2 checkpoint — the retraction in §1 stands: this branch is not that PR, and this
PR is not that work.

---

## 8. Honest gaps

**8.1 The ATLAS campaign now runs here — 1512 ids in six sub-chunks, 1491 passed /
21 skipped, 120/120 mutants killed.** This replaces the earlier entry that reported the
tree as *not run* for want of ~18 GiB. Two things were wrong with that picture, and both
are now measured rather than argued:

* the footprint was **per process, not per chunk** — one test builds the whole kill
  ledger and never frees a tree. Splitting the tree into chunks therefore did not bound
  it; suppressing bytecode did (§8.1.1);
* the split is only worth anything if it is proven, so `SUB_CHUNKS` is checked against
  the CI selection before execution (`11842 = 11842`, §4.2) and by a unit test that pins
  the sub-chunks as disjoint and exhaustive.

Result of the whole tree, in §4.2: **1491 passed, 21 skipped** over the six sub-chunks
(`124 + 190 + 97 + 164 + 406 + 510`), with the 120-mutant campaign itself at
**124 passed in 711.9 s** — the same numbers the tree produced last time it ran
completely at `4bcee33`. The 21 skips are FFmpeg/ffprobe absence and are *not* counted
as kills; three of them say so explicitly ("mutant is only killed by a real-ffmpeg test
that skips without ffmpeg/ffprobe on PATH (NOT a kill)").

**8.1.1 What made it fit — a measured number, not a bigger disk.**
With bytecode writing on, each mutant tree cost ~53 MB and the campaign died at mutant
115 of 120 with `OSError: [Errno 28]`. `chunk_env` now exports
`PYTHONDONTWRITEBYTECODE=1`, so a copy carries sources only: measured 1.35 GiB at 70
trees and 1.75 GiB at 91, i.e. ≈19 MB per tree and ≈2.3 GiB for all 120. The budget in
`SPACE_BUDGETS` is that measurement (3 GiB, down from a defensive 8 GiB) and the run
completed with no space skip. Nothing about the tests changed — only whether Python
caches bytecode in a directory that lives for one chunk.

**8.2 The P0 bundle is regenerated — on a real PostgreSQL, not by hand.**
`docs/evidence/p0/evidence.json` now carries `all_checks_passed: True` from
`scripts/p0_evidence.py` against PostgreSQL 16.2 at migration head `20261112connect`:
migrate 0, persistence 0, **DR proof 0** (dump → verify → restore → row preserved),
backend policy 0, truth gate 0, and the 20 000-row restore benchmark 0. The earlier
bundle was carried forward as a gap because this environment had no PostgreSQL; the
number in the file is the script's own output and no field was edited.

Two environment facts are worth recording because they cost time and are not bugs:
`dr_proof.py` and `p0_benchmark.py` both target `nexus_restore_target` by default, so
that database must exist before the bundle runs; and the pgserver socket URI
(`?host=/socket`) is rejected by `storage/db.py:205`, so the cluster must be started on
a TCP port.

**8.3 The parity run is not GitHub Actions.**
`CHUNKED_SUITE_VERIFICATION_WITH_COLLECTION_EQUIVALENCE` means: the *selection* is
proven identical to CI's, the tests really ran, and the environment is *not* CI's
(no runner image, no service containers, no workflow-level triggers, other jobs in
`ci.yml` not run). It is a substitute for nothing; it is the best local signal
available while §6.2 holds.

**8.4 The local dependency audit is red on exactly three advisories, and cannot
honestly be made green.** `python scripts/dependency_audit.py` reports 21 advisories:
18 accepted by the reviewed allow-list (with the reasons the gate re-derives itself) and
**3 blocking, all `soupsieve` 2.10**:

| advisory | aliases | declared fix |
|---|---|---|
| `CVE-2026-126190` | `GHSA-mmhr-3c5m-f64f` | `4.0.0` |
| `CVE-2026-126193` | `GHSA-7g44-g9rc-x53w` | `3.0.2` |
| `CVE-2026-126191` | `GHSA-6655-6qjp-jx7x` | `3.0.2` |

**Every declared fix version is unpublished**, which is the whole problem:

```
$ python -m pip index versions soupsieve
soupsieve (2.10)
Available versions: 2.10, 2.9.2, 2.9.1, 2.9, 2.8.4, … 0.4
  INSTALLED: 2.10
  LATEST:    2.10
$ python -m pip install -U soupsieve      # → still 2.10, "already satisfied"
```

The package's entire published history is 2.x; there is no 3.0.2 and no 4.0.0 to install,
so `pip install -U` cannot clear the advisory and a version pin cannot either. The two
acceptance reasons the gate understands cannot express this either — `no-fix-released`
self-expires the moment pip-audit reports a non-empty fix list (it does: `4.0.0`/`3.0.2`),
and `transitive-cap` needs a real capping requirement (beautifulsoup4 requires
`soupsieve>1.2`, with no upper bound). **No allow-list entry was invented to silence
it**; the gate's design earns its keep here by refusing an automatic acceptance and
leaving a human decision where a human decision belongs. Separately: seven `pip`
advisories that appeared in the first local run were the sandbox's own `pip 23.0.1` and
disappeared after upgrading it to 26.3 — an environment artefact, not a repository one.
CI's `dependency-audit` job is green on the same `pyproject.toml` (§6.3.2); the honest
statement is that the two environments resolve different advisory sets, not that one of
them is wrong.

**8.5 Branch protection could not be read.** `gh api
repos/bot523h/nexus-ai-agent/branches/main/protection` returns HTTP 403 ("Resource
not accessible by integration") for this token, so the required-check policy is
**not verified** here — and it is not modified either, per the directive.

**8.6 `git diff --check` is clean except three intentional Markdown line breaks.**
`git diff --check deab2b3e..HEAD` used to report four findings; the one that was dirt — a
stray blank line at the end of `pyproject.toml` — is fixed in `f1dbaf0`. The three that
remain are the two-space hard line breaks in `MORPHEUS_3DGS_CRANIAL_DOCTRINE.md`, which are
Markdown syntax and would change the rendered document if stripped. They are named here
instead of being quietly rewritten.

---

## 9. Reproducing this ledger

```bash
python -m venv /home/user/.venv && /home/user/.venv/bin/pip install -e ".[dev]"
/home/user/.venv/bin/pip install "ruff==0.16.8"          # the pin from ci.yml:880

/home/user/.venv/bin/ruff check . && /home/user/.venv/bin/ruff format --check .
/home/user/.venv/bin/python -m mypy src
/home/user/.venv/bin/deptry .                            # whole tree, 9da4619
/home/user/.venv/bin/python -m nexus_ai_agent.diagnostics.truth --root .

COVERAGE_FILE=/tmp/.coverage_arena /home/user/.venv/bin/python scripts/pack_coverage.py
COVERAGE_FILE=/tmp/.coverage_arena /home/user/.venv/bin/python scripts/ci_parity.py --json-out /tmp/parity.json
/home/user/.venv/bin/python -m pytest -q -m "not slow" -k "mutation or mutant or kill" \
    --ignore=tests/creative/atlas                        # 486 killed, §6.3.2
/home/user/.venv/bin/python scripts/ci_parity.py "tests/creative/atlas#mutation-120"
                                                         # 124 passed, 120/120 mutants
/home/user/.venv/bin/python -m pytest tests/creative/packs   # benchmarks, median + MAD
```

`scripts/ci_parity.py --list` prints the chunks; `--skip-collection-proof` exists
for triage and marks the run as unequivalent in its own JSON.
