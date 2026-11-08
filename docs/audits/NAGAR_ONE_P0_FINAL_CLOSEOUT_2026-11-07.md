# NAGAR-ONE P0 Final Closeout & PR Reconciliation — Evidence Report

- **Mission ID:** `NAGAR-ONE-P0-FINAL-CLOSEOUT-AND-PR-RECONCILIATION-02` (P0 / Production Integrity)
- **Date:** 2026-11-07 (UTC)
- **Baseline `main`:** `6838c543`
- **Final `main` (verified live):** `5ed6d4e1`
- **Merge authorization:** granted by the owner via GitHub (API merge).

This report is SHA-bound: every claim below names the commit or check run it rests on.

---

## 1. Merge ledger (live GitHub main)

| PR | Title | Head SHA | Merge commit | Base | Verified |
|----|-------|----------|--------------|------|----------|
| #320 | docs(nagar-one stage-0): product contract + technical truth matrix | `cf037b45` | `a8ce54a4` | `main` | API `merged: true` |
| #321 | fix(nagar-one stage-2): make the boundary-law register unambiguous | `6a30aea0` | `1919dc3d` | `main` | API `merged: true` |
| #319 | feat(task-282 slice-2): durable artifact→revision binding (Stage 1) | `c5b7da56` | `65c020cf` | `main` | API `merged: true` |
| #322 | docs(nagar-one): handoff report for the 2026-11-06 campaign | `b51bcf87` | `5ed6d4e1` | `main` | API `merged: true` |
| #323 | docs(process): NAGAR-ONE engineering skill + session memory | `0e7c4f4d` | — (closed) | `main` | closed as redundant |

Merge order was #320 → #321 → #319 → #322. #321 landed before #319 because #321 renames the
duplicate storage-backend law (`R22`→`R25`) and #319 adds `R29`; both edit the same §3 region of
`docs/architecture/MODULE_MAP.md`, so #321 first makes the register unambiguous and #319's row lands
on top. Each PR required an up-to-date branch (protection is `strict: true`) and was updated with
`main` before merge.

## 2. P0 hardening delivered (fence at the confirmation commit boundary)

Commit `6420432b` (P0 fence + crash-consistency docs), part of #319:

- **Execution-identity fence re-check at the confirm boundary.** `_owns_execution` is re-read
  immediately *before* the durable artifact→revision binding is committed in
  `adapters/in_process_job_queue.py`, closing the window where an attempt could be superseded
  between publication and the confirmation commit.
- **Deterministic fault-injection tests** in `tests/integration/test_durable_project_revision_pinning.py`:
  a superseded attempt between publication and confirm leaves **no** binding; a crash after the
  confirm-commit but before `COMPLETED` (`_SimulatedCrash(BaseException)`) leaves the binding + bytes
  but no `COMPLETED` claim (honest recovery). Both were proven non-vacuous by temporary revert.
- **Mutation** `artifact_confirmation_refusal_ignored` added; campaign `scripts/durable_project_state_mutations.py` = **25** mutants, **25/25 killed**, baseline GREEN before and after.
- Full suite on the hardened head: **6423 passed, 39 skipped** (`PYTEST_EXIT=0`).

## 3. Boundary-law register reconciliation

`main` before the campaign carried a **duplicate `R22`** (two distinct rows) and a **gap** (`R24`→`R26`).

- **#321** renamed the duplicate storage-backend law `R22`→`R25` (filling the gap).
- **#319** adds **`R29`** (durable artifact→revision binding reuses the one storage schema; validated
  content address; exact-revision pin; digest re-measured at bind and on read; typed
  `ArtifactBindingError`; pinned job fails closed).
- Merge resolution: keep `R25` (from main) and `R29` (from the branch).
- **Post-merge verification on `5ed6d4e1`:** register is `R1..R29`, **no duplicates, no gaps**;
  `tests/architecture/test_repo_truth_consistency.py` + `tests/unit/test_truth_doctor.py` pass.

## 4. PR #294 disposition (security)

- OpenGrep `python.sqlalchemy.security.sqlalchemy-execute-raw-query` at `provenance/graph.py:612` is a
  **false positive**: the interpolated value is a closed-set column name from
  `_DIRECTION_COLUMNS` (`_direction_column` raises on anything else); every *value* is
  parameter-bound. Pinned by `test_a_malformed_direction_is_refused_not_silently_defaulted` and
  `test_a_refused_direction_never_reaches_the_statement`.
- Evidence reply posted to the review thread (`discussion_r4505047112`); the thread was **resolved**.
- #294 remains a **separate** open PR (large provenance package, law R28) and will require a rebase
  onto current `main`; not merged in this campaign by design.

## 5. Residuals (honest, not silently closed)

- **Schema-2 pinning enforcement (T5)** — DEFERRED, recorded in `docs/DECISION_LOG.md` **D-0038**.
  Both producers (`nagar/ibpc/service.py`, cognition bridge) still emit schema-2 with **zero**
  `revision_id`; enforcing a pin reds 14 canonical-execution tests → a cross-slice change.
- **Concurrent-revision atomic lock** — mitigated by re-load + re-validate; a transaction-atomic
  lock belongs to the storage-boundary slice (D-0038).
- **#294** — unmerged; needs its own review + rebase (see §4).

## 6. Verification posture (what is CI-verified vs local-only)

- **CI-verified on the merged heads:** the three required checks — `lint (ruff + mypy + version
  lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)` — are green on each
  PR's final head, plus the mutation/parity/extras jobs.
- **Local-only in this session:** the full 6423-test suite and the 25/25 mutation campaign were run
  locally on the hardened head; the merged `main` heads were verified green by CI. A fresh full-suite
  run on `5ed6d4e1` was not separately executed here.

## 7. Board evidence

`scripts/agent_board.py record-pr-merge` verified each merge against the live `main` ref
(API `merged: true`, base `main`, `merge_commit_sha` match, `merge-base --is-ancestor`):
#319→`65c020cf`, #320→`a8ce54a4`, #321→`1919dc3d`, #322→`5ed6d4e1`. `agent_board.py check` reports
**no overlap**.

---

## خلاصهٔ فارسی

- **خط پایه:** `6838c543` — **main نهایی (تأییدشده):** `5ed6d4e1`.
- **ادغام‌شده‌ها:** #320→`a8ce54a4`، #321→`1919dc3d`، #319→`65c020cf`، #322→`5ed6d4e1` (ترتیب:
  #320→#321→#319→#322). **#323 بسته شد** (تکراری: فایل‌ها عیناً روی main بودند و فقط MEMORY.md را
  عقب می‌برد).
- **سخت‌سازی P0 (`6420432b`):** بازبینی مالکیت (`_owns_execution`) دقیقاً پیش از ثبت binding؛
  تست‌های تزریق خطا (تلاش منسوخ‌شده binding نمی‌سازد؛ سقوط پس از confirm بدون ادعای COMPLETED)؛
  جهش جدید `artifact_confirmation_refusal_ignored` → **۲۵/۲۵ کشته**؛ مجموعهٔ کامل **۶۴۲۳ پاس**.
- **ثبت قانون‌ها:** تکرار `R22` و شکاف `R24→R26` رفع شد؛ `R22`→`R25` (از #321) و افزودن `R29`
  (از #319)؛ رجیستر `R1..R29` بدون تکرار و بدون شکاف.
- **#294 (امنیت):** هشدار OpenGrep **مثبت‌کاذب** است (نام ستون از مجموعهٔ بسته، مقادیر پارامتری)؛
  پاسخ با شواهد ثبت و ترد **resolved** شد؛ PR جدا باقی می‌ماند و نیاز به rebase دارد.
- **باقی‌مانده‌های صادقانه (D-0038):** الزام pin برای schema-2 به تعویق افتاد (تغییر بین‌برشی)؛
  قفل اتمیک پنجرهٔ رقابت revision در اسلایس مرز ذخیره‌سازی.
- **وضعیت راستی‌آزمایی:** سه چک الزامی روی headهای نهایی سبزند؛ مجموعهٔ کامل و کمپین جهش در این
  نشست به‌صورت محلی اجرا شدند.

_This report was created by an AI agent (OpenHands) on behalf of the user._
