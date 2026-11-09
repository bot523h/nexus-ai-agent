# گزارش نهایی مهندسی — NAGAR Creative Capability Composition Runtime

**شاخه:** `arena/da6837a6-nexus-ai-agent` · **SHA پایه:** `5ed6d4e10be55f5baf5e9f2a8aa99775577621db` (برابر با `origin/main`)

این سند، گزارش نهایی یکپارچه‌سازی «runtime ترکیب قابلیت‌های خلاق NAGAR» در معماری واقعی
NEXUS است. تمام اعداد، نام‌ها، مسیرها و امضای توابع در این گزارش از اجرای واقعی در همین
محیط استخراج شده‌اند. هرجا چیزی **اثبات نشده** باشد، صریحاً با برچسب
`INFERRED` یا `DEFERRED` مشخص شده و هرگز به‌عنوان `VERIFIED` عرضه نشده است.

واژه‌نامه وضعیت‌ها (طبق دستور کار):

| وضعیت | معنا |
|---|---|
| `VERIFIED` | با اجرای واقعی و قابل بازتولید در همین محیط اثبات شده است. |
| `VERIFIED_WITH_LIMITATIONS` | اثبات شده، اما با محدودیت صریح (مثلاً نبود GPU یا نبود `ffprobe`). |
| `HARDENED_BUT_NOT_COMPLETE` | کنترل‌ها اعمال شده‌اند اما پوشش کامل نیست. |
| `BLOCKED` | مجوزهای سیستم‌عاملی مانع اجرای کنترل است. |
| `DEFERRED` | عمداً به آینده موکول شده، با دلیل مشخص. |

---

## ۱. خلاصهٔ اجرایی

NAGAR Creative Capability Composition Runtime به‌صورت **یک لایهٔ ترکیب‌کنندهٔ نازک** روی
زیرساخت موجود NEXUS ساخته شد — نه به‌عنوان یک زیرساخت موازی. کامپایلر ترکیب فقط **منطق
DAG و اعتبارسنجی تایپی** را مالک است؛ رجیستری قابلیت‌ها، صف کار، اتوریتی، CommandBus،
سیاست مجوز، اثر انگشت پاسپورت و مرجع تأیید نهایی همگی **کانونی و دست‌نخورده** باقی ماندند.

* **۶۰** عملیات در رجیستری زنده (۵۷ پیش از این شاخه + ۳ عملیات پک جدید).
* **۱۹۴** تست جدید در ۱۱ فایل، همگی سبز؛ به‌علاوهٔ کل مجموعهٔ تست مخزن.
* **۳** جریان کامل از صف تا گذرنامهٔ اثر انگشت روی صف canonical اجرا شد.
* **۰** موارد `TODO`/`FIXME`/`TBD` در صفحات معماری (گیت دروازهٔ اسناد).
* یک **دروازهٔ معماری** که می‌گوید «هیچ CommandBus دومی ساخته نمی‌شود» در ابتدا قرمز شد؛
  اجراکنندهٔ ترکیب به کارخانهٔ canonical (`build_job_bus`) هدایت شد، نه اینکه گیت دور زده شود.

---

## ۲. تازگی (Novelty) — تعریف دقیق

مهم است که تازگی این کار **با دقت** تعریف شود، چون ادعای بیش از حد، تنها راه از دست دادن
اعتبار کل بسته است. این کار در هیچ‌یک از این شش دسته «اول» نیست:

| دسته | وضعیت این کار |
|---|---|
| گردش‌کار (workflow) | **نیست.** یک گردش‌کار، دنبالهٔ ثابتی از گام‌هاست. اینجا گراف از روی درخواست ساخته می‌شود. |
| ترکیب تایپی (typed composition) | **نیست، به‌تنهایی.** تایپ‌ها وجود دارند، اما تایپ به‌تنهایی تازگی نیست. |
| کامپایل DAG | **نیست، به‌تنهایی.** کامپایل DAG شناخته‌شده است؛ اینجا فقط بخشی از آن است. |
| قابلیت مشتق (derived capability) | **نیست، به‌تنهایی.** مشتق‌سازی قابلیت به‌تنهایی تازگی نیست. |
| سنتز پویا (dynamic synthesis) | **نیست.** هیچ موتور سنتز، جست‌وجوی برنامه یا تولید کد وجود ندارد. |
| موتور اصلی (original engine) | **نیست.** موتور رسانه همان FFmpeg کانونی است. |

**تعریف دقیق تازگی این کار:**

> یک **لایهٔ ترکیب تایپیِ مبتنی بر رجیستری** که *هیچ مالکیتی از زیرساخت اجرا ندارد*:
> خروجی آن **IR ترکیبی نسخه‌دار** است که با **اثر انگشت پایدار** به **رجیستری کانونی**
> قفل می‌شود، و **اجرا، مجوز، صف و تأیید** همگی توسط زیرساخت کانونی انجام می‌شوند.

یعنی تازگی در **ترکیب** سه چیز است که معمولاً جدا می‌شوند:

1. **نوع رسانهٔ تایپی** که در `OperationSpec` کانونی زندگی می‌کند (نه در یک رجیستری موازی)؛
2. **قفل‌شدن برنامه بر رجیستری** با `registry_fingerprint` — یعنی برنامهٔ کامپایل‌شده با
   رجیستریِ متفاوت **قابل بازپخش پنهانی نیست**؛
3. **تفکیک سخت اثبات از تأیید معنایی**، جایی که تأییدکننده مسیر اندازه‌گیریِ خودش را
   دارد و به گزارش تولیدکننده تکیه نمی‌کند.

بنابراین ادعای درست: **«ترکیب تایپیِ مبتنی بر رجیستری با اثر انگشت پایدار، بدون مالکیت
زیرساخت»**. ادعای «موتور ترکیب جدید» نادرست است و در این گزارش داده نشده است.

### ۲.۱ چرا «بدون مالکیت زیرساخت» خودش یک محدودیت معماری است، نه یک تعارف

اگر این لایه رجیستری، صف یا اتوریتی خودش را می‌ساخت، سه چیز هم‌زمان خراب می‌شد:
حقیقتِ توانایی (توانایی‌های ثبت‌شده در یک جای دیگر)، مالکیتِ اجرا (دو صف، دو مسیر
بازپخش) و مرجع تأیید (دو تعریف «تأییدشده»). گیت معماری
`tests/architecture/test_composition_boundary.py` این را به‌صورت ماشینی نگه می‌دارد:
بستهٔ `creative/composition/` هیچ `Registry`، هیچ `JobQueue` و هیچ `CommandBus` خودش را
نمی‌سازد؛ کارگر `creative_composition` در `worker.py::default_job_handlers()` ثبت شده و
تأییدکنندهٔ آن در `adapters/in_process_job_queue.py::default_artifact_verifiers()`.

---

## ۳. نامزدهای قابلیت NAGAR (حداقل سه نامزد)

### ۳.۱ نامزد A — «ترجمهٔ ایستای ویدئو به ویدئوی عمودی با هویت بصری حفظ‌شده»

* **مسئله:** ناشر می‌خواهد یک ویدئوی ۱۶:۹ را به ۹:۱۶ تبدیل کند بدون آنکه چهره/سوژه از
  کادر خارج شود. تبدیل هندسی ساده (center-crop) سوژه را می‌برد.
* **قابلیت‌های constituent:** `media.resize_image` (برای پیش‌پردازش قاب)،
  `media.grayscale_image` (برای استخراج ماسک وزنی)، `media.image_to_video`
  (تولید خروجی نهایی)، و یک قابلیت **تشخیص سوژه** که هنوز در رجیستری نیست
  (`scene.auto_reframe_subject` — در سند کاتالوگ موجود است اما `MISSING` است).
* **قراردادها:** ورودی `video` (کدک H.264، ۱۹۲۰×۱۰۸۰، ۲۵fps)، خروجی `video`
  (H.264، ۱۰۸۰×۱۹۲۰، ۲۵fps، بدون تغییر نرخ فریم)، میان‌دادهٔ `image` (PNG خاکستری).
* **تمایز:** خروجی باید *هم‌ارتفاع* و *هم‌نرخ‌فریم* باشد و زیرنویس‌ها نباید قطع شوند.
* **محدودیت‌ها:** تشخیص سوژه در رجیستری موجود نیست؛ بدون آن، نامزد به
  `DEFERRED` است.
* **امکان‌پذیری:** متوسط — بخش رسانه‌ای با پک `nexus.media.transform` قابل ساخت است؛
  بخش تشخیص سوژه به قابلیت جدید نیاز دارد.
* **آزمایش ابطال‌پذیر:** اگر `scene.auto_reframe_subject` اضافه شود، آیا خروجی ۹:۱۶
  هنوز ۲۵fps و ۱۰۸۰ پیکسل عرض دارد؟ اگر نه، نامزد رد می‌شود.
* **معیار سنجش:** نسبت پیکسل‌های بریده‌شدهٔ سوژه در ۲۰ کلیپ عمومی؛ معیار پذیرش:
  ≤ ۲٪ در ۹۵٪ کلیپ‌ها.

### ۳.۲ نامزد B — «بستهٔ استیل با نگاشت رنگ مشتق»

* **مسئله:** یک عکس خام باید به یک استیل «برندشده» (رنگ‌تست، کنتراست، وینیت) تبدیل شود.
* **قابلیت‌های constituent:** `media.resize_image` + `media.grayscale_image`
  (برای ساخت ماسک لومینسانس) + یک قابلیت نگاشت رنگ. نگاشت رنگ در کاتالوگ
  (`color.deband_denoise`، `color.hdr_tonemap`، `color.white_balance`) وجود دارد ولی
  `MISSING` است.
* **قراردادها:** ورودی/خروجی `image` PNG؛ مشتقات: `output_sha256`، `output_path`،
  `source_asset_id` — همان الگوی `media_derived_fields` که در همین شاخه تعریف شد.
* **تمایز:** ماسک خاکستری به‌عنوان ورودی یک عملیات رنگی، یعنی *ترکیب واقعی* که در آن
  خروجی یک گره، ورودی گره بعدی است.
* **محدودیت‌ها:** عملیات رنگی موجود نیست ⇒ نامزد `DEFERRED` تا زمان افزوده‌شدن.
* **امکان‌پذیری:** بالا پس از افزوده‌شدن سه قابلیت رنگی (که خودشان در کاتالوگ محصول
  هستند و فقط پیاده‌سازی نشده‌اند).
* **آزمایش ابطال‌پذیر:** آیا میانگین روشنایی خروجی در بازهٔ هدف می‌ماند و آیا دلتای
  رنگ نسبت به نسخهٔ پایه کمتر از آستانه است؟
* **معیار سنجش:** ΔE میانگین ≤ ۳ در برابر یک LUT مرجع.

### ۳.۳ نامزد C — «زنجیرهٔ آرشیو با تبدیل چندنرخی»

* **مسئله:** یک ویدئوی اصلی باید هم‌زمان به نسخهٔ ۱۰۸۰p (اصلی) و ۴۸۰p (proxy) تبدیل شود
  و هر دو باید گذرنامهٔ پاسپورت مستقل داشته باشند.
* **قابلیت‌های constituent:** `media.image_to_video`/`media.resize_image` به‌عنوان پایه،
  به‌علاوهٔ `delivery`/`edit` موجود در رجیستری (`nexus.color.delivery`).
* **قراردادها:** یک ورودی، **چند خروجی** (`expected_outputs` چندتایی در IR پشتیبانی
  می‌شود)، هر خروجی با `ProofCarryingArtifact` و اثر انگشت خودش.
* **تمایز:** چند خروجی از یک گراف، با شاخه‌های موازی که هرکدام تأیید مستقل دارند.
* **محدودیت‌ها:** امروز executor هر گره را **ترتیبی** اجرا می‌کند؛ اجرای موازی واقعی
  پیاده نشده است ⇒ اجرای موازی `DEFERRED` است.
* **امکان‌پذیری:** متوسط.
* **آزمایش ابطال‌پذیر:** آیا هر دو خروجی تأیید معنایی مستقل می‌گیرند و آیا اثر انگشت
  آن‌ها متفاوت است؟
* **معیار سنجش:** دو گذرنامهٔ مستقل با `status == VERIFIED` و اثر انگشت‌های متمایز.

### ۳.۴ دربارهٔ چندزبانی (Rust/Java/Go)

**هیچ مرز چندزبانی اضافه نشده است.** دلیل: محدودیت اندازه‌گیری‌شده‌ای که توجیه‌کنندهٔ آن
باشد وجود ندارد. اندازه‌گیری واقعی این شاخه (بخش ۸) نشان می‌دهد که کل زنجیرهٔ سه‌گرهی
رسانه‌ای در میانهٔ **۶۹۰ میلی‌ثانیه** (حالت اجراکننده) و **۸۹۱ میلی‌ثانیه** (حالت صف)
تمام می‌شود؛ سهم غالب **خود FFmpeg** است (≈ ۵۶۹ میلی‌ثانیه)، نه لایهٔ پایتون.
جایگزینی پایتون با Rust تنها زمان را کم می‌کند چون زمان در پایتون **نیست**؛ هزینهٔ
نگهداری، نقطهٔ شکست trust-plane و پیچیدگی دو موتور پرداخت می‌شود بدون آنکه محدودیت
واقعی برطرف شود. تا وقتی یک محدودیت اندازه‌گیری‌شده (مثلاً تأخیر IPC، یا عدم توان
قطع‌کردن گروه فرایند در سطح پایتون) با عدد مستند وجود نداشته باشد، افزودن زبان دیگر
یعنی ادعای بدون شاهد.

---

## ۴. قرارداد قابلیت و قرارداد رسانه

سند کامل: [`docs/architecture/COMPOSITION_RUNTIME.md`](COMPOSITION_RUNTIME.md). خلاصهٔ
نکات کلیدی با ارجاع به کد:

### ۴.۱ هویت و نسخه‌بندی

هر گره به‌صورت اجباری این چهار چیز را حمل می‌کند و کامپایلر هر چهار را در برابر رجیستری
زنده بررسی می‌کند:

```python
node.operation  # "media.resize_image"
node.capability_id  # "media.resize_image"
node.capability_version  # "1.0.0"
node.operation_schema_version  # 1
```

انحراف هر کدام خطای تایپی با کد پایدار می‌دهد: `capability_version_drift`،
`schema_version_drift`. اگر `capability_id` گره با توضیح‌دهندهٔ عملیات نخواند،
کامپایلر `capability_version_drift` می‌دهد (آزمون:
`tests/unit/test_composition_runtime.py::test_the_compiler_refuses_a_node_whose_capability_does_not_match_its_operation`).

### ۴.۲ قرارداد رسانهٔ تایپی

`MediaContract` در `src/nexus_ai_agent/creative/studio/models.py` این ابعاد را اعلام می‌کند:
`kind`، `container`، `codec`، `width`، `height`، `pixel_format`، `color_space`،
`frame_rate`، `time_base`، `sample_rate`، `channel_layout`، `bit_depth`، `alpha`،
`duration_us`. سازگاری **اشتقاقی** است: اگر گرهٔ بعدی `color_space="gray"` بخواهد و
گرهٔ قبلی `color_space="rgb"` بدهد، قرارداد ناسازگار است و کامپایلر `type_mismatch`
می‌دهد.

### ۴.۳ اسکیمای پارامتر نسخه‌دار

مدل پارامتر از **همان** `input_model` کانونی ساخته می‌شود
(`compiler.py::_params_model`): فیلدهایی که executor پر می‌کند
(`media_derived_fields`) اختیاری می‌شوند، بقیه با نوع/بازه/enum خود باقی می‌مانند و
`extra="forbid"` تضمین می‌کند پارامتر ناشناخته به worker نرسد. خطاها:
`invalid_parameters` (نقض نوع/بازه/enum) و `unknown_parameter` (کلید اضافه).

### ۴.۴ مدل منابع

`ResourceRequirements` / `ResourceEstimate` **RAM تخمینی**، **RAM اوج هم‌زمان**،
**دیسک**، **اسکرچ**، **CPU**، **GPU**، **شبکه**، **timeout** و **هم‌زمانی** را جدا
می‌کنند. چون یک ترکیب گره‌ها را **ترتیبی** اجرا می‌کند، اوج ≠ مجموع؛ مدل هر دو را
گزارش می‌کند و `peak_concurrent_ram_mb` عدد اوج است.

---

## ۵. تهدیدنامه و سخت‌سازی

سند کامل: [`docs/architecture/COMPOSITION_THREAT_MODEL.md`](COMPOSITION_THREAT_MODEL.md).
خلاصهٔ آن:

* مدل تهدید **STRIDE** با **۱۶ تهدید** (`T1`…`T16`)، که هر کدام **نماد enforcing** و
  **آزمون اثبات‌کنندهٔ نام‌بردنی** دارد.
* کنترل‌های واقعاً اعمال‌شده: `shell=False` و فقط argv، ** allow-list ** مسیر مطلق
  باینری، allow-list متغیرهای محیطی با حذف اسرار، پوشهٔ موقت اختصاصی هر گره،
  containment مسیر، رد symlink، سقف اندازهٔ ورودی و خروجی، timeout،
  **مدیریت گروه فرایند** با `start_new_session=True` + `killpg` (SIGTERM → SIGKILL)،
  و تشخیص‌های محدودشده (bounded).
* کنترل‌های سطح سیستم‌عامل که در این محیط **مسدود** هستند و صریحاً `BLOCKED` علامت
  خورده‌اند: namespace اختصاصی، cgroup، seccomp، جداسازی کاربر، سهمیهٔ منابع، و
  فنس شبکه. دلیل: مجوز سیستم‌عامل در این محیط اجازه نمی‌دهد (بدون `CAP_SYS_ADMIN` /
  بدون root). این‌ها «انجام‌نشده» اعلام نشده‌اند به‌عنوان انجام‌شده.
* **fail-closed پیش از dispatch** برای هر ۱۰ شرط خواسته‌شده: قابلیت ناشناس، قابلیت
  بی‌اعتماد، مجوز ناکافی، پک بی‌اعتماد، اسکیمای نامعتبر، ورودی الزامی گمشده، مجوز
  ناسازگار، سهمیهٔ فراتر، شبکهٔ ممنوع، و ارجاع اثر نامعتبر.

---

## ۶. دفتر مجوز (Twelve-Category Licence Ledger)

سند تفصیلی: [`docs/architecture/COMPOSITION_RUNTIME.md`](COMPOSITION_RUNTIME.md) §«Licence ledger».
دسته‌ها **دوازده‌تا** و جدا از هم‌اند:

`adapter_code` · `runtime_software` · `library` · `model` · `model_weights` · `dataset` ·
`input_asset` · `distribution_restrictions` · `commercial_permission` ·
`attribution_notices` · `copyleft` · `network_use`

### ۶.۱ حقیقت اندازه‌گیری‌شدهٔ FFmpeg (نه به‌خاطر سپرده)

از همان باینری که واقعاً اجرا شد خوانده شد
(`license.py::ffmpeg_build_facts` → `ffmpeg -version` و `ffmpeg -hide_banner -encoders`):

| قلم | مقدار اندازه‌گیری‌شده |
|---|---|
| مسیر باینری | `/home/user/nexus-ai-agent/.venv/lib/python3.11/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v9.0.2` |
| نسخه | `n9.0.2-17-g2a571b6068-20260930` |
| `-buildconf` | `--enable-gpl --enable-version3 --enable-libx264 --enable-libx265 --disable-libfdk-aac …` (static) |
| `gpl` | `True` |
| `version3` | `True` |
| `nonfree` | `False` |
| `libx264` | `True` |
| `libx265` | `True` |
| `libvpx-vp9` | `True` |
| `mpeg4` | `True` |
| SHA-256 کل `-buildconf` | `1473b6410dc4c9647899005fcf0b08edd0289f2677ce83c992d79d5d60ed433a` |

### ۶.۲ قاعدهٔ «مجوز ابزار ≠ حق رسانهٔ خروجی»

دفتر مجوز این را **ماشینی** رعایت می‌کند: رکورد `runtime_software` دربارهٔ *ابزار* است و
دامنهٔ توزیعش صریحاً می‌گوید «حاکمیت رسانهٔ تولیدشده تابع شرایط خودِ دارایی ورودی است،
نه این رکورد». حضور `libx264` **هیچ** چیزی دربارهٔ حق تجاریِ رسانهٔ خروجی ثابت نمی‌کند؛
آن حق در دستهٔ `input_asset` و `commercial_permission` زندگی می‌کند.

### ۶.۳ وضعیت واقعی دفتر برای اجرای ثبت‌شده

```
effective_allow_commercial : False
commercial_use_verified    : False
unknown_categories         : model, model_weights, dataset, input_asset,
                             distribution_restrictions, commercial_permission,
                             attribution_notices, copyleft, network_use
```

یعنی **۹ دسته نامعلوم** باقی مانده‌اند. `input_asset` به‌صراحت با `spdx_id="UNKNOWN"`
ثبت می‌شود (SHA-256 فایل مصرف‌شده به‌عنوان شاهد)، و `UNKNOWN` در
`unknown_categories` باقی می‌ماند ⇒ `effective_allow_commercial` همیشه `False` است
مگر آنکه **همهٔ** دسته‌ها هم مجاز و هم اثبات‌شده باشند
(`tests/unit/test_composition_license.py`).

---

## ۷. گزارش تأیید (Verification Report)

### ۷.۱ تفکیک لایهٔ یکپارچگی از لایهٔ معنایی

| لایه | چه چیزی را می‌سنجد | مسیر اندازه‌گیری | تست اثبات‌کننده |
|---|---|---|---|
| **یکپارچگی** | هش ورودی/خروجی، هش مانیفست، نسخهٔ قابلیت‌ها، پارامترها، نسخهٔ ابزار، اثر انگشت رجیستری، شناسهٔ تلاش، رسید اجرا، دودواده | `proof.py::verify_integrity` | `tests/unit/test_composition_proof.py` |
| **معنایی** | رمزگشایی واقعی، ابعاد، فضای رنگ، قالب پیکسل، آلفا، خاکستری بودن؛ کانتینر/کدک/ابعاد/نرخ فریم/مدت/تعداد فریم/جریان صوتی و تبدیل‌های موردانتظار | `proof.py::verify_image_semantics` / `verify_video_semantics` | `tests/unit/test_composition_proof.py`, `tests/unit/test_composition_verifier.py` |

**نکتهٔ کلیدی:** «دوباره هش‌کردن» **تحقیق معنایی نیست**. این را در یک آزمون واقعی
مثبت اثبات شد: ابتدا بررسی grayscale به‌صورت «دامنهٔ هر کانال» نوشته شده بود و یک تصویر
**تماماً قرمز** را «خاکستری» قبول می‌کرد (هر کانال ثابت بود ولی کانال‌ها با هم فرق
داشتند). این یک **نقص واقعی** بود؛ اکنون مقایسه **بایت‌به‌بایت سه کانال در سطح پیکسل**
انجام می‌شود و آزمون `test_image_semantics_refuse_a_coloured_image_claiming_grayscale`
آن را می‌گیرد.

### ۷.۲ اجرای واقعی resize ← grayscale ← image_to_video

اجرای ثبت‌شده در `evidence/composition_evidence.json` (تولیدشده با
`python scripts/collect_composition_evidence.py`):

| قلم | مقدار |
|---|---|
| اثر انگشت برنامه | `sha256:6eceb42be923659971e40517fa1b124f0d9c7ae174004e085af7914f6abf18da` |
| اثر انگشت رجیستری | `sha256:b30075f938eb61600b8281bda34ea6c09a3422a01df681fdf51768678110fe41` |
| هش ورودی | `sha256:ee4167edf543dc95a5c5ed1322f1427cf8fb8d8907a44da2f450fb615b9f505f` (۷۸۲ بایت) |
| هش خروجی | `sha256:2f28cfc31bcc1d5003b269aaab9f0186b7b8211d183372fc4afd7fa83a4de327` (۲۴۷۳ بایت) |
| یکپارچگی | `verified=True` · روش `independent_rehash` |
| معنایی | `verified=True` · روش `independent_video_decode` |
| اندازه‌گیری معنایی | `width=640`، `height=480`، `codec=h264`، `pixel_format=yuv420p`، `frame_rate=25.0`، `duration_us=1000000`، `duration_delta_us=0`، `decoded_frames=25`، `expected_frames_from_contract=25`، `has_audio=false` |
| دودواده | `resize:media.resize_image@1.0.0` → `gray:media.grayscale_image@1.0.0` → `video:media.image_to_video@1.0.0` |
| نسخهٔ بازبینی حالت | ۲ / ۴ / ۶ (هر گره دو نسخه جلو می‌رود) |
| نسخهٔ ابزار | `ffmpeg=n9.0.2-17-g2a571b6068-20260930`، `ffmpeg_buildconf_sha256=1473b64…` |

بنابراین: میان‌یابِ resize واقعاً resized است (ابعاد ۶۴۰×۴۸۰)، پیکسل‌ها واقعاً
خاکستری‌اند (بررسی پیکسلی)، ویدئو **کاملاً رمزگشایی** می‌شود (۲۵ فریم رمزگشایی‌شده از
۲۵ فریم موردانتظار)، ابعاد با برنامه می‌خواند، مدت منطقی است (دلتا صفر) و هش/دودواده
ثبت شده است.

### ۷.۳ استقلالِ تأییدکننده از تولیدکننده

تأیید در دو جای مستقل رخ می‌دهد:

1. **درون اجراکننده** (`executor.py::_collect_artifacts`) — برای هر گره، نه فقط
   خروجی‌های نهایی؛ یک میان‌یابِ با اندازهٔ غلط یا غیرخاکستری باید کل ترکیب را
   متوقف کند.
2. **در صف** (`creative/composition/verifier.py::composition_verifier`) — verifier
   ثبت‌شدهٔ صف که **قرارداد** را از ردیف پایدار می‌خواند و **اندازه‌گیری** را خودش با
   رمزگشای خودش انجام می‌دهد. به گزارش تولیدکننده تکیه نمی‌کند.

آزمون E2E این را با دست‌کاری عمدی اثبات می‌کند: پس از ساخت گذرنامهٔ `VERIFIED`، یک بایت
از فایل اثر تغییر می‌کند و گذرنامهٔ بازسازی‌شده به `COMPROMISED` می‌رود
(`tests/integration/test_composition_canonical_e2e.py::test_a_tampered_artifact_flips_the_passport_to_compromised`).

### ۷.۴ پیوند با Artifact Passport و Provenance کانونی

گذرنامه از همان مرجع کانونی ساخته می‌شود:
`nexus_ai_agent/provenance/passport.py::PassportBuilder(journal, queue).build(job_id)`.
هیچ سازوکار گذرنامهٔ موازی ساخته نشد. آزمون E2E وضعیت‌های
`VERIFIED` و `COMPROMISED` را روی همان سازوکار کانونی می‌سنجد.

---

## ۸. گزارش بنچمارک

اسکریپت: `scripts/bench_composition.py` (هستهٔ اندازه‌گیری در
`src/nexus_ai_agent/continuum/composition_bench.py`، چون `scripts/` نصب‌شدنی نیست).
خروجی‌ها: `evidence/composition_benchmark.json` (حالت اجراکننده) و
`evidence/composition_benchmark_queue.json` (حالت صف).

### ۸.۱ محیط

| قلم | مقدار |
|---|---|
| Python | 3.11.2 |
| سکو | `Linux-6.1.158+-x86_64-with-glibc2.36` |
| CPU | ۲ هسته |
| RAM | ۳۹۳۹ MB |
| GPU | **ندارد** |
| FFmpeg | `imageio-ffmpeg 0.8.1` → `ffmpeg-linux-x86_64-v9.0.2` |
| اندازهٔ برنامه | ۳ گره (resize → grayscale → image_to_video) |
| رزولوشن/مدت | ۶۴۰×۴۸۰ / ۱ s / ۲۵ fps (حالت صف) و ۱۲۸۰×۷۲۰ / ۱ s / ۲۵ fps (حالت اجراکننده) |

### ۸.۲ حالت صف (مسیر کانونی) — ۱۰ اجرای قابل‌مقایسه

اندازه‌گیری دوباره روی کامیت `514a41a` پس از رفع موج دوم بازبینی
(`scripts/bench_composition.py --runs 10 --queue`):

| مرحله | میانگین (ms) | میانه (ms) | P95 (ms) | کمینه (ms) | بیشینه (ms) |
|---|---|---|---|---|---|
| `compile` | 81.798 | 72.461 | 161.117 | 68.983 | 161.117 |
| `queue` | 2.821 | 2.684 | 3.894 | 2.485 | 3.894 |
| `worker` (برداشت + اجرا) | 718.731 | 714.271 | 764.657 | 684.996 | 764.657 |
| `verify` (مستقل) | 49.652 | 48.715 | 53.797 | 47.294 | 53.797 |
| `total` | 890.632 | 859.442 | 1151.853 | 826.467 | 1151.853 |

* نرخ خطا: **۰٫۰۰** (۰ از ۱۰)
* اثر انگشت برنامه در هر ۱۰ اجرا **یکسان**: `sha256:f366e646e8f63843a9e9004069481328c5db6619dfa388690c7c7556bf7d356e` ⇒ بازتولید‌پذیری `true`
* اندازهٔ خروجی: میانگین ۲۹۹۰ بایت، بیشینه ۲۹۹۰ بایت (۱۰۰٪ یکنواخت)
* اوج RSS: ۹۵٫۸۲ MB · دیسک/سی‌پیو در گزارش ثبت نشده چون ابزار آن را اندازه نمی‌گیرد ⇒ `INFERRED` (بی‌طرفانه)
* زمان دیوار: ۸٫۹۸۴ s برای ۱۰ اجرا

### ۸.۳ حالت اجراکننده — ۱۰ اجرای قابل‌مقایسه

| مرحله | میانگین (ms) | میانه (ms) | P95 (ms) | کمینه (ms) | بیشینه (ms) |
|---|---|---|---|---|---|
| `compile` | 76.106 | 69.468 | 106.167 | 65.648 | 106.167 |
| `execute` | 597.672 | 574.774 | 767.010 | 556.969 | 767.010 |
| `verify` | 56.003 | 52.970 | 76.824 | 51.159 | 76.824 |
| `total` | 729.794 | 709.089 | 911.401 | 673.783 | 911.401 |

نرخ خطا: **۰٫۰۰** · اثر انگشت بازتولیدپذیر: `true` · اوج RSS: ۷۸٫۷۲ MB · زمان دیوار: ۷٫۴۱۶ s

> این اعداد **کمی کندتر** از اندازه‌گیری پیشین هستند (اجراکننده: ۷۲۹٫۸ در برابر
> ۶۳۸٫۸ ms). علت، بدتر شدن کد نیست: در همین دور یک `asyncio.to_thread` واقعی برای
> اجرای بلوکه‌کننده اضافه شد و هر گره پیش از encode یک pre-flight مجوزدهی و یک
> بازسنجی اندازه/هش ورودی می‌گیرد. همان اندازه‌گیری روی ماشین ۲ هسته‌ای و با نویز
> محیطی انجام شده است؛ P95 در هر دو حالت زیر ۱٫۲ ثانیه مانده است.

### ۸.۴ خوانش

سهم غالب زمان **خود FFmpeg** است، نه لایهٔ ترکیب: `execute` ≈ ۵۹۸ ms از
`total` ≈ ۷۳۰ ms (۸۲٪). لایهٔ کامپایل ≈ ۷۶ ms (که عمدتاً ساخت رجیستری کانونی است، چون
هر اجرا رجیستری زنده را می‌سازد). صف عملاً بی‌هزینه است (۲٫۸ ms). این توزیع دقیقاً
دلیل تصمیم «بدون زبان چندزبانی» در §۳٫۴ است.

---

## ۹. آزمون‌ها

### ۹.۱ مجموعه‌های جدید (همه سبز، اجرای واقعی)

| فایل | نتیجه |
|---|---|
| `tests/unit/test_composition_compiler.py` | **۲۲ passed** |
| `tests/unit/test_composition_contracts.py` | **۲۵ passed** |
| `tests/unit/test_composition_license.py` | **۲۱ passed** |
| `tests/unit/test_composition_proof.py` | **۲۰ passed** |
| `tests/unit/test_composition_runtime.py` | **۱۶ passed** |
| `tests/unit/test_composition_security.py` | **۳۳ passed** |
| `tests/unit/test_composition_verifier.py` | **۱۱ passed** |
| `tests/unit/test_media_transform_pack.py` | **۱۹ passed** |
| `tests/unit/test_media_still_primitives.py` | **۱۱ passed** |
| `tests/architecture/test_composition_boundary.py` | **۱۰ passed** |
| `tests/integration/test_composition_canonical_e2e.py` | **۶ passed** |
| **جمع** | **۱۹۴ passed** |

### ۹.۲ پوشش ۲۴ رفتار خواسته‌شده

| # | رفتار | آزمون اثبات‌کننده | وضعیت |
|---|---|---|---|
| ۱ | ترکیب معتبر | `test_composition_compiler.py` (۲۲ تست) | `VERIFIED` |
| ۲ | ناسازگاری نوع | `test_a_type_mismatch_is_refused` | `VERIFIED` |
| ۳ | چرخه | `test_a_cycle_is_refused` | `VERIFIED` |
| ۴ | binding گمشده | `test_a_missing_binding_is_refused` | `VERIFIED` |
| ۵ | قابلیت ناشناس | `test_the_worker_refuses_an_unknown_capability` | `VERIFIED` |
| ۶ | پارامتر نامعتبر | `test_invalid_parameters` / `test_unknown_parameter` | `VERIFIED` |
| ۷ | actor غیرمجاز | `test_composition_security.py` (گیت اتوریتی bus) | `VERIFIED` |
| ۸ | مجوز ناکافی | `test_media_transform_pack.py` (نردبان B/B/C) | `VERIFIED` |
| ۹ | پک بی‌اعتماد | `test_composition_boundary.py::test_the_pack_substrate_stays_data_only` | `VERIFIED` |
| ۱۰ | قابلیت آزمایشی | `test_composition_boundary.py` + گیت lifecycle کانونی | `VERIFIED` |
| ۱۱ | سهمیهٔ منابع | `test_composition_contracts.py` (مدل منابع) | `VERIFIED_WITH_LIMITATIONS` — تخمین مدل‌شده است، نه cgroup |
| ۱۲ | انکار شبکه | `test_composition_security.py` + `network_policy` در مانیفست | `VERIFIED` |
| ۱۳ | مجوز ناشناس | `test_composition_license.py` | `VERIFIED` |
| ۱۴ | مجوز ناسازگار | `test_composition_license.py` (جدایی ۱۲ دسته) | `VERIFIED` |
| ۱۵ | path traversal | `test_composition_security.py` | `VERIFIED` |
| ۱۶ | فرار symlink | `test_composition_security.py` | `VERIFIED` |
| ۱۷ | timeout | `test_composition_security.py` (timeout گروه فرایند) | `VERIFIED` |
| ۱۸ | لغو (cancellation) | `test_composition_security.py` (SIGTERM→SIGKILL) | `VERIFIED` |
| ۱۹ | شکست جزئی DAG | `test_composition_runtime.py` (توقف در میانه) | `VERIFIED` |
| ۲۰ | ارسال تکراری | `test_composition_canonical_e2e.py` (idempotent redelivery) | `VERIFIED` |
| ۲۱ | retry ایدمپوتنت | همان بالا | `VERIFIED` |
| ۲۲ | دست‌کاری اثر | `test_a_tampered_artifact_flips_the_passport_to_compromised` | `VERIFIED` |
| ۲۳ | ناسازگاری معنایی | `test_composition_verifier.py` (رنگی در برابر خاکستری) | `VERIFIED` |
| ۲۴ | drift نسخهٔ رجیستری | `test_composition_compiler.py` (`registry_drift`) | `VERIFIED` |

### ۹.۳ صادقانه‌دربارهٔ «mock-only»

آزمون‌هایی که **واقعاً** فرایند واقعی اجرا می‌کنند: `test_media_still_primitives.py`
(FFmpeg واقعی روی PNG واقعی)، `test_composition_proof.py` (رمزگشایی واقعی)، و
`test_composition_canonical_e2e.py` (FFmpeg واقعی از طریق صف canonical).
آزمون‌هایی که **بدون** mock هستند اما فرایند واقعی را **نباید** اجرا کنند (مثلاً
«قابلیت ناشناس» یا «parameter نامعتبر») عمداً fail-closed پیش از dispatch را ثابت
می‌کنند و این رفتار را با **نبود** هر فرایند فرزند اثبات می‌کنند. تنها جایی که از
مock استفاده شده، `test_composition_runtime.py` برای **تزریق یک bus** با
`ProjectAccess` محدود است — نه برای ادعای رفتار امنیتی.

### ۹.۴ آزمون جهش (mutation testing)

دو آزمون جهشِ **واقعاً اجراشده** در همین شاخه و نتیجهٔ آن‌ها:

| جهش | نتیجه | شاهد |
|---|---|---|
| تغییر بررسی grayscale از «دامنهٔ کانال» به «مقایسهٔ پیکسلی» | آزمون قبلاً **نمی‌گرفت** (نقص کشف‌شده) | تصویر قرمز اشتباهاً پذیرفته می‌شد ⇒ اکنون رد می‌شود |
| تغییر `plan_to_dict` از شکل wire به شکل `model_dump` | آزمون گردش‌ونگاشت **قرمز** شد | `tests/unit/test_composition_runtime.py` |
| جایگزینی `subprocess.run` با `Popen` + گروه فرایند | گیت معماری «فقط یک جای اجرا» **قرمز** شد | گیت به‌جای دور زدن، به‌روزرسانی و **تقویت** شد (بررسی `start_new_session` و `killpg`) |

مستندسازی جهش کامل (مانند `scripts/*_mutations.py` مخزن) برای این بسته **انجام نشده**
⇒ `DEFERRED`، و به‌صراحت در §۱۱ فهرست شده است.

### ۹.۵ کل مجموعهٔ تست مخزن

```
$ python -m pytest -q -m "not slow"
6631 passed, 39 skipped, 1164 warnings in 567.24s (0:09:27)
```

بازاندازه‌گیری پس از موج دوم بازبینی (۲۰ آزمون تازه در همین دور):

```
$ ./venv/bin/python -m pytest -q -p no:randomly
6651 passed, 39 skipped, 1163 warnings in 611.53s (0:10:11)
```

**مبنای مقایسه** — همان مجموعه روی `origin/main` تمیز، در یک worktree جداگانه
(`git worktree add /tmp/main-baseline origin/main`) با همان مفسر و همان محیط مجازی:

```
6427 passed, 39 skipped, 1163 warnings in 564.46s (0:09:24)
```

یعنی **روی `main` هیچ خطایی وجود ندارد**؛ مبنا یک مبنای پاک است و هر خطایی در شاخهٔ
حاضر قابل انتساب به همین شاخه خواهد بود. اختلاف: **‎+۲۰۴ تست** و **‎۰ خطا**.

> نکتهٔ روش‌شناختی که ثبت آن مهم است: نصب editable، مفسر را به این checkout اشاره
> می‌دهد. در اولین اندازه‌گیری مبنا، فایل `.pth` موقتاً به `/tmp/main-baseline/src`
> تغییر داده نشد و در نتیجه «خطای پیش‌فرض» سه‌تایی گزارش شد که **در واقع خطای این
> شاخه بود، نه خطای main**. مبنا با همان اصلاح دوباره اندازه‌گیری شد و صفر خطا به دست
> آمد. این دقیقاً همان نوع اندازه‌گیری آلوده‌ای است که ممیزی وجودش برای جلوگیری از آن است.

### ۹.۶ یک artifact committed که این شاخه مجاز بوده بود بازتولید کند

`tests/fixtures/cognition_quality_eval/v2_baseline.json` یک **baseline رگرسیون
بازبینی‌شده** است که عملیات‌های رجیستری زنده را ثبت می‌کند. افزودن سه عملیات آن را
تغییر می‌دهد، بنابراین از **مسیر بازبینی کانونی**
(`python scripts/cognition_quality_eval.py --write-candidate …` ← بازبینی ← commit)
بازتولید شد. تنها تغییر: سه عملیات `media.*` و اثر انگشت
`268adff247b9060a` → `f4868122ed9e87af` (و دو ثابتِ اثر انگشت در
`tests/unit/test_cognition_quality_eval.py`).

---

## ۱۰. E2E کانونی (Gate 8)

### ۱۰.۱ مسیر کانونی واقعی

```mermaid
flowchart TD
    A[درخواست: typed intent] --> B[CompositionCompiler<br/>compile]
    B --> C[Canonical Capability Resolution<br/>build_runtime_registry]
    C --> D{Policy / Authority<br/>CommandBus.authorize}
    D -->|رد| E[fail-closed: policy_refused]
    D -->|قبول| F[Canonical JobQueue<br/>InProcessJobQueue.enqueue]
    F --> G[Registered Worker<br/>worker.default_job_handlers<br/>creative_composition]
    G --> H[Media execution<br/>FFmpeg در پوشهٔ اختصاصی هر گره]
    H --> I[Independent Verification<br/>verify_media_semantics]
    I --> J{سالم؟}
    J -->|بله| K[Evidence + ArtifactClaim<br/>canonical dialect]
    J -->|خیر| L[semantic_mismatch]
    K --> M[Artifact Passport<br/>PassportBuilder.build]
    M --> N[Project Graph / Lineage]
```

* **Job ID واقعی:** بله — از `InProcessJobQueue.enqueue` بازگردانده می‌شود و در آزمون
  E2E روی وضعیت پایدار (`queue.get_status`) مشاهده می‌شود.
* **فراخوانی واقعی worker:** بله — از `worker.py::default_job_handlers()` گرفته می‌شود و
  در صف ثبت می‌شود، نه اینکه مستقیم صدا زده شود.
* **اثر واقعی:** بله — فایل MP4 واقعی روی دیسک.
* **شاهد پیوند‌خورده به تلاش:** بله — `evidence` شامل `receipts` با `state_revision`،
  `state_hash`، هش ورودی و خروجی هر گره.
* **گذرنامه از مرجع موجود:** بله — `provenance/passport.py::PassportBuilder`.

### ۱۰.۲ جریان‌های اجراشده

| جریان | وضعیت | شاهد |
|---|---|---|
| Image → Image (resize ← grayscale) | `VERIFIED` | `test_the_image_flow_produces_a_real_verified_artifact` |
| Image → Video (resize ← grayscale ← image_to_video) | `VERIFIED` | `test_the_video_flow_produces_a_real_verified_artifact` |
| Video → Video | `DEFERRED` | دلیل: رجیستری کانونی هیچ عملیات «video → video» ندارد؛ `media.image_to_video` تنها تولیدکنندهٔ ویدئو است. افزودن یک عملیات ویدئو-به-ویدئو یعنی افزودن قابلیت خارج از دامنهٔ این شاخه. به‌جای ساختن mock، دلیل ثبت شد. |
| Audio → Video | `DEFERRED` | دلیل: هیچ قابلیت «audio → video» در رجیستری وجود ندارد. |

جریان سوم/چهارم عمداً **با mock ساخته نشدند**؛ دلیل مشخص ثبت شد، مطابق دستور کار.

---

## ۱۱. اجرای نهایی و وضعیت‌ها

اجرای نهایی روی **درخت پاک** (پس از commit) و با همان محیط:

| گیت | فرمان | نتیجه | وضعیت |
|---|---|---|---|
| کل مجموعهٔ تست | `python -m pytest -q -m "not slow"` | `6631 passed, 39 skipped, 1164 warnings in 567.24s` | `VERIFIED` |
| مبنای `main` تمیز | همان فرمان در worktree جداگانه | `6427 passed, 39 skipped, 0 failed` | `VERIFIED` |
| lint | `ruff check .` | `All checks passed!` | `VERIFIED` |
| قالب | `ruff format --check .` | `993 files already formatted` | `VERIFIED` |
| انواع | `mypy src` | `Success: no issues found in 373 source files` | `VERIFIED` |
| گیت اسناد | `python -m pytest tests/unit/test_docs_integrity.py -q` | `117 passed` | `VERIFIED` |
| گیت حقیقت عملیات | `python -m nexus_ai_agent.nagar --check` | `TRUTH GATE: GREEN` | `VERIFIED` |
| پوشش پک | `python scripts/pack_coverage.py` | `ACCEPTED` — `media` 227/227 (۱۰۰٪)، مجموع ۹۷٫۱۷٪، `pytest exit: 0`، `evidence: verified` | `VERIFIED` |
| مجموعه‌های جدید ترکیب | ۱۱ فایل | `194 passed` (به‌تفکیک در §۹.۱) | `VERIFIED` |
| E2E کانونی | `pytest tests/integration/test_composition_canonical_e2e.py -q` | `6 passed` | `VERIFIED` |
| بنچمارک (صف) | `scripts/bench_composition.py --runs 10 --queue` | ۱۰ اجرا، نرخ خطا `0.0`، اثر انگشت تکرارپذیر | `VERIFIED` |
| بنچمارک (اجراکننده) | `scripts/bench_composition.py --runs 10` | ۱۰ اجرا، نرخ خطا `0.0`، اثر انگشت تکرارپذیر | `VERIFIED` |

### ۱۱.۱ جدول وضعیت نهایی

| حوزه | وضعیت | دلیل کوتاه |
|---|---|---|
| ادغام کانونی (Registry/Queue/Bus/Policy/Provenance/Verification) | `VERIFIED` | گیت معماری ماشینی + E2E واقعی |
| قرارداد قابلیت و IR | `VERIFIED` | ۲۲ + ۲۵ آزمون کامپایلر/قرارداد |
| اقتدار پیش از اجرا (pre-flight روی خود bus) | `VERIFIED` | `CommandBus.check_authorization` + گیت معماری «فقط bus» |
| هفت یافتهٔ بازبینی بیرونی | `VERIFIED` | همه رفع شد، هر یک با آزمون ردّکننده |
| مدل تهدید و سخت‌سازی در سطح فرایند | `VERIFIED` | ۳۳ آزمون امنیتی شامل timeout گروه فرایند |
| مدل تهدید و سخت‌سازی در سطح سیستم‌عامل (namespace/cgroup/seccomp/user-separation/egress) | `BLOCKED` | مجوز سیستم‌عامل در این محیط موجود نیست؛ صریحاً علامت خورده |
| سهمیهٔ منابع | `HARDENED_BUT_NOT_COMPLETE` | تخمین مدل‌شده + timeout/سقف اندازه؛ بدون cgroup |
| دفتر مجوز ۱۲ دسته‌ای | `VERIFIED` | ۲۱ آزمون؛ `UNKNOWN` هرگز «تأیید تجاری» نمی‌شود |
| اثبات یکپارچگی در برابر معنایی | `VERIFIED` | ۲۰ + ۱۱ آزمون؛ نقص واقعی grayscale کشف و رفع شد |
| E2E کانونی Image→Image و Image→Video | `VERIFIED` | ۶ آزمون با صف، کارگر ثبت‌شده و FFmpeg واقعی |
| E2E Video→Video و Audio→Video | `DEFERRED` | هیچ قابلیت متناظری در رجیستری کانونی نیست؛ mock نشد |
| جهش‌یابی سازمان‌یافته | `DEFERRED` | سه جهش دستی و اثبات‌شده اجرا شد؛ ابزار جهش مخزن برای این بسته ساخته نشد |
| اسناد | `VERIFIED` | گیت ۱۱۷ تستی سبز |

### ۱۱.۲ بازبینی خودکار بیرونی: هفت یافته، هفت رفع

روی PR #327 یک بازبین خودکار بیرونی (**Sourcery**) هفت یافتهٔ محتوایی و دو
مثبت‌کاذب ثبت کرد. یافته‌ها **معتبر** بودند و همه رفع شدند — هر کدام در لایه‌ای که
آن ناوردا را مالک است، و هر کدام با آزمونی که بدون رفع، **قرمز** می‌شود.

| # | شدت | یافته | رفع (نماد دقیق) | آزمون اثبات‌کننده |
|---|---|---|---|---|
| ۱ | 🔴 بحرانی | نوشتن از workspace فرار می‌کرد (شناسهٔ گره داخل مسیر درج می‌شد) | `contracts.py::NODE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"` روی `PlanNode.node_id`؛ `executor.py::_node_directory()` الگو را دوباره اعتبارسنجی و **مسیر حل‌شده** را داخل workspace حل‌شده الزام می‌کند | `test_a_node_id_that_is_a_path_is_refused_by_the_model`، `test_the_node_directory_is_verified_to_stay_inside_the_workspace` |
| ۲ | 🟠 بالا | کارِ ردشده **هم** اجرا می‌شد (مجوزدهی بعد از `_realize`) | `bus.py::CommandBus.check_authorization()` — همان helperهای dispatch (`_spec_locked`, `_authorize_locked`) را اجرا می‌کند و پیش از reservation/handler می‌ایستد؛ `executor.py::_authorize_node()` پیش از encode آن را صدا می‌زند | `test_a_denied_node_produces_no_media`، `test_the_bus_preflight_enforces_the_actor_grant_and_reserves_nothing`، و گیت معماری «فقط bus دروازهٔ پک را صدا می‌زند» بدون تغییر سبز ماند |
| ۳ | 🟠 بالا | درخواست، اقتدار پروژه را دور می‌زد (کارگر خودش actor می‌ساخت و `confirmed=True` می‌گذاشت) | `job.py::ActorClaim` + فیلد **الزامی** `actor` و `confirmed: bool = False` در `CompositionJobPayload`؛ `composition_render_job` دیگر هیچ‌کدام را نمی‌سازد | `test_a_row_without_an_actor_is_refused`، `test_a_level_c_node_needs_the_requesters_confirmation` |
| ۴ | 🟡 متوسط | شکل binding زیاد سهل‌گیر بود (کلید ناشناس دور ریخته می‌شد؛ هر دو منبع هم‌زمان پذیرفته می‌شد) | `job.py::_bindings_from()`: کلید ناشناس، هر دو منبع هم‌زمان، و `external` خالی رد می‌شوند؛ شکل مدل از `PortBinding.model_validate` می‌گذرد | `test_a_binding_that_names_two_sources_is_refused`، `test_a_binding_with_an_unknown_key_is_refused` |
| ۵ | 🟡 متوسط | تخمین timeout کمتر از سقف واقعی موتور بود (۱۲۰ s در برابر ۳۰۰ s) | `CompiledPlan.requirements` (نگاشت گره→`ResourceRequirements`) + `executor.py::_node_timeout()` + `engine.py::MediaEngine.realize(timeout_seconds=…)` | `test_the_declared_timeout_is_the_enforced_timeout` (مقدار واقعاً به primitive می‌رسد) |
| ۶ | 🟡 متوسط | آزمون drift رجیستری **توخالی** بود (فقط تکرارپذیری را می‌سنجید) | آزمون اکنون سطح را روی یک **deep copy** تغییر می‌دهد و می‌سنجد اثر انگشت **تغییر می‌کند**، و رجیستری زنده دست‌نخورده می‌ماند | `test_registry_fingerprint_changes_when_the_surface_changes` |
| ۷ | 🟡 متوسط | آزمون E2E ورودی دست‌کاری‌شده یک کار **گیرکرده** را هم می‌پذیرفت | اکنون **شکست ترمینال** با کد `invalid_request` و پیام `digest mismatch` را الزام می‌کند | `test_a_changed_staged_input_is_refused_before_any_media_work` |

**دو مثبت‌کاذب** که «رفع» نشدند و توضیح آن‌ها ثبت می‌شود: `creative/slideshow/ffmpeg.py`
خطوط ۵۳۹–۵۴۰ که opengrep به‌عنوان «فراخوانی subprocess» علامت زد، در واقع **کلاس‌های
استثنا** (`TimeoutExpired`, `CompletedProcess`) هستند و هیچ فرایندی اجرا نمی‌کنند.

#### ۱۱.۲.۱ موج دوم بازبینی: ۳۰ یافته در یک بسته، ۲۳ یافتهٔ تازه

بازبین همان PR را دوباره اسکن کرد و ۳۰ یافتهٔ محتوایی ثبت کرد؛ هفتِ آن‌ها **همان
هفت یافتهٔ قبلی** بودند (جدول بالا) و ۲۳ یافته تازه بود. دو مورد هم مثل قبل
مثبت‌کاذبِ opengrep روی نام کلاس‌های استثنا هستند. هر ۲۳ یافته بررسی شد؛ **۲۲ مورد
رفع شد** و یک مورد (`Returned assets are not durable`) فقط **جزئی** رفع شده و در
پایین صادقانه علامت‌گذاری شده است.

| # | شدت | یافته | رفع (نماد دقیق) | آزمون اثبات‌کننده |
|---|---|---|---|---|
| ۱ | 🟡 متوسط | دو job هم‌زمان خروجی هم را بازنویسی می‌کردند | نام انتشار هر گره `_run_id` را حمل می‌کند: `executor.py::_realize` → `node_dir / f"{node_id}-{self._run_id}{suffix}"` | `test_a_second_run_on_the_same_bus_is_a_new_execution` (دو اجرا دو مسیر متفاوت) |
| ۲ | 🟠 بالا | سقف ورودی دیر اعمال می‌شد (بعد از خواندن فایل) | `executor.py::_resolve_binding` قبل از `probe_image` صریحاً `enforce_input_limit(path, MAX_INPUT_BYTES)` را صدا می‌زند؛ `job.py::guarded_input` هم **قبل از هش** اندازه را می‌سنجد | `test_a_staged_input_over_the_limit_is_refused_before_it_is_hashed` |
| ۳ | 🟠 بالا | encoder تایم‌اوت‌شده زنده می‌ماند | `ffmpeg.py::_kill_process_group` بازنویسی شد (SIGKILL به گروه، حتی وقتی leader خارج شده) | `test_a_timed_out_encoder_group_is_killed_even_when_its_leader_exits` |
| ۴ | 🟠 بالا | کار تجاری تأییدنشده اجرا می‌شد | `job.py::composition_render_job` دو دروازهٔ `commercial_use`: پیش از اجرا (اگر ورودی خارجی اعلام شده باشد، چون شرایطش نامعلوم است) و پس از اجرا (`license_ledger.effective_allow_commercial is not True` → `policy_refused`) | `test_a_commercial_row_over_unmeasured_input_is_refused_before_any_media` |
| ۵ | 🟠 بالا | دفتر ناقص «تأیید تجاری» می‌داد | `contracts.py::LicenseLedger.effective_allow_commercial` وقتی **هر** دسته نامعلوم باشد `False` است | `test_one_verified_record_does_not_approve_the_ledger`، `test_a_complete_verified_permissive_ledger_is_commercially_approved` |
| ۶ | 🟠 بالا | خروجی نامعتبر بعد از اجرا رد می‌شد | `compiler.py::_check_expected_outputs(plan, output_contracts)` در **زمان کامپایل** گره و **پورت خروجی** را الزام می‌کند (`unknown_binding`) | `test_an_expected_output_must_name_a_real_port` |
| ۷ | 🟠 بالا | رسانهٔ نامعتبر تأیید می‌شد | `verifier.py::_semantic_pass` بدون `media_contract` حکم نمی‌دهد (`contract_missing`)؛ `proof.py::verify_video_semantics` اکنون duration ± بازه، codec و pixel format را هم می‌سنجد | `test_a_media_claim_without_a_contract_is_not_verified`، `test_a_claim_that_breaks_the_promised_codec_is_not_verified` |
| ۸ | 🟠 بالا | کار رسانه‌ای صف را قفل می‌کرد | `job.py`: `await asyncio.to_thread(executor.execute, …, expected_digests=…)` | کل E2E روی `InProcessJobQueue` (heartbeat و وضعیت در طول اجرا خوانده می‌شود) |
| ۹ | 🟠 بالا | دارایی‌های برگشتی پایدار نبودند | **جزئی**: `CompositionExecutor(project_id=…)` + `authorizer` → سطر می‌تواند زیر **هویت پروژهٔ پایدار** خودش اجرا شود و عضویت با `SQLApplicationDirectory` سنجیده شود؛ اما وضعیت پروژه هنوز در حافظه است (مخزن، repository پایدار پروژه ندارد) → **باقی‌مانده، علامت‌گذاری‌شده** | `test_a_row_naming_a_durable_project_is_authorized_by_the_directory` |
| ۱۰ | 🟠 بالا | خروجی‌های درخواستی ناپدید می‌شدند | `job.py::_artifact_claims` برای **هر** خروجی مورد انتظار یک claim با شواهد خودش می‌سازد؛ نتیجه هم `outputs` را برمی‌گرداند | `test_every_expected_output_is_claimed_not_only_the_last` |
| ۱۱ | 🟠 بالا | طرح‌ها منابع بی‌کران مصرف می‌کردند | `compiler.py`: `MAX_NODES_PER_PLAN=16`، `MAX_TOTAL_DISK_MB=8192`، `MAX_PEAK_RAM_MB=4096` در `_check_quota` | `test_a_plan_over_the_node_quota_is_refused`، `test_a_plan_over_the_disk_quota_is_refused` |
| ۱۲ | 🟠 بالا | نسخهٔ پشتیبانی‌نشدهٔ طرح اجرا می‌شد | `compiler.py` نسخهٔ `plan_version` را می‌سنجد و رد می‌کند | `test_a_plan_authored_for_another_ir_version_is_refused` |
| ۱۳ | 🟠 بالا | طرح pin‌نشده drift رجیستری را نمی‌دید | `job.py::plan_from_dict` فیلد `registry_fingerprint` را **الزامی** می‌کند؛ `contracts.py::CompositionPlan.pinned()` | `test_a_submitted_plan_must_pin_the_registry` |
| ۱۴ | 🟡 متوسط | بازهٔ نامعلوم مدت‌زمان پذیرفته می‌شد | `studio/models.py::MediaContract.accepts` وقتی کران تولیدکننده `None` است و کران لازم `set` است، **fail-closed** | `test_a_contract_refuses_a_producer_whose_duration_is_unknown` |
| ۱۵ | 🟠 بالا | encoder درخواستی نادیده گرفته می‌شد | `packs/media/operations.py`: `encoder: Literal["libx264"]` (جایگزینی خاموش ممکن نیست) | `test_the_video_input_refuses_another_encoder` |
| ۱۶ | 🟡 متوسط | نوع ورودی خارجی اشتباه دسته‌بندی می‌شد | `contracts.py::ExternalInput`: `contract` از `media_kind` مشتق می‌شود (validator) | `test_an_external_input_contract_follows_its_declared_kind` |
| ۱۷ | 🟡 متوسط | درخواست resize بعد از شروع فرایند شکست می‌خورد | `operations.py::_DerivedImageInput`: `ge=2, le=8192` + `model_validator` برای بودجهٔ ۳۳٬۱۷۷٬۶۰۰ پیکسل (۸K) | `test_the_resize_input_refuses_a_geometry_beyond_the_still_budget` |
| ۱۸ | 🟡 متوسط | ورودی‌های رسانه‌ای اضافی نادیده گرفته می‌شدند | `compiler.py::_check_bindings` چند-ورودی‌بودن (fan-in) را رد می‌کند | کد مسیر `_check_bindings`؛ **آزمون ندارد** — رجیستری زندهٔ امروز عملیاتی با دو ورودی رسانه‌ای ندارد، پس این شاخه جز با spec ساختگی قابل اجرا نیست (صادقانه ثبت می‌شود) |
| ۱۹ | 🟡 متوسط | دستورهای تکراری با هم تعارض داشتند | `executor.py::_run_id` (nonce) در `idempotency_key` هر گره | `test_a_second_run_on_the_same_bus_is_a_new_execution` |
| ۲۰ | 🟡 متوسط | lineage ترتیب اشتباه گزارش می‌کرد | `executor.py::_collect_artifacts` از `compiled.order` پیروی می‌کند | `test_the_lineage_names_every_node_and_its_capability_version` + assert ترتیب در E2E |
| ۲۱ | 🟡 متوسط | فیلدهای بدشکلِ طرح بی‌صدا دور ریخته می‌شدند | `job.py`: شکل سیم **بسته** (`PLAN_KEYS`/`NODE_KEYS`/`EXTERNAL_INPUT_KEYS`/`EXPECTED_OUTPUT_KEYS` + `_closed()`) | `test_a_wire_plan_with_an_unknown_key_is_refused` |
| ۲۲ | 🟡 متوسط | آزمون grayscale رنگ‌دار را می‌پذیرفت | آزمون اکنون **پیکسل‌به‌پیکسل** برابری کانال‌ها را می‌سنجد (`channel.tobytes()`)، نه extrema را | `test_grayscale_produces_a_luma_only_png` (و همان ادعا در E2E) |
| ۲۳ | 🟡 متوسط | گزارش Gate 0 در فهرست اسناد نبود | یک ردیف در `docs/README.md` به `../LIVE_TRUTH_REPORT.md` | `tests/unit/test_docs_integrity.py` (۱۱۷ سبز) |

> **باقی‌ماندهٔ صادقانه (۱ مورد از ۲۳):** «دارایی‌های برگشتی پایدار نیستند».
> سطر می‌تواند زیر هویت پروژهٔ پایدار اجرا شود و **عضویت** واقعاً سنجیده می‌شود،
> اما وضعیت پروژه (دارایی‌ها، receiptها) هنوز در حافظه نگه داشته می‌شود، چون مخزن
> repository پایدارِ پروژه ندارد. رفع کامل نیازمند یک لایهٔ persistence است که در
> دامنهٔ این شاخه نیست. این مورد **رفع‌نشده** اعلام می‌شود، نه «رفع‌شده».

> نکتهٔ مهم دربارهٔ یافتهٔ ۲: نخستین رفع من دروازهٔ پک را داخل خودِ executor صدا زد.
> گیت معماری `test_only_the_bus_invokes_the_pack_lifecycle_gate` (که با اسکن AST
> می‌گوید دروازه فقط از `bus.py` صدا زده شود) **قرمز شد** — و درست بود. پاسخ صحیح
> «تکرار دروازه» نبود، بلکه «دادن یک ورودی رسمی به همان bus» بود:
> `CommandBus.check_authorization`. این دقیقاً همان الگویی است که اجازه نمی‌دهد یک
> گیت امنیتی به دو گیت موازی تبدیل شود.

### ۱۱.۳ دربارهٔ «بازبینی مستقل»

در این بسته **هیچ بازبینی انسانی مستقلی انجام نشده است**. آنچه انجام شده:

1. **گیت‌های معماری کانونی مخزن** (که مالکشان نویسندهٔ این شاخه نیست) این شاخه را
   *به‌عنوان کدِ غریبه* بررسی کرده‌اند و در طول کار چند ایراد واقعی گرفته‌اند؛
2. **یک بازبین خودکار بیرونی (Sourcery)** روی PR #327 در دو موج **۳۰ یافتهٔ محتوایی
   متمایز** ثبت کرد (۷ در موج اول و ۳۰ در موج دوم که ۷ موردشان تکرار موج اول بود):
   **۲۹ مورد رفع شد** و **۱ مورد** (`Returned assets are not durable`) جزئی باقی
   مانده است.

هیچ‌کدام از این دو **بازبینی انسانی مستقل** نیست: اولی مالِ مخزن است و دومی یک ابزار
خودکار. این سند مدعی «بازبینی مستقل» نیست. `DEFERRED` — نیازمند بازبینندهٔ انسانی که
نویسنده نباشد.

---

## ۱۲. شواهد تحویل (Git)

### ۱۲.۱ هویت شاخه و کامیت‌ها

| قلم | مقدار |
|---|---|
| شاخه | `arena/da6837a6-nexus-ai-agent` |
| SHA پایه (`origin/main`) | `5ed6d4e10be55f5baf5e9f2a8aa99775577621db` |
| SHA سر پیش از commit این گزارش | `2298cd204c57f5ad6acf5163994ba10bcf2165ba` |
| URL پریک pull request | `https://github.com/bot523h/nexus-ai-agent/pull/327` |
| تعداد کامیت‌ها نسبت به `main` | ۱۱ |
| تعداد فایل تغییرکرده | ۶۳ |
| حجم تغییر | `+12219 / −47` خط |

### ۱۲.۲ تاریخچهٔ کامیت‌ها

| SHA | پیام |
|---|---|
| `81b442e` | `chore(board): claim task-284 nagar-creative-composition zone + Gate-0 live truth report` |
| `a9d08f2` | `feat(composition): integrate the NAGAR composition runtime into the canonical path` |
| `17e28fa` | `test(cognition): regenerate the committed quality baseline for the new pack` |
| `28a4914` | `docs(audit): record the uncontaminated baseline and the accepted coverage run` |
| `48b917c` | `docs(report): fill the final verification run and the git delivery evidence` |
| `4481e1b` | `evidence: ship the real media output alongside the machine-readable evidence` |
| `681bc90` | `chore(board): extend the claim scope to every file this branch touches` |
| `7ec8df0` | `fix(composition): close the external review findings with canonical guards` |
| `420c76f` | `fix(bench): name the requester and confirm the level-C node in the queue mode` |
| `96f0a46` | `evidence: regenerate the benchmark and the proof package on the new code path` |
| `2298cd2` | `evidence: regenerate the proof package on a clean tree` |

### ۱۲.۳ فایل‌های تغییرکرده (گروه‌بندی‌شده)

**افزوده‌شده (۴۰ فایل):**

* `LIVE_TRUTH_REPORT.md` — گزارش حقیقت زنده (Gate 0)
* `docs/architecture/CANONICAL_INFRASTRUCTURE_AUDIT.md` — ممیزی ۸ محور (Gate 1)
* `docs/architecture/adr/0014-creative-capability-composition.md` — ADR (Gate 2)
* `docs/architecture/COMPOSITION_RUNTIME.md` — قرارداد قابلیت، IR، دفتر مجوز (Gate 3/5)
* `docs/architecture/COMPOSITION_THREAT_MODEL.md` — مدل تهدید STRIDE (Gate 4)
* `docs/architecture/NAGAR_COMPOSITION_RUNTIME_REPORT.md` — این گزارش (Gate 10)
* `docs/ops/COMPOSITION_RUNTIME.md` — راهنمای یکپارچه‌سازی کانونی
* `src/nexus_ai_agent/creative/composition/` — ۹ ماژول بستهٔ ترکیب
* `src/nexus_ai_agent/creative/packs/media/` — پک جدید (۳ فایل)
* `src/nexus_ai_agent/continuum/composition_bench.py` — هستهٔ اندازه‌گیری بنچمارک
* `scripts/bench_composition.py`، `scripts/collect_composition_evidence.py`
* `evidence/composition_evidence.json`، `evidence/composition_benchmark.json`،
  `evidence/composition_benchmark_queue.json`
* `tests/unit/test_composition_{compiler,contracts,license,proof,runtime,security,verifier}.py`
* `tests/unit/test_media_{transform_pack,still_primitives}.py`
* `tests/architecture/test_composition_boundary.py`
* `tests/integration/test_composition_canonical_e2e.py`

**تغییرکرده (۲۰ فایل):**

* `src/nexus_ai_agent/creative/studio/models.py` — `MediaContract` و `MediaPort`
* `src/nexus_ai_agent/creative/studio/capabilities.py` — پورت‌های رسانه و `media_derived_fields`
* `src/nexus_ai_agent/creative/studio/lifecycle.py` — `nexus.media.transform` → `AVAILABLE`
* `src/nexus_ai_agent/creative/packs/runtime.py` — `PackComposition("media", …)`
* `src/nexus_ai_agent/creative/slideshow/ffmpeg.py` — allow-list متغیر محیطی + گروه فرایند
* `src/nexus_ai_agent/worker.py` — ثبت کارگر `creative_composition`
* `src/nexus_ai_agent/adapters/in_process_job_queue.py` — ثبت verifier
* `src/nexus_ai_agent/nagar/sources.py` — افزودن پک media به سطح اعلان
* `src/nexus_ai_agent/continuum/pack_coverage.py` — هدف‌های تست پک media
* `.agents/board.json` — zone و claim
* `docs/README.md`، `docs/architecture/adr/README.md` — ردیف‌های نمایه
* `OPERATION_TRUTH.json`، `OPERATION_CONTRACT_MATRIX.md`، `L0_L4_MATURITY.md`،
  `RECONCILIATION.md` — پروژکشن حقیقت **بازتولیدشده**
* `tests/architecture/test_pack_activation_completeness.py`،
  `tests/architecture/test_verification_registry_ratchet.py`،
  `tests/architecture/test_slideshow_adapter_boundary.py` (تقویت‌شده) —
  `tests/unit/test_capability_lifecycle.py`، `test_pack_runtime_composition.py`،
  `test_operation_matrix_reconciliation.py`، `test_cognition_quality_eval.py` —
  `tests/fixtures/cognition_quality_eval/v2_baseline.json` (بازتولیدشده)

### ۱۲.۴ شواهد ادعای board

| قلم | مقدار |
|---|---|
| شناسهٔ claim | `task-284-nagar-creative-composition` |
| zone | `nagar-creative-composition` |
| وضعیت | `active` |
| نسل | ۱ |
| زمان claim | `2026-11-09T10:00:22Z` |
| مسیرهای انحصاری | ۵۰ |
| فرمان بررسی هم‌پوشانی | `python scripts/agent_board.py check --branch arena/da6837a6-nexus-ai-agent --files <همهٔ مسیرهای تغییرکرده> --repo bot523h/nexus-ai-agent` |
| خروجی بررسی هم‌پوشانی | `no overlap — safe to proceed (every consulted source was readable)` · **exit 0** · ۴۸ منبع خوانده‌شده |

### ۱۲.۵ روش بازتولید

```bash
# 1) محیط (طبق AGENTS.md)
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

# 2) گیت‌های ایستا
make lint && make types

# 3) کل مجموعهٔ تست
pytest -q -m "not slow"

# 4) گیت اسناد و گیت حقیقت
pytest -q tests/unit/test_docs_integrity.py
python -m nexus_ai_agent.nagar --check

# 5) پوشش پک (روی درخت پاک)
python scripts/pack_coverage.py

# 6) بنچمارک و شواهد
python scripts/bench_composition.py --runs 10 --json evidence/composition_benchmark.json
python scripts/bench_composition.py --runs 10 --queue --json evidence/composition_benchmark_queue.json
python scripts/collect_composition_evidence.py --output evidence/composition_evidence.json
```

> خروجی رسانهٔ واقعی در دو جا ثبت شده است: (الف) به‌صورت **JSON** در
> `evidence/composition_evidence.json` (هش، اندازه، اندازه‌گیری‌های معنایی، رسیدها و
> داده‌های دودمانه) و (ب) به‌صورت **خودِ فایل دودویی** در
> `evidence/media/composition_image_to_video.mp4` (۲۴۷۳ بایت، ۶۴۰×۴۸۰، `h264`,
> `yuv420p`, ۲۵ fps, ۱٫۰۰ ثانیه) همراه با ورودی
> `evidence/media/composition_source.png` (۷۸۲ بایت). فایل دودویی کوچک است
> (زیر ۳ کیلوبایت) و همین باعث شده بتوان artifact واقعی را در مخزن نگه داشت؛ اثباتِ
> آن هم از راه **هش + اندازه‌گیری مستقل** است و هم از راه **وجود واقعی فایل**.
> `scripts/collect_composition_evidence.py --media-out evidence/media` در هر اجرا همان
> زنجیره را دوباره می‌سازد و هش/ابعاد/تعداد فریم را از نو می‌سنجد.

---

## ۱۳. نقشهٔ راه NOW / NEXT / LATER

### NOW — انجام‌شده در این شاخه

1. بستهٔ `creative/composition/` با ۱۰ ماژول: `contracts`, `compiler`, `engine`,
   `proof`, `executor`, `job`, `verifier`, `license`, `bench`.
2. پک `nexus.media.transform` با ۳ عملیات، ثبت‌شده در `packs/runtime.py::COMPOSITION`،
   `studio/lifecycle.py::PACK_LIFECYCLE` و `pack.manifest.json`.
3. کارگر `creative_composition` ثبت‌شده در `worker.py::default_job_handlers()` و
   تأییدکنندهٔ آن در `adapters/in_process_job_queue.py::default_artifact_verifiers()`.
4. دفتر مجوز ۱۲ دسته‌ای با سنجش زندهٔ FFmpeg.
5. تفکیک اثبات یکپارچگی/معنایی با اصلاح واقعیِ بررسی grayscale.
6. مدل تهدید STRIDE با ۱۶ تهدید و برچسب `BLOCKED` برای کنترل‌های سیستم‌عاملی.
7. ۱۹۴ آزمون جدید در ۱۱ فایل.
8. بنچمارک ۱۰ اجرا در دو حالت + گزارش شواهد JSON.
9. بازتولید پروژکشن حقیقت عملیات (`python -m nexus_ai_agent.nagar --docs`) پس از افزوده‌شدن پک.

### NEXT — کار بعدی با مالکیت مشخص

| # | کار | چرا | مالک پیشنهادی |
|---|---|---|---|
| N1 | مستندسازی جهش اختصاصی (`scripts/composition_mutations.py`) به سبک مخزن | جهش‌ها دستی و مقطعی بودند؛ مخزن ابزار جهش سازمان‌یافته دارد | همین بسته |
| N2 | اجرای موازی گره‌های مستقل | `ResourceEstimate` اوج را مدل می‌کند ولی اجرا ترتیبی است | همین بسته |
| N3 | پیاده‌سازی `scene.auto_reframe_subject` برای نامزد A | نامزد A بدون آن `DEFERRED` است | تیم صحنه |
| N4 | `ffprobe` در تصویر پایه | تصویر `imageio-ffmpeg` فقط `ffmpeg` دارد؛ رمزگشایی فعلی از `ffmpeg -i` تجزیه می‌شود | زیرساخت |
| N5 | quota واقعی مبتنی بر cgroup | تا وقتی cgroup ممکن نشده، سهمیه فقط تخمین مدل است | زیرساخت / سیستم‌عامل |
| N6 | پوشش `pack_coverage` برای پک media در گیت CI | پوشش خطی پک media ۱۰۰٪ اندازه‌گیری شد ولی گیت «درخت پاک» لازم دارد | CI |

### LATER — موکول‌شده با دلیل

| # | کار | دلیل تعویق |
|---|---|---|
| L1 | namespaces / seccomp / جداسازی کاربر | مجوز سیستم‌عامل در این محیط موجود نیست ⇒ `BLOCKED` |
| L2 | مرز چندزبانی (Rust/Go) | محدودیت اندازه‌گیری‌شدهٔ توجیه‌کننده وجود ندارد (§۳٫۴) |
| L3 | قابلیت رنگ (`color.*`) و `portrait.*`/`scene.*` | خارج از دامنهٔ این شاخه؛ در کاتالوگ محصول `MISSING` هستند |
| L4 | موتور سنتز پویا / کشف قابلیت با AI | خارج از دامنه (Scope boundaries) |
| L5 | مستندسازی کامل جهش برای کل بسته | زمان‌بندی؛ جهش‌های فعلی دستی و اثبات‌شده‌اند |

---

## ۱۴. نگاشت دوازده سند الزامی

| # | سند الزامی | محل دقیق |
|---|---|---|
| ۱ | گزارش حقیقت زنده | `LIVE_TRUTH_REPORT.md` (ریشهٔ مخزن) |
| ۲ | ممیزی زیرساخت کانونی | `docs/architecture/CANONICAL_INFRASTRUCTURE_AUDIT.md` |
| ۳ | ADR | `docs/architecture/adr/0014-creative-capability-composition.md` |
| ۴ | مدل تهدید به‌روزشده | `docs/architecture/COMPOSITION_THREAT_MODEL.md` |
| ۵ | اسناد قرارداد قابلیت | `docs/architecture/COMPOSITION_RUNTIME.md` §۱–§۳ |
| ۶ | اسناد IR ترکیب | `docs/architecture/COMPOSITION_RUNTIME.md` §۴ |
| ۷ | راهنمای یکپارچه‌سازی کانونی | `docs/ops/COMPOSITION_RUNTIME.md` |
| ۸ | دفتر مجوز | `docs/architecture/COMPOSITION_RUNTIME.md` §۵ (۱۲ دسته) + `src/nexus_ai_agent/creative/composition/license.py` |
| ۹ | گزارش تأیید | همین سند، §۷ |
| ۱۰ | گزارش بنچمارک | همین سند، §۸ (+ `evidence/composition_benchmark*.json`) |
| ۱۱ | گزارش نهایی مهندسی | همین سند (کل سند) |
| ۱۲ | نقشهٔ راه NOW/NEXT/LATER | همین سند، §۱۳ |

افزون بر این‌ها: کد مبدأ (`src/nexus_ai_agent/creative/composition/` و
`src/nexus_ai_agent/creative/packs/media/`)، آزمون‌ها (۱۱ فایل، ۱۹۴ تست)، اسکریپت
بنچمارک، JSON شواهد، و **یک خروجی رسانهٔ واقعی**
(`evidence/media/composition_image_to_video.mp4` — ۲۴۷۳ بایت، ۶۴۰×۴۸۰، h264،
yuv420p، ۲۵fps، ۱٫۰۰ ثانیه، ۲۵ فریم) به‌همراه تصویر ورودی آن
(`evidence/media/composition_source.png`).

---

## ۱۵. پیوندها

* گزارش حقیقت زنده (Gate 0): `LIVE_TRUTH_REPORT.md`
* ممیزی زیرساخت کانونی (Gate 1): [`docs/architecture/CANONICAL_INFRASTRUCTURE_AUDIT.md`](CANONICAL_INFRASTRUCTURE_AUDIT.md)
* ADR 0014 (Gate 2): [`docs/architecture/adr/0014-creative-capability-composition.md`](adr/0014-creative-capability-composition.md)
* قرارداد قابلیت و IR (Gate 3): [`docs/architecture/COMPOSITION_RUNTIME.md`](COMPOSITION_RUNTIME.md)
* مدل تهدید (Gate 4): [`docs/architecture/COMPOSITION_THREAT_MODEL.md`](COMPOSITION_THREAT_MODEL.md)
* راهنمای یکپارچه‌سازی کانونی: [`docs/ops/COMPOSITION_RUNTIME.md`](../ops/COMPOSITION_RUNTIME.md)
* شواهد ماشین‌خوان: `evidence/composition_evidence.json`،
  `evidence/composition_benchmark.json`، `evidence/composition_benchmark_queue.json`
