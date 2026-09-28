# گزارش مأموریت خودکار تولید، تثبیت و مهندسی خلاقانه — 2026-09-28

شاخه: `arena/01a0e742-nexus-ai-agent` · نویسنده: `arena/01a0e742-nexus-ai-agent` (مالک مأموریت)
وضعیت کلی: **PARTIALLY_DONE** — کنترل انتشار بازسازی و اثبات شد؛ کاندیدای ده‌محوری به‌دلیل اجاره‌های فعال منتشر نشد و یک نقض حریم خصوصی در خودِ کاندیدا **بازتولید و مستند** شد.

---

## A — LIVE TRUTH

| موضوع | مقدار مشاهده‌شده (زنده، 2026-09-28) |
|---|---|
| `origin/main` | `e5b326b2eaf691a638d030ad57acf1ce60016ef0` (Merge PR #110) — از جلسهٔ قبل جابه‌جا نشده |
| کاندیدای ده‌محوری | `ac2b7d501a…` به‌صورت **قرنطینه** حفظ شد (تگ محلی `preserved-production-ac2b7d5`، stash نام‌دار، پچ با sha256 `10485146…`) و از اجدادِ انتشار خارج شد |
| تِیپ شاخهٔ من | `0d1ec8d` + کامیت مستندات این جلسه — همهٔ کامیت‌های خروجی فقط در محدودهٔ claiming خودم |
| PR #117 | OPEN، mergeable؛ چک‌های سبز فعلی متعلق به `bdc4106` منتشرشده‌اند، نه به `ac2b7d5` |
| PRهای موازی | #116 (W2, 9e79531) و #113 (W1) باز؛ اجاره‌های فعال تا 2026-09-29 22:39Z و 2026-09-28 21:10Z |
| اجرای زندهٔ preflight | سه اجرا: `shallow_history` → پنج خطای وضعیت legacy → `SUCCESS` با frontier ملزم‌شده به `frontier_sha256` |

کشف مهم: کپی `origin/arena/...` محلی گمراه‌کننده بود؛ داورِ جدید به‌جای رفرس‌های محلی، `git ls-remote` زنده را ملاک می‌گیرد و جابه‌جایی frontier را هم در پایان دوباره کنترل می‌کند [FILE:scripts/agent_board_remote.py:239].

## B — WHAT CHANGED

| فایل | تغییر |
|---|---|
| `scripts/agent_board_remote.py` (جدید) | داور انتشار: مشاهدهٔ frontier زنده، شمارش کل کامیت‌های خروجی با `diff-tree -m --no-renames`، ارزیابی اجاره‌ها با تقدم «تیپِ مالک»، خروجی JSON ملزم‌شده به hash [FILE:scripts/agent_board_remote.py:177] |
| `scripts/agent_board.py` | زیرفرمان `preflight` با کد خروج 0/1/2 [FILE:scripts/agent_board.py:488]؛ متن `check` به «صرفاً مشورتی» اصلاح شد [FILE:scripts/agent_board.py:21] |
| `tests/unit/test_agent_board_remote.py` (جدید) | ۱۳ آزمون شامل مخازن Git واقعی یک‌بارمصرف [FILE:tests/unit/test_agent_board_remote.py:109] |
| `scripts/probes/consent_generation_race.py` (جدید) | کاوشگر اجرایی نقض invariant رضایت/فراموشی روی سورسِ منتخب [FILE:scripts/probes/consent_generation_race.py:6] |
| `.github/workflows/publication-frontier.yml` (جدید) | دو job: آزمون مدل داور + آژیر پس‌از-push روی کل محدودهٔ منتشرشده |
| `docs/architecture/adr/0007-remote-publication-frontier.md`، `docs/ops/INTEGRATION_PREFLIGHT.md`، `docs/audits/2026-09-28-dependency-triage.json`، این گزارش | لایهٔ تصمیم/عملیات/شواهد |
| `AGENTS.md`، `docs/architecture/MODULE_MAP.md`، `docs/DECISION_LOG.md`، `docs/architecture/adr/README.md`، `docs/README.md`، `.agents/board.json` | همگام‌سازی قراردادها و فهرست‌ها |

کاندیدای `ac2b7d5` **هیچ تغییری در این شاخه ایجاد نکرد**؛ فقط بایگانی شد.

## C — WHY

1. **مرز انتشار به‌جای diff کاری.** اثبات زنده: فایلی که کامیت قدیمی به ناحیهٔ اجاره‌دار آورده و کامیت بعدی revert کرده، از `diff remote..HEAD` غایب است ولی در تاریخچهٔ push هست — آزمون واقعیِ [FILE:tests/unit/test_agent_board_remote.py:109] این را روی مخزن یک‌بارمصرف رد می‌کند. پس تأیید انتشار باید روی «محدودهٔ خروجی کامل» تعریف شود، نه فهرست فایل‌های اعلامی (ADR-0007).
2. **تقدم تیپِ مالک.** نسخهٔ «آزادشده» در شاخهٔ ثالث نمی‌تواند اجارهٔ مالک را باطل کند؛ اگر تیپ مالک موجود نیست و ردیف ارثی زنده است، `owner_head_missing` → `NOT_VERIFIED` [FILE:scripts/agent_board_remote.py:163]. این مستقیماً از حادثهٔ PR#112/#113 درآمده است.
3. **قرنطینه به‌جای گروگان‌گیری شاخه.** حفظ `ac2b7d5` به‌صورت تگ+stash+پچِ هش‌شده اجازه داد «ایمنی انتشار» به‌صورت مستقل منتشر شود، بدون حذف کار، بدون push نابجا و بدون شکستن قاعدهٔ ثابت‌بودن شاخهٔ نشست.
4. **کاوشگر به‌جای آزمون سبز.** چون `features/ai_memory.py` در اجارهٔ W2 است، نقصِ یافت‌شده را «تعمیر مخفی» نکردم؛ کاوشگری ساختم که روی سورسِ منتخب اجرا می‌شود و خروج 1 یعنی نقض invariant — شواهد برای مالک W2 و مالک gates.
5. **کالیبراسیون صریح وضعیت‌های legacy.** پنج رکورد دورن‌از دورفتاده (`active_in_review_PR33`، `done_partial_residue_queued`، `queued_reserved_for_B`، دو اجارهٔ زنده با `exclusive_paths: []`) به‌جای سکوتِ مجاز، به‌صورت قواعد بازبینی‌شده در مدل وضعیت ثبت شدند.

## D — VERIFICATION

| شواهد | نتیجه |
|---|---|
| `pytest tests/unit/test_agent_board.py tests/unit/test_agent_board_remote.py` | **40 passed** در 1.58s (`ci-artifacts/mission-20260928/guard-tests.log`) |
| کاوشگر رضایت روی کاندیدای قرنطینه | **FAILURE در هر دو حالت** — پروفایل کهنه پس از `forget_user` بازنوشته شد، حتی با `consent="denied"` (`ci-artifacts/mission-20260928/consent-race.json`، سورس sha256 `8ac6aa7a…`؛ کپی سورس [FILE:ci-artifacts/mission-20260928/candidate_ai_memory.py:137] و [FILE:ci-artifacts/mission-20260928/candidate_ai_memory.py:189]) |
| preflight زنده | سه اجرا با نتایج صادقانه (بخش A)؛ JSON در `ci-artifacts/mission-20260928/preflight-initial.json` |
| lint/format | Ruff 0.16.8 روی فایل‌های جدید/تغییریافته پاک |
| CI | گردش‌کار جدید **فقط محلی و UNVERIFIED از راه دور**؛ چک‌های PR#117 متعلق به `bdc4106` هستند، نه این تغییرات |
| فراتر رفتن از محدوده | هیچ: `production_verified=false` در همهٔ خروجی‌های ماشین‌خوان |

محدودیت‌ها: اجاره‌دار بودن `features/ai_memory.py` یعنی **رفع** نقص یافت‌شده در این جلسه ممنوع بود [FILE:src/nexus_ai_agent/features/ai_memory.py:225]؛ کاوشگر فقط شواهد تولید می‌کند.

## E — SECURITY

| یافته | وضعیت | دلیل مبتنی بر مدرک |
|---|---|---|
| PYSEC-2026-311 / 3813 / 3814 / 3815 (chromadb 1.5.9) | NOT_REACHABLE_WITH_EVIDENCE | صفر مورد `trust_remote_code` در `src/` و `tests/`؛ تنها نقطهٔ ساخت، کلاینت جاسازی‌شده است: [FILE:src/nexus_ai_agent/features/rag.py:184] |
| PYSEC-2026-2132 (click 8.1.8) | **BLOCKED** | صفر فراخوان `click.edit`؛ رفع در 8.3.3 است ولی `gTTS>=2.5,<3` [FILE:pyproject.toml:15] و متادیتای PyPI نسخهٔ 2.5.4 نیازمند `click<8.2` است |
| PYSEC-2026-2447 (diskcache 5.6.3) | NOT_REACHABLE_WITH_EVIDENCE | صفر import در `src/` (فقط ترانزیتیو)؛ CVSS4.0 برابر 5.2 با بردار محلی و دسترسی نوشتن به پوشهٔ cache |

هیچ هشداری mask نشد؛ JSON کامل با دستورهای grepِ قابل‌تکرار: `docs/audits/2026-09-28-dependency-triage.json`. خودِ داور انتشار هم یک کنترل امنیتی است: نشت قبلی «publication leak» (نشت فایل اجاره‌دار در تاریخچه) اکنون با `diff-tree -m` روی کل محدوده بسته می‌شود و URLهای remote در خطاها هرگز چاپ نمی‌شوند.

## F — PRODUCTION STATUS (per axis)

| محور | وضعیت |
|---|---|
| داور مرز انتشار (جدید) | VERIFIED محلی (۴۰ آزمون + اجرای زنده)؛ UNVERIFIED از راه دور (CI جدید هنوز run نشده) |
| کاندیدای ده‌محوری `ac2b7d5` | BLOCKED — اجاره‌های W1/W2/DR + نقض حریم خصوصی بازتولیدشده |
| رضایت/فراموشی (W2- leased) | FAILURE اثبات‌شده در کاندیدا؛ UNVERIFIED در سورس W2 تا اجرای کاوشگر بر اتحاد reconciled |
| توزیع وابستگی‌ها | UNVERIFIED در production (فقط قفل محلی + triage اولیه) |
| بازیابی/RESTORE | UNVERIFIED برای RTO/RPO واقعی (مطابق گزارش قبلی) |
| هر ادعای production | همچنان `production_verified: false` |

## G — COORDINATION

- claiming: `mission-publication-frontier-01a0e742` با zone محدود به فایل‌های جدید/مشورتی [FILE:.agents/board.json:831]؛ push فوری کامیت board.
- اجاره‌های فعال رعایت شد: W2 (#116)، W1 (#113)، DR (#96). هیچ فایل اجاره‌داری تغییر نکرد؛ `features/ai_memory.py` صرفاً خوانده و از آرشیو مقایسه شد.
- handoffهای قبلی همچنان باز: کامنت‌های PR #116/#113/#96 (issuecomment-5869185755 / 5869186010 / 5869186221) و وضعیت PR #117 (issuecomment-5869223205).
- معیار پذیرش جدید به تسک اضافه شد: پیش از هر ادغام آینده، خروجی کاوشگر رضایت روی اتحادِ reconciled باید 0 شود.

## H — REMAINING RISKS

1. کاندیدای `ac2b7d5` یک نقض حریم خصوصی تأییدشده دارد؛ حتی پس از آزادی اجاره‌ها، ادغامِ بدون رفع و بدون خروج 0 کاوشگر ممنوع است.
2. داور، push را **اتمیک همراه خودِ push** مهار نمی‌کند (شرط آگاهانه؛ پنجرهٔ race کوچک و با آژیر CI پس‌از-push پوشش داده می‌شود).
3. کالیبراسیون وضعیت‌های legacy تصمیم انسانی است؛ وضعیت‌های جدیدِ ناشناخته fail-close می‌شوند (امن، ولی پرتکرار تا پاک‌سازی boardها).
4. اعتبار «NOT_REACHABLE» وابسته به ثبات معماری است (مثلاً راه‌اندازی سرور Chroma در آینده یافته‌ها را زنده می‌کند).
5. CI محلیِ جدید و lock جدید هنوز شاهد اجرای راه دور ندارند.

## I — CREATIVE DISCOVERIES

1. **«مرز انتشار» به‌عنوان یک موجودیت مشاهده‌شونده.** به‌جای «چک کردن فایل‌ها»، واحد تحلیل، جفتِ (frontier زنده، محدودهٔ خروجی کامل) است. این مدل دو کوری هم‌زمان را می‌بندد: کوری نسبت به تاریخچه (revert) و کوری نسبت به مالکیت (کپی board). آزمون revert [FILE:tests/unit/test_agent_board_remote.py:109] ثابت می‌کند که حتی `git diff` هم این موجودیت را نمی‌بیند.
2. **اجاره به‌عنوان صدورِ مالک، نه وضعیت محیطی.** رد اجاره فقط از «تیپِ فعلی مالک» معتبر است؛ هر کپی ثالث شنیده‌شدن است. این قرارداد، حادثهٔ واقعی PR#112/#113 را به یک قانون کلی تبدیل می‌کند.
3. **UNKNOWN به‌عنوان نتیجهٔ درجه‌یک.** `NOT_VERIFIED` (خروج 2) هم‌رتبهٔ SUCCESS/REJECTED است؛ «تاریخچهٔ کم‌عمق»، «board خراب» و «frontier متحرک» دیگر هرگز به «بدون تداخل» ترجمه نمی‌شوند.
4. **الگوی کاوشگر برای کد اجاره‌دار.** وقتی رفع ممنوع است، یک اجراییِ مستقل با معنای «خروج 1 = نقض invariant» هم شواهد می‌سازد و هم دروازهٔ پذیرش آینده می‌شود — به‌جای آزمون سبزی که حقیقت را پنهان کند.
5. **قرنطینهٔ محتوایی به‌جای انجماد شاخه.** تگ + stash نام‌دار + پچِ هش‌شده، کارِ بلاک‌شده را بدون قربانی‌کردن «قابلیت انتشار مستقلِ ایمنی» حفظ می‌کند.

## J — NEXT LEVER

**یک قدم با بیشترین اهرم: حل سه handoff (W2/W1/DR) و سپس ادغام مشروطِ `ac2b7d5` طبق دروازهٔ کاوشگر.** چرا: تنها این مسیر هم ۱۱هزار خط کار آزمون‌شدهٔ قفل‌شده را آزاد می‌کند، هم نقص حریم خصوصیِ اثبات‌شده را از طریق مالک مجازش (W2) بسته و با خروج 0 کاوشگر اثبات می‌کند، هم دروازهٔ انتشار تازه‌ساخته را به اولین آزمون واقعی خود می‌رساند. هر کار دیگری (از جمله publish این guard) یا در حال حاضر انجام شده، یا پس از آزادی این مسیر ارزان‌تر می‌شود.
