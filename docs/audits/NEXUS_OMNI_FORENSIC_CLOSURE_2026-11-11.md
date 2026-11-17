# NEXUS-OMNI-FORENSIC-CLOSURE-2026-11-11 — Forensic Report

**Mission:** NEXUS-OMNI-FORENSIC-CLOSURE-2026-11-11 (P0 stability/reliability closure on `main`).
**Date:** 2026-11-10 (evening) – 2026-11-11.
**Scope:** verify real truth (Stage 0), repair what is broken, prove with evidence, report negatives honestly.
**Hard red lines honoured:** no merge, no force-push, no branch-protection change, no deletion.

Every claim below is labelled **Verified**, **Evidence-Based Estimate**, **Experimental**, or **Unknown**.

---

## 0. Reality (Stage 0)

| Fact | Value | Status |
| --- | --- | --- |
| Base branch | `main` = `5fe94512` (merge of PR #324) | Verified |
| PR under repair | #330 `arena/nagar-delta-dialogue-vs1`, head was `cf141762` | Verified |
| CI on `cf141762` | 4 jobs RED: `test`, `python-parity` 3.10/3.11/3.12 | Verified |
| Environment | `ci_install.sh ".[dev]"` → `torch==2.14.1+cpu`, no CUDA, pytest 9.2.0 | Verified |
| Editable install | `/home/openhands/.local/lib/python3.13/site-packages` (user-site) | Verified |
| System site-packages | **not writable** by uid 10001 → `python -I` drops the editable | Verified |

---

## 1. The real defect: OPERATION_TRUTH drift (PROVEN, FIXED, GREEN)

### Root cause (Verified)

PR #330 added `tests/unit/test_ibpc_delta.py`, which names the creative operation id
`timeline.trim`. `nagar.sources.read_suite_evidence` scans `tests/**/*.py` with `rglob`, so a
fresh recomputation immediately listed the new file under
`timeline.trim.suite_reference_files`. The committed `OPERATION_TRUTH.json` was never
regenerated, so the stored projection no longer equalled a fresh recomputation.

### Fix (Verified)

Regenerated with the official generator, `python -m nexus_ai_agent.nagar` (**not** `--docs` —
the plain invocation is what writes `OPERATION_TRUTH.json`; `--docs` additionally rewrites the
Markdown projections). The diff was exactly the two declared non-deterministic fields
(`source_revision`, `generated_at`) plus the new suite reference. Committed `6c367704`, pushed
**fast-forward** (`cf141762..6c367704`, no force).

### Proof (Verified)

- Truth gate locally: **18 passed**; mutation suite: **23 passed**.
- Determinism: only the declared non-deterministic fields differ between two runs.
- **CI run `42910614078` on `6c367704`: all 24 jobs SUCCESS**, including the four that were red.
- PR #330 is now `MERGEABLE` (was `BLOCKED`).

### `--check` fail-closed contract (Verified)

| Tree state | `--check` exit |
| --- | --- |
| clean (projection matches sources) | `0` + `TRUTH GATE: GREEN` |
| one suite reference removed | `1` + `JSON DRIFT: [evidence_drift] …` |

---

## 2. Original engineering: a local pre-push truth gate (PR #331)

### The gap (Verified)

The gate existed and was correct, but **no local rail ran it**:

- `.pre-commit-config.yaml` carries only `ruff` + `mypy` — no truth hook;
- the pre-commit hooks are **not installed** in a fresh checkout (`make hooks` is required);
- nothing in `scripts/` or the `Makefile` referenced the truth gate.

So drift was only ever caught by CI, on an already-pushed SHA — the exact failure that redded
PR #330. This is a recurrence risk for the whole truth-projection mechanism.

### The change (Verified)

- `Makefile`: `truth-check` target (declared `.PHONY`) running
  `python -m nexus_ai_agent.nagar --check` — the same never-writing, exit-non-zero shape CI uses.
- `tests/unit/test_ci_truth_parity.py`: three checks that keep the rail wired, mirroring the
  existing `test_ci_lint_parity.py` pattern:
  1. the target exists and runs exactly the gate command;
  2. the target is `.PHONY` (a stray file cannot no-op the rail);
  3. the command is fail-closed on the live tree.

### Adversarial proof (Verified)

GREEN → MUTANT → RED → RESTORE → GREEN:

| Mutant | Expected | Observed |
| --- | --- | --- |
| repoint the target to `@echo skip` | `test_makefile_declares_truth_check_target` RED | RED |
| drop `truth-check` from `.PHONY` | `test_truth_check_target_is_phony` RED | RED |
| restore | all GREEN | GREEN |

Regression slice (`truth_parity` + `ci_lint_parity` + `ci_extras_parity` + `operation_truth_gate`
+ `operation_truth_mutations`): **85 passed**. `ruff check`, `ruff format --check`, `mypy`: clean.

**CI run `42918018092` on `17843ac1`: all 24 jobs SUCCESS.** PR #331 is `MERGEABLE`.

---

## 3. IBPC triad wiring — honest status (Stage 2-F)

**Verified by reading the source, not by report.**

| Module | Reachability | Wired into the canonical path? |
| --- | --- | --- |
| `nagar/ibpc/delta.py` | re-exported by `intent.py` | **half-wired** (intent only) |
| `nagar/ibpc/shadow.py` | `ibpc/__init__.py` (reachability law R20) | **NOT_WIRED** to `service.py`/approval |
| `nagar/ibpc/signature.py` | `ibpc/__init__.py` | **NOT_WIRED** to `service.py`/approval |

The canonical preview path is `service.py → preview.py`, independent of shadow/signature. The
`ibpc/__init__.py` docstring's canonical chain does **not** list the triad; the three modules
appear in `__all__` for reachability only. This is a real, reportable gap — not hidden.

### What *is* proven (Verified)

- The real-ffmpeg integration tests **do run** (they are not skipped): `imageio-ffmpeg` resolves
  the binary, so `tests/integration/test_ibpc_shadow_ffmpeg.py` (11) and
  `tests/integration/test_ibpc_signature_ffmpeg.py` execute against a real `ffmpeg`.
- Triad suite: **204 passed** (unit delta/shadow/signature + both ffmpeg integration suites + e2e).
- A standalone vertical-slice proof (real ffmpeg, no mocks) held **9/9** claims:
  real keyframe build (~107 ms), independent verifier, determinism (two runs bit-identical),
  single-flipped-digest tamper detection, distinct-source ⇒ distinct composite, 64-D L2-normalised
  CIE-Lab histogram, deterministic signature, similarity search ranking, typed empty-scope refusal.

---

## 4. Security & policy (Stage 3)

- `shell=True`: 4 textual hits, **1 real** (`cli.py` `ShellTool(enable_shell=True)`); the other 3
  are docstrings. (Verified)
- Shadow/Signature spawn ffmpeg via `subprocess.run(cmd_list, capture_output=True, timeout=…)` —
  no shell, explicit timeouts. (Verified)
- An architecture boundary test enforces the subprocess-spawn allow-list. (Verified)
- CI permissions = `contents: read` (least privilege); push gated to `main`/tags; concurrency
  `cancel-in-progress`. (Verified)

---

## 5. Architecture single-authority audit (Stage 7)

- **CommandBus**: a single definition (`creative/studio/bus.py`). No parallel bus. (Verified)
- **Approval**: two modules, two **distinct domains**, not duplication —
  `agent/approval.py::ApprovalSystem` is the durable, DB-backed approval for sensitive actions;
  `ibpc/approval.py::ApprovalLedger` is in-memory, identity-bound evidence of a human decision
  that explicitly is **not** a permission grant (the canonical Policy/CommandBus still authorize).
  (Verified by reading both docstrings.)

---

## 6. CI/CD forensics (Stage 5)

- 24 jobs audited; all green after the fix. (Verified)
- **Repo-level misconfiguration:** `gh pr checks` reports a `CircleCI Pipeline` **failure on every
  PR** — the repository has **no `.circleci/`** and CircleCI is **not** a required check. Required
  checks on `main` are exactly `lint`, `test`, `merge-base-guard`. This makes every PR
  `UNSTABLE`, but not blocked. (Verified)
- **Diagnostic gotcha:** an Actions run where every job "fails" in ~10 s is usually a **cancelled
  superseded run** (`cancel-in-progress`), not a real failure. Always check
  `gh run view <id> --json conclusion` and `gh run list --branch <b>` for the current run.
  (Verified — cost a false alarm this session.)

---

## 7. Pre-existing failures (NOT introduced, honestly reported)

The full local suite is **5973 passed / 2 failed / 13 skipped**. The two failures are in
`tests/unit/test_selfhood_ledger.py`:

- `test_crash_restores_every_charter_entry_and_open_commitment`
- `test_two_processes_cannot_both_open_the_same_commitment`

**Root cause (Verified):** the tests spawn `python -I` (isolated ⇒ `-s` disables user-site). The
editable install lives in the user-site directory; under `python -I` it is invisible, so the
child cannot import `nexus_ai_agent`. CI installs into system site-packages, so `-I` is harmless
there. The same two failures reproduce **identically on `main`**; PR #330 does not touch selfhood.
This is an environment artefact of the local container, not a code defect.

---

## 8. PR landscape (Stage 4/6) — read-only, not touched

- ~48 open PRs; PR #325/#326/#327 carry 29–32 human reviews; PR #330 had none.
- PR #326/#327 are `DIRTY`/`CONFLICTING`; PR #325 is `BLOCKED`/`MERGEABLE` (task-282).
- **No other agent's PR was modified.** The `pr-repair-83` claim on PR #330 was kept and its
  remote board verified correct (`active`), with both peer leases (`task-282…`, `task-324…`)
  intact.

---

## 9. Verdict

| Question | Answer |
| --- | --- |
| Is `main`-bound CI stable after these changes? | **Yes** — PR #330 and PR #331 each pass all 24 jobs. |
| Was the defect real and its root cause proven? | **Yes** — projection drift, reproduced and fixed deterministically. |
| Is the fix genuinely killable/verifiable? | **Yes** — adversarial mutant proof on the new rail; fail-closed `--check`. |
| Are the IBPC triad modules fully wired? | **No** — honestly `NOT_WIRED` to `service.py`; a follow-up, not a hidden claim. |
| Any red lines crossed? | **No** — no merge, no force-push, no protection change, no deletion. |

**Deliverables shipped:** PR #330 repaired (green), PR #331 opened (green) adding a local
pre-push truth gate + parity guard.

*This report was produced by an AI agent (OpenHands) on behalf of the repository owner.*
