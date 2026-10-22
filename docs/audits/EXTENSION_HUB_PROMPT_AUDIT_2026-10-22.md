# ممیزی سند «معماری Extension Hub» و پرامپت پیاده‌سازی فاز NOW

> **نوع سند:** record dated (طبق `AGENTS.md` §7 — این سند *source of truth* معماری نیست؛
> نمای زندهٔ معماری در `docs/architecture/` است).
> **تاریخ:** 2026-10-22 · **شاخه:** `arena/f6713d82-nexus-ai-agent` · **بیس:** `04469ed`
> **دامنه:** بررسی سند بیرونی «Extension Hub برای NEXUS AI Agent — نگارش اول» و پرامپت
> پیاده‌سازی ضمیمهٔ آن، در برابر وضعیت *واقعی و اندازه‌گیری‌شدهٔ* درخت کد.
> **تغییر کد:** هیچ. این سند فقط گزارش است.

---

## ۰. خلاصهٔ اجرایی

سند ورودی یک معماری *عموماً درست* را برای یک پروژهٔ **خالی** توصیف می‌کند. مشکل این است که
`nexus-ai-agent` خالی نیست: این ریپو از قبل یک زیرسیستم پک (capability pack) با
**manifest سخت‌گیرانه، trust root امضای Ed25519، دروازهٔ فعال‌سازی، vocabulary مجوز،
بودجهٔ منابع، و ۴۰ تست معماری fail-closed** دارد. پرامپت هیچ‌کدام از این‌ها را نمی‌داند،
چون نویسنده‌اش صراحتاً اعلام کرده به کد دسترسی نداشته («جست‌وجوی عمومی GitHub آن را پیدا
نکرد»).

نتیجهٔ اجرای این پرامپت روی این ریپو، به‌احتمال زیاد یکی از این سه حالت است:

| حالت | احتمال | نتیجه |
|---|---|---|
| ایجنت قانون «اگر تناقض دارد اجرا نکن، گزارش بده» را جدی بگیرد | بالا | **صفر خط کد** + یک گزارش تناقض. خروجی مفید ولی نه پیاده‌سازی. |
| ایجنت یک `extension_hub/` موازی بسازد | متوسط | **پس‌رفت معماری**: سه schema برای manifest، دو authority برای اعتماد، نقض R7/R9، و ماژول orphan که R20 ردش می‌کند. |
| ایجنت درست عمل کند | پایین | چون پرامپت هیچ اشاره‌ای به `creative/packs/`، `AGENTS.md` یا `MODULE_MAP.md §3` ندارد، مسیر درست را باید *خلاف* پرامپت پیدا کند. |

**جمع‌بندی عددی:** از ۹ معیار فعال‌سازی که سند پیشنهاد می‌دهد، **۵ مورد در ریپو پیاده‌سازی
شده** (در `packs/verify.py`)، **۱ مورد جزئی**، و **۳ مورد واقعاً غایب** — یعنی نوآوری
واقعی سند تنها در ~۳۰٪ از ادعای آن است. از ۶ لایهٔ معماری پیشنهادی، **۴ لایه معادل
پیاده‌شده دارد** و ۲ لایه غایب است.

---

## ۱. واقعیت ریپو — اندازه‌گیری‌شده، نه ادعایی

همهٔ اعداد زیر با دستور روی درخت `04469ed` تولید شده‌اند و قابل بازتولید هستند.

```
src/    69,250 خط پایتون   ·   31 زیرپکیج
tests/  71,919 خط          ·   281 فایل تست   ·   40 تست معماری (AST/JSON، بدون I/O)
نسخهٔ بسته: 3.13.0
```

### ۱.۱ آنچه سند «نبود» فرض کرده و **هست**

| ادعای سند | واقعیت در درخت | شاهد |
|---|---|---|
| «باید Manifest Registry بسازیم» | هست: `CapabilityPackManifest` با `extra="forbid"` و `frozen=True`، schema `nexus.capability-pack.v1` | `creative/packs/manifest.py` |
| «باید License Gate بسازیم» | **نصفش** هست: هر artifact فیلد `license` دارد، ولی هیچ اعتبارسنجی SPDX روی آن نیست | `manifest.py::ArtifactSpec` |
| «باید Test Gate بسازیم» | هست، به شکل قوی‌تر: `verify_manifest()` گزارش کامل findings می‌دهد (error/warning) + دروازهٔ فعال‌سازی | `creative/packs/verify.py` |
| «باید Sandboxed Runtime بسازیم» | فیلد `sandbox: process_isolated \| in_process \| none` در manifest هست؛ **اجرای واقعی process isolation نیست** — پک‌ها کد درون‌فرآیندی خالص‌اند | ۶ manifest + قانون R7 |
| «باید Review/Approval Queue بسازیم» | هست ولی برای *خودتغییری ایجنت*: `PendingApproval` + `/approve` `/reject` روی تلگرام | `agent/approval.py`, `storage/models.py` |
| «باید Output Adapter به Asset Library بسازیم» | Asset Library کامل هست (blob store محتوامحور + `AuthorizationSeam` + ingest با `idempotency_key`) ولی **هیچ adapter از پک به آن وجود ندارد** | `creative/assets/{models,ports,service,ingestion,authz,blob_store}.py` |
| «باید Creative Graph node/edge schema تعریف کنیم» | هست: `CreativeGraph` append-only با `GraphNode(kind, parents)` — ولی **فقط در حافظه**، بدون مسیر نوشتن پایدار | `creative/spine/models.py:198` |
| «باید Audit & Observability Log بسازیم» | هست، قوی‌تر از پیشنهاد سند: ژورنال زنجیره‌هش (hash-chained) + **Artifact Passport** که artefact را *دوباره اندازه می‌گیرد* به‌جای اعتماد به ادعا | `provenance/{journal,passport,observer,models}.py` |
| «باید Resource Broker بسازیم» | **واقعاً غایب**. `hardware_requirements` در manifest اعلام می‌شود ولی هیچ‌جا اندازه‌گیری نمی‌شود. صفر اشاره به VRAM/GPU detection در `src/` | `grep vram\|nvidia\|detect_gpu src/` → خالی |
| «از MCP استفاده کن» | **صفر اشاره به MCP در کل ریپو** (`.py`, `.md`, `.toml`). ریپو MCP-native نیست. | `grep -ril "\bmcp\b"` → خالی |
| «SBOM و اسکن آسیب‌پذیری» | نزدیک‌ترین معادل: `deptry` (declared-vs-imported) + `scripts/extras_matrix.py` + `.github/DEPENDENCY_MATRIX.md`. اسکن CVE نداریم. | `pyproject.toml [tool.deptry]` |

### ۱.۲ وضعیت ۶ پک shipped (اندازه‌گیری مستقیم از manifest ها)

```
package_id                   caps  artifacts  signature     unknown permissions
nexus.audio.studio             10      0      PLACEHOLDER   write_automation_curves
nexus.language.caption         10      3      real          —
nexus.color.delivery            7      0      PLACEHOLDER   export_timeline_interchange,
                                                            write_color_layers,
                                                            write_derived_delivery_assets
nexus.edit.timeline             9      0      PLACEHOLDER   mutate_timeline_structure
nexus.motion.graphics          10      0      PLACEHOLDER   write_motion_layers
nexus.slideshow.compose         6      0      real          —
                              ───      ─
                              52       3
```

سه artifact پک caption:

```
whisper-small-int8   onnx_model  license="model-license-reference"  sha256=replace-with-real-digest…
persian-aligner      onnx_model  license="model-license-reference"  sha256=replace-with-real-digest…
vazirmatn-regular    font        license="OFL-1.1"                  sha256=replace-with-real-digest…
```

### ۱.۳ سه یافتهٔ واقعی که این ممیزی از کد بیرون کشید

این‌ها ارزشمندترین خروجی این سند هستند، چون هیچ‌کدام در سند ورودی نیست و هر سه
**شکاف بین «ادعا» و «اجرا»** را نشان می‌دهند — دقیقاً همان چیزی که فرهنگ این ریپو
(`docs/architecture/TRUTH_DOCTOR.md`, law R16) علیه آن طراحی شده.

**یافتهٔ A — trust root عمداً خالی است.**

```json
// src/nexus_ai_agent/creative/packs/trust_root.json
{ "schema": "nexus.pack-trust-root.v1", "version": 1, "threshold": 1, "publishers": {} }
```

یعنی: **امروز هیچ پک خارجی‌ای نمی‌تواند فعال شود.** `PackRegistry.activate()` هر
`anchor="external"` را با `signature_state ∉ TRUSTED_SIGNATURE_STATES` رد می‌کند
(`registry.py`)، و با `publishers: {}` هر امضایی به `unknown_publisher` می‌رسد.
این *عمدی* است (کامنت داخل فایل توضیح داده) و fail-closed درست است — ولی یعنی
«Extension Hub» در این ریپو یک زیرساخت **ساخته‌شده و دست‌نخورده** است، نه یک چیز
نبود. پرامپت ورودی می‌خواهد از صفر چیزی بسازد که ۹۰٪‌اش هست و فقط onboarding ندارد.

**یافتهٔ B — ۵ از ۶ پک، مجوزهایی خارج از vocabulary اعلام می‌کنند.**

`KNOWN_PERMISSIONS` در `manifest.py` دقیقاً ۸ توکن است:

```
read_project_audio, read_project_video_frames, read_project_images,
write_transcript_assets, write_derived_caption_assets, write_derived_video_assets,
write_timeline_layers, egress_media_optin
```

ولی ۵ پک مجموعاً **۶ توکن خارج از این لیست** اعلام می‌کنند (جدول §1.2).
`verify.py` این را فقط `unknown_permission` با `severity="warning"` ثبت می‌کند —
«never granted» ولی **activation را بلاک نمی‌کند**. یعنی manifest ها چیزی را طلب
می‌کنند که runtime آن را نمی‌شناسد، و این در وضعیت هشدار می‌ماند نه خطا.
این یک drift واقعی بین manifest و vocabulary است.

**یافتهٔ C — `model-license-reference` یک مجوز نیست؛ یک placeholder است.**

دو مدل ONNX پک caption (whisper-small-int8, persian-aligner) فیلد `license` را با
رشتهٔ `"model-license-reference"` پر کرده‌اند. هیچ اعتبارسنجی آن را رد نمی‌کند، چون
`ArtifactSpec.license` صرفاً `AnnotatedStr = Field(min_length=1)` است — بدون SPDX،
بدون allowlist، بدون فیلد `commercial_use`. دقیقاً همان ریسکی که سند ورودی به‌درستی
رویش انگشت گذاشته («مجوز مدل جدا از مجوز کد است»)، در همین ریپو **به‌شکل فعال
وجود دارد** و هیچ گیتی جلویش را نمی‌گیرد.

> **این سه یافته، هستهٔ کار واقعی فاز NOW را تعریف می‌کنند — نه ساختن یک Hub موازی.**

---

## ۲. پرامپت چقدر بهتر است؟ ارزیابی تفکیکی

سند ورودی را باید در دو سطح جدا قضاوت کرد، چون کیفیتشان متفاوت است.

### سطح اول: سند معماری (§1 تا §8) — نمره ۷/۱۰

**نقاط قوت واقعی:**

1. **تشخیص درست مسئله.** «به‌جای monolith plugin import، یک hub مجزا» — این واقعاً
   الگوی درست است و ریپو هم *همین* را انتخاب کرده (پک‌ها داده‌اند نه کد، در فرآیند
   جدا، با دروازهٔ فعال‌سازی). یعنی جهت‌گیری سند با معماری موجود **هم‌راستاست**.
2. **تفکیک مجوز کد از مجوز مدل/دارایی** (§2 معیار ۲، §7 bullet اول). این بهترین
   نکتهٔ کل سند است و — همان‌طور که یافتهٔ C نشان داد — **دقیقاً همان شکافی است که
   امروز در ریپو باز است**. این تنها بخشی از سند است که اگر مستقیم اجرا شود، ارزش
   افزودهٔ خالص دارد.
3. **fail-closed بودن دروازه‌ها.** «هیچ کدی اجرا نمی‌شود تا manifest تأیید شود»،
   «نتیجهٔ تست واقعی نه ادعای README»، «عدم اعتماد به star/issue». این ادبیات
   *دقیقاً* ادبیات خود ریپو است (`trust.py`: «a manifest can never supply the thing
   that decides whether the manifest is trusted»). هم‌صدایی فرهنگی بالاست.
4. **دسته‌بندی NOW/NEXT/LATER/NOT_WORTH** برای خود hub، و اینکه NOT_WORTH را صریح
   تعریف کرده. انضباط دامنه دارد.
5. **درخواست صریح گزارش صادقانه** («هیچ ادعای انجام‌شد بدون نتیجهٔ تست واقعی قابل
   نمایش نکن»). این با قانون evidence در `AGENTS.md` §2 هم‌خوان است.

**نقاط ضعف:**

1. **بی‌خبری از کد — که خودش اعتراف کرده.** این یک ضعف *جزئی* نیست، یک ضعف
   *ساختاری* است: بدون شناخت `creative/packs/`، سند یک معماری موازی طراحی کرده.
2. **طرحوارهٔ manifest ضعیف‌تر از موجود.** مقایسه کنید:

   | فیلد سند پیشنهادی | معادل در `nexus.capability-pack.v1` | برنده |
   |---|---|---|
   | `id: string` | `package_id` با regex `^nexus(\.[a-z][a-z0-9_]*)+$` | **ریپو** (namespace اجباری) |
   | `version: semver` | `version` + `parse_semver()` سخت‌گیر (بدون range/prefix) | **ریپو** |
   | `capabilities: [...]` | `capabilities` با regex `domain.operation` + **قید اینکه domain باید در package_id باشد** + رد تکراری | **ریپو** (جلوگیری از غصب namespace) |
   | `code_license`, `model_asset_license` | `artifacts[].license` (per-artifact) | **سند** در تفکیک کد/مدل؛ **ریپو** در دانه‌بندی per-artifact → ادغام هر دو بهتر است |
   | `commercial_use: allowed\|restricted\|forbidden` | — | **سند** (کاملاً غایب) |
   | `install_footprint_mb`, `gpu_required: bool` | `download_size_mb`, `hardware_requirements{min/recommended_ram_mb, cpu_threads, gpu{optional,webgpu,features}, disk_free_mb}` + `resource_budget{max_resident_model_mb, max_audio_minutes_in_memory, max_preview_resolution}` | **ریپو** (یک order of magnitude غنی‌تر) |
   | `runtime: local-only\|api-required\|hybrid` | `runtime.native{adapter_id, sandbox, network_required}` + `runtime.browser{preferred, fallback, worker_required}` + `network_policy{upload_media, runtime_network, pack_update}` | **ریپو** |
   | `nexus_compat: semver range` | `compatibility.min_nagar_version` + `protocol_version` + `state_schema` | **ریپو** (range را عمداً رد می‌کند؛ `parse_semver` range قبول نمی‌کند — یک تصمیم مستند) |
   | `test_status: untested\|passed\|failed` | `VerificationReport` با `errors()`/`warnings()`/`trusted()`/`ok()`/`summary()` + `pending_capabilities` + `signature_state` | **ریپو** (بیش از سه‌حالته؛ findings ساخت‌یافته) |
   | `risk_tier`, `priority` | `RiskLevel` در `tools/base.py`؛ اولویت در `.agents/board.json` | موازی — باید نگاشت شود، نه اختراع دوباره |
   | `output_contract: "asset-library-v1"` | `AssetLibrary` فقط ۶ media type در allowlist بسته (`SUPPORTED_MEDIA_TYPES` در `assets/models.py`) | **ریپو** — و سند این را ندیده: خروجی text/3d/LUT امروز **رد می‌شود** |
   | — | `security{signature_algorithm, signature, trusted_publisher, allow_arbitrary_native_code: Literal[False], allow_arbitrary_wasm_imports: Literal[False]}` | **ریپو** (سند هیچ معادل امضا/ناشر ندارد) |

   **نتیجه:** ۳ فیلد سند ارزش افزودن دارند (`commercial_use`، تفکیک صریح
   code/model license، `last_verified`)؛ بقیه یا معادل ضعیف‌ترند یا تکراری.
3. **پیشنهاد MCP بدون بررسی.** سند می‌گوید «از MCP استفاده کن چون استاندارد باز است
   و نیازی به نوشتن IPC اختصاصی نیست». ولی ریپو **MCP-native نیست** (صفر اشاره) و
   افزودن `mcp` به `dependencies` با `deptry` (که در `make lint` و CI است) و با
   `scripts/extras_matrix.py` (که هر extra را به یک leg CI با smoke test اجباری
   وصل می‌کند) تصادم می‌کند. یعنی MCP یک *وابستگی جدید در هسته* است — دقیقاً همان
   چیزی که §2 معیار ۵ سند («هرگز در dependency tree هسته merge نشود») منع می‌کند.
   **سند با خودش در تضاد است.**
4. **«هر افزونه = فرآیند/کانتینر جدا» با قانون بنیادین ریپو در تضاد است.**
   `manifest.py` docstring: *«packs are data, never code»* و
   `allow_arbitrary_native_code: Literal[False]` — یعنی manifest ای که خلافش را
   ادعا کند **اعتبارسنجی نمی‌شود**. تست `test_pack_manifest_is_data_only.py` همین
   را روی بایت خام JSON هم اعمال می‌کند و کلیدهای `entrypoint`, `post_install`,
   `shell` را در **هر عمقی** رد می‌کند. «spawn کردن MCP server شخص ثالث» یعنی
   اجرای کد دلخواه شخص ثالث — که این قانون، صریحاً و با `Literal[False]`، ممنوع کرده.
5. **اولویت‌بندی NOW بزرگ‌تر از توان یک فاز است.** NOW سند = manifest schema +
   registry + license gate + sandboxed runner + output adapter + review queue
   integration + نمونه end-to-end + CI. در ریپویی با ۴۰ تست معماری، قرارداد
   مستندسازی (§7 `AGENTS.md`)، و فرآیند claim/release، این یک wave کامل است نه یک
   فاز. برای مقایسه: خود ریپو کارهای کوچک‌تر را به `task-255` تا `task-264` تفکیک
   کرده، هرکدام با یک zone و یک acceptance criteria.

### سطح دوم: پرامپت ایجنت (§9) — نمره ۴/۱۰

این بخش ضعیف‌تر از سند معماری است، به چهار دلیل مشخص:

**۱. فرآیند هماهنگی ریپو را کاملاً نادیده می‌گیرد.** `AGENTS.md` یک قرارداد
چندعاملی *الزامی* است با پنج قانون:

- `CLAIM BEFORE YOU CODE` — `python scripts/agent_board.py next/claim --branch …` و
  **push فوری** claim. پرامپت هیچ اشاره‌ای ندارد.
- `ONE OWNER PER FILE-ZONE` — `agent_board.py check --files …`؛ exit 1 یعنی overlap
  اثبات‌شده و **نباید push کرد**. ساختن `extension_hub/` + دست‌زدن به
  `creative/packs/manifest.py` یعنی ورود به zone های `pack-trust-root`،
  `cli-packs-registry`، `nagar-asset-library` و `docs-architecture` — حداقل چهار zone.
- `ONE GATES OWNER` — فقط یک ایجنت gate کامل را روی کار main-bound اجرا می‌کند؛
  بقیه می‌نویسند «deferred to gates owner». پرامپت می‌گوید «تست باید در CI موجود
  پروژه اجرا و سبز شود» بدون اینکه این تفکیک را بداند.
- قرارداد مستندسازی (§7): هر سند در `docs/README.md` ایندکس، هر لینک نسبی resolve،
  هر fence نمودار balanced (`tests/unit/test_docs_integrity.py`)، و **هر قانون
  مرزی باید تست اجراکننده‌اش را در `MODULE_MAP.md §3` نام ببرد**. پرامپت هیچ‌کدام
  را نمی‌خواهد — پس خروجی‌اش همان لحظه CI را قرمز می‌کند.

**۲. قانون R20 را نقض می‌کند.** `test_no_orphan_modules.py` هر ماژول
`src/nexus_ai_agent/**` را که هیچ‌کس import نکند، **رد می‌کند**. docstring تست
صریح است: ۹ ماژول «aspirational» در ممیزی ۲۰۲۶-۱۰-۱۵ پیدا و **حذف** شدند
(`core/async_db.py` که خودش را «foundation for all async DB operations» می‌نامید).
یک `extension_hub/` که «فعلاً فقط registry و license gate دارد» و به هیچ مسیر
اجرای واقعی وصل نیست، دقیقاً همان الگویی است که این تست برای کشتنش نوشته شده.
پرامپت این را نمی‌داند، پس ایجنت را به سمت ساخت کد مرده هل می‌دهد.

**۳. قانون R7 و R9 را نادیده می‌گیرد.** `MODULE_MAP.md §3`:

- **R7:** هر پک فقط stdlib + pydantic + `creative.studio` + `creative.packs`؛
  **بدون `subprocess`/`socket`/`ctypes`/`shutil`/`os.system`**، بدون reach-through
  به `storage`/`llm`/`bot`. این در تست به شکل *allowlist* اجرا می‌شود نه blocklist —
  `ALLOWED_PACK_TOP_LEVEL` در `test_audio_pack_boundary.py` و
  `test_edit_pack_boundary.py` دقیقاً ۱۴ ماژول است
  (`__future__, collections, dataclasses, datetime, hashlib, importlib, json,
  pathlib, re, typing, uuid, pydantic, nexus_ai_agent`) و `subprocess`/`socket`/
  `ctypes` در آن نیستند. یعنی افزودن‌شان **خطای قطعی CI** است، نه یک هشدار.
- **R9:** در lane رندر، **دقیقاً یک سایت `subprocess`** (`rendering/executor.py`)،
  هرگز `shell=True`، **بدون spawn از پک**. تست
  `test_slideshow_adapter_boundary.py::test_only_the_render_lane_spawns_a_process`.

«Sandboxed Runner که هر افزونه را به‌صورت subprocess اجرا می‌کند» مستقیماً با R7
(برای پک‌ها) و با روح R9 (یک سایت spawn، کنترل‌شده) در تضاد است. *دقت فنی:*
`test_rendering_lane_boundary.py` فقط `creative/rendering/*.py` را scan می‌کند، پس
یک `extension_hub/runner.py` **امروز** توسط آن تست گرفته نمی‌شود — یعنی کار
«می‌گذرد». ولی عبور از تست به‌معنی عبور از قانون معماری نیست؛ معنایش این است که
قانون ناقص است و باید با ADR + قانون جدید RNN پوشش داده شود. پرامپت هیچ‌کدام را
درخواست نکرده.

**۴. یک تناقض داخلی حل‌نشده دارد.** پرامپت هم می‌گوید «هر افزونه MCP server مستقل
است که hub آن را spawn/kill می‌کند» و هم می‌گوید «هیچ وابستگی افزونه‌ای نباید در
dependency tree اصلی ادغام شود» و هم «هیچ افزونه‌ای نباید دسترسی نوشتن مستقیم
داشته باشد». spawn کردن فرآیند شخص ثالث با allow-list دامنه و least-privilege،
بدون container/namespace (که سند خودش در NEXT گذاشته)، **روی POSIX خام قابل تضمین
نیست**. یعنی NOW سند یک sandbox ادعا می‌کند که ابزارش را NEXT ساخته. این یک
شکاف ترتیبی واقعی است، نه یک ایراد سلیقه‌ای.

---

## ۳. چه چیزی را درست گفته و باید نگه داشت

برای اینکه گزارش یک‌طرفه نباشد — این پنج نکته از سند ورودی **ارزش انتقال به طرح
نهایی** را دارند:

1. تفکیک `code_license` از `model_asset_license` (یافتهٔ C آن را تأیید عملی کرد).
2. `commercial_use: allowed | restricted | forbidden` به‌عنوان یک فیلد *سیاستی* صریح.
3. `last_verified` (ISO-date) — چون ریپو امروز «کی آخرین بار این manifest راستی‌آزمایی
   شد» را ثبت نمی‌کند، و law R16 (`test_repo_truth_consistency.py`) دقیقاً علیه
   ادعاهای تاریخ‌مصرف‌گذشته است.
4. هزینهٔ اجرا به‌عنوان یک *بعد تصمیم* (local-only / api-required / hybrid) — که در
   ریپو به شکل `network_policy.runtime_network` و `native.network_required` هست، ولی
   **هیچ برآورد هزینهٔ هر فراخوانی** وجود ندارد.
5. «وضعیت نگهداری سیگنال است نه معیار» — منطبق با `MODULE_MAP.md §4`: «the gates
   verify **structure**, not behaviour».

---

## ۴. نقشهٔ شکاف واقعی (Ground Truth Gap Map)

جدول زیر خروجی اصلی این ممیزی است: وضعیت هر معیار سند، **اندازه‌گیری‌شده از کد**.

| # | معیار سند | وضعیت واقعی | شاهد | شکاف |
|---|---|---|---|---|
| ۱ | مجوز کد (SPDX) | 🟡 جزئی | `LICENSE` در ریشه (MIT، ۱۰۶۴ بایت) | برای *پک* هیچ فیلد `code_license` وجود ندارد؛ فقط per-artifact |
| ۲ | مجوز مدل/دارایی | 🔴 **غایب (فیلد هست، گیت نیست)** | `ArtifactSpec.license: str` بدون اعتبارسنج؛ `grep license verify.py` → **خالی** | SPDX validation، allowlist، placeholder detection |
| ۳ | مجوز استفادهٔ تجاری | 🔴 **غایب کامل** | هیچ فیلد `commercial_use` در هیچ schema | افزودن فیلد + دروازه |
| ۴ | هزینهٔ اجرا | 🟡 نصف | `network_policy`, `native.network_required` | برآورد هزینه/فراخوانی، سقف بودجه، rate limit |
| ۵ | وابستگی‌ها و حجم نصب | 🟢 **قوی** | `download_size_mb`, `external_binaries`, `deptry`, `extras_matrix.py`, `DEPENDENCY_MATRIX.md` | SBOM per-pack (امروز فقط per-repo) |
| ۶ | سازگاری معماری + تست واقعی | 🟢 **قوی** | `verify_manifest()` + `PackRegistry.activate()` + R7/R8/R9 + `composition_issues()` | conformance *رفتاری* (خود `§4` هم اعتراف می‌کند gates فقط structure هستند) |
| ۷ | اتصال خروجی به Asset Library / Creative Graph | 🔴 **غایب** | صفر adapter بین `packs/` و `assets/`؛ `CreativeGraph` فقط in-memory؛ `task-124`/P0-9 (مسیر نوشتن حافظهٔ گراف) هنوز باز | **بزرگ‌ترین شکاف واقعی** |
| ۸ | وضعیت نگهداری | 🟢 هست (در لایهٔ board) | `.agents/board.json` با `claims`, `deferred_log`, `takeover_log`, TTL ۲۴h | برای *پک خارجی* معنی ندارد (پک خارجی repo ندارد) |
| ۹ | SBOM + اسکن آسیب‌پذیری | 🔴 غایب | فقط `deptry` (declared-vs-imported، نه CVE) | اسکن CVE روی `external_binaries` و `artifacts` |
| — | Trust root / امضا | 🟢 **قوی‌تر از سند** | `trust.py` (Ed25519, ۹ trust state, canonical signing bytes, revocation), ADR 0006, `scripts/pack_trust_mutations.py` | **`publishers: {}` — صفر ناشر onboard شده** (یافتهٔ A) |
| — | Permission vocabulary | 🟡 drift | `KNOWN_PERMISSIONS` = ۸ توکن؛ ۶ توکن خارج از آن در ۵ manifest اعلام شده | **یافتهٔ B** — warning نه error |
| — | Resource Broker (RAM/disk/CPU/GPU واقعی) | 🔴 **غایب کامل** | `hardware_requirements` فقط اعلامی؛ `grep vram\|nvidia src/` → خالی؛ `psutil` هست ولی فقط در `agent/self_monitor.py` و `agent/self_vitals.py` برای RSS/disk خودِ فرآیند | اندازه‌گیری واقعی + `resource_blocked` در activation gate |
| — | مرور/کشف پک (Marketplace) | 🟡 CLI هست | `nexus packs list` / `verify` / `activate` در `cli.py:512,567,618` | UI؛ سند درست این را LATER گذاشته |

**خلاصه:** ۴ سبز، ۴ زرد، ۵ قرمز. کار واقعی = **بستن ۵ قرمز**، نه ساختن یک hub موازی.

---

## ۵. اگر من اجرا کنم — چه چیزی متفاوت، خلاقانه‌تر و دقیق‌تر است

پاسخ صادقانه به سؤال «چقدر می‌توانی خلاقانه‌تر اجرا کنی»: خلاقیت در این ریپو به معنی
اختراع زیرسیستم جدید نیست — **به معنی پیدا کردن کوتاه‌ترین مسیر قانونی بین چیزی که
هست و چیزی که باید باشد** است، چون ۴۰ تست معماری و یک قرارداد چندعاملی، مرزها را
از قبل سفت کرده‌اند. خلاقیت واقعی در *کشف* این است که ۹۰٪ زیرساخت موجود است و فقط
سه قفل باز نشده.

### اصل حاکم: سه قانونی که طرح من از سند ورودی می‌گیرد

1. **یک authority، نه دو.** هر دروازهٔ جدید باید داخل `VerificationReport` موجود
   بنشیند و از `PackRegistry.activate()` عبور کند. هیچ registry دوم، هیچ schema دوم،
   هیچ trust root دوم. (`trust.py` این اصل را در docstring خودش فرموله کرده:
   «never allowed to self-authorise each other».)
2. **ادعا در برابر شواهد در برابر authority.** الگوی سه‌لایهٔ `trust.py`
   (claim = manifest، evidence = امضا، authority = trust root) را به **مجوز** و
   **منابع** تعمیم می‌دهم: `license` یک ادعاست؛ SPDX database شواهد؛ سیاست پروژه
   authority. `minimum_ram_mb: 2048` یک ادعاست؛ `psutil.virtual_memory()` شواهد؛
   activation gate authority. این تعمیم، نوآوری مفهومی اصلی طرح من است و کاملاً
   با idiom موجود ریپو هم‌خوان است.
3. **یک finding جدید، یک تست که آن finding را واقعاً تولید کند.** نه تست mock.
   ریپو ۱۳ اسکریپت mutation دارد (`scripts/*_mutations.py`) که ثابت می‌کنند گیت‌ها
   *قابل کشتن* هستند؛ هر گیت جدید هم باید mutation probe خودش را بگیرد.

### پنج اسلایس پیشنهادی (هرکدام مستقل، additive، با zone مشخص)

**اسلایس ۱ — `pack-license-gate` (بالاترین ارزش/ریسک‌پذیری؛ پیشنهاد می‌کنم از اینجا شروع شود)**

- افزودن `LicensePolicy` به `creative/packs/` با: یک SPDX allowlist (داده، نه کد)،
  تشخیص placeholder (الگوی `"replace-with"` که `digest_is_placeholder` از قبل دارد)،
  و یک فیلد جدید `commercial_use: Literal["allowed","restricted","forbidden"]` روی
  سطح manifest + `artifacts[]`.
- افزودن سه finding code جدید به `verify_manifest()`:
  `unrecognized_license` (error), `placeholder_license` (error),
  `commercial_use_conflict` (error) — با همان الگوی severity موجود.
- افزودن یک دروازه به `activate()`: مجوز تأیید نشده ⇒ **عدم فعال‌سازی**، دقیقاً مثل
  `PACK-SEC-001` برای امضا.
- **ارزش اثباتی:** این گیت، روی درخت *امروز*، باید ۲ finding واقعی تولید کند
  (`whisper-small-int8`, `persian-aligner` با `model-license-reference`) — یعنی
  «کار می‌کند» قابل نمایش است، نه ادعا.
- **تلهٔ صادقانه که باید از قبل گزارش شود:** اعمال این گیت، پک caption را
  *غیرفعال* می‌کند. پس یا (الف) مجوز واقعی whisper-small (MIT) و aligner را
  جایگزین می‌کنیم، یا (ب) گیت را به‌صورت warning-only معرفی می‌کنیم و یک task
  جدا برای اصلاح manifest ها باز می‌کنیم. تصمیم (الف) بهتر است ولی **باید صاحب
  ریپو انتخاب کند** — این یک تغییر رفتار سیستم است و طبق `AGENTS.md` §7 باید به
  `docs/DECISION_LOG.md` برود.
- zone: `pack-trust-root` (هم‌خانواده) + `docs-architecture`. تست‌ها:
  `tests/unit/test_pack_license_gate.py` +
  `tests/architecture/test_pack_license_boundary.py` + `scripts/pack_license_mutations.py`.

**اسلایس ۲ — `pack-permission-vocabulary-drift` (کوچک، سریع، یک bug واقعی)**

- تصمیم: `unknown_permission` از `warning` به `error` ارتقا یابد، یا ۶ توکن
  گم‌شده به `KNOWN_PERMISSIONS` اضافه شوند.
- اندازه‌گیری من می‌گوید هر ۶ توکن *معنادار* هستند (`write_automation_curves`,
  `mutate_timeline_structure`, `write_motion_layers`, `write_color_layers`,
  `export_timeline_interchange`, `write_derived_delivery_assets`) — یعنی احتمالاً
  vocabulary عقب مانده، نه اینکه manifest ها اشتباه کنند. پس (ب) درست‌تر است:
  افزودن توکن‌ها + یک تست که `KNOWN_PERMISSIONS` را با union مجوزهای همهٔ
  manifest های shipped مقایسه می‌کند تا drift آینده *غیرممکن* شود.
- ارزش: این یک تست **fitness function** جدید است که یک کلاس drift را برای همیشه
  می‌بندد. هزینه: ~۳۰ خط.

**اسلایس ۳ — `resource-broker` (پرکردن بزرگ‌ترین حفرهٔ اجرایی)**

- یک `ResourceProbe` port در `application/ports/` (طبق recipe «Add a port» در
  `MODULE_MAP.md §5`: `Protocol` با متدهای typed async + `idempotency_key` برای
  عملیات مخرب) + adapter در `adapters/` که `psutil` را اندازه می‌گیرد
  (RAM/disk/CPU threads؛ GPU فعلاً `unknown` صادقانه، نه حدس).
- افزودن `resource_state: satisfied | insufficient | unknown` به activation — با
  این قانون که `unknown` **فعال‌سازی را بلاک نمی‌کند** ولی گزارش می‌شود
  (fail-closed برای `insufficient`، fail-honest برای `unknown` — تفکیکی که
  `trust.py` با `NO_TRUST_ROOT` در برابر `SIGNATURE_INVALID` از قبل الگویش را دارد).
- ارزش: تبدیل `hardware_requirements` از یک ادعای تشریفاتی به یک گیت اندازه‌گیری‌شده.
  امروز هیچ کجای `src/` این را چک نمی‌کند.

**اسلایس ۴ — `pack-to-asset-output-contract` (بزرگ‌ترین شکاف معماری؛ سنگین‌تر)**

- تعریف یک **تنها** مسیر نوشتن: outcome یک operation → `AttemptStaging`
  (`execution/staging.py`) → `AssetIngestion` (`creative/assets/ingestion.py`) از
  طریق `AuthorizationSeam.require(actor_kind, actor_id, project_id, permission)`.
- نگاشت صریح `permission` پک → `SUPPORTED_MEDIA_TYPES`. و **گزارش صادقانه** اینکه
  خروجی غیررسانه‌ای (LUT، OTIO، transcript متنی) امروز در Asset Library
  **پذیرفته نمی‌شود** — این یک محدودیت واقعی است، نه چیزی که با یک adapter حل شود.
- این اسلایس با `task-255-staging-render-wiring` (اولین مورد `next_work` در board)
  هم‌پوشانی دارد ⇒ **قبل از هر کاری باید در board بررسی و claim شود**، وگرنه
  collision قطعی است.

**اسلایس ۵ — `external-publisher-onboarding` (بازکردن قفل یافتهٔ A)**

- یک runbook + یک CLI (`nexus packs trust add-publisher`) که کلید عمومی Ed25519 را
  به `trust_root.json` اضافه می‌کند، با یک *fixture publisher* برای تست end-to-end.
- این اسلایس «Extension Hub» را از یک زیرساخت دست‌نخورده به یک مسیر **قابل استفاده**
  تبدیل می‌کند — بدون یک خط کد موازی، فقط با بازکردن قفلی که عمداً بسته شده.
- ارزش: بعد از این، «ثبت یک پک خارجی → license gate → trust gate → resource gate →
  activation» یک مسیر end-to-end *واقعی* است که می‌توان با یک fixture اثباتش کرد.
  دقیقاً همان چیزی که سند ورودی می‌خواست، ولی با ۱۰٪ کد و بدون هیچ نقض معماری.

### ایدهٔ خلاقانه‌تر از سند ورودی — «Capability Bridge» به‌جای «MCP spawn»

سند ورودی می‌خواهد کد شخص ثالث را به‌عنوان فرآیند spawn کند. این با «packs are data,
never code» در تضاد بنیادین است. طرح جایگزین من:

> **پل، خودش یک پک first-party است.** یک ابزار خارجی (مثلاً یک مخزن GitHub) به‌عنوان
> `external_binaries` در manifest *اعلام* می‌شود (فیلدی که از قبل هست و
> `verify_manifest` آن را بررسی می‌کند)، و **تنها** سایت مجاز اجرا، همان الگوی
> `tools/system_shell.py` است: یک flag-grammar allowlist صریح (ADR 0011) + مرز
> `WorkspaceFilesystem` با `O_NOFOLLOW`/`dir_fd` + `shell=False`.
>
> یعنی به‌جای import کردن یا spawn کردن کد ناشناخته، **دستورالعمل اجرای آن را
> به‌صورت داده اعلام می‌کنیم** و اجرای واقعی از یک لایهٔ first-party، امضاشده،
> و از قبل hardening‌شده عبور می‌کند.

چرا این خلاقانه‌تر *و* بهتر است:

- تنها security asset واقعاً اثبات‌شدهٔ ریپو را *بازاستفاده* می‌کند به‌جای ساختن یک
  sandbox جدید ضعیف‌تر. docstring `system_shell.py` مستند می‌کند که **blocklist قبلی
  در عمل شکست خورد** (`date -f FILE` یک file-disclosure primitive بود) و allowlist
  جایگزینش شد. ساختن sandbox جدید از صفر، یعنی تکرار همان اشتباه با تجربهٔ کمتر.
- `Literal[False]` ها دست‌نخورده می‌مانند؛ R7 و R9 نقض نمی‌شوند.
- MCP به‌عنوان یک *extra اختیاری* (`[project.optional-dependencies]`) قابل افزودن است
  **بعداً**، با یک leg در `extras-matrix` — بدون اینکه هسته به آن وابسته شود. این
  ترتیب، تضاد داخلی سند ورودی (§4 در برابر §2 معیار ۵) را حل می‌کند.

### ایدهٔ دوم — «Measured Risk Tier» به‌جای `risk_tier` اعلامی

سند ورودی `risk_tier: low|medium|high` را در manifest می‌گذارد — یعنی **پک خودش
ریسک خودش را اعلام می‌کند**. این دقیقاً همان ضدالگویی است که `trust.py` علیه آن
نوشته شده («a manifest is a **claim**»). جایگزین:

> ریسک را از **شواهد اندازه‌گیری‌شده** مشتق کن، نه از ادعا:
> `signature_state` (verified / placeholder / revoked) × `license_state`
> × `network_required` × `external_binaries` × `permissions granted` ×
> `resource_state`. سپس این ریسک مشتق‌شده را داخل **Artifact Passport** موجود
> (`provenance/passport.py`) ثبت کن — که از قبل artefact را *دوباره اندازه می‌گیرد*
> به‌جای اعتماد به ادعا، و با `PassportStatus`/`Finding` ساخت‌یافته است.
>
> نتیجه: صف بازبینی بر اساس ریسک **اثبات‌شده** triage می‌کند، و هر «auto-approve»
> یک زنجیرهٔ شاهد دارد که در ژورنال hash-chained ثبت شده. حذف یا دست‌کاری‌اش
> قابل تشخیص است (`verify_chain`).

این ایده، دو زیرسیستم موجود (`packs/trust` و `provenance/`) را به هم وصل می‌کند —
اتصال‌هایی که سند ورودی هیچ‌کدام را نمی‌دید، چون نمی‌دانست وجود دارند.

### ترتیب پیشنهادی (با دلیل)

```
اسلایس ۲ (permission drift)   ← ۳۰ خط، یک bug واقعی، بدون ریسک. اعتمادسازی.
اسلایس ۱ (license gate)       ← بیشترین ارزش؛ مستقیماً ریسک حقوقی واقعی را می‌بندد.
اسلایس ۵ (publisher onboarding)← قفل «hub غیرقابل استفاده» را باز می‌کند.
اسلایس ۳ (resource broker)    ← port جدید + adapter؛ مستقل از ۱ و ۵.
اسلایس ۴ (output contract)    ← سنگین؛ باید با task-255 در board هماهنگ شود.
```

---

## ۶. آنچه عمداً NEXT/LATER گذاشته می‌شود (و چرا)

| مورد | چرا حالا نه |
|---|---|
| Container isolation واقعی (Docker/K8s per plugin) | ریپو `docker-compose.yml` با «exactly `{bot, dashboard}`» دارد و R4 هر Celery/Redis و dispatch توزیع‌شده را **رد می‌کند**. افزودن کانتینر per-plugin یک تغییر معماری کلان است، نه یک اسلایس. |
| افزودن MCP به هسته | تضاد با `deptry` + `extras_matrix`؛ و تا وقتی یک استفاده‌کنندهٔ واقعی و تست‌شده ندارد، R20 آن را orphan می‌کند. |
| Marketplace UI | خود سند ورودی هم درست این را LATER گذاشته. |
| اسکن CVE / SBOM per-pack | ارزشمند، ولی نیاز به یک منبع دادهٔ آفلاین دارد و ریپو offline-first است؛ تصمیم دربارهٔ منبع داده باید قبل از کد گرفته شود. |
| GPU/VRAM detection واقعی | بدون سخت‌افزار قابل *اثبات* نیست. `unknown` صادقانه بهتر از حدس است (الگوی `creative/caption/unavailable_adapter.py` در ریپو دقیقاً همین کار را می‌کند). |
| نوشتن پایدار Creative Graph | این `task-124`/P0-9 است و در board باز. تکرارش collision است. |

---

## ۷. چه چیزی فرض شد (چون اطلاعات کافی نبود)

صادقانه، طبق همان استانداردی که سند ورودی از ایجنت خواسته:

1. **فرض کردم `main` @ `04469ed` وضعیت مرجع است.** PR باز یا کار در جریان روی
   شاخه‌های دیگر را بررسی نکردم (`gh pr list` اجرا نشد) — اگر اسلایس ۱ یا ۴ قرار
   باشد اجرا شود، این بررسی **الزامی** است.
2. **فرض نکردم که gate ها امروز سبز هستند.** هیچ تستی اجرا نکردم (طبق درخواست صریح
   شما: «هیچ چیزی را فعلاً تغییر نده»). پس ادعای «۴۰ تست معماری» از *شمارش فایل*
   می‌آید، نه از اجرای موفق آن‌ها.
3. **مجوز واقعی whisper-small و persian-aligner را بررسی نکردم** — نیاز به دسترسی
   شبکه به منبع مدل دارد. یافتهٔ C فقط می‌گوید فیلد placeholder است، نه اینکه
   مجوز واقعی چیست.
4. **نمی‌دانم `publishers: {}` عمدی-دائمی است یا موقت.** کامنت داخل `trust_root.json`
   می‌گوید «no external publisher has been onboarded» که به *موقت بودن* اشاره دارد،
   ولی تصمیم نهایی با صاحب ریپو است. اسلایس ۵ روی همین فرض بنا شده.
5. **ارتباط `PendingApproval` با asset ingestion را بررسی نکردم.** فرض من این است که
   این دو امروز به هم وصل نیستند (هیچ import متقابلی ندیدم)، ولی این را با تست
   اثبات نکردم.

---

## ۸. پرامپت بازنویسی‌شده (نسخهٔ پیشنهادی برای فاز NOW)

اگر بخواهید همین کار را به یک ایجنت کدنویس بدهید، این نسخه را پیشنهاد می‌کنم.
تفاوت کلیدی: **نام فایل‌ها، zone ها، قانون‌ها و تست‌های واقعی را می‌برد**، پس ایجنت
نمی‌تواند موازی‌سازی کند یا ادعای بی‌پایه بکند.

```text
نقش: مهندس ارشد سیستم روی مخزن nexus-ai-agent (شاخهٔ کاری خودت، بیس main).

قانون صفر — قبل از هر خط کد، این چهار فایل را بخوان و در گزارش به آن‌ها ارجاع بده:
  1. AGENTS.md                      (قرارداد چندعاملی: claim/release، zone، gates owner)
  2. docs/architecture/MODULE_MAP.md §3 (قانون‌های مرزی R1..R21 و تست اجراکنندهٔ هرکدام)
  3. docs/architecture/MODULE_MAP.md §5 (recipe افزودن پک / port / فرمان)
  4. src/nexus_ai_agent/creative/packs/{manifest,verify,registry,trust,runtime}.py

ممنوعیت‌های مطلق (اگر نقض شد، کار را متوقف و گزارش کن):
  × ساخت هیچ registry / manifest schema / trust root / approval queue موازی.
    تنها manifest schema مجاز: nexus.capability-pack.v1.
    تنها authority اعتماد: creative/packs/trust.py + trust_root.json.
    تنها گزارش دروازه: creative/packs/verify.py::VerificationReport.
    تنها مسیر فعال‌سازی: creative/packs/registry.py::PackRegistry.activate.
  × افزودن هر وابستگی جدید به [project.dependencies] (deptry + extras_matrix رد می‌کنند).
  × relax کردن allow_arbitrary_native_code / allow_arbitrary_wasm_imports از Literal[False].
  × spawn کردن subprocess از داخل creative/packs/** (R7) یا افزودن سایت subprocess
    جدید به creative/rendering/** (R9 — فقط executor.py).
  × تغییر trust_root.json به‌جز افزودن کلید *عمومی* یک publisher (بدون material خصوصی).

هدف فاز NOW — دو اسلایس، هرکدام جداگانه قابل merge:

اسلایس A: License Gate (اولویت اول)
  A1. یک LicensePolicy داده‌محور در creative/packs/ (SPDX allowlist به‌عنوان داده،
      نه کد) + تشخیص placeholder با همان الگوی digest_is_placeholder.
  A2. فیلد جدید commercial_use: Literal["allowed","restricted","forbidden"]
      — additive روی nexus.capability-pack.v1، با default که رفتار امروز را عوض نکند.
  A3. سه finding code جدید در verify_manifest(): unrecognized_license (error)،
      placeholder_license (error)، commercial_use_conflict (error).
      severity را از الگوی موجود VerificationIssue بگیر، نه از سلیقهٔ خودت.
  A4. دروازه در activate(): license تأییدنشده ⇒ عدم فعال‌سازی (هم‌الگوی PACK-SEC-001).
  A5. گزارش صادقانه و *قابل اجرا*: خروجی گیت روی هر ۶ manifest shipped.
      انتظار مستند من این است که پک caption دو finding واقعی بگیرد
      (whisper-small-int8 و persian-aligner با license="model-license-reference").
      اگر خروجی تو این نبود، یا گیتت کار نمی‌کند یا من اشتباه کرده‌ام — هر دو را
      با دستور و خروجی واقعی نشان بده.
  A6. اگر گیت، پک caption را غیرفعال می‌کند: manifest را *بدون اجازهٔ من* تغییر نده.
      دو گزینه را با پیامد هرکدام گزارش کن و منتظر تصمیم بمان. این یک تغییر رفتار
      سیستم است و طبق AGENTS.md §7 باید به docs/DECISION_LOG.md برود.

اسلایس B: Permission Vocabulary Drift (کوچک، سریع)
  B1. union همهٔ permissions اعلام‌شده در ۶ manifest shipped را با
      manifest.py::KNOWN_PERMISSIONS مقایسه کن و جدول اختلاف را در گزارش بگذار.
      انتظار مستند من: ۶ توکن خارج از vocabulary در ۵ پک.
  B2. یک تست fitness-function بنویس که این مقایسه را برای همیشه اجباری کند:
      هر manifest shipped باید فقط توکن‌های شناخته‌شده اعلام کند.
  B3. تصمیم (افزودن توکن‌ها به KNOWN_PERMISSIONS در برابر ارتقای unknown_permission
      به error) را *پیشنهاد* بده با دلیل، ولی اعمالش نکن تا تأیید کنم.

الزامات فرآیندی (بدون این‌ها کار پذیرفته نیست):
  P1. python scripts/agent_board.py show → next → claim (با zone صحیح) و push فوری claim.
  P2. قبل از push: python scripts/agent_board.py check --files <changed> — exit 1 یعنی
      توقف. exit 2 یعنی منبع ناخوانا، نه pass.
  P3. تست‌ها: unit + یک architecture gate + یک mutation probe
      (الگو: scripts/pack_trust_mutations.py). تست بدون mutation probe یعنی
      ثابت نکرده‌ای گیت قابل کشتن است.
  P4. مستندسازی: MODULE_MAP.md §3 (قانون جدید + تست اجراکننده‌اش)، §2 (اگر پکیج جدید)،
      docs/README.md index، و docs/DECISION_LOG.md برای هر تغییر رفتار سیستم.
      pytest -q tests/unit/test_docs_integrity.py باید سبز باشد.
  P5. اگر gates owner نیستی، در PR body بنویس «deferred to gates owner»؛
      چک‌های محلی تشخیصی (ruff، pytest هدفمند) آزاد و مطلوب است.

گزارش نهایی — دقیقاً این چهار بخش، با شاهد:
  ۱. چه ساخته و *اجرا* شد: مسیر فایل‌ها + دستور دقیق + خروجی واقعی (نه «tests pass»).
  ۲. یافته‌های ناخواسته: هر drift یا تضادی که حین کار کشف کردی و در پرامپت نبود.
  ۳. فرض‌ها: هر چیزی که بدون اطلاعات کافی فرض کردی، با برچسب «اثبات‌نشده».
  ۴. NEXT/LATER: چه چیزی را عمداً انجام ندادی و چرا (با ارجاع به قانون R یا task board).

اگر هر بخش از این طراحی با معماری فعلی تناقض دارد: اجرا نکن، گزارش کن، جایگزین بده.
این جمله یک تعارف نیست — در این ریپو ۴۰ تست معماری آن را اعمال می‌کنند.
```

---

## ۹. جمع‌بندی نهایی

| سؤال شما | پاسخ |
|---|---|
| این پرامپت چقدر بهتر است؟ | **سند معماری: ۷/۱۰** (جهت درست، ۳ فیلد ارزش‌افزا، تفکیک مجوز کد/مدل عالی). **پرامپت اجرایی: ۴/۱۰** (بی‌خبر از ۹۰٪ زیرساخت موجود، ناقض R7/R9/R20، نادیده‌گیرندهٔ قرارداد `AGENTS.md`، و دارای یک تضاد داخلی بین MCP و «وابستگی هسته ممنوع»). |
| آیا خلاقانه‌تر قابل اجراست؟ | بله — ولی نه با اختراع بیشتر. خلاقیت اینجا یعنی: **(۱)** کشف اینکه hub از قبل ساخته شده و فقط `trust_root.json` خالی است؛ **(۲)** تعمیم الگوی claim/evidence/authority از `trust.py` به مجوز و منابع؛ **(۳)** «Capability Bridge» به‌جای MCP spawn تا `Literal[False]` ها نشکنند؛ **(۴)** «Measured Risk Tier» مشتق از شواهد و ثبت‌شده در Artifact Passport به‌جای `risk_tier` اعلامی؛ **(۵)** استفاده از mutation probe به‌عنوان اثبات اینکه گیت واقعاً گیت است. |
| دقیق‌تر؟ | بله، و قابل اندازه‌گیری: سند ورودی صفر ارجاع به فایل واقعی دارد؛ این گزارش ۴۰+ شاهد مسیر-محور دارد و همهٔ اعدادش با دستور روی درخت بازتولید می‌شوند. سه یافتهٔ A/B/C مستقیماً از manifest ها و `trust_root.json` استخراج شده‌اند، نه از توصیف. |
| بهتر؟ | بله در یک معیار مشخص: **این طرح merge شدنی است.** طرح سند ورودی روی این ریپو یا صفر کد تولید می‌کند (به‌خاطر بند «تناقض ⇒ اجرا نکن») یا CI را قرمز می‌کند (R20/R7/R9/docs integrity). |

**توصیهٔ عملی:** سند ورودی را دور نریزید. سه فیلد آن (`commercial_use`، تفکیک صریح
code/model license، `last_verified`) و اصل «مجوز مدل جدا از مجوز کد» را بردارید و به
`nexus.capability-pack.v1` اضافه کنید — به‌عنوان **توسعهٔ additive** یک schema موجود،
نه یک schema جدید. بقیهٔ سند، توصیف زیرساختی است که این ریپو از قبل دارد و بهتر از
توصیف سند ساخته است.
