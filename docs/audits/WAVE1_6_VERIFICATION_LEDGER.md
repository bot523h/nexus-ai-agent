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
git rev-list --count deab2b3e..HEAD              # 1495
git rev-list --count --first-parent deab2b3e..HEAD   # 42
git rev-list --count da3e5ed7..HEAD               # 21
```

The directive issued for this closeout expected **161 commits**. No measurement
reproduces that figure: not the total (1495), not the first-parent count (42), not
the wave window (21). The three real counts are given above; 161 is not quoted
anywhere else in this file. The 1495 is dominated by 1453 commits that a previous
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

`483403d..HEAD` — 21 commits. Everything in §3–§7 is one of these.

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
| 21 | *(this file's commit)* | 5 | this ledger + `docs/evidence/p0/ci_parity_addendum.json` |

Integrity: no `rebase`, no `reset --hard` on an existing commit, no force-push was
used at any point. The only history edit was `git commit --amend` on `ce46a62`
seconds after it was created and **before any push**, to replace a commit message
that a shell heredoc had truncated to a single `Co-authored-by:` line; its tree was
not touched.

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

These were the gates **before** this branch's first CI run; §6.3 then records what
that run found and what changed. Measured at `ce46a62` unless a different revision is
named; at that point the only delta to the ledger commit was `pyproject.toml` (deptry
configuration) and documentation, so the suites below applied to the tree as committed.
The CI-run follow-up in §6.3 changed `pyproject.toml`, `.github/workflows/ci.yml`, the
PostgreSQL schema golden and one architecture sentence, and re-ran the unit and
architecture suites — see §6.3.4 for those numbers.

| Gate | Command | Result |
|------|---------|--------|
| Lint | `ruff check .` | `All checks passed!` (ruff 0.16.8 — the CI pin, `.github/workflows/ci.yml:880`) |
| Format | `ruff format --check .` | 1395 files already formatted |
| Types | `mypy src` | `Success: no issues found in 552 source files` |
| Dependencies | `deptry .` | `Success! No dependency issues found.` (whole tree, `9da4619`) |
| Dependencies (gate scope) | `deptry src` | `Success! No dependency issues found.` |
| Operation truth | `python -m nexus_ai_agent.diagnostics.truth --root .` | `OK — no findings` (version-lockstep, docs-index, law-test-resolution, law-numbering, claim-witnesses, fail-open-defaults, board-truth) |
| Pack coverage | `python scripts/pack_coverage.py` | **ACCEPTED**, `9da4619`, 9/9 packs ≥ 95%, source `sha256:8289dab0…` |
| Chunked parity | `python scripts/ci_parity.py` | **exit 0**, 14/15 chunks green, 1 skipped for space, collection equivalence proven at 11 837 = 11 837 |
| Mutation harnesses | `pytest -k "mutation or mutant or kill"` | **486 killed**, 1 skipped, 0 survived |
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
`collection: CI 11837 ids, union 11837 ids, missing 0, duplicated 0, unexpected 0`
→ `collection equivalence proven: the chunks are exactly the CI selection`.

| Chunk | Result | Collected | Wall |
|---|---|---:|---:|
| `tests/architecture` | 406 passed | 406 | 63.2 s |
| `tests/bench` | 3 passed | 3 | 2.2 s |
| `tests/creative/audio_engine` | 163 passed | 163 | 5.8 s |
| `tests/creative/chronos` | 240 passed | 240 | 12.9 s |
| `tests/creative/composition` | 306 passed | 306 | 81.7 s |
| `tests/creative/director` | 68 passed | 68 | 1.7 s |
| `tests/creative/episode_causality_lab` | 183 passed | 183 | 6.7 s |
| `tests/creative/morpheus` | 281 passed | 281 | 4.0 s |
| `tests/creative/packs` | 155 passed | 155 | 7.6 s |
| `tests/creative/speech_cinema` | 284 passed, 4 deselected | 284 | 33.3 s |
| `tests/creative/veritas` | 648 passed | 648 | 17.7 s |
| `tests/integration` | 661 passed, 36 skipped | 697 | 241.4 s |
| `tests/storage` | 43 passed | 43 | 1.8 s |
| `tests/unit` | 6833 passed, 15 skipped | 6848 | 640.1 s |
| `tests/creative/atlas` | **not run** (space) | 1512 | — |

**Totals: 10 274 passed, 51 skipped, 4 deselected, 0 failed, 0 errors.** The one
chunk that did not run is §8.1.

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
| `source_revision` | the commit the file was regenerated at (recorded in the file itself) |
| catalog operations | 70 |
| runtime registry | 88 |
| overlap | 47 · gaps 23 · extras 41 |
| universe | 111 |
| executable surface | 9 |
| migration head | `20261112connect` |
| truth doctor | `OK — no findings` |

The suite-executed count in the truth file comes from the same resolution pass the
doctor re-checks (`law-test-resolution`), so it cannot drift from the tree.

`docs/evidence/p0/ci_parity_addendum.json` records this wave's commands, exit
codes, timings, the SHA-256 of the parity record and of the coverage artifact, and
the two limitations of §8. It contains no invented output: every field was captured
from a run (see §9 for the generator).

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

**6.3.4 Re-run after the fixes.** Unit + architecture suites were re-run on the patched
tree (see the commit that carries this paragraph for the numbers), and the same gates
listed in §4 were re-verified: ruff, `ruff format --check`, `mypy src`, `deptry .` and
the truth doctor.

## 7. Where this work is published

Pushed to `origin/arena/fd837e50-nexus-ai-agent` (the only branch this session may
push to). A pull request to `main` from this branch carries the Run ID and the
per-chunk CI results; PR #343 remains a separate, untouched branch (§1).

---

## 8. Honest gaps

**8.1 The ATLAS chunk did not run here — 1512 tests, reported as not run.**
`tests/creative/atlas` needs ~18 GiB of temporary space; this machine had 8.2 GiB
free, and the runner says so instead of silently dropping it
(`skipped_for_space: [{"path": "tests/creative/atlas", "needs_bytes": 19327352832}]`).
Its sources and tests are **byte-identical** to `4bcee33`, where the campaign last
ran to completion (1491 passed, 21 skipped, 120/120 mutants killed):

```bash
git diff --stat 4bcee33 HEAD -- tests/creative/atlas src/nexus_ai_agent/creative/atlas
# (empty)
```

And collection equivalence still *counts* its 1512 ids against the CI selection, so
the numbers in §4.2 are not silently short: the missing piece is execution, not
coverage of the selection.

**8.2 No PostgreSQL for the P0 bundle.** `docs/evidence/p0/evidence.json` is
untouched: its probes need a live PostgreSQL and no value was fabricated. The gap
narrowed during §6.3 — a real PostgreSQL 16.2 (`pgserver`) was used for the schema
golden and the `migrate-postgres` legs — but that instance is scratch-space and is
not the P0 bundle's database, so the bundle still needs a Postgres-backed run.

**8.3 The parity run is not GitHub Actions.**
`CHUNKED_SUITE_VERIFICATION_WITH_COLLECTION_EQUIVALENCE` means: the *selection* is
proven identical to CI's, the tests really ran, and the environment is *not* CI's
(no runner image, no service containers, no workflow-level triggers, other jobs in
`ci.yml` not run). It is a substitute for nothing; it is the best local signal
available while §6.2 holds.

**8.4 The local dependency audit and CI disagree about three `soupsieve`
advisories.** §6.3.2: locally blocking, green in CI on the same `pyproject.toml`, and
the closure is identical to `main`'s. No allow-list entry was added on the strength of
either run — an acceptance needs a security review this closeout does not perform — so
the discrepancy is documented as open instead of being closed by assertion.

**8.5 Branch protection could not be read.** `gh api
repos/bot523h/nexus-ai-agent/branches/main/protection` returns HTTP 403 ("Resource
not accessible by integration") for this token, so the required-check policy is
**not verified** here — and it is not modified either, per the directive.

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
    --ignore=tests/creative/atlas                        # 486 killed
/home/user/.venv/bin/python -m pytest tests/creative/packs   # benchmarks, median + MAD
```

`scripts/ci_parity.py --list` prints the chunks; `--skip-collection-proof` exists
for triage and marks the run as unequivalent in its own JSON.
