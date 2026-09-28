# گزارش اثبات سخت‌سازی (Hardening Proof) — 2026-09-28

شاخه: `arena/01a0e742-nexus-ai-agent` · مالک: `hardening-proof-01a0e742`
وضعیت نهایی: **PARTIALLY_VERIFIED** — مرز انتشار سخت‌سازی و اثبات شد؛ نقص حریم خصوصی روی منبع main **و** تیپ W2 به‌صورت اجرایی مستند شد و به مالک W2 تحویل گردید؛ قرنطینهٔ `ac2b7d5` در بازسازی sandbox از دست رفت (ثبت شفاف)؛ ریشهٔ قرمزی سوئیت کامل CI یافت و در محدودهٔ مالکیت این شاخه اصلاح شد.

هر بخش با ساختار «Evidence → Observation → Conclusion» است.

---

## A — Live Truth

| موضوع | Evidence → Observation → Conclusion |
|---|---|
| main | `git ls-remote origin refs/heads/main` → `e5b326b2eaf6…` → main نسبت به سشن قبل جابه‌جا نشده است. |
| شاخهٔ من | remote tip = `5903dda` (پس از pushهای این سشن) → هویت شاخهٔ نشست حفظ شده؛ upstream ست شد. |
| sandbox | reflog فقط `clone` + `checkout`، `.git/shallow=e5b326b`، نبودِ `bdc4106/2bddbbb/ac2b7d5` پیش از fetch → **sandbox از نو ساخته شده بود** (clone تازه + فایل‌های worktree قبلی روی آن). |
| بازیابی | `git fetch --unshallow` + `git reset --mixed origin/arena/…` → `git status` = **صفر drift** → کارهای committed سالم بازگشتند. |
| ac2b7d5 | تگ/stash/پچ غایب؛ `git cat-file ac2b7d5…` حتی پس از unshallow fail می‌شود؛ `git ls-remote --tags` = ۱۲ تگ متعلق به دیگران → **کاندیدا از دست رفته و غیرقابل بازیابی است** (هرگز push نشده بود). |
| PRها | ۳۵ PR باز. #96 CONFLICTING، #117 draft MERGEABLE، #112/#113/#115/#116 MERGEABLE، #93 MERGEABLE → وضعیت‌ها ثبت شد؛ هیچ PRی merge نشده. |
| CI روی SHA دقیق | check-runs API برای `2bddbbb` = ۴۲ چک: **python-parity (3.10/3.11/3.12) failure**، **test (not slow) failure**، **Publication frontier guard failure (exit 4)**؛ wheel-contract و lint و extras و continuum سبز. همان jobها روی تیپ #113/#116 سبزند → قرمزی مختص diff شاخهٔ من بود. |
| اختلاف با گزارش قبلی | گزارش قبلی «CI جدید UNVERIFIED» نوشته بود؛ حقیقت زنده: اجرا شده و **شکست خورده**. همچنین «قرنطینه حفظ شده» دیگر صحت ندارد. هر دو در این گزارش اصلاح شد. |

## B — Ownership / Lease Truth (از منبع remote، نه فایل محلی)

| Workstream | Owner (branch) | Files | Current SHA | Lease State | Allowed Action |
|---|---|---|---|---|---|
| W2 | `arena/01a0e4f6` (PR #116) | ۳۱ مسیر: `llm/*`، `ai_chat`، **`ai_memory`**، `summarizer`، `video_director`، ۱۵ تست gateway، `scripts/llm_gateway_mutations.py`، `LLM_GATEWAY.md`، ADR-0012 | `9e795318fa76` | `active_in_review`، LIVE تا **2026-09-29T22:39Z** | هیچ تغییری؛ فقط خواندن و کاوشگر |
| W1 | `arena/01a0e442` (PR #113) | ۱۳ مسیر: `application/runtime.py`، `bot/app|handlers|feature_handlers|knowledge_handlers`، `agent_manager`، `gemini_provider`، `storage/db.py`، `conftest.py`، ۴ تست | `6a867a77ca7e` | `active`، LIVE تا **2026-09-28T21:10Z** | هیچ |
| DR | `arena/01a0dee1` (PR #96) | ۱۳ مسیر: `maintenance/*`، `api/app.py`، `deploy_smoke`، `maintenance.yml`، `Dockerfile`، ۴ تست، ۲ ران‌بوک | `01eb2217f443` | `active`، LIVE تا **2026-09-28T19:30Z** (به‌علاوه task-164 با paths خالی تا 19:15Z) | هیچ |
| خودم | `arena/01a0e742` | guard/پروف/آزمون‌های داور/workflow خودم/ران‌بوک/گزارش‌ها/board/`pyproject.toml` | `5903dda` | `active` (این مأموریت) | publish پس از preflight |

Observation: overlap-scan میان فایل‌های برنامه‌ریزی‌شدهٔ من و سه zone → **صفر تداخل**. Conclusion: تمام تغییرات این سشن در محدودهٔ مجاز ماند.

## C — Reconciliation Design

ادغام W1+W2+DR+من هم‌اکنون به سه دلیل ممکن نیست و طراحی reconciled به این شرط‌ها گره خورده است:
1. هر سه lease زنده‌اند (Evidence: جدول B) → هر ادغامی که فایل مشترک (`gemini_provider.py` بین W1/W2، `db.py`/`conftest.py` در W1 و …) را لمس کند، نقض مالکیت است.
2. نقص حریم خصوصی (بخش D) باید توسط W2 رفع شود؛ ادغام بدون آن = انتشار نقض حریم خصوصی.
3. W2 در board خود ADR-0012 و ۱۵ فایل تست gateway جدید دارد که با main نیستند؛ پس از آزادی leaseها، بازادغام سه شاخه + من لازم است (ترتیب پیشنهادی: W1 → W2 → DR → بازسازی کاندیدای ده‌محوری روی آن، با دروازهٔ کاوشگر).
No-rewrite: هیچ forward-fix ای روی شاخهٔ دیگران ساخته نشد؛ فقط probe مستقل تحویل شد.

## D — Privacy Invariant Evidence

Evidence: `scripts/probes/consent_generation_race.py` (۸ حالت قطعی با باری‌ر دومرحله‌ای، SQLite موقت واقعی، فیک LLM)؛ خروجی‌ها در `ci-artifacts/hardening/probe-main-source.json` و `probe-w2-tip.json`.

| حالت | kind | منبع main/شاخه (sha `13cea73c…`) | تیپ W2 `9e795318` (sha `31edb93f…`) |
|---|---|---|---|
| consent granted (کنترل) | control | ✓ ذخیرهٔ عادی | ✓ |
| consent denied (کنترل) | control | ✓ بدون تماس LLM | ✓ |
| forget قبل از پایان extraction | invariant | **نقض** — پروفایل پاک‌شده بازنوشته شد | **نقض** |
| forget همزمان + cancel درخت Journal | invariant | امن (no resurrection) | امن |
| stale writer (بازپخش writer با payload گرفته‌شده) | invariant | **نقض** | **نقض** |
| replay درخواست پس از forget (consent=denied) | invariant | امن — صفر تماس LLM | امن |
| forget دوباره / کاربر غایب | invariant | امن — tombstone پایدار | امن |
| دو extraction همزمان + forget بین‌شان | invariant | **نقض** (هر دو نویسنده) | **نقض** |

Conclusion: اینوریانت «پس از `forget_user` هیچ مسیری نباید پروفایل پاک‌شده را بازنویسی کند» در **سه حالت** روی هر دو منبع شکسته می‌شود؛ کنترل‌ها سالم‌اند پس هارنس معتبر است. خروج ۱ (FAILURE) برای مالک W2. رفع (توکن ابطال/نسخهٔ ذخیره‌سازی که نوشتنِ pre-forget را post-forge مردود کند) باید در فایل اجاره‌دار W2 انجام شود؛ این سشن عمداً آن را ننوشت (قانون مالکیت).

## E — Code / Test Changes (این سشن)

| تغییر | دلیل (Evidence→Conclusion) |
|---|---|
| `tests/unit/test_agent_board_remote.py` +۸ آزمون adversarial (۴۹ مجموعه) | فاز ۴ مأموریت؛ هر سناریوی خواسته‌شده executable شد. دو فرض اولیهٔ من در نوشتن آزمون‌ها غلط بود (rewrite بدون publish) — رفتار داور در هر دو مورد درست بود و آزمون اصلاح شد، نه داور. |
| `.github/workflows/publication-frontier.yml` | Evidence: exit 4 بازتولید محلی با pytest 9.1.1 بدون `pytest-asyncio` و بدون نصب پکیج (conftest وارد پروژه می‌شود) + skipped بودن job پس‌از-push/detached-HEAD. Conclusion: نصب `pytest-asyncio` + `--noconftest` برای job مدل؛ ساخت شاخه در job پس‌از-push؛ گام جدید «داور باید detached HEAD را رد کند». |
| `scripts/probes/consent_generation_race.py` | گسترش به ۸ حالت الزامی (فاز ۲). دو باگ هارنس در حین ساخت پیدا و رفع شد (گیت‌های index-based؛ باری‌ر دومرحله‌ای). |
| `pyproject.toml` (dev extra += `wheel>=0.43`) | Evidence: ۸ خطای `test_wheel_install` در سوئیت کامل محلی با پیام «Unmet dependencies: wheel … not installed»؛ همان job در workflow مستقل چون خودش `wheel` نصب می‌کند سبز می‌شد. Conclusion: backend ساخت باید بخشی از `[dev]` باشد. این همان ریشهٔ قرمزی `test`/`python-parity` در CI بود. |
| `docs/ops/INTEGRATION_PREFLIGHT.md` | ثبت حادثهٔ sandbox-restore، از‌دست‌رفتن قرنطینه (اصلاح صریح رکورد قبلی)، ماتریس adversarial، تعمیر CI داور. |
| `docs/DECISION_LOG.md` | پیوستِ حادثه/تصحیح dated (بدون بازنویسی تاریخچه). |
| board | claiming/zone/معیارها/شواهد + یادداشت کاوشگر. |

در این سشن **هیچ فایل اجاره‌داری تغییر نکرد** (مقایسهٔ مسیر-به-مسیر با جدول B).

## F — CI / Remote Evidence

- **پیش از این سشن، روی `2bddbbb`:** test/parity قرمز (ریشه: `wheel`) و guard قرمز (ریشه: pytest9/conftest) — هر دو محلی بازتولید و رفع شد؛ wheel-contract سبز بود چون خودش `wheel` نصب می‌کند (این ناسازگاری، سرنخ ریشه بود).
- **بعد از تغییرات:** سوئیت کامل محلی (fulenv، پایتون ۳.۱۱، `-m "not slow"`): **۲۹۹۱ passed / ۰ error** (پیش از fix: ۲۹۹۱ passed + ۸ error). کاوشگر: FAILURE×۲ هدفمند. آزمون‌های داور+board: ۴۹ passed. docs-integrity: green (اجرای محلی). CI دقیق روی SHA انتشار: پس از push زنده کنترل می‌شود و در گام F نهایی ثبت می‌گردد؛ محدودیت: لاگ‌های blob گیت‌هاب در این محیط دریافت نشد (EOF) — قضاوت فقط از check-runs API/annotations.
- production evidence: **هیچ**؛ `production_verified=false` در همهٔ خروجی‌های کاوشگر.
- **CI زنده روی SHA دقیق `d1a7423` (check-runs API):** `lease model` ✓ success، `pushed range` ✓ success، `lint-fast` ✓، `test (not slow)` ✓ success، `python-parity 3.11` ✓، `3.12` ✓؛ `python-parity 3.10` در رانِ push-event **failure** و در رانِ pull-event روی **همان درخت** success → ناسازگار = flake در ۳٫۱۰؛ علت UNVERIFIED (لاگ/آرتیفکت blob از این sandbox دریافت نشد — EOF تکرارشونده؛ rerun نیز ۴۰۳). wheel-contract 3.10/3.11/3.12 همگی success. مسیر قرمز پنهان نشد؛ پایش ران بعدی در ادامهٔ همین بخش ثبت می‌شود.

## G — PR / Board Coordination

- تسک `hardening-proof-01a0e742` claimed و فوراً pushed شد (`3065ff7`)، zone یک‌بار برای `pyproject.toml` گسترش یافت (`5903dda`)، بدون تداخل.
- یادداشت مالکیت/کاوشگر روی board ثبت شد.
- تحویل به W2: کامنت روی PR #116 با جدول ۸ حالت + invariant دقیق + مسیر فراخوانی + پیشنهاد مرز معماری (invalidation token در لایهٔ persistence).
- وضعیت روی PR #117 نیز ثبت می‌شود؛ درخواست‌های handoff قبلی (#113/#116/#96) همچنان پابرجا.

## H — Remaining Blocks

1. **BLOCKED — privacy fix:** مالکش W2 است؛ اجاره تا 2026-09-29 22:39Z زنده. (کاوشگر = دروازهٔ پذیرش آینده.)
2. **BLOCKED — integration:** سه lease فعال؛ ادغام W1/W2/DR/من فقط پس از handoff/expiry + بازادغام + preflight.
3. **PERMANENT LOSS — `ac2b7d5`:** قابل بازسازی نیست؛ کار ده‌محوری باید پس از آزادی مسیر، از نو و این بار با فیکس حریم خصوصی ساخته شود. (قرنطینه = حفظ نبود؛ اکنون هیچ است.)
4. **UNVERIFIED:** اجرای راه‌دور workflow اصلاح‌شده تا لحظهٔ push بعدی؛ CI کامل روی SHA جدید (پس از push کنترل می‌شود)؛ آیتم‌های production قبلی (RTO/RPO، cloud واقعی) همچنان UNVERIFIED.
5. لاگ‌های blob CI از این sandbox دریافت‌نشدنی ماند (EOF) — تشخیص‌ها از بازتولید محلی + annotations.

## I — Exact Next Action

**یک قدم:** پس از سبز شدن CI روی SHA جدید (این سشن انجام می‌دهد)، نوبتِ دستِ W2 است: رفع invalidation در `ai_memory.py` با خروج ۰ کاوشگر روی تیپ W2. بلافاصله پس از آن (یا با انقضای leaseها) درخواست ادغام W1→W2→DR و بازسازی کاندیدا. هر کار دیگری در همین لحظه یا lease-نقض است یا دوباره‌کاری.

## J — Release Decision Gate

| شرط DoD | وضعیت |
|---|---|
| live main / PR graph / ownership reconciled | ✓ verified (A,B) |
| no unauthorized lease mutation | ✓ (صفر تداخل؛ هیچ فایل zone دیگری لمس نشد) |
| privacy reproducer | ✓ اجرایی ×۸؛ **fix = تحویل به W2 (BLOCKED تا ایشان)** |
| privacy race tests green | ✗ → FAILURE×۲ ثبت‌شده (عمداً؛ red path پنهان نشد) |
| remote-boundary guard green | ✓ ۴۹ آزمون + ماتریس adversarial |
| historical commit-range / stale-CI | ✓ range-guard روی تاریخچهٔ واقعی + مقایسهٔ SHA-محور چک‌ها (قاعده در workflow) |
| quarantine provenance | ✗ **LOST** — ثبت شفاف (H-3) |
| exact release range | ✓ preflight + outgoing_files هر push |
| local tests green | ✓ ۲۹۹۱/۰ error پس از fix |
| CI green on exact release SHA | ⏳ بلافاصله پس از push این سشن تأیید می‌شود (F) |
| no production claim | ✓ |

**نتیجه: PARTIALLY_VERIFIED** — نه READY_FOR_CONDITIONAL_MERGE (چون دو شرط DoD هنوز ✗ است) و نه صرفاً BLOCKED (چون مرز انتشار، قرمزی CI و شواهد حریم خصوصی به وضعیت قابل‌اثبات رسیدند).
