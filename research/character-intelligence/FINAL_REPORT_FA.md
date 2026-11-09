# گزارش نهایی فارسی — هوش شخصیت نگار / Character Capsule

**مأموریت:** NAGAR Character Intelligence (ARENA-B) — نمونهٔ مستقل، نسخه‌بندی‌شده و
اندازه‌گیری‌شده برای «کپسول شخصیت» و «موتور راستی‌آزمایی پیوستگی»، بدون هیچ تداخلی با
Arena 1.

**شاخه:** `arena/337c3bc1-nexus-ai-agent` — **SHA این گزارش:** در بخش «وضعیت گیت‌ها»
ثبت شده است.

**تاریخ اجرا:** ۲۰۲۶-۱۱-۰۹ — همهٔ اعداد زیر از اجرای واقعی در همین محیط استخراج شده‌اند.

---

## ۱) خلاصهٔ مدیریتی

یک کپسول شخصیت واقعی با شناسهٔ `NAGAR-CHAR-000001` ساخته شد که هویت تغییرناپذیر را از
تغییرات مجاز جدا می‌کند، به همراه موتور راستی‌آزمایی مستقل با هشت وضعیت بسته،
بنچمارک ۱۲ سناریویی، بستهٔ شواهد content-addressed و ۵۸ تست که همه واقعاً اجرا شده‌اند.

**نتیجهٔ صادقانه:** ترتیب و ساختار تصمیم‌گیری درست است، اما حاشیهٔ اطمینان باریک است؛
ابعاد چهره در split هلد‌اوت FAR = ۰٫۶۰۰ و FRR = ۰٫۲۵۰ دارند و این یعنی نمونهٔ اولیه،
**گیت تولید نیست**.

---

## ۲) محیط واقعی اجرا

| مورد | مقدار |
|---|---|
| سیستم | `Linux-6.1.158+-x86_64-with-glibc2.36` |
| CPU / RAM | ۲ vCPU / حدود ۳ گیگابایت |
| پایتون | 3.11.2 |
| GPU | ندارد — `NOT_MEASURED: no GPU device present` |
| ffmpeg / torch / onnxruntime / opencv | نصب نیست |
| کتابخانه‌ها | numpy 2.4.6، pillow 13.0.0، PyNaCl، pytest 9.2.0، ruff |

## ۳) دستورهای اجراشده (قابل تکرار)

```bash
# راه‌اندازی
python3 -m venv .venv && . .venv/bin/activate
pip install numpy pillow pynacl pytest ruff
pip install -e . --no-deps

# بنچمارک (۶۳ فریم، ۱۲ سناریو)
PYTHONPATH=. python -m experiments.character_capsule.benchmark \
    --work-dir experiments/character_capsule/.work
# → 12/12 سناریو، exit 0

# تست‌ها
PYTHONPATH=. python -m pytest tests/character_capsule --noconftest -q
# → 58 passed in 28.76s

# لینت
ruff check experiments/character_capsule scripts/character_capsule tests/character_capsule
# → All checks passed

# گیت‌های مخزن (بدون تداخل)
PYTHONPATH=. python -m pytest tests/architecture/test_scripts_import_boundary.py \
  tests/architecture/test_test_suite_hygiene.py tests/architecture/test_no_orphan_modules.py \
  tests/architecture/test_modular_monolith.py tests/architecture/test_repo_truth_consistency.py \
  tests/unit/test_docs_integrity.py tests/unit/test_agent_board.py --noconftest -q
# → 259 passed

# راستی‌آزمایی یک شات
PYTHONPATH=. python scripts/character_capsule/verify_shot.py \
  --calibration experiments/character_capsule/.work/results/calibration.json \
  --image experiments/character_capsule/.work/frames/000001-shot-angle.png \
  --reference "experiments/character_capsule/.work/frames/000001-ref-*.png" \
  --shot-id SH-102
# → VERIFIED_WITH_LIMITATIONS

# بررسی بستهٔ شواهد
PYTHONPATH=. python scripts/character_capsule/check_evidence.py \
  --bundle experiments/character_capsule/.work/results/evidence_bundle.json \
  --asset-dir experiments/character_capsule/.work/results
# → status UNSIGNED (exit 2): یکپارچگی قابل بررسی است، اصالت نه
```

## ۴) شناسه‌های محتوایی (digest) همین اجرا

| آرتیفکت | digest |
|---|---|
| پیکره (۶۳ فریم، ۳ شخصیت) | `sha256:b62debe73ce66e37d9917efc3812097229177e56a34599d33ccdf82047cb2bc1` |
| کالیبراسیون | `sha256:c4abbfa644fc214e6fd89f50a0c8384f16a9d39aa1a1c1e2e4db7aeaa51b951b` |
| نتایج بنچمارک | `sha256:a57faefa3d18863f3afe2df44f92f5a1867fc771d61118abb434b2eb05252c1d` |
| بستهٔ شواهد | `sha256:dda24a9da5e672234be06606c3f4a9b4cd78cd11431d773a7312bd5a514b3e36` |
| claim بستهٔ شواهد | `sha256:35be37f371622579e31b2ccc207bade574701435cf52d5cb2a87352173ebe977` |

پیکره از نظر بازتولیدپذیری تست شده است: دو اجرا در دو پوشهٔ مستقل، digest یکسان و
دنبالهٔ verdict یکسان می‌دهند.

## ۵) نتیجهٔ ۱۲ سناریو

| # | سناریو | verdict واقعی |
|---|---|---|
| ۱ | همان شخص، همان دوربین | `VERIFIED_WITH_LIMITATIONS` |
| ۲ | همان شخص، زاویهٔ ۳۲ درجه | `VERIFIED_WITH_LIMITATIONS` |
| ۳ | همان شخص، نور ۳۴۰۰K شدت ۱٫۱۸ | `INSUFFICIENT_EVIDENCE` (گیت کیفیت) |
| ۴ | همان شخص، لبخند | `VERIFIED_WITH_LIMITATIONS` |
| ۵ | همان شخص، لباس دوم ثبت‌شده | `VERIFIED_WITH_LIMITATIONS` |
| ۶ | همان شخص، صحنهٔ دیگر + زخم canon | `REVIEW_REQUIRED` |
| ۷ | همان شخص، فریم تمام‌قد | `REVIEW_REQUIRED` |
| ۸ | همان شخص، سکانس گام | `REVIEW_REQUIRED` |
| ۹ | **شخص دیگر شبیه (look-alike)** | `IDENTITY_DRIFT` |
| ۱۰ | **تعارض canon** (`scar=none` بعد از EVT-001) | `CANON_CONFLICT` |
| ۱۱ | اضافه: لباس برنامه‌ریزی‌نشده | `WARDROBE_DRIFT` |
| ۱۲ | اضافه: هویت صدا | `VERIFIED_WITH_LIMITATIONS` |

زمان کل اجرا: ۶٫۸۴ ثانیه — میانگین هر سناریو ۲۴۹ میلی‌ثانیه — اوج حافظه ۵۷٫۳ مگابایت.

## ۶) اعداد کالیبراسیون (روی شخصیت هلد‌اوت)

| بعد | accept ≤ | reject ≥ | n (واقعی/جعلی) | EER هلد‌اوت | FAR | FRR |
|---|---|---|---|---|---|---|
| body | 0.1000 | 0.6655 | 6 / 6 | 0.000 | 0.000 | 0.000 |
| **face** | 0.1149 | 0.2149 | 7 / 19 | **0.325** | **0.600** | **0.250** |
| motion | 1.4422 | 1.5422 | 6 / 1 | 0.000 (n=1) | 0.000 | 0.000 |
| scene | 0.0604 | 0.1604 | 1314 / 10 | `NOT_MEASURED` | — | — |
| voice | 1.3496 | 1.4496 | 6 / 1 | 0.000 (n=1) | 0.000 | 0.000 |
| wardrobe | 0.7047 | 2.0869 | 78 / 54 | 0.000 | 0.000 | 0.077 |
| hair | — | — | — | فقط advisory | — | — |

## ۷) چه چیزی واقعاً کار می‌کند

1. **هویت تغییرناپذیر:** ویرایش فیلد شناسه با `ScopeViolation` رد می‌شود؛ تنها راه،
   ساخت `identity_version` جدید است (تست‌شده با ۱۶ تست).
2. **تایم‌لاین لباس:** تغییر برنامه‌ریزی‌شده مجاز، تغییر خارج از برنامه →
   `WARDROBE_DRIFT`؛ دورهٔ زمانی لباس (era) اجرا می‌شود.
3. **canon:** واقعیت‌های تغییرناپذیر، واقعیت‌های تغییرپذیر و دفتر رویداد؛ «سکوت دربارهٔ یک اثر»
   دیگر تعارض محسوب نمی‌شود، بلکه `REVIEW_REQUIRED` است.
4. **راستی‌آزمای مستقل:** هشت وضعیت بسته با ترتیب اولویت ثابت؛
   `INSUFFICIENT_EVIDENCE` بالاتر از وضعیت‌های تأییدشده است.
5. **گیت کیفیت:** فریم clipped/کوچک به‌جای «drift» به `NOT_MEASURED` و در نتیجه
   `INSUFFICIENT_EVIDENCE` می‌رسد (منطبق با رویکرد ISO/IEC 29794-5).
6. **شواهد:** بستهٔ content-addressed با یک hard binding و امضای اختیاری Ed25519؛
   دست‌کاری یک بایت، status را به `INVALID` می‌برد (تست‌شده).

## ۸) محدودیت‌های اعلام‌شده (بدون آرایه‌آرایی)

| ادعایی که **نمی‌شود** | دلیل |
|---|---|
| «تشخیص چهره در حد تولید است» | FAR ۰٫۶۰ در ۴ نمونهٔ واقعی و ۱۰ نمونهٔ جعلی — ضعیف |
| «انتقال آستانه‌ها به عکس واقعی» | پیکره رویه‌ای (procedural) است → `NOT_MEASURED` |
| «پیوستگی مو قابل راستی‌آزمایی است» | نمونهٔ تغییر مو در پیکره نیست → آستانه‌ای ندارد، فقط advisory |
| «مقایسه با مدل‌های یادگیری عمیق انجام شد» | هیچ وزن مجاز آفلاین موجود نبود → `NOT_MEASURED` |
| «شواهد امضا شده است» | کلید امضا در این محیط نیست → `UNSIGNED` و صریحاً اعلام می‌شود |
| «هزینهٔ GPU بر شات» | GPU موجود نیست → `NOT_MEASURED` |
| «بازبینی انسانی تأیید کرد» | بازبین انسانی موجود نبود → `NOT_MEASURED` |

**نکتهٔ مهم دربارهٔ ۱۲/۱۲:** این عدد یعنی «هر سناریو یکی از وضعیت‌های از پیش اعلام‌شده
را تولید کرد»، نه «دقت ۱۰۰٪». پنج سناریو در فاصلهٔ چهرهٔ ۰٫۰۰۰ هستند چون فریم ارزیابی
تقریباً همان فریم مرجع است؛ و split هلد‌اوت همان بعد چهره FAR ۰٫۶۰ دارد. عدد ۱۲/۱۲
را نباید به‌عنوان دقت تولید گزارش کرد.

## ۹) انطباق با Arena 1 (صفر تداخل)

مسیرهای ممنوعه با `git status --porcelain` بررسی شدند و **صفر تغییر** دارند:
`src/nexus_ai_agent/creative/**`، `src/nexus_ai_agent/nagar/**`، `docs/architecture/adr/**`،
`pyproject.toml`، CI. کل تغییرات این مأموریت در چهار مسیر جدید است:

```
research/character-intelligence/     ← ۲۲ سند (شامل ۵ ADR و همین گزارش)
experiments/character_capsule/       ← ۱۶ ماژول نمونهٔ اولیه
tests/character_capsule/             ← ۶ ماژول، ۵۸ تست
scripts/character_capsule/           ← ۳ CLI نازک
.agents/board.json                   ← فقط از طریق CLI رسمی برد (zone + claim)
```

هیچ ماژول نمونه اولیه، چیزی از `nexus_ai_agent` ایمپورت نمی‌کند؛ هیچ صف/باس/رجیستری
جدیدی ساخته نشده است. گزارش کامل: `ARENA1_COMPATIBILITY_REPORT.md`.

## ۱۰) تصمیم‌های ثبت‌شده (ADR)

| ADR | تصمیم |
|---|---|
| ADR-0001 | هویت تغییرناپذیر، ظاهر به‌صورت revision |
| ADR-0002 | هشت وضعیت بسته با ترتیب اولویت ثابت |
| ADR-0003 | آستانه‌ها کالیبرهٔ هر پیکره، هیچ‌گاه hardcode |
| ADR-0004 | توصیفگر کلاسیک در نمونه، امبدینگ پشت seam و DEFERRED |
| ADR-0005 | صفر کوپلینگ با Arena 1 |

## ۱۱) گام بعدی پیشنهادی (NEXT)

1. کش observation گالری (کاهش ۲۴۹ms به کمتر از ۲۵ms) — EXPERIMENTAL
2. افزودن نمونهٔ تغییر مو برای کالیبره کردن بعد مو — EXPERIMENTAL
3. نمونه‌های انسداد (شال روی کت) — EXPERIMENTAL
4. look-alike های بیشتر برای منحنی FAR با نمونهٔ کافی — EXPERIMENTAL
5. امضای آرتیفکت کالیبراسیون — EXPERIMENTAL

## ۱۲) وضعیت گیت‌ها و SHA

- شاخه: `arena/337c3bc1-nexus-ai-agent`
- کامیت پایه: `5ed6d4e` (merge PR #322)
- کامیت برد (zone + claim): `b7df701` — فقط از طریق CLI رسمی برد
- کامیت‌های این مأموریت: روی همان شاخه، بلافاصله بعد از `b7df701`
  (SHA دقیق در توضیح PR و در `git log` شاخه ثبت شده است؛ ذکر SHA یک کامیت داخل
  همان کامیت باعث مرجع چرخشی می‌شود و به همین دلیل اینجا تکرار نشده)
- گیت‌ها: `58 passed` (تست‌های اختصاصی)، `259 passed` (گیت‌های مخزن)،
  `ruff: All checks passed`

**جمع‌بندی یک‌خطی:** کپسول شخصیت و راستی‌آزمای پیوستگی، واقعی، نسخه‌بندی‌شده،
قابل‌تکرار و صادق دربارهٔ ضعف خودش هستند؛ نه بیشتر و نه کمتر.
