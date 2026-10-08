# Continuum final recovery and closure (task-184, task-154), 2026-09-27

Session branch: `arena/01a0e1e0-nexus-ai-agent`. Successor PR: **#102**. It supersedes #95 and #98.

> **Continuation (2026-09-27, session branch `arena/01a0e2bb-nexus-ai-agent`).** A session is bound to
> exactly one branch, so this work cannot be pushed to `arena/01a0e1e0`. The previous session's corrected
> head `6198510f0a06a803ba445e03bba1e44306eaf0f9` was **never pushed and does not exist** — `git fetch origin
> 6198510f0a06a803ba445e03bba1e44306eaf0f9` answers `upload-pack: not our ref`. Its content is therefore
> **re-derived** here from the failing head `eb6551b`, never copied from a report, and the delivery vehicle
> becomes a successor PR raised from `arena/01a0e2bb-nexus-ai-agent`. #95, #98 and #102 stay open until that
> successor is merged to `main` and evidenced (§12).

Every claim below carries one of four labels: **PROVEN**, **PARTIALLY PROVEN**, **NOT PROVEN** or **BLOCKED**. A claim is PROVEN only by an artifact named here: a commit, a run, a job, an artifact or a local log.

> **Scope of this version.** This file is committed *inside* PR #102, so it cannot contain the CI run of the commit that carries it, or the merge. Those facts live in two places: the evidence comments on PR #102, and the closure addendum (§12) that lands on `main` after the merge. Sections that depend on them say so, and are labelled NOT PROVEN in this version.

---

## 1. Live truth (Phase 0, re-proven before any edit)

| Item | Value | Label |
|---|---|---|
| `main` at session start | `6624a133329a22f1b66dce463e0b207385ee8416`, unprotected, all merge methods allowed | PROVEN (GitHub API) |
| `main` CI at start | run 36221530309, success | PROVEN |
| Surviving remote ancestor | `48db0561f8b866f45561a225022532beb84e5d87` (board commit), CI run 36305593463 success | PROVEN |
| #95 / #98 | OPEN; heads `7a8d7a7` and `c26c2ba`; both are ancestors of this branch | PROVEN (`git merge-base --is-ancestor`) |
| `maintenance` scheduled workflow | red on `main` before this work; not touched here | PROVEN (pre-existing) |
| task-185 files | inherited byte for byte from the #95 lineage; untouched | PROVEN (`agent_board.py check`) |
| `main` moved during CI | PR #101 merged at 2026-09-27T11:06:10Z as `e6b06e04d1e541aa5809694e42a571579fa4f460`; #102 turned CONFLICTING | PROVEN (see §3) |

## 2. Recovery event

- An earlier local lineage of this work was never pushed, and it was lost when the sandbox was re-provisioned. It consisted of `ccb4a0f`, `e427be2`, `c44bf2c`, `04aaffb`, `45fdb0c` and `7bda42c5b3922f7364c5c91b55e3f70a67548aa0`. **HISTORICAL EVIDENCE LOST — CONTENT RECONSTRUCTED.** These objects do not exist and were **not** recreated. No result reported for them is used as evidence. That includes "2839 passed" and "73 mutations".
- Forensic backup, taken before any branch move, at `/home/user/forensics/continuum-recovery-2026-09-27/` (sandbox-local). It holds `git diff --binary`, a tar of the untracked files, and a sha256/size manifest of the 38 changed paths.
- The surviving working tree hashed to git tree `ddc76334c0579c611d8aecafa7d429c6ffc9faa6`. `git reset --mixed 48db056` moved HEAD only, and the tree hash was re-verified as identical afterwards.
- The committed snapshot inside C2 (`dca3390`) still carried the stale record, whose step pointed at the lost `04aaffb`. Before republishing, `nexus continuum verify` rejected it without any workaround. It exited 1 with:

  ```
  state loss detected: recorded good commit 04aaffb… is not reachable from HEAD dca3390…
  test count mismatch: expected 2869, found 2871
  ```

  — PROVEN (the verifier detects exactly the state loss that occurred).

## 3. New lineage

The lineage is fast-forward only on the remote. There was no force push, no rebase and no history rewrite.

| Commit | Content |
|---|---|
| `48db056` | surviving ancestor |
| `fa94fa4936b16b614926fac7b165b15b75787970` | C1: code, tests and CI (20 files). Message: "fix(continuum): complete evidence closure after sandbox recovery" |
| `dca3390aa8d5ef106ac6fd0133b245140fb7b3d0` | C2: docs and board, plus the stale snapshot (see §2) |
| `350afbd00f1fd85a08028b2c64b29a54a394e943` | C3: snapshot republished at `dca3390` (2871 tests, cpython 3.11.2) |
| `65b7f8a5a9a83e44b91c43c64e75182af153d23b` | C4: repair of a pre-existing flaky test (§4, F3), and a read-only annotation step in `continuum-evidence` |
| `c950434152275a102d1a75dca71e5c227d92d2f9` | C5: snapshot republished at `65b7f8a` |
| `e3abb8f7bcf77a40b94e9e4110485ced7f5a74ca` | merge of `origin/main` (`e6b06e0`, PR #101). ci.yml keeps both new jobs. The board merge is additive and was resolved at JSON level: the result minus main's additions equals this branch's board. |
| `f68757e16fde4b3a878b27d93f534d8c5831ed30` | F2 fix: the substrate target for the trust root (§4) |
| *(this commit)* | snapshot republished at `f68757e` (2924 tests), docs, and this audit |

Status: PROVEN for every hash above (`git log`, `git ls-remote`).

## 4. Code changes and findings

- **Contract (D-0023).**
  - Pack coverage is a blocking 95% per-pack gate: a canonical run, a verified measurement, and every pack ≥ 95%.
  - The snapshot verifier fails closed.
  - There is a replayable mutation campaign of 81 entries, including the `ci` family CI1–CI7, which mutates the `continuum-evidence` job itself.
  - Status: PROVEN by tests and by the campaign (§6).
- **F1 (recovery audit): hollow pack.**
  - A pack whose every module is comment-only was dropped from the report, and therefore from the threshold check.
  - It is now the measurement issue `hollow_packs`.
  - RED was shown before the fix. There is a regression test and mutation D6.
  - Status: PROVEN.
- **F2 (found by the merge): trust root not measured.**
  - After merging PR #101, the canonical run on `e3abb8f` was **NOT ACCEPTED**: `core` 79.26% < 95% (`ed25519.py` 27.27%, `trust.py` 43.75%, `verify.py` 94.35%). The new substrate modules' contract suite, `tests/unit/test_pack_trust_root.py`, was not a canonical target. The gate failed closed.
  - Fix: add that suite to `SUBSTRATE_TEST_TARGETS`, giving 27 targets. `core` is now 95.89% (980/1022). No line was excluded, and neither the threshold nor the denominator changed.
  - Regression test: `test_every_tested_substrate_module_is_exercised_by_the_canonical_targets`. It was RED on `['ed25519', 'trust']` before the fix and is GREEN after.
  - Status: PROVEN.
- **F3 (found by #102's CI): flaky test, out of scope and trust-breaking.**
  - `test_number_guess_keeps_state` (from `fe58145`, already on `main`) guessed a fixed 50 against `random.randint(1, 100)`.
  - It failed pull_request run 36313347629, job 108603348287, with `AssertionError: assert '🎉 آفرین! عدد 50 بود. با 1 حدس پیدا شد!' in (…)`. The push run 36313310134 on the byte-identical tree `c5160975…` was green, and so was python-parity (3.12) on the same merge checkout.
  - The failure was reproduced locally by pinning the RNG to 50: same assertion text.
  - The test now pins the secret. It passes with the global RNG forced to 1, 50 and 100. Production code is unchanged.
  - Status: PROVEN.

## 5. Test evidence (local, cpython 3.11.2 only)

| Head | Result | Label |
|---|---|---|
| `350afbd` | full `pytest`: **2841 passed, 30 skipped** (`/home/user/forensics/evidence-350afbd/pytest-full.log`) | PROVEN (local) |
| `f68757e` | canonical coverage **ACCEPTED**: TOTAL 97.37% (5078/5215), 27 targets, 397 tests passed, source digest `sha256:5c3fe6f5…`. The per-pack results are in the table below. | PROVEN (local) |
| final head | the full local chain is recorded in the PR #102 evidence comment | NOT PROVEN in this version |

Per-pack coverage at `f68757e`:

| Pack | Coverage |
|---|---|
| audio | 96.55% |
| caption | 97.09% |
| core | 95.89% |
| delivery | 100% |
| edit | 96.46% |
| motion | 98.55% |
| slideshow | 97.89% |

## 6. Mutation evidence

- `350afbd`, local: `nexus.continuum-mutations/1`, 81 entries: **80 killed, 1 not-applicable** (D2, which is observable only on 3.10), `passed: true`.
  - CI1–CI7 were all killed.
  - Every entry follows the cycle mutation → invariant → RED → restoration (sha256) → GREEN.
  - sha256 `261d6ccb…6ebc`.
  - Status: PROVEN (local).
- 3.10 / 3.12, on `350afbd`: the campaign step of `continuum-evidence` passed on every leg (§8). The campaign exits 0 only when every applicable mutation is killed and restored, so on 3.10 D2 was killed. The per-record JSON of those legs sits in artifacts that this sandbox could not download: the blob store refused TLS. Status: PARTIALLY PROVEN (verdict proven, records not inspected). From `65b7f8a` on, the job prints every record as a check-run annotation.

## 7. Reproducibility evidence (`350afbd`, local)

Each artifact was produced twice and compared with `cmp`. The byte-identical results:

| Artifact | sha256 |
|---|---|
| `pack-coverage.json` | `d572ea98…0f03` |
| `continuum-gate.json` | `a5888361…a601` |
| `continuum-mutations.json` | `261d6ccb…6ebc` |

`--verify-artifact` exited 0. No nondeterminism was observed. Status: PROVEN (local). CI repeats the same ×2 `cmp` on every leg.

## 8. CI evidence

**Push run 36313310134 on `350afbd`:** success, all 15 jobs. Status: PROVEN.

| Leg | Job | Artifact | Artifact ID | Digest |
|---|---|---|---|---|
| continuum-evidence 3.10 | 108603243286 | `continuum-evidence-350afbd…-py3.10` | 10930067314 | `sha256:55161968…7369` |
| continuum-evidence 3.11 | 108603243284 | `continuum-evidence-350afbd…-py3.11` | 10929912523 | `sha256:7d6836b4…be45` |
| continuum-evidence 3.12 | 108603243230 | `continuum-evidence-350afbd…-py3.12` | 10929867986 | `sha256:c4601d0e…dbd9` |

**Pull_request run 36313347629 on `350afbd`:**
- 14 of 15 jobs were green, including all three continuum-evidence legs.
- `test` failed on F3 (§4).
- Status: PROVEN (failure root-caused and repaired, not rerun into green).

**Push run 36314996133 on `c950434`:** superseded by the merge of `main`. Cancelling it was refused with HTTP 403, so its result is not used as evidence.

**Final head:** see the PR #102 evidence comment and §12. Status: NOT PROVEN in this version.

## 9. Python matrix

| Python | `350afbd` (push run 36313310134) | Final head |
|---|---|---|
| 3.10 | PROVEN, job 108603243286 | NOT PROVEN in this version |
| 3.11 | PROVEN, job 108603243284 (local 3.11.2 as well) | NOT PROVEN in this version |
| 3.12 | PROVEN, job 108603243230 | NOT PROVEN in this version |

## 10. PR / merge / main

- PR #102: open, base `main`, head `eb6551b`. The body carries the exact SHA and the lineage.
  Its `test` job and all three `python-parity` legs are **red**:
  `docs/audits/CONTINUUM_CLOSURE_2026-09-27.md` — the file you are reading — was added without a row in
  `docs/README.md`, and `tests/unit/test_docs_integrity.py::test_every_document_is_indexed_in_docs_readme`
  rejects exactly that. Reproduced locally on `eb6551b`: `1 failed, 2893 passed, 30 skipped`. The fix
  indexes the document; the invariant, the threshold and the measurement are untouched.
- Delivery vehicle: a successor PR from `arena/01a0e2bb-nexus-ai-agent`, whose history contains
  `eb6551b` (so every commit above is preserved verbatim) plus `main` `f53923d` merged in.
- Merge, `main` ancestry, `main` CI, `continuum verify` on `main`, release lineage: recorded in §12 after the merge. Status: NOT PROVEN in this version.

## 11. Limitations

- Python 3.10 and 3.12 cannot be installed in the sandbox. Their evidence is CI-only.
- Artifact and log downloads from the Actions blob store fail with TLS errors in the sandbox. Artifact identity is proven by GitHub's recorded digest. Content is proven through check-run annotations, from `65b7f8a` on.
- `.nexus/continuum.json` is machine-bound (D-0006): cpython 3.11.2, alembic 1.20.0, SQLAlchemy 2.0.54. CI reports it with `blocking: false`, and `verify` reports it STALE after any later change to an evidence root.
- `release_lineage`: no `v3.13.0` tag or GitHub Release exists. It is reported as OPEN (a diagnostic), not RED.
- The scheduled `maintenance` workflow was red on `main` before this work. It is outside this scope.

## 12. Closure

Status: **NOT PROVEN in this version.** Closure requires all of the following:
- green CI on the exact final head for 3.10, 3.11 and 3.12;
- the merge of #102 with the expected head;
- verified `main` ancestry and `main` CI;
- `nexus continuum verify` on `main`.

When they hold, the closure addendum on `main` records them. Only then do task-154 and task-184 become `done` and #95/#98 get closed as superseded.
