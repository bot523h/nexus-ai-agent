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
git rev-list --count deab2b3e..77a2b8b                  # 1517
git rev-list --count --first-parent deab2b3e..77a2b8b   # 64
git rev-list --count da3e5ed7..77a2b8b                  # 41
git rev-list --count '483403d^..77a2b8b'                # 41   (the §2.3 window, inclusive)
```

All four are `git rev-list` output at **`77a2b8b`**, the last commit of this wave whose
tree carries code; the commits after it are documentation and evidence only, and each adds
exactly one to every count, so a reader who runs the commands at the pushed `HEAD` will see
three larger numbers. That is the reason the SHAs are named here instead of "HEAD": the
previous revision of this section printed **1511 / 58 / 35** in the block, **1512 / 59 / 36**
in the sentence below it and "19 of the 35" against a table that has 35 rows, so no two
numbers agreed and none of them reproduced. The rule this section now follows: a count is
stated against a SHA, or it is not stated.

Of the 41, 19 (rows 1–19 of §2.3) are the pre-closeout waves and the four preserved
commits that came with them; 22 (rows 20–41) are this closeout, including the second audit
wave of §2.3b.

The **diff** is smaller than the history and easier to review:
`git diff --shortstat deab2b3e..2ff8b30` reports **512 files, +123 972 / −2 841** (the
insertion count moves by a few hundred with each documentation commit; the file and
deletion counts do not). §2.4 is a reading guide for exactly those files.

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
| `10c14e4` | `df1ea42` | `ci(test): raise the pytest timeout 30m -> 45m on the suite itself` (**job** timeout, not pytest's — see below) |
| `5a80116` | `f2c4db4` | `test(morpheus): make P53 nodal-equilibrium assert LAPACK-build stable` |
| `7189a8a` | `64e1392` | `ci(migrate-postgres): pin runner to ubuntu-24.04 (PG16 client) + annotate restore-atomicity failures` |

Three commits that already existed only here were preserved unchanged — nothing was
reset, rebased or force-pushed to make room: `d1fb569`, `4bcee33`, `40cf83c`.

The first row needed a correction, because this file previously repeated its subject line
without reading the diff, and the subject is misleading:

```
$ git show df1ea42 -- .github/workflows/ci.yml | grep -E '^[-+].*timeout-minutes'
-timeout-minutes: 30
+timeout-minutes: 45
-timeout-minutes: 30
+timeout-minutes: 45  # see the test job: the integrated suite outgrew 30
```

It raises the **GitHub Actions job wall-clock limit** (`timeout-minutes`) on the `test`
and `python-parity` jobs from 30 to 45 minutes. It does not touch pytest's `--timeout`
per-test limit, it does not raise an assertion, and it does not rename a required check.
"On the suite itself" in the upstream message means "the job that runs the suite"; the
ceiling that was hit was the job's. §6.3.5 records what that means for the slow 3.10 leg,
including the margin it left.

### 2.3 The wave window, commit by commit

`483403d^..77a2b8b` — **41 commits**, inclusive of `483403d` itself. Everything in §3–§7
is one of these, and rows 37–43 are the second audit wave (§2.3b). The previous revision
printed `483403d..HEAD` for 35 commits, but that range *excludes* its own first row — 34 is
what it counts.

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
| 32 | `f574e48` | 4 | the mutation budget replaced by the measured peak (3 GiB) — **the revision §4 is measured at** |
| 33 | `f395487` | 8 | the ledger re-measured, §2.4 review guide, §7/#343 supersession |
| 34 | `f1dbaf0` | 8 | one stray blank line at EOF in `pyproject.toml` |
| 35 | `2ff8b30` | 8 | §8.6 diff-check residue + the ATLAS reproduction line |
| 36 | `59fac7c` | 8 | the run on the pushed head, and counts that read off `git` |
| 37 | `854cafb` | A2 | the three `soupsieve` advisories closed with a real floor, not a waiver (§8.4) |
| 38 | `bcf74e7` | A2 | reconciler recovers the crash window; quarantine never overwrites a witness (§2.3b) |
| 39 | `3eaf7e4` | A2 | engine ports composed at the bus root; `intent_submit` stops calling a live route DEFERRED |
| 40 | `e7d8ad8` | A2 | the DR drill proves restore *content*, not a row count (§8.2) |
| 41 | `77a2b8b` | A2 | cherry-pick `cd8db338` — hybrid CAS resource packs (17 files, no conflict) |


Commits 28–29 are documentation only; 30–32 are behaviour, evidence and budgets; §4 is
measured at `77a2b8b` (41), the last commit of this wave whose tree carries code.

### 2.3b The second audit wave — five findings and one merge (rows 37–41)

A code audit of the pushed head produced five findings and one merge request. Each is
closed by a commit that names the failure mode, and by at least one test that fails on the
old code — the point being that every one of these was invisible to a green suite.

| # | Finding (as the audit stated it) | What the code does now | Where it is proven |
|---|---|---|---|
| 37 | three `soupsieve` advisories cannot be closed because no fix is published | patched releases **are** published (3.0.2 … 4.1.1); they declare `Requires-Python >= 3.11.5` and this sandbox is 3.11.2, so the repository declares the real floor `soupsieve>=4.1.1; python_version >= "3.11.5"` and the gate accepts it with a self-expiring reason (§8.4) | 33 tests in `tests/unit/test_dependency_audit_gate.py`; `scripts/dependency_audit.py` → **21 accepted / 0 blocking**, exit 0 |
| 38a | Case A quarantined a *healthy* artifact after a crash between publication and completion | `_repair_eligibility` reads the pin from `result` **or** the immutable job payload, accepts unfinished owned rows, and still refuses a terminal failure; bytes must match any recorded content address | 4 new crash-window tests in `test_db_disk_artifact_reconciler.py` |
| 38b | `os.replace` into a fixed quarantine path silently destroyed the first witness | `_unique_target` gives every quarantined file a collision-free name (content address, then a counter) for the artifact, its sidecar and the receipt | `test_quarantine_never_overwrites_an_existing_witness` (two crashes → two witnesses) |
| 38c | engines outlived the loop (`ResourceWarning: unclosed database`) | `storage/db.py.dispose_all_engines()` called from a `finally` in the reconciler CLI; writing that test exposed a second bug — `--database-path` handed the store a context manager instead of a factory, so the flag crashed the CLI the moment it was used | `test_the_reconciler_cli_disposes_its_engines`, plus the `SAWarning` the harness itself used to leak |
| 39a | six engine ports exist but only `audio_engine` is injected at the composition root | `build_job_bus` composes the port for the operation it is asked to run (still exactly one engine); an explicit mapping still wins | 3 tests incl. one that asserts the root composes without being asked |
| 39b | the Studio overview says `intent_submit` is `DEFERRED` ("no preview/approval session API") while `POST /api/studio/projects/{id}/renders` is live | `VERIFIED_WITH_LIMITATIONS`, with the real limits named (signature required, configured projects only, no approval session); a test reads the mounted OpenAPI contract so the row cannot outlive the route either way | `tests/unit/test_studio_session.py` |
| 40 | the DR proof verifies a row *count*, which a body-less restore satisfies | ordered SHA-256 over the query text on both sides; `content_digest_matches` gates both scripts and the bundle (§8.2) | 12 tests in `tests/unit/test_content_digest.py`; two real restores |
| 41 | merge `cd8db338` from `arena/a74ff8ec-nexus-ai-agent` | `git cherry-pick -x` — 17 added files under `storage/resource_packs/` + its tests, **zero conflicts** | `pytest tests/storage/resource_packs` → 146 passed |

**One procedural fact about these SHAs.** The sandbox running this closeout was reset
mid-wave and came back as a fresh clone: the local objects for rows 37–41 were gone, while
the working tree still carried every edit (Arena restores the workspace, not the repository's
object database). The five commits were therefore re-created from the identical tree and
re-verified — `mypy src` 561 files, 101 tests across the touched suites, and the P0 bundle
regenerated at `77a2b8b` with the same content hashes as before the reset
(`bc35d143…c87c8f9` for the benchmark, `06cdd248…d436bbb` for the DR table) — so the SHAs
in this table are the surviving ones, and the pre-reset ones (`4a3d328`, `4c2cfc7`,
`3eaf7e4`(pre-reset `398a6dd`), `2ea8e9d`, `f7e9271`, `fb55af8`, `f4e4a92`) name nothing any more. Nothing was
force-pushed to cover it: the pushed tip was still `59fac7c` and the lost commits had never
left the sandbox.

Row 41 is a cherry-pick rather than a merge on purpose: the PR under review is this branch,
and merging a foreign branch would drag its entire history (and its own `main` base) into a
diff a reviewer is asked to read. `-x` records the origin SHA in the commit body, so the
content is traceable to `cd8db338` without that cost.


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
| Format | `ruff format --check .` | 1414 files already formatted |
| Types | `mypy src` | `Success: no issues found in 561 source files` |
| Dependencies | `deptry .` | `Success! No dependency issues found.` (whole tree, `77a2b8b`) |
| Dependencies (gate scope) | `deptry src` | `Success! No dependency issues found.` |
| Operation truth | `python -m nexus_ai_agent.diagnostics.truth --root .` | `OK — no findings` (version-lockstep, docs-index, law-test-resolution, law-numbering, claim-witnesses, fail-open-defaults, board-truth) |
| Dependency audit | `python scripts/dependency_audit.py` | **exit 0** — 21 advisories: **21 accepted, 0 blocking** (§8.4). The previous revision of this table said `exit 1` / 3 blocking; that is closed, not hidden |
| Pack coverage | `python scripts/pack_coverage.py` | **ACCEPTED**, `77a2b8b`, 9/9 packs ≥ 95 %, 97.65 % total, source `sha256:ceca5076…`, `evidence: verified` |
| Chunked parity | `python scripts/ci_parity.py` | **exit 0**, **20/20 chunks green, none skipped**, collection equivalence proven at **11 842 = 11 842** |
| Mutation harnesses | `pytest -k "mutation or mutant or kill"` + the ATLAS campaigns | **486 killed** repo-wide (§6.3.2) **and 120/120 inside `atlas#mutation-120`** (§8.1); 0 survived |
| Benchmarks | `pytest tests/creative/packs` | 155 passed; medians 1.6–65.5 ms against 2000–3000 ms budgets |

### 4.1 Pack coverage (ACCEPTED at `77a2b8b`)

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

**6.3.5 The run on the pushed head — 24/24 SUCCESS.**
Run [`43746293967`](https://github.com/bot523h/nexus-ai-agent/actions/runs/43746293967) on
`2ff8b30` (`pull_request`, 18:21 → 20:42 UTC) finished with **every job green**:

```
test (pytest -m "not slow")           python-parity (3.10) (3.11) (3.12)   migrate-postgres
lint (ruff + mypy + lockstep)         lint-fast (lockstep + pinned ruff)   dependency-audit
continuum-evidence (3.10/3.11/3.12)   extras-matrix (core/speech/translate/pdf)
merge-base-guard (base == main)       release-lineage
trust-mutations   temporal-mutations   governance-mutations
execution-core-mutations   api-gate-mutations   remote-key-mutations   b2-blob-tier-mutations
```

Two honest notes rather than a clean sweep of the story:

* `python-parity (3.10)` needed **two reruns**. The first attempt was killed by the runner
  ~4 minutes in and never finalised a log, so it is not diagnosable from here; the second
  ran into the **job's own 45-minute wall-clock ceiling** (`timeout-minutes` — not pytest's
  per-test `--timeout`, which `df1ea42` never touched, §2.2); the third finished green at
  ~43 minutes. The leg is an order of magnitude slower than 3.11/3.12 (~7 min) on the same
  selection, which is worth a look in a future wave.
  **The ceiling itself was raised, and this file has to say so instead of denying it.** The
  previous revision of this bullet ended "its timeout was not raised to make it pass", which
  was true only in the narrow sense that *this run* did not raise it: `df1ea42` — a
  cherry-pick of `10c14e4` from PR #343, made before this closeout and not by it — had
  already moved `timeout-minutes` from 30 to 45 on both the `test` and `python-parity` jobs.
  The honest statement is three-part: nothing in this closeout raised a timeout; nothing was
  raised to convert a **red** leg into a green one (the leg that hit the ceiling was
  **rerun**, not re-timed); and the margin it leaves is thin — 43 minutes of a 45-minute
  budget, which is 2 minutes of headroom, not a comfortable one. No workflow concurrency, no
  branch-protection rule and no required-check name was changed at any point, and the
  toolchain rules for this closeout forbid weakening CI to manufacture green; a 15-minute
  raise inherited from an earlier wave does not weaken an assertion, but a reader deserves to
  see both the raise and its margin rather than a sentence denying that a raise happened.
* The **CI** `dependency-audit` job is green on the same `pyproject.toml` while the same
  script run locally is red on three `soupsieve` advisories (§8.4). **That contradiction is
  closed in the second audit wave (§2.3b, §8.4):** the earlier reading — "no fix has been
  published" — was wrong, the repository now declares a real floor, and
  `python scripts/dependency_audit.py` reports **21 advisories: 21 accepted, 0 blocking**,
  exit 0, locally *and* in CI. The local venv still resolves `soupsieve 2.10` because this
  sandbox runs Python 3.11.2 and every patched release requires ≥ 3.11.5; the acceptances are
  written so they expire by themselves rather than pretending that fact away.

**6.3.6 What this run does not prove.** It is the `pull_request` run on this branch, not a
run on `main` after a merge; the CircleCI status of §6.1 is unaffected by it; and the
branch-protection policy of §8.5 remains unread.

---

## 7. Where this work is published

| Thing | Value |
|---|---|
| Branch pushed | `origin/arena/fd837e50-nexus-ai-agent` (the only branch this session may push to; no force-push, no rebase) |
| Pull request | **#344** — https://github.com/bot523h/nexus-ai-agent/pull/344, base `main`, `OPEN` / `MERGEABLE` |
| PR #344 title | "Waves 1–7 closeout: bounded CI parity, median benchmarks, traced history (not PR #343)" |
| PR #343 | **superseded and closed** with a comment pointing at #344, its three unique commits cherry-picked (§2.2); its branch `arena/750b78f5-nexus-ai-agent` is preserved, nothing was force-pushed or deleted |
| CI on `2ff8b30` | **run `43746293967` — 24/24 jobs SUCCESS** (§6.3.5); the documentation commits after it carry their own run, recorded in the PR body |

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

**8.2 The P0 bundle is regenerated — on a real PostgreSQL, and now at content level.**
`docs/evidence/p0/evidence.json` carries `all_checks_passed: True` from
`scripts/p0_evidence.py` at `77a2b8b`, against PostgreSQL 16.2 at migration head
`20261112connect`: migrate 0, persistence 0, **DR proof 0**, backend policy 0, truth gate 0,
and the 20 000-row restore benchmark 0. The bundle also carries a `content_evidence` block
that did not exist before this wave:

```
benchmark.rows = 20000, restored_rows = 20000
benchmark.source_content_sha256   = bc35d143…c87c8f9
benchmark.restored_content_sha256 = bc35d143…c87c8f9
benchmark.content_digest_matches  = true
dr_proof_content_digest_matches   = true
all_content_matches               = true
```

That block exists because of an audit finding that the drill was weaker than it read: a row
**count** is satisfied by a restore that keeps 20 000 row headers and loses every body, and
by one that reinserts the same rows in a different order. Both scripts now hash the ordered
output of a fixed query (`SELECT id, name, body FROM bench_rows ORDER BY id`) before the
dump and after the restore, and both exit non-zero on a mismatch, so `exit_code: 0` in this
bundle means "same bytes in the same order", not "same number of rows". `dr_proof.py`
gained the same property on a 514-row table (512 payload rows + the sentinel + a NULL;
`06cdd248…d436bbb` on both sides) and reports it as the literal
`content_digest_matches=true`, which the bundle greps for. The bundle additionally refuses a
`benchmark.json` whose `generated_at` predates the run, so a stale file cannot be published
under a zero exit code.

The previous revision of this section reported "DR proof 0 (dump → verify → restore → row
preserved)", which was accurate about the script and is now superseded by the paragraph
above. Nothing here was hand-edited: every number is the script's own output.

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

**8.4 The three `soupsieve` advisories: what this file got wrong, and how it is closed.**

The previous revision of this section claimed that *every declared fix version is
unpublished* — "the package's entire published history is 2.x; there is no 3.0.2 and no
4.0.0 to install". **That was false**, and the way it was established is the interesting
part: the check was `python -m pip index versions soupsieve`, which lists only the releases
the *current interpreter* can install. On Python 3.11.2 that tops out at 2.10, so a whole
published range was invisible and the absence was read as proof.

```
$ curl -s https://pypi.org/pypi/soupsieve/json | python -c "
import json, sys
d = json.load(sys.stdin)
print([r for r in sorted(d['releases']) if r.startswith(('3.', '4.'))])
print('latest', d['info']['version'], 'requires_python', d['info']['requires_python'])"
['3.0', '3.0.1', '3.0.2', '3.0.3', '3.0.4', '4.0', '4.0.1', '4.1', '4.1.1']
latest 4.1.1 requires_python >=3.11.5
```

(`4.0.0` itself does not exist — the audit's "4.0.0 is the declared fix" matches pip-audit's
report, which names a fix version that was never published under that exact number while
`4.0` and `4.0.1` were. The declared floor is `4.1.1` for that reason: it is the newest
release, and the earliest one this gate can prove.)

So the three advisories (`CVE-2026-126190` / `-126191` / `-126193`, aliases
`GHSA-mmhr-3c5m-f64f`, `GHSA-6655-6qjp-jx7x`, `GHSA-7g44-g9rc-x53w`) **do** have published
patches; the real blocker is `Requires-Python >= 3.11.5`, and this sandbox is Python 3.11.2.
Two ways out were rejected:

* `pip install --ignore-requires-python soupsieve==4.1.1` — installed it, and BeautifulSoup
  then raised `SelectorSyntaxError: Invalid character 'p' position 0` on `select_one('p.x')`,
  because 4.x's selector engine uses constructs 3.11.2 does not provide. A broken import is
  not a security fix; it was rolled back to 2.10 immediately and the flag is never used.
* fabricating an allow-list entry (`no-fix-released`) — refused by the constraint on this
  work and, before that, by the gate: pip-audit *does* report fix versions, so the reason
  self-expires on the next run.

What the repository does instead is declare the floor honestly and let the gate police it:

```toml
"beautifulsoup4>=4.12",
# SECURITY FLOOR … patched soupsieve declares Requires-Python >= 3.11.5
"soupsieve>=4.1.1; python_version >= '3.11.5'",
```

`beautifulsoup4` requires `soupsieve>=1.6.1` with **no upper cap**, so the floor cannot
conflict with it, and every interpreter that can install 4.1.1 gets it. The three
acceptances use a new gate reason, `fix-requires-newer-python`, which the gate re-derives
from live metadata on every run and which expires automatically when any of four things
happens: the advisory moves past the declared floor, the `pyproject.toml` floor disappears,
the running interpreter becomes new enough to install the patch, or PyPI's metadata cannot
be read. (The deptry `DEP002` ignore for `soupsieve` exists because the floor is deliberately
never imported — a declared-but-unimported pin is the classic false positive, and the ignore
is scoped to that one package with its reason in `pyproject.toml`.)

```
$ python scripts/dependency_audit.py
ACCEPTED  CVE-2026-126193 (soupsieve) — every published fix (3.0.2) declares
          Requires-Python >=3.11.5; this interpreter is 3.11.2 (installed 2.10), and
          'soupsieve>=4.1.1' covers the interpreters that can take it
21 advisories: 21 accepted, 0 blocking          # exit 0
```

The honest residue: **this sandbox still runs `soupsieve 2.10` and still carries the three
advisories at runtime.** Nothing here pretends otherwise — the floor is a *declaration* for
the interpreters that can take it, the acceptances are self-expiring, and the proof that the
declaration holds is CI's 3.11/3.12 legs resolving 4.1.1 where the advisory is absent. A
3.12 interpreter could not be obtained here to demonstrate it locally: `python.org` and
`objects.githubusercontent.com` are outside this sandbox's egress allow-list, and the image
ships no development headers, so a CPython source build is not possible either. 33 tests in
`tests/unit/test_dependency_audit_gate.py` pin the gate's behaviour, including that the new
reason refuses an unpublished fix, a missing floor, and an unreadable index.

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
