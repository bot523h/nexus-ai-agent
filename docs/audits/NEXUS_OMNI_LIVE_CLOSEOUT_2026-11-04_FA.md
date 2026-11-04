# بستنِ زندهٔ NEXUS-OMNI — گزارش تحویل (۲۰۲۶-۱۱-۰۴)

> **وضعیت سند:** سند تاریخ‌دار و زندهٔ بستن کمپین NEXUS-OMNI. شش گزارش مورخ
> ۲۰۲۶-۱۱-۰۳ بازنویسی **نشده‌اند**؛ آن‌ها اسناد تاریخ‌دار و خواندنی‌اند. این سند
> وضعیت *پس از* ادغام هفت PR را ثبت می‌کند.
>
> **مبنای حقیقت:** `main` @ `b63a22a86dd396ca3508a133693461295cc8d1ff`
> (از GitHub بازخوانی شد، ۲۰۲۶-۱۱-۰۴). آخرین اجرای CI روی `main`:
> run `41847992497` → `completed / success` (۲۲ موفق، ۰ شکست، ۱ skipped).

## ۱. عکس فوری آرنا (Live Truth)

| مورد | مقدار (از GitHub بازخوانی شد) |
|---|---|
| `main` HEAD | `b63a22a86dd396ca3508a133693461295cc8d1ff` |
| آخرین CI روی `main` | run `41847992497` — `completed / success` |
| نتیجهٔ شغل‌ها | ۲۲ success · ۰ failure · ۱ skipped |
| PRهای باز | **۴۲** پیش از این PR؛ **۴۳** با احتساب #311 |
| برد (board) | schema 2 — ۱۸۵ ادعا · ۸۸ منطقه · ۱۰ `next_work` |
| هفت PR ادغام‌شده | #304 #305 #306 #307 #308 #309 #310 |

SHAهای ادغام (همه با `pulls/{n}.merge_commit_sha` از GitHub تأیید شدند):

| PR | Merge SHA | موضوع |
|---|---|---|
| #304 | `6b62e391` | نصب حداقلی کمپین queue-ownership (رفع `Errno 28`) |
| #308 | `7180cd35` | نصب Torch فقط-CPU (رفع سرخِ `main`) |
| #310 | `ed69d981` | ثبت وظیفهٔ بنیاد JCS (RFC 8785) در برد |
| #305 | `6883f038` | اعتبارسنجی `R2_BUCKET` و بسته‌شدن در خطا |
| #306 | `c474f574` | شش گزارش ممیزی NEXUS-OMNI |
| #307 | `408e9599` | ارتقای pinهای LangGraph (GHSA-6hmw-9432-67qq) |
| #309 | `b63a22a8` | لایهٔ B2 بک‌بلِیز برای نگهداری (دوگانه با R2) |

## ۲. گزارش Manus — موجود نیست (صادقانه)

درخواست شده بود که گزارش `ci-artifacts/audit-20261104/NEXUS_FULL_READONLY_AUDIT_2026-11-04.md`
و فایل‌های همراه (`evidence_ledger.json`، `independent_mutations.json`،
`boundary_probes.json`، `pytest_baseline_final.txt`، `cleanup_final.txt`، `مهار/*.log`)
بررسی شوند.

**نتیجهٔ جست‌وجو:** این فایل‌ها **در محیط کاری و در هیچ‌یک از ۲۳۰ شاخهٔ مخزن وجود ندارند.**

- `find /` برای نام‌های بالا: هیچ نتیجه‌ای.
- `ci-artifacts/` وجود ندارد.
- `git log --all` و `git ls-files` برای این نام‌ها: هیچ نتیجه‌ای.

بنابراین هیچ ادعایی دربارهٔ محتوای آن گزارش‌ها ثبت نمی‌شود و هیچ «۲۰ یافته» یا
«۱۳ وظیفهٔ اصلاحی» از آن منبع بازنویسی نمی‌شود؛ زیرا متن واقعی آن در دسترس نیست.
این سند بر پایهٔ **شش گزارش مورخ ۲۰۲۶-۱۱-۰۳ (که اکنون روی `main` هستند)** و
**شواهد مستقیم GitHub** تهیه شده است.

## ۳. وضعیت دقیق CI

- **CI اصلی روی `main`:** سبز. run `41847992497` روی `b63a22a8` = `completed / success`.
- **ریشهٔ سرخیِ پیشین:** از `6b62e391` به بعد، شغل‌های `python-parity`،
  `extras-matrix (speech)`، `backup-db` و `continuum-evidence` با
  `OSError: [Errno 28] No space left on device` می‌مردند، چون
  `pip install -e ".[dev]"` زنجیرهٔ CUDA/Torch (~۲.۳GB) را می‌کشید.
  #308 با `scripts/ci_install.sh` (Torch فقط-CPU از
  `download.pytorch.org/whl/cpu` با `--no-deps --isolated`، سپس PyPI با
  `--isolated -c constraints/ci-cpu-torch.txt`، سپس postcondition بسته) آن را رفع کرد.
- **`workflow_dispatch` و امنیت workflowها:** بررسی جداگانه در PHASE F.

## ۴. تغییرات B2 و R2

- **B2 (ترجیحی):** `src/nexus_ai_agent/storage/providers/b2.py` اضافه شد (#309)؛
  انتخاب لایه در `build_blob_provider`؛ B2 کامل برنده است، B2 ناقص بدون R2 کامل
  `ProviderUnavailable` می‌دهد (هرگز نوشتن خاموش در باکت اشتباه)، B2 ناقص در کنار
  R2 کامل به R2 برمی‌گردد.
- **R2 (فالبک):** #305 اعتبارسنجی نام باکت و بسته‌شدن در خطا را افزود.
- پیش‌نیازها: `NEXUS_B2_KEY_ID`، `NEXUS_B2_APPLICATION_KEY`، `NEXUS_B2_BUCKET`،
  `NEXUS_B2_ENDPOINT`، `NEXUS_B2_REGION`؛ یا `R2_ACCOUNT_ID`، `R2_ACCESS_KEY_ID`،
  `R2_SECRET_ACCESS_KEY`، `R2_BUCKET`.
- **هیچ مقدار Secretی خوانده، نمایش داده، یا تغییر داده نشد.**

## ۵. وضعیت واقعی پشتیبان‌گیری و بازیابی

این بخش با شواهد اجرای واقعی CI بررسی شد:

- **restore-drill (پای محلی SQLite):** آخرین اجرای **موفق**
  (run `41304383416`، `restore-drill` = `success`). این پای محلی، بازیابی از یک
  آرتیفکت ساخته‌شده با همان تابع بکاپ را اثبات می‌کند — نه بازیابی از B2/R2.
- **backup-db (پای تولیدی):** آخرین اجراها **شکست خورده‌اند**
  (run `41817663855` روی `6b62e391`). پیام دقیق preflight:
  `missing repository secrets: NEXUS_DATABASE_URL`.
  یعنی چهار Secret مربوط به R2 **تنظیم شده‌اند**، اما `NEXUS_DATABASE_URL`
  تنظیم نشده است. کار به‌درستی **بسته** (fail-closed) عمل کرده و آرتیفکتی تولید نکرده است.
- **housekeeping:** در اجراهای زمان‌بندی‌شده `skipped` است (شرط زمان‌بندی دوشنبه).
- **نتیجه:** مسیر remote (آپلود/بازیابی از B2 یا R2) **اثبات‌نشده** است و باید
  `OWNER_ACTION_REQUIRED` ثبت شود.

### راهنمای تنظیم (بدون نمایش مقدار)

1. `NEXUS_DATABASE_URL` — یک URL کامل PostgreSQL (`postgresql://…`). **الزامی برای backup-db.**
2. لایهٔ B2 یا R2 — یکی از دو مجموعهٔ کامل بالا.
3. محیط و باکت آزمایشی جداگانه تعریف شود؛ **باکت تولیدی برای آزمایش حذف/بازنویسی نشود.**
4. پس از تنظیم، اجرای واقعی و غیرمخرب `workflow_dispatch` توسط مالک انجام شود
   تا مسیر remote **VERIFIED** شود.

## ۶. SEC-4 — دروازهٔ یکپارچهٔ خروج اطلاعات به Provider

**مسیر تأییدشده (از کد):**

```
bot/handlers.py::ai_cmd  →  GeminiEngine.chat  →  httpx.AsyncClient  →  generativelanguage.googleapis.com
```

- `ai_cmd` (خط ~۴۶۱) مستقیماً `gemini_engine.chat(...)` را صدا می‌زند و **هیچ
  گیتِ Privacy/Policy ندارد**.
- `GeminiEngine._call_gemini` (خط ~۱۹۵) با `httpx.AsyncClient` به Google می‌زند.
- دروازهٔ egress موجود در `features/ai_memory.py` فقط برای *استخراج حافظه* است و
  به مسیر چت وصل نیست.
- `nagar/ibpc/privacy.py` یک `PrivacyPolicy` دارد، اما در مسیر `/ai` قدیمی
  فراخوانی نمی‌شود.

**وضعیت:** باز. معماری پیشنهادی (گیت در مرز واقعی فراخوانی Provider، نه Patch در
هر Handler) در PHASE D ثبت می‌شود. تا تصمیم صریح مالک، **تغییر رفتار Runtime ممنوع** است.

## ۷. وضعیت PRهای باز (دسته‌بندی)

| دسته | شمار | نمونه‌ها |
|---|---|---|
| READY (کوچک، برد-هم‌راستا) | ۳ | #285، #290، #301 |
| NEEDS_REBASE (فقط کهنه) | ~۱۵ | #57، #60، #63، #73، #84، #92، #121، #122، #137، #157 … |
| NEEDS_FIX | ~۱۰ | #33، #99، #113، #115، #117، #122، #149 … |
| DUPLICATE/SUPERSEDED | ۲ جفت | #113/#115، #135/#140 |
| NEEDS_SECURITY_REVIEW | ~۶ | #58، #66، #149، #205، #284، #294 |
| NEEDS_EXTRACTION | ~۸ | #33، #69، #118، #193، #205، #230، #247، #297 |
| DEFERRED (Draft) | ۸ | #58، #92، #114، #117، #118، #150، #204، #257، #273، #284، #285 |

جدول کامل per-PR در بخش ۹ (Ledger) و در `docs/audits/NEXUS_OMNI_PR_CONVERGENCE_LEDGER_2026-11-03.md`.

## ۸. رانش تخته (Board Drift) و اصلاح آن

پیش از این سند، شش ادعای `task-omni-*` با وضعیت واقعی ناسازگار بودند:

| وظیفه | وضعیت پیشین | وضعیت واقعی | اصلاح |
|---|---|---|---|
| `task-omni-ci-disk-budget-01` | `expired` | PR #304 ادغام شد | `completed_merged` |
| `task-omni-ci-cpu-torch-01` | `expired` | PR #308 ادغام شد | `completed_merged` |
| `task-omni-r2-bucket-validation-01` | `active` | PR #305 ادغام شد | `completed_merged` |
| `task-omni-audit-reports-01` | `active` | PR #306 ادغام شد | `completed_merged` |
| `task-omni-dep-supply-chain-01` | `active` | PR #307 ادغام شد | `completed_merged` |
| `task-omni-b2-maintenance-tier-01` | `active` | PR #309 ادغام شد | `completed_merged` |

هر اصلاح با `merge_commit_sha` و `delivered_pr` و `reconciled_at` ثبت شد؛ `generation`
یک پله جلو رفت (fencing)؛ مالک/گیت آزاد شد. `task-omni-sec4-legacy-privacy-01`
همچنان **`deferred`** و `task-jcs-rfc8785-foundation-01` همچنان **`queued`** است —
هیچ‌کدام به‌اشتباه `completed` نشدند.

## ۹. آنچه هنوز اثبات نشده است (NOT PROVEN)

1. **مسیر remote بکاپ/بازیابی از B2/R2:** نیازمند `NEXUS_DATABASE_URL` و یک باکت
   آزمایشی جداگانه است. وضعیت: `OWNER_ACTION_REQUIRED`.
2. **SEC-4:** بدون تصمیم مالک، هیچ تغییر Runtime مجاز نیست. وضعیت: `مسدود`.
3. **adversarial تست‌های SEC-4:** تعریف شده‌اند اما اجرا نشده‌اند (وابسته به تصمیم).
4. **استقرار Production / Rollback:** دسترسی وجود ندارد. وضعیت: `NOT_PROVEN`.
5. **بازیابی واقعی از باکت تولیدی:** عمداً انجام نشد (باکت تولیدی دست‌نخورده می‌ماند).
6. **جفت‌های تکراری PR (#113/#115، #135/#140):** نیازمند بررسی مالکیت و سپس
   پیشنهاد بستن — ادغام/بستن خودسرانه مجاز نیست.

## ۱۰. برنامهٔ اصلاحی و مالک هر کار

| اولویت | کار | مالک | وضعیت |
|---|---|---|---|
| P0 | تکمیل Secretهای بکاپ (`NEXUS_DATABASE_URL` + لایهٔ B2/R2) | مالک | `OWNER_ACTION_REQUIRED` |
| P0 | تصمیم‌های پنج‌گانهٔ SEC-4 | مالک | `مسدود` |
| P1 | اجرای واقعی `workflow_dispatch` بکاپ + اثبات restore remote | مالک/مهندس SRE | `به تعویق` |
| P1 | استخراج PRهای بزرگ (#33/#69/#118/#193/#205/#247) | مالک PR | `به تعویق` |
| P1 | تعیین تکلیف جفت‌های تکراری (#113/#115، #135/#140) | مالک | `به تعویق` |
| P2 | hash-pinning و provenance برای Torch | مهندس CI | `به تعویق` |
| P2 | تست‌های ناسازگاری Python 3.10/3.11/3.12 | مهندس CI | `به تعویق` |
| P3 | کار JCS (RFC 8785) | مالک `task-jcs-rfc8785-foundation-01` | `queued` |

## ۱۱. وضعیت نهایی هر یافته

- **تأییدشده:** سبزیِ CI روی `main`؛ ادغام هفت PR؛ بسته‌شدنِ fail-closed گیتِ
  HMAC/SSRF/Shell؛ لایهٔ B2؛ اعتبارسنجی R2؛ pinهای CVE.
- **با محدودیت‌ها تأیید شد:** نصب CPU-only Torch (بدون hash-pinning).
- **سخت‌شده اما کامل نشده:** preflight بکاپ (بسته عمل می‌کند، اما Secretها ناقص‌اند).
- **مسدود:** SEC-4 (منتظر تصمیم مالک).
- **به تعویق افتاده:** PRهای بزرگ، جفت‌های تکراری، استقرار Production.

## ۱۲. پیوست — شواهد دقیق فازهای C/E/F

### C. شواهد دقیق بکاپ (از لاگ CI)
- run `41817663855` (روی `6b62e391`) → شغل `backup-db` = `failure`؛
  `housekeeping`/`restore-drill` = `skipped`.
- خط خطای واقعی preflight: `::error missing repository secrets: NEXUS_DATABASE_URL`
  → سپس `Refusing production backup; no artifact was produced or uploaded.`
- **استنباط قطعی:** چهار Secret مربوط به R2 (`R2_ACCOUNT_ID`، `R2_ACCESS_KEY_ID`،
  `R2_SECRET_ACCESS_KEY`، `R2_BUCKET`) تنظیم شده‌اند (چون پیام «missing» آن‌ها را
  نام نمی‌برد)، اما `NEXUS_DATABASE_URL` تنظیم نشده است.
- `restore-drill` (پای محلی) در run `41304383416` = `success` — اثبات بازیابی از
  آرتیفکت SQLite ساخته‌شده با همان تابع بکاپ، **نه** بازیابی از B2/R2.
- **وضعیت:** `OWNER_ACTION_REQUIRED` برای تنظیم `NEXUS_DATABASE_URL` (و در صورت
  تمایل، لایهٔ B2 کامل)؛ مسیر remote `NOT_PROVEN`.

### F. شواهد دقیق CI/Supply-Chain
- **همهٔ چهار workflow فایل روی `main`** (`ci.yml`، `langsmith-smoke.yml`،
  `maintenance.yml`، `queue-ownership-mutations.yml`) دارای بلوک `permissions:` هستند
  و همهٔ `uses:` با SHA پین شده‌اند (بدون `@vN`/`@main`)؛ هیچ `pull_request_target`
  وجود ندارد.
- **صحت ادعای Torch:** بررسی مستقیم ایندکس
  `https://download.pytorch.org/whl/cpu/torch/` نشان می‌دهد `torch==2.15.0` برای
  cp310 **وجود ندارد** ولی `2.14.1` برای cp310/cp311/cp312 **وجود دارد** — یعنی
  توضیح `constraints/ci-cpu-torch.txt` («2.15.0 cp310 را حذف کرد») **صحیح** است.
- **`extras_matrix.py check`** = `ok` (legs: core/pdf/speech/translate؛ parity:
  3.10/3.11/3.12) — هم‌خوانی pyproject ↔ CI ↔ تست.
- **دکتر مستندات:** `docs/ops/b2-storage.md` و `docs/ops/r2-storage.md` که
  workflow به آن‌ها ارجاع می‌دهد موجودند.
- **مشاهدهٔ P3:** `queue-ownership-mutations.yml` به‌جای `scripts/ci_install.sh` از
  یک `pip install` دستی بدون `--isolated` استفاده می‌کند؛ چون Torch نصب نمی‌کند
  خطر dependency-confusion ندارد، اما برای یکنواختی می‌تواند به `ci_install.sh`
  منتقل شود. وضعیت: `به تعویق` (بدون تأثیر بر امنیت فعلی).

### E. سه کار کوچک، مستقل و دارای مالکیت روشن (برای شروع)
1. **#285** — `chore(board): synchronize task-278 deferred status` (فقط
   `.agents/board.json`، +14/−6). مالک: `arena/nagar-causal-memory-director-board-sync-20261030-0042`.
2. **#290** — `chore(board): register task-282 durable project state lifecycle`
   (فقط `.agents/board.json`، +63/−1). مالک: `arena/nexus-durable-project-state-board-20261030-1028`.
3. **#301** — `chore(automation): engineering-debt watchdog` (چهار فایل
   `.agents/evidence/AGENT_DEBT_WATCHDOG_*.md`). مالک: `automation/debt-watchdog`.

هر سه کوچک و قابل‌بررسی‌اند، اما همه `dirty`/`behind` هستند و **نیازمند
به‌روزرسانی روی `main` و تأیید مالک** پیش از ادغام‌اند؛ هیچ‌یک بدون بررسی ادغام نشد.

## ۱۳. بازتولید

```bash
# حقیقت زنده
git fetch origin && git rev-parse origin/main
gh api repos/bot523h/nexus-ai-agent/actions/runs?branch=main\&per_page=1

# گیت تخته
python scripts/agent_board.py show
pytest -q tests/unit/test_agent_board.py

# یکپارچگی مستندات
pytest -q tests/unit/test_docs_integrity.py
```

---

_این سند توسط یک عامل هوش مصنوعی (OpenHands) به نمایندگی از مالک ساخته شده است._
