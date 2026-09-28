# گزارش اجرای اصلاح قراردادهای runtime و بستهٔ قابل نصب

**تاریخ:** ۲۰۲۶-۰۹-۲۸

**شاخه:** `arena/01a0e742-nexus-ai-agent`

**مبنای ممیزی:** `e5b326b2eaf691a638d030ad57acf1ce60016ef0`

**دامنه:** سه شکستِ session، خروجی رسانه و بسته‌بندی؛ همراه با ایرادهای مستقیم و هم‌خانواده‌ای که آزمایش واقعی آشکار کرد. این گزارش به معنی رفع تمام یافته‌های ممیزی یا آمادگی کامل production نیست.

## ۱. نتیجهٔ اجرایی

تشخیص اولیه به اصلاح کد تبدیل شد؛ نه فقط تغییر مستندات یا permissive کردن mockها:

| قرارداد | قبل | بعد |
|---|---|---|
| دیتابیس | مصرف `.exec()` روی SQLAlchemy AsyncSession | `execute(...).scalars()` و نوع مشخص session factory |
| مسیر پیش‌فرض DB | نادیده گرفتن `NEXUS_DB_PATH` در session بدون آرگومان | استفاده از تنظیمات واقعی؛ تقدم PostgreSQL حفظ شده |
| هویت مسیر SQLite | امکان اشتراک اتصال برای نام نسبی یکسان در دو cwd | مسیر canonical پیش از انتخاب اتصال و cache |
| تصویر/TTS | انتظار `file_path` در برابر خروجی `path` | قرارداد مشترک تایپ‌شده، اعتبارسنجی موفقیت و ارسال فایل واقعی |
| عمر stream | فایل بدون مدیریت عمر روشن | بسته‌شدن در موفقیت، خطای ارسال و cancellation |
| wheel | بدون JSON و migrationهای لازم | ۲۶ JSON، ۱۵ locale، شش manifest و شش فایل Alembic |
| CI | اثبات اصلی از نصب editable | workflow مستقل نصب wheel در محیط تمیز برای Python 3.10/3.11/3.12 |

کدهای اصلی: [FILE:src/nexus_ai_agent/bot/handlers.py:127-157] [FILE:src/nexus_ai_agent/storage/db.py:154-156] [FILE:src/nexus_ai_agent/storage/db.py:329-356] [FILE:src/nexus_ai_agent/bot/handlers.py:413-428] [FILE:src/nexus_ai_agent/bot/handlers.py:478-491] [FILE:setup.py:17-34] [FILE:pyproject.toml:79-81] [FILE:.github/workflows/wheel-contract.yml:1-52]

## ۲. تصمیم‌های معماری و دلیل آن‌ها

### دیتابیس: اصلاح مصرف‌کننده، نه جعل API

SQLAlchemy AsyncSession حفظ شد؛ هیچ `.exec()` ساختگی به آن اضافه نشد. شش مصرف‌کنندهٔ async در handlerها و یک مصرف‌کننده در onboarding اصلاح شدند. خروجی query به model تبدیل می‌شود، نه SQLAlchemy Row. نوع factory نیز از `Any` به async context manager مشخص تغییر کرد تا type checker تکرار همین اشتباه را تشخیص دهد.

در onboarding، خطای سابق توسط `except` پوشیده می‌شد و کاربر دارای زبان ذخیره‌شده دوباره تازه‌وارد محسوب می‌شد؛ تست جدید آن را با دیتابیس واقعی پوشش می‌دهد. [FILE:src/nexus_ai_agent/features/onboarding.py:38-56] [FILE:tests/integration/test_runtime_contracts.py:106-138]

دو ایراد مسیر نیز ابتدا بازتولید شدند: session بدون آرگومان به فایل تنظیم‌شده نمی‌رفت؛ و تغییر cwd با نام نسبی یکسان می‌توانست اتصال قبلی را نگه دارد. اصلاح شامل استفاده از `Settings.db_path` و canonical کردن مسیر است. مقدار ویژهٔ `:memory:` تغییر نکرده و مسیر صریح SQLite همچنان بر متغیر محیطی PostgreSQL تقدم دارد. [FILE:src/nexus_ai_agent/storage/db.py:154-156] [FILE:src/nexus_ai_agent/storage/db.py:193-209] [FILE:src/nexus_ai_agent/storage/db.py:329-356] [FILE:tests/integration/test_runtime_contracts.py:260-293]

### رسانه: قرارداد مشترک با حفظ سازگاری

به‌جای شکستن یک‌بارهٔ API دیکشنری موجود، `MediaResult`، `ImageResult` و `SpeechResult` تعریف شدند. producerها خروجی تایپ‌شده دارند؛ consumer فقط قرارداد صریح `success=True`, `path`, `error=None` را می‌پذیرد. کلید قدیمی/حدسی، مسیر خالی، موفقیت غیر boolean و خروجی متناقض پذیرفته نمی‌شود. [FILE:src/nexus_ai_agent/features/media_result.py:1-47]

بازکردن فایل و ارسال دو مرز جدا هستند: خطای تولید/فایل پاسخ ناموفق می‌دهد؛ خطای Telegram به‌عنوان خطای تولید بازنویسی نمی‌شود. stream در همهٔ مسیرها بسته می‌شود، اما فایل cache متعلق به موتور بی‌دلیل حذف نمی‌شود. gTTS نیز به dependency هسته اضافه شد تا `/tts` متکی به نصب تصادفی کاربر نباشد؛ extra محلی Whisper دست‌نخورده ماند. [FILE:src/nexus_ai_agent/bot/handlers.py:413-428] [FILE:src/nexus_ai_agent/bot/handlers.py:478-491] [FILE:pyproject.toml:15]

### بسته‌بندی: یک منبع migration، دو روش نصب

`migrations/` ریشه تنها منبعِ قابل ویرایش باقی ماند. build hook بدون import برنامه، آن را در `storage/_alembic` بسته‌بندی می‌کند. `MANIFEST.in` ورودی‌ها را وارد sdist می‌کند تا ساخت از sdist هم همان منابع را داشته باشد؛ پوشهٔ migration تولیدشده در build قبلی پیش از کپی پاک می‌شود تا revision حذف‌شده باقی نماند. [FILE:setup.py:1-34] [FILE:MANIFEST.in:1-6]

runtime در نصب wheel از منابع نصب‌شده و در editable از منبع checkout استفاده می‌کند؛ به `alembic.ini` پوشهٔ جاری اعتماد نمی‌کند. بستهٔ ناقص خطای روشن می‌دهد. escape کردن `%` نیز برای مسیر/URL در ConfigParser لحاظ شده است. نسخهٔ schema یا revision جدیدی اضافه نشد. [FILE:src/nexus_ai_agent/storage/migrations.py:102-131] [FILE:tests/unit/test_wheel_install.py:201-230]

## ۳. شواهد آزمایش

### قبل از اصلاح

فرمان زیر با تست‌های اولیه روی کد معیوب اجرا شد:

```bash
pytest -q tests/integration/test_runtime_contracts.py \
  tests/unit/test_wheel_install.py --tb=short
```

**نتیجه: ۲۲ شکست، ۸ پاس.** شکست‌ها شامل session واقعی، onboarding، عدم ارسال رسانه، منابع مفقود wheel و migration نصب‌شده بود. سپس دو تست جدید مسیر DB نیز پیش از اصلاح آن بخش اجرا شدند: **هر دو شکست خوردند**. خروجی تشخیصی در `ci-artifacts/audit-repair/red.log` و `db-path-red.log` تولید شد؛ این فایل‌های موقت جزو سورس نسخه‌بندی‌شده نیستند.

### پس از اصلاح

| اجرای مشاهده‌شده | نتیجه |
|---|---|
| اجرای نهایی مشترک ۳۴ فایل منتخب | **۵۳۱ پاس، یک skip، سه warning؛ ۷۳٫۲۱ ثانیه** |
| قراردادهای جدید، با import برنامه از wheel نصب‌شده | **۵۱ پاس** |
| مجموعهٔ رگرسیون اول | **۱۷۳ پاس**؛ سه هشدار قدیمی `Session.query` |
| مجموعهٔ رگرسیون تکمیلی | **۲۳۲ پاس، یک skip** |
| مستندات و ساختار/رفتار board | **۷۵ پاس** |
| Ruff روی فایل‌های Python تغییرکرده | پاس |
| mypy روی هفت فایل source تغییرکرده | بدون خطا |

اجرای نهایی شامل سه فایل قرارداد جدید، دو مجموعهٔ فرمان‌های پایین، و `tests/unit/test_agent_board.py` و `tests/unit/test_docs_integrity.py` بود؛ نتایج سطرهای مجزا را دوباره به عدد ۵۳۱ اضافه نکنید. این اعداد اجرای انتخابی هستند، نه ادعای اجرای کل suite یا coverage سراسری. تست skip دقیقاً `test_eight_independent_postgres_creators_all_succeed` با دلیل `requires PostgreSQL` است. دامنهٔ دقیق مجموعه‌ها در پایین ثبت شده است.

برای تست نصب‌شده، ابتدا wheel ساخته و با `pip install --no-deps --no-index --target ...` نصب شد. سپس Python با `-I` اجرا شد، مسیر target به ابتدای مسیر import اضافه شد و تعلق بسته به target بررسی شد؛ pytest همان سه فایل قرارداد را علیه کد نصب‌شده اجرا کرد. dependencyها از محیط تشخیصی تأمین شدند، نه از نصب کامل production.

تست‌های توزیع نیز مستقلاً هر دو مسیر **wheel مستقیم** و **sdist→wheel** را می‌سازند؛ منبع build را حذف می‌کنند؛ در cwd دیگر با Python ایزوله اجرا می‌شوند؛ و تعلق تمام ماژول‌های importشدهٔ برنامه به target را assert می‌کنند. migration دو بار upgrade می‌شود، drift بررسی می‌شود، upsert واقعی اجرا می‌شود، downgrade و upgrade مجدد نیز آزمایش می‌شود. [FILE:tests/unit/test_wheel_install.py:1-198]

موفقیت رسانه با موتور واقعی و خروجی واقعی همان موتور آزموده شده؛ فقط HTTP/gTTS و ارسال Telegram شبیه‌سازی شدند. بستن stream در ارسال موفق، RuntimeError، OSError و CancelledError بررسی می‌شود. همچنین dispatcher واقعی Telegram با guard واقعی آزمایش می‌شود: کاربر غیرمجاز به graph/DB راه ندارد و کاربر مجاز از مسیر دیتابیس واقعی عبور می‌کند؛ پاسخ graph در این تست کنترل‌شده است. [FILE:tests/integration/test_runtime_contracts.py:162-257] [FILE:tests/integration/test_runtime_contracts.py:296-343]

### فرمان‌های رگرسیون قابل تکرار

در این جلسه `pytest`، `ruff` و `mypy` از `/home/user/.venv-fix/bin/` اجرا شدند.

```bash
pytest -q tests/unit/test_db.py tests/unit/test_migrations.py \
  tests/unit/test_migration_metadata.py tests/unit/test_adopt_pg.py \
  tests/unit/test_migrate_postgres_guard.py tests/unit/test_ai_memory_consent.py \
  tests/unit/test_surface_onboarding.py tests/unit/test_handlers.py \
  tests/unit/test_phase0_command_fixes.py tests/unit/test_feature_wiring.py \
  tests/unit/test_access_guard.py tests/unit/test_safe_paths.py \
  tests/unit/test_imagine_command.py tests/unit/test_slideshow_render.py \
  tests/unit/test_ci_extras_parity.py tests/architecture/test_import_boundaries.py

pytest -q tests/unit/test_database_url.py tests/unit/test_create_all_race.py \
  tests/unit/test_postgres_checkpointer.py tests/integration/test_migrate_adoption.py \
  tests/integration/test_migrate_cli.py tests/integration/test_migrate_race_condition.py \
  tests/integration/test_postgres_create_all_race.py tests/integration/test_graph.py \
  tests/integration/test_bot_slideshow_flow.py tests/unit/test_pack_runtime_composition.py \
  tests/unit/test_pack_manifest_verify.py tests/unit/test_image_gen_adapters.py \
  tests/unit/test_i18n_parity.py

mypy src/nexus_ai_agent/bot/handlers.py \
  src/nexus_ai_agent/features/{image_gen,speech,onboarding,media_result}.py \
  src/nexus_ai_agent/storage/{db,migrations}.py
```

### موجودی wheel مشاهده‌شده

| مورد | ممیزی اولیه | پس از اصلاح |
|---|---:|---:|
| تمام ورودی‌های ZIP | ۲۵۳ | ۲۸۶ |
| JSON | ۰ | ۲۶ |
| locale | ۰ | ۱۵ |
| pack manifest | ۰ | ۶ |
| فایل‌های Alembic بسته‌بندی‌شده | ۰ | ۶ |

شش فایل Alembic عبارت‌اند از config، env، template و سه revision. تست، محتویات JSONها و فایل‌های migration را با منبع canonical مقایسه می‌کند؛ شمارش به‌تنهایی معیار قبولی نیست. [FILE:tests/unit/test_wheel_install.py:110-130]

## ۴. محدودیت‌های تأیید و کارهای انجام‌نشده

- محیط محلی Python 3.11 بود. تست زندهٔ Telegram، Pollinations یا Google TTS انجام نشد؛ چنین تستی به مجوز شبکه، حساب و مشاهدهٔ سرویس واقعی نیاز دارد.
- محیط تشخیصی فاقد شش dependency سنگین/غیرلازم برای این دامنه بود: sentence-transformers، llama-cpp-python، chromadb، flashrank، langchain-community و litellm. نصب کامل production از این اجرای محلی نتیجه نمی‌شود.
- workflow جدید نصب کامل wheel و `pip check` را برای سه نسخهٔ Python درخواست می‌کند. نتیجهٔ CI راه دور و enforce شدن آن به‌عنوان required check، مستقل از پاس محلی است؛ در این گزارش ادعای سبز بودن آن نشده است. [FILE:.github/workflows/wheel-contract.yml:1-52]
- database migration واقعی PostgreSQL در این محیط اجرا نشد؛ انتخاب backend و رفتارهای شبیه‌سازی‌شده/SQLite تست شدند.
- رفع خواندن model فایل ابری، به معنی رفع قرارداد upload، جداسازی کلید tenant یا تضمین سلامت providerهای ابری نیست.
- مسیر جداگانهٔ Gemini در handler، سیاست FakeLLM، dashboard بدون token، حذف سراسری حافظه، retry scheduler و مالکیت چندپردازه‌ای صف در این patch بازنویسی نشدند.
- ممیزی قبلی و درصدهای آن به commit مبنا مربوط‌اند؛ برای زیباتر کردن وضعیت، گزارش تاریخی بازنویسی نشد.

## ۵. نکتهٔ مهم پیش از استقرار

**اگر قبلاً `NEXUS_DB_PATH` سفارشی داشته‌اید، از هر دو فایلِ مسیر تنظیم‌شده و `data/app.sqlite` قدیمی backup بگیرید.** ممکن است نسخهٔ قبلی بین این دو محل داده نوشته باشد. این اصلاح از این پس مسیر صحیح را انتخاب می‌کند؛ داده‌های قدیمی را خودکار merge، جابه‌جا یا پاک نمی‌کند. برای هر محیط باید بررسی شود کدام فایل دادهٔ معتبر دارد. راهنمای زنده نیز به‌روزرسانی شده است. [FILE:docs/architecture/DATA_AND_STORAGE.md:64-84]

پیشنهاد rollout: ابتدا محیط آزمایشی با کپی backup، سپس نصب wheel، migration و smoke، بعد پایلوت محدود. تست موفق محلی مجوز حذف backup یا انتشار بدون بررسی باقی‌مانده‌های امنیتی نیست.

## ۶. ده کار بعدی؛ جدا از اصلاح تحویل‌شده

۱. رسید واقعی upload و جداسازی کلید فایل کاربران؛ معیار: دو فایل هم‌نام، restart و download صحیح.

۲. یک مالک برای ساخت موتورهای runtime؛ معیار: handler همان نمونهٔ پیکربندی‌شده را استفاده کند.

۳. خطای typed و محدودسازی FakeLLM؛ معیار: quota/timeout پاسخ موفق عادی نسازند.

۴. fail-closed کردن dashboard عمومی؛ معیار: نبود token مسیر ناامن باز نکند.

۵. سقف concurrency و backlog؛ معیار: overload قابل پیش‌بینی و حافظهٔ محدود.

۶. cancellation فرایند render؛ معیار: process زنده یا artifact منتشرشدهٔ ناقص باقی نماند.

۷. قرارداد حذف همهٔ داده‌های وعده‌داده‌شده؛ معیار: storeهای مشمول پس از حذف خالی باشند.

۸. recovery زمان‌بندی‌ها و delivery کمپین‌ها؛ معیار: restart کار ثبت‌شده را گم نکند.

۹. lock وابستگی production و vulnerability scan؛ معیار: نصب قابل بازتولید با گزارش resolve‌شده.

۱۰. تمرین backup/restore و readiness؛ معیار: بازیابی سنجیده‌شده، نه صرف وجود فایل backup.

**جمع‌بندی:** سه شکست مورد درخواست اصلاح و با آزمون منفی/مثبت پوشش داده شدند؛ دو نقص مسیر دیتابیس و onboarding نیز در همان دامنه بسته شدند. زیرساخت اثبات نسخهٔ قابل نصب اضافه شد. این یک اصلاح مهندسی محدود و قابل بررسی است، نه ادعای اینکه تمام پروژه اکنون بی‌نقص یا آمادهٔ انتشار عمومی شده است.
