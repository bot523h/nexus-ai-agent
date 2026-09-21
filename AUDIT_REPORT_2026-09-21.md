# گزارش ممیزی معماری عمیق — NEXUS AI Agent v3.12.0

**تاریخ ممیزی:** ۲۱ سپتامبر ۲۰۲۶ · **ممیزی‌کن:** معمار ارشد / ناظر مالک‌محور (Arena Agent)
**دامنه:** کل مخزن `bot523h/nexus-ai-agent` در کامیت `04d8820` (merge PR#29)
**روش:** بازخوانی خط‌به‌خطِ ~۴۲٬۰۰۰ سطر پایتون، اجرای کامل دروازه‌های کیفیت، ردیابی سیم‌کشی واقعیِ هر قابلیت از فرمان تلگرام تا موتور، و ممیزی امنیتی تهدید-محور.

---

## ۱) خلاصه اجرایی

این ریپو **دو محصول متفاوت** در یک مخزن است و قلب یافته‌های من همین است:

| جهان | محتوا | وضعیت |
|---|---|---|
| **هسته‌ی مستحکم (فاز ۰–۶)** | چک‌پوینت LangGraph، چرخه‌ی حیات، Alembic/PostgreSQL، صف کارِ بادوام، استودیوی خلاقه (Nagar)، تولید تصویر Wave 3 | مهندسی درجه‌A: پورت/آداپتور، تست مرزی معماری، سند تصمیم، fail-closed |
| **پوسته‌ی محصول تلگرام (v1.x–v2.x)** | ۸۰+ فرمان تلگرام: بازی‌ها، ابزارها، گیمیفیکیشن، مدیریت کانال، ریفرال، میهمان‌نوازی i18n | بخش بزرگی **شبیه‌سازی‌شده/پوسته‌ای** است؛ موتورهای واقعی ساخته و تست شده‌اند اما به فرمان‌ها وصل نشده‌اند |

**نمره‌ی کلی: B−** — هسته شایسته‌ی A است، اما شکاف «ادعا در برابر واقعیت» در README، حفره‌های احراز هویت، و سیم‌کشی مرده، اعتبار محصولی را تنزل می‌دهد.

**نتیجه‌ی دروازه‌ها در همین ممیزی (اجرای واقعی):**
- `ruff check` + `ruff format --check` → ✅ پاک (۳۰۷ فایل)
- `mypy src` → ✅ موفق (۱۹۳ فایل، بدون خطا)
- `pytest -q -m "not slow"` → ✅ **۷۷۳ پاس / ۲۰ اسکیپ در ۴۴ ثانیه** (۶۷۸ تابع تست)

---

## ۲) متریک‌های پایه

| متریک | مقدار |
|---|---|
| فایل‌های پایتون | ۲۸۴ (src: ۱۹۳ فایل / ۲۸٬۰۸۶ سطر، tests: ۱۳٬۷۴۰ سطر) |
| تعداد کل تست‌ها | ۷۷۳ پاس + ۲۰ اسکیپ (Postgres-only) |
| معماری محیط مجازی پس از نصب | **~۷ GB** (!) — torch + CUDA 13 کامل |
| بزرگ‌ترین فایل | `bot/handlers.py` با ۱٬۶۰۶ خط |
| تست‌های مرزی معماری | ۱۱ فایل در `tests/architecture/` |
| CI | دو ورک‌فلو: `ci.yml` (lint+mypy+test+Postgres واقعی) و `maintenance.yml` (بکاپ شبانه R2) |
| PRهای ادغام‌شده | ۲۹ (طبق تاریخچه) · VERSION = 3.12.0 |

---

## ۳) معماری واقعی — آنچه در زمین اتفاق می‌افتد

جریان اصلی پیام: `Telegram → PTB handlers (handlers.py) → LangGraph graph → LLM Provider → پاسخ`

نکات کلیدی نقشه:

1. **لایه‌ی Ports & Adapters فقط در هسته‌ی جدید رعایت می‌شود.** `application/ports/` پنج پورت تمیز دارد و تست `tests/architecture/test_import_boundaries.py` با «baseline منجمدِ» ۳۷ تخلفِ پدربزرگ‌شده (`legacy_baseline.json`) جلوی تخلف جدید را می‌گیرد. این طراحی صادقانه است: تخلف‌های میراث **انکار نمی‌شوند، ثبت شده‌اند**.
2. **`features/*` خارج از این نظم است.** ۲۴ ماژول فیچر که همگی `sqlmodel` و engine‌های همگام (sync) خودشان را وارد می‌کنند — همگی در baselineِ میراث‌اند.
3. **دو زیرسیستم دیتابیس موازی:** هندلرهای تلگرام از `get_session` نامتقارن (async) و فیچرها از `Session` همگام با engine تازه‌به‌تازه استفاده می‌کنند. در یک فرآیند، دو پشته‌ی اتصال به یک فایل SQLite.

---

## ۴) نقاط قوت (این‌ها را دست‌نخورده نگه دارید)

1. **SSRF Guard نمونه‌ی آموزشی است** (`core/ssrf_guard.py`): اعتبارسنجی https-only، رد همه‌ی بازه‌های خصوصی، و بستن پنجره‌ی TOCTOU/DNS-rebinding با بازبینیِ IP در لحظه‌ی اتصال از طریق `httpcore` backend سفارشی. تست آن هم واقعی است (`tests/unit/test_http_client_ssrf.py`).
2. **صف کارِ بادوام و درون‌فرآیندی** (`adapters/in_process_job_queue.py`): SQLite به‌عنوان hand-off، کلید idempotency، `resume_pending`، هوک اتمام fail-safe. دقیقاً همان چیزی که یک «مدولار مونولیت» بدون بروکر نیاز دارد.
3. **پایبندی به سند تصمیم:** `docs/DECISION_LOG.md` (۵۸۰ سطر)، `REQUIREMENTS_LEDGER.md` با وضعیت چهار-ارزشی DONE/PARTIAL/MISSING/DEFERRED، `.nexus/continuum.json` (schema v2)، و `ROADMAP_STATUS.md` با انکرِ کامیت برای هر موج. انضباط فرایندیِ این سطح به‌ندرت دیده می‌شود.
4. **امنیت fail-closed در هسته‌ی خلاقه:** پادِ Gemini بدون `paid_tier=true` + تخمین مثبت اپراتور ممکن نیست؛ CORS پیش‌فرض بسته؛ HMAC برای endpointهای جهش‌یاف؛ webhook secret با مقایسه‌ی constant-time؛ خروج رسانه‌ی اسلایدشو opt-in.
5. **CI واقعی:** مهاجرت Alembic روی PostgreSQL سرویس‌کانتینر واقعی اجرا و استمپ `f4a9c2e71b08` بررسی می‌شود؛ بکاپ شبانه‌ی زمان‌بندی‌شده با concurrency group.
6. **توازن نادر تست:** ۴۴ ثانیه برای ۷۷۳ تست، با تست‌های قراردادیِ آداپتور (contract) و تست‌های خصمانه (`test_lifecycle_adversarial.py`).
7. **صداقت در CHANGELOG:** مستند کرده که تخمین هزینه «فاکتور نیست»، کش ماندگار نیست، و در دسترس‌ بودن Pollinations تضمین ندارد.

---

## ۵) یافته‌های بحرانی (P0) — باید در اسپرینت بعدی حل شوند

### P0-1 · «ویژگی‌های شبیه‌سازی‌شده» — شکاف صداقت محصول
فرمان‌هایی که README به‌عنوان قابلیت می‌فروشد، در واقع پاسخ ثابت برمی‌گردانند:

| فرمان | واقعیت در کد | مرجع |
|---|---|---|
| `/calc` | `"🧮 Result: 2 + 2 = 4"` ثابت | `bot/handlers.py:365` |
| `/tr` | `"🌐 Translated: Hello -> سلام"` ثابت | `handlers.py:361` |
| `/convert` | `"💱 100 USD = 6,000,000 IRT"` ثابت | `handlers.py:357` |
| `/remind` | «برای ۳۰ دقیقه تنظیم شد» — هیچ رمیندری ثبت نمی‌شود | `handlers.py:353` |
| `/vision` | `"I see a beautiful landscape in this image."` — تحلیل تصویر جعلی | `handlers.py:415–427` |
| `/wordle`, `/guess_start`, `/poll` | فقط پیام «شروع شد» — منطق `games.py` وصل نیست | `handlers.py:336–345` |
| `/daily`, `/xp_leaderboard` | `"+50 XP!"` و `"UserX: 5000 XP"` جعلی | `handlers.py:1218–1224` |
| `/ban`, `/unban`, `/schedule`, `/stats`, `/welcome`, `/pin` | همگی `(simulated)` با آمار هاردکد `"150 members, 1.2k messages/day"` | `handlers.py:257–290` |
| `/docs`, `/doc_delete`, `/chat_with_doc` | «نسخه دمو» — RAG واقعی (`features/rag.py`) به این فرمان‌ها وصل نیست | `handlers.py:1546–1552` |
| `/newchat` | «پاک شد» — هیچ‌چیز پاک نمی‌شود | `handlers.py:840–845` |

**تحلیل مالکانه:** موتورهای واقعی (`features/tools.py` شامل `ReminderSystem/Translator/UnitConverter/Calculator`، `WordleFA`، `NumberGuess`، `QuickPoll`) نوشته شده‌اند اما `features/tools.py` **در کل `src` هیچ ایمپورتی ندارد** — کد مرده‌ی ۳۷۹ سطری. این یعنی تست‌ها موتور را پاس می‌کنند و «تسک‌لیست‌های todo.md» را هم تیک می‌زنند، اما کاربر نهایی با یک صحنه‌سازی روبه‌روست. **تست هندلرها فقط «ثبت‌شدن فرمان» را assert می‌کند نه عملکردش را** (`tests/unit/test_handlers.py:8–32`).

### P0-2 · احراز هویت فقط روی ۲ مسیر از ~۸۰ فرمان
`AuthMiddleware` با طراحی درستِ deny-by-default (`bot/middleware.py:31–47`) فقط در `on_message` (`handlers.py:856`) و `imagine_cmd` (`handlers.py:455`) اعمال می‌شود. در استقرار «مالک-تنها»، هر غریبه‌ای که آی‌دی بات را پیدا کند می‌تواند `/ai` (سوزاندن سهمیه Gemini شما)، `/cloud`، `/tts`، `/summarize`، `/learn`، `/memory` و `/forget_me` را صدا بزند. فرمان‌های مالکی (`/broadcast`، `/owner`، `/update`، `/approve`) جداگانه `is_owner` دارند و امن‌اند — اما حصارِ کلی وجود ندارد.

### P0-3 · دکمه‌ی «تأیید فورس‌جوین» همیشه قبول می‌کند
در `handlers.py:986` مدیریت `ForceJoinManager()` **بدون instance بات** ساخته می‌شود؛ نتیجه در `features/force_join.py:112–114`: «بدون بات نمی‌توانم بررسی کنم → `return True`». یعنی گیت عضویتِ کانال به‌طور پیش‌فرض برای همه باز است — دقیقاً برعکسِ ادعای «anti-bypass» در README.

### P0-4 · حلقه‌ی ویروسی ریفرال ذاتاً مرده است
- دو `CommandHandler("start")` ثبت شده است (`handlers.py:1319` و `handlers.py:1501`). در PTB، اولین هندلرِ برنده در گروه ۰ همه‌ی آپدیت‌ها را می‌برد؛ پس `start_referral_handler` **هرگز اجرا نمی‌شود**.
- مهم‌تر: `ReferralEngine.process_referral()` (`features/referral.py:127`) **در هیچ‌جای `src` فراخوانی نمی‌شود**. کد و لینک تولید می‌شود، اما هیچ ریفرالی ثبت یا پاداشی پرداخت نمی‌شود. کل «Referral Viral Loop با ۶ سطح نمایی» فعلاً یک نمایشگر آمار خالی است.

### P0-5 · نشت داده در داشبورد عمومی
`GET /api/dashboard/recent_users` (`api/dashboard.py:46–54`) **بدون هیچ احراز هویتی** `telegram_id` و `username` کاربران واقعی را برمی‌گرداند و `docker-compose.yml` پورت ۸۰۰۰ را publish می‌کند. CORS بسته جلوی مرورگر را می‌گیرد، نه `curl` را. HMAC فقط روی `POST /creative/video-edit` است. این یک نقض حریم خصوصی کاربران تلگرام است.

### P0-6 · مسیر عبور (Path Traversal) در `/cloud` و `/download`
- `/cloud`: `tmp_path = tmp_dir / (doc.file_name or "unnamed")` — نام فایلِ کنترل‌شده‌ی کاربر بدون پاک‌سازی (`handlers.py:607–612`).
- `/download`: `local_path = dl_dir / filename` با `filename = " ".join(context.args)` — یک `file_name` ذخیره‌شده با `../../` می‌نویسد خارج از پوشه (و هر فایلی را reply می‌کند) (`handlers.py:687–706`).
تنها ابزار مسیر-محافظ در ریپو (`tools/files.py`) برای این مسیرها به کار نرفته است. ضمناً `open(local_path, "rb")` بدون بستن (فایل‌هندل نشتی) در همین مسیر.

### P0-7 · هر پیام آزادِ کاربر بدون رضایت به Gemini ارسال می‌شود
`on_message` به‌ازای هر پیام: `AIMemoryEngine()` جدید + `asyncio.create_task(memory_engine.update_from_message(...))` (`handlers.py:873–876`). نتیجه:
- هر پیام کاربر برای «استخراج اطلاعات شخصی» به Google می‌رود — بدون opt-in، بدون نمایش در `/help`؛
- حتی بدون `GEMINI_API_KEY` این کار ادامه می‌یابد (فقط خطا لاگ می‌شود)؛
- `create_task` بدون نگه‌داشتن مرجع → ریسک GC نیمه‌راه و بلعیده‌شدن استثنا.

### P0-8 · سیم‌کشی دوبل موتورها — موتورهای «مستند» عملاً بایپس می‌شوند
`bot/app.py:_init_v2_engines` یک `ConversationStore`، `GeminiRequestQueue` و `GeminiEngine` متصل به هر دو می‌سازد و در `bot_data` می‌گذارد؛ اما `build_handlers` نمونه‌های دومی می‌سازد (`handlers.py:172–190`) و **هیچ هندلری جز `job_queue` از `bot_data` نمی‌خواند** (تنها مراجع: `handlers.py:1500–1501`). یعنی:
- «حافظه‌ی مکالمه‌ی پایدار v2.1» و «صف منصفانه‌ی درخواست Gemini» که README می‌فروشد، در مسیر واقعیِ `/ai` فعال نیستند؛
- موتورهای بلاتکلیف در `bot_data` حافظه و کانستراکشن هدر می‌دهند.

### P0-9 · حافظه‌ی بلندمدتِ گراف «فقط-خواندنیِ خالی» است
`LongTermMemory.store()` در هیچ‌جای `src` فراخوانی نمی‌شود؛ گراف هر نوبت `search` می‌کند (`orchestration/graph.py:37–44`) روی جدولی که هرگز نوشته نمی‌شود → `memory_context` همیشه `""`. به‌علاوه `embed()` همه‌ی پرووایدرها «هش شبه-تصادفی قطعی» است نه امبدینگ معنایی (docstring خودِ `litellm_provider.py` صادقانه اعتراف می‌کند). ادعای «Vector Memory» در README از دید کاربر = هیچ.

### P0-10 · چت ناشناس غیرقابل استفاده و فیورجوین بدون بات
`AnonymousChatManager()` نیز بدون بات ساخته می‌شود (`handlers.py:290`) و هیچ مسیری برای تحویل پیام بین دو جفت‌شده وجود ندارد (هندلر پیام، مسیر anon ندارد). صف هم درون-حافظه‌ای است و با ری‌استارت می‌میرد. `/anon_start` در عمل یک پیام‌خوشامد بی‌بهانه است.

---

## ۶) یافته‌های مهم (P1)

1. **پرووایدر Internxt تقریباً ساختگی است** (`storage/unified_cloud.py:226–257`): endpoint مستند و اثبات‌نشده، `download` → `ProviderUnavailable`، `list_files` → `[]`، usage هاردکد ۱۰GB. README آن را یکی از «۵+ پرووایدر واقعی» می‌شمارد.
2. **بلاک‌شدن event loop به‌صورت سیستماتیک:** `broadcast_cmd` مستقیماً sync engine (`handlers.py:930–935`)؛ تمام فیچرها (`ads`, `analytics`, `engagement`, `force_join`, `gamification`, `games`, `moderation`, `owner_control`, `personality`, `referral`, `viral_engine`, `anonymous_chat`) از `Session` همگام + `_sync_engine()` تازه‌به‌تازه استفاده می‌کنند؛ `AdvancedRAGEngine.add/query` کاری سنگین (امبدینگ!) را روی لوپ اجرا می‌کنند (`features/rag.py:45–53`). زیر بار واقعی، بات مکث‌های چند‌ثانیه‌ای می‌خورد.
3. **RAG شکننده:** chunking ثابت ۱۰۰۰ کاراکتری بدون overlap، `collection.add` با id الگویی `chunk_{file_id}_{i}` که در افزودن مجدد سند روی برخورد id کرش می‌کند، و `chat_with_doc` (مصرف‌کننده‌ی نهایی) اصلاً stub است.
4. **دو RateLimiter موازی با نگهداشت بی‌کران:** `bot/middleware.RateLimiter._events` و `ai_chat._RateLimiter._minute_buckets/_daily_counts` هیچ‌گاه کلید کاربران را حذف نمی‌کنند → رشد بی‌کران حافظه در جمعیت بزرگ (`middleware.py:12`، `ai_chat.py:47–67`). نسخه‌ی سومی (`InMemoryRateLimiter`) هم هست که لاک دارد — سه لیمتر برای یک کار!
5. **`SafeAsyncTransport` به‌ازای هر درخواست** ساخته می‌شود (`core/http_client.py:196–199`) → هیچ connection pooling و TLS-reuse واقعی؛ و `get_json` خطای 5xx را بلعیده و `{}` برمی‌گرداند — خطا با «داده‌ی خالی» در تفاوت‌ناپذیری می‌میرد.
6. **i18n تزئینی:** ۱۵ زبان ادعا می‌شود؛ `en/fa` شامل ۶۳ کلید و ۱۳ زبان دیگر ۲۳–۲۶ کلید؛ و **هیچ هندلری متن‌هایش را از i18n نمی‌گیرد** (تنها مصرف‌کننده: `features/onboarding.py`). ترجیح زبان ذخیره می‌شود و هیچ اثری در پاسخ‌ها ندارد.
7. **`/image` قدیمی بدون auth/limiter** در حالی که `/imagine` جدید gated است — ناهماهنگی سیاست روی یک منبع خارجی واحد (Pollinations).
8. **فایل‌هندل‌های بازِ بدون بستن** در `reply_photo`/`reply_voice` (`handlers.py:447–450`, `497–499`, `706–710`).
9. **دو نسخه‌ی واگرای `termux_install.sh`** در ریشه و `scripts/` (md5 متفاوت) — کدام مرجع است؟
10. **`datetime.utcnow()`** منسوخ‌شده در پایتون ۳.۱۲ (`features/ai_memory.py`, `agents/store/agent_manager.py`) — ناسازگار با `%` آگاه-از-منطقه‌ی بقیه‌ی کد.
11. **`handlers.py` یک «خدایگروه» ۱٬۶۰۶ خطی است** با صدها کلوزر تودرتو؛ تفکیک به `*_handlers.py` آغاز شده ولی نیمه‌کاره مانده.
12. **هسته‌ی LangGraph هنوز MVP است:** پلنر یک‌مرحله‌ای، اکزکیوتور «noop» با کامنت «Tools are wired in later» (`orchestration/graph.py:57–71`)؛ روتر صرفاً keyword-match انگلیسی — برای کاربر فارسی، intent «task» تقریباً هرگز فعال نمی‌شود (کلمات کلیدی انگلیسی‌اند).
13. **Roadmap drift جزئی:** `ROADMAP_STATUS.md` می‌گوید «Release 3.12.0 — THIS PR — pending merge» ولی PR#29 ادغام شده؛ `continuum.json` هم روی `52329e6` ایستاده در حالی که HEAD برابر `04d8820` است.
14. **ابزار پوشش (coverage) پیکربندی نشده** — برای پروژه‌ای با این حجم تست، نداشتن عدد پوشش و gate آن غفلت است.

---

## ۷) بهبودهای جزئی (P2)

- `requires-python = ">=3.10"` اما `mypy python_version = "3.12"` و CI روی ۳.۱۲ — کف پشتیبانی روی کاغذ ۳.۱۰ است، عملاً آزموده ۳.۱۲. یا bump کنید یا Matrix CI اضافه کنید.
- پین‌های مستند (`httpx==0.28.1` به‌خاطر API خصوصی `httpx._transports.default`, `uvicorn==0.53.0`, `boto3==1.43.98`) — بدهی شفاف است ولی بدهی؛ جای یک issue «ارتقای httpx» خالی است.
- `smoke` CLI پارامتر `input` شدو-سازِ builtin.
- `docker-compose.yml` سرویس dashboard را بدون هیچ محدودیت دسترسی بالا می‌آورد (مرتبط با P0-5).
- توصیه‌ی «_ = storage» و کامنت‌های حذف‌شده‌ی نیمه‌کاره در `bot/app.py:205–207` (presence heartbeat حذف‌شده) نشانه‌ی جراحی‌های ناتمام است.
- `.gitignore` فاقد `data/` ریشه‌ای به‌صورت `**/data/` — داده در زیرپوشه‌های دیگر می‌تواند سرایت کند.

---

## ۸) ممیزی امنیت — جدول جمع‌بندی

| کنترل | وضعیت | توضیح |
|---|---|---|
| SSRF / DNS-rebinding | 🟢 قوی | عمق دوبل + تست واقعی |
| Webhook secret | 🟢 خوب | constant-time، fail-closed 403/503 |
| HMAC endpoint جهش‌یاف | 🟢 خوب | fail-closed 503، پنجره ±۳۰۰ ثانیه |
| CORS | 🟢 خوب | پیش‌فرض کاملاً بسته |
| احراز هویت سطح بات | 🔴 حیاتی | فقط ۲ مسیر (P0-2) |
| فورس‌جوین | 🔴 حیاتی | همیشه-قبول بدون بات (P0-3) |
| نشت PII داشبورد | 🔴 حیاتی | telegram_id عمومی (P0-5) |
| Path traversal | 🔴 حیاتی | `/cloud`, `/download` (P0-6) |
| خروج داده به LLM | 🟠 پرخطر | هر پیام → Gemini بدون رضایت (P0-7) |
| Shell tool | 🟡 قابل قبول | allowlist + محصور workspace؛ پیش‌فرض خاموش |
| رمزهای hardcoded | 🟢 پاک | اسکن مثبت؛ همه از env |
| وابستگی‌ها | 🟠 حجیم | ۷GB + CUDA؛ سطح حمله‌ی supply-chain بزرگ |

---

## ۹) کیفیت تست — نگاه صادقانه

- **نقطه‌ی قوت:** هسته‌ی فاز ۴–۶ به‌طرح خیره‌کننده‌ای پوشش دارد (قرارداد آداپتور چک‌پوینت ۴۰۷ خطی، تست‌های خصمانه، تست‌های مرزی AST، تست race هم‌زمانی create_all، تست قرارداد PG روی Postgres واقعی).
- **نقطه‌ی ضعف:** برای لایه‌ی تلگرامِ v1/v2 فقط «ثبت فرمان» تست می‌شود؛ رفتارِ شبیه‌سازی‌شده هرگز fail نمی‌شود چون هیچ‌کس assert نکرده «/calc باید واقعا حساب کند». **تست‌های موجود، توهم محصول را گواهی می‌کنند.**
- ۲۰ اسکیپ عمدتاً PG-only است و در CI leg جداگانه دارد — تصمیم درستی است.

---

## ۱۰) وابستگی‌ها و بسته‌بندی

نصب `-e ".[dev]"` در این ممیزی **~۷ گیگابایت** حجم آورد (torch+CUDA کامل برای `sentence-transformers`، chromadb، llama-cpp کامپایل‌شده). در حالی که:
- امبدینگ واقعیِ معنایی هیچ‌جا مصرف نمی‌شود (هش است!)،
- llama.cpp فقط در مسیر local-LLM اختیاری است،
- RAG فقط در job اختیاری PDF مصرف می‌شود.

**پیشنهاد جدی:** تفکیک extras به `[rag]`، `[local-llm]`، `[speech]`؛ هسته فقط با PTB+SQLModel+httpx+litellm سبک شود. این هم استقرار Koyeb/Termux را نجات می‌دهد هم footprint امنیتی را نصف می‌کند. ضمناً نصب، همزمان `httpx2`/`httpcore2` را هم آورد (آلودگی اکوسیستم chromadb) که شایسته‌ی قفل‌گذاری با constraints است.

---

## ۱۱) جدول «ادعا در برابر واقعیت» (نمونه‌های کلیدی README)

| ادعای README | واقعیت کد | داوری |
|---|---|---|
| «Tools — Reminders, translation, unit conversion, calculator» | ۴ فرمان جعلی؛ موتور واقعی کد مرده | ❌ |
| «Games — Quiz, Wordle, guessing, polls» | فقط Quiz واقعی؛ ۳ مورد نمایشی | ⚠️ نیمه |
| «/vision — Image analysis via Gemini Vision» | رشته‌ی ثابت | ❌ |
| «Channel Management — Post, schedule, ban…» | همگی simulated | ❌ |
| «Referral — 6 tiers, dual-reward» | ثبت ریفرال مرده (P0-4) | ❌ |
| «15-language support» | انتخاب زبان ذخیره می‌شود؛ UI ترجمه نمی‌شود | ⚠️ |
| «57GB+ free cloud (5 providers)» | Internxt ساختگی؛ دانلود فقط Dropbox/pCloud واقعی | ⚠️ |
| «Smart Moderation, Gamification, Analytics» | موتور واقعی؛ ولی XP روزانه/لیدربورد جعلی و ورودی moderation به جریان پیام وصل نیست | ⚠️ |
| «/imagine, /slideshow, R2, Alembic/PG, webhook» | واقعی و باکیفیت | ✅ |
| «anti-bypass force join» | همیشه-قبول (P0-3) | ❌ معکوس |

---

## ۱۲) توصیه‌های اولویت‌بندی‌شده (نقشه‌ی راه پیشنهادی)

**هفته‌ی ۱ — توقف خون‌ریزی (امنیت و صداقت):**
1. میدل‌ور سراسری PTB (group `-1`, `TypeHandler`) برای auth+rate روی همه‌ی ورودی‌ها — یک نقطه‌ی کنترل به‌جای دو مورد پراکنده. *(~۱ روز)*
2. `/api/dashboard/*`: حذف `telegram_id` از پاسخ + توکن بیرر ساده یا bind به localhost؛ حذف publish پورت در compose وقتی auth نیست. *(~۲ ساعت)*
3. پاک‌سازی نام فایل در `/cloud` و `/download` (uuid داخلی + whitelist پسوند + `resolve().is_relative_to`). *(~۲ ساعت)*
4. «گذر صداقت» از README: بخش «Simulated commands» یا حذف فرمان‌های جعلی تا پاک‌شدن. *(نیم روز)*

**هفته‌های ۲–۳ — سیم‌کشی حقیقت:**
5. تک‌منبع‌سازی موتورها: حذف کانستراکشن دوم در `build_handlers`؛ تزریق از `bot_data` (یا برعکس). ConversationStore/RequestQueue واقعاً فعال شوند. *(~۱ روز)*
6. ریفرال: merge هندلر `/start` دوم با پارس `start_param` و فراخوانی `process_referral`. *(نیم روز)*
7. فورس‌جوین و anon chat: تزریق `context.application.bot` در ساخت مدیرها. *(~۱ ساعت)*
8. `AIMemoryEngine`: پیش‌فرض خاموش (`NEXUS_AI_MEMORY=off`)، opt-in، با صف و سقف نرخ. *(نیم روز)*
9. اتصال `/wordle`, `/guess`, `/poll`, `/remind`, `/calc`, `/tr`, `/convert`, `/daily`, `/leaderboard` به موتورهای موجود و مرده — همه از قبل نوشته و تست شده‌اند! *(۲–۳ روز)*

**ماه ۱–۲ — معماری و اقتصاد:**
10. رژیم لاغری وابستگی‌ها (extras: rag/local-llm) — نصب هسته زیر ۲۰۰MB.
11. مهاجرت تدریجی فیچرها به `get_session` نامتقارن و حذف `_sync_engine`های per-call؛ بسته‌بندی کوئری‌های سنگین در `asyncio.to_thread`.
12. شکستن `handlers.py` به ماژول‌های دامنه‌ای + تست رفتاری (not فقط ثبت) برای هر فرمان واقعی.
13. پیکربندی `pytest-cov` با gate ۸۰٪ برای `creative/`, `storage/`, `adapters/`, `core/`.
14. نوشتن `memory_context` را وصل کنید یا بردارید: یا `LongTermMemory.store` بعد از هر نوبت، یا حذف ادعا.
15. روتر intent چندزبانه (حداقل fa/en) یا تفویض به LLM ارزان.

---

## ۱۳) اطمینان و شکاف‌ها (Confidence & Gaps)

- **اطمینان بالا (اجرای مستقیم):** نتایج lint/mypy/pytest، یافته‌های P0-1 تا P0-9 (همه با ردیابی import/call-side تأیید شدند؛ هر «مرده» بودن با grep دوطرفه‌ی تعریف/مصرف چک شد).
- **اطمینان متوسط:** واقعی-نبودن endpoint Internxt (بر اساس نبود مستندات عمومی شناخته‌شده؛ فراخوانی زنده‌ی شبکه انجام ندادم)؛ رفتار دقیق Pollinations در تولید.
- **بررسی‌نشده:** تست‌های `slow` (اجرای کامل با فلگ معکوس زمان‌بر بود)، رفتار runtime واقعی بات با توکن تلگرام زنده، بارسنجی SQLite تحت هم‌زمانی واقعی، و مصوبه‌ی انسانی پشت `ARCH_BASELINE_APPROVED`.
- **تضاد منابع:** `todo*.md` همه‌چیز را «انجام‌شده» اعلام می‌کنند در حالی که سطح فرمان، شبیه‌سازی است؛ من معیار را «کدِ وصل‌شده به مسیر کاربر» قرار دادم نه «فایل ایجادشده».

---

## ۱۴) الحاقیه (۲۱ سپتامبر ۲۰۲۶، v3.13.0) — چه‌چیزهایی از این گزارش اجرا شد

**تصحیح یک نکته در خودِ این سند:** جمله‌ی پایانی می‌گوید «این سند بخشی از مخزن نیست و
کامیت نشده» — این درست نیست؛ همین فایل در PR#30 کامیت و ادغام شده است. مهم‌تر اینکه
`scope` ثبت‌شده در `.agents/board.json` برای `P0-security-batch` وعده‌ی «global auth
middleware, /api/dashboard PII removal, /cloud + /download path traversal fixes» را
می‌داد، ولی PR#30 فقط همین پروتکل و همین گزارش را تحویل داد و **هیچ‌کدام از آن سه
مورد کد نشدند**.

وضعیت هر یافته پس از شاخه‌ی `arena/01a0c3aa-nexus-ai-agent` (v3.13.0):

| یافته | وضعیت |
|---|---|
| P0-1 ویژگی‌های شبیه‌سازی‌شده | 🟨 **بخشی حل شد** — `/calc` `/tr` `/convert` `/remind` `/wordle` `/guess_*` `/poll` `/quiz` `/leaderboard` `/daily` `/xp_leaderboard` `/achievements` واقعی شدند؛ کانال‌مدیریتی، moderation، `/vision` و RAG هنوز پوسته‌اند و صادقانه در README فهرست شده‌اند |
| P0-2 احراز هویت روی ۲ مسیر | ✅ **حل شد** — `BotAccessGate` در گروه `-۱` PTB |
| P0-3 دکمه‌ی فورس‌جوین همیشه قبول | ✅ **حل شد** — fail-closed + تزریق بات + رفع باگ `enabled is True` |
| P0-4 حلقه‌ی ریفرال مرده | ✅ **حل شد** — `/start ref_` هندل می‌شود، هندلر تکراری حذف شد |
| P0-5 نشت PII داشبورد | ✅ **حل شد** — `{id, display}` + توکن اختیاری |
| P0-6 Path traversal | ✅ **حل شد** — `core/paths.py` |
| P0-7 خروج هر پیام به Gemini | ❌ باز |
| P0-8 سیم‌کشی دوبل موتورها | ❌ باز |
| P0-9 حافظه‌ی بلندمدت خالی | ❌ باز |
| P0-10 چت ناشناس بدون بات | 🟨 **بخشی حل شد** — تحویل پیام واقعی شد؛ صف هنوز درون‌حافظه‌ای است |
| P1-4 رشد بی‌کران RateLimiter | ✅ **حل شد** — `MAX_TRACKED_USERS` |
| P1-8 فایل‌هندل باز | ✅ **حل شد** در `/download` (و `/image` بدون تغییر ماند) |

**چهار باگ تولیدیِ جدید که این ممیزی ندیده بود** (همه با تستِ رفتاری پیدا و رفع
شدند): هاردکد بودن مسیر دیتابیس در `get_session()`؛ شش فراخوانی `session.exec()` روی
`AsyncSession` که **هر پیام آزاد کاربر** را کرش می‌کرد؛ ناتوانی کاربر جدید در گرفتن
`/daily`؛ و `except` پهن در `features/onboarding.py` که `AttributeError` خودش را
می‌بلعید.

نتیجه‌ی دروازه‌ها پس از تغییرات: `837 passed / 20 skipped` · `ruff` پاک (۳۱۸ فایل) ·
`mypy` موفق (۱۹۴ فایل) · `nexus continuum verify` سبز.
جزئیات کامل: `docs/CHECKUP_2026-09-21_v3.13.0.md`.
