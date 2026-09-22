# Research Intelligence Package — خلاصه اجرایی (Agent 4 مستقل)

> **ماموریت:** لایه تحقیقاتی معماری و ریسک — کاملاً جدا از Nagar, PR47, Tasks 159/160/161, Agent1/2, Creative Studio  
> **تاریخ:** 2026-09-22 — شاخه `arena/01a0cb4d-nexus-ai-agent` (مبتنی بر 192b227, فقط READ-ONLY روی ریپازیتوری)  
> **وضعیت نهایی:** **RESEARCH COMPLETE**  
> **خروجی:** `research/01` تا `10` + این فایل

---

## چکیده 30 ثانیه‌ای

- **چشم‌انداز 2026 = گراف + اجرای پایدار + پروتکل استاندارد ابزار + هوش محلی.** این چهار تکه هر چهار به بلوغ M3 رسیده‌اند و برای یک سیستم offline-first مثل NEXUS هر چهار لازم‌اند — نه انتخابی.
- **معماری غالب تولیدی:** LangGraph (گراف state-machine + checkpoint) ; MCP (استاندارد ابزار/Registry) ; Temporal فقط برای workflows ساعت/روز ; Hybrid Search (BM25+dense+RRF) برای RAG پیش‌فرض است نه Vector خالص.
- **ریسک اصلی امروز:** Indirect prompt injection و Memory poisoning — هر دو Residual High و با هیچ لایه‌ای صفر نمی‌شوند — و RAG بدون ACL/ provenance بزرگ‌ترین مسیر exfil است.
- **دیدگاه برای NEXUS:** طراحی monolith + queue سبک SQLite + sqlite-vec + fallback chain مبتنی بر LiteLLM + deny-by-default فعلی «درست» است تا ~1k کاربر فعال؛ نقطه شکستِ مقرر 100k کاربر یا workflows روزهایه که رویداد معماری (ADR) می‌خواهد نه پَچ.

---

## ۱) مهم‌ترین یافته‌ها (Top 10)

| # | یافته | وضعیت | اطمینان |
|---|-------|--------|---------|
| F1 | چهار ستون 2026: Graph (LangGraph v1.0) + Durable Execution (Temporal v1.30) + MCP 2026-07-28 stateless + Local (GGUF/Ollama/MLX) — هر کدام production | VERIFIED | HIGH |
| F2 | هیچ «exactly-once» عمومی وجود ندارد — Celery/Kafka/Redis همه at-least-once؛ EOS فقط scope ترانزاکشنی باریک است | VERIFIED (docs) | HIGH |
| F3 | Hybrid Search پیش‌فرض تولیدی است: WANDS +7.4% NDCG vs dense یا BM25 تنها؛ MTEB +4.11% | VERIFIED (benchmark) | HIGH |
| F4 | offline-first سرتاسری امروز ممکن است: llama.cpp/Ollama + sqlite-vec + ONNX/ExecuTorch + MCP stdio + SQLite sidecar | VERIFIED | HIGH |
| F5 | indirect prompt injection همچنان رتبه ۱ OWASP 2025 برای دو دوره متوالی است؛ mitigations کاهش می‌دهد ولی حذف نمی‌کند | VERIFIED | HIGH |
| F6 | GraphRAG سه برابر بهتر روی کوئری‌های schema-محور اما پرهزینه (LLM extraction) — فقط وقتی رابطه‌محور است می‌صرفد | PARTIALLY (دامنه‌ای) | MEDIUM |
| F7 | MCP Registry متای‌ریجستری (metadata ≠ code) و از 2025-09-08 پیش‌نمایش زنده دارد؛ 75+ کانکتور؛ همه big vendors پیوسته‌اند | VERIFIED | HIGH |
| F8 | اکثریت عامل‌های تولیدی هنوز ReAct حلقه ساده‌اند — تیم‌ها بیش‌مهندسی می‌کنند (TheAIEngineer 2026) | VERIFIED (مشاهدات صنعت) | MEDIUM |
| F9 | هیچ فریم‌ورکی policy-gate بومی ندارد (Cordum 2026) — حاکمیت باید در Bus/Registry میزبان باشد | VERIFIED | HIGH |
| F10 | هزینه trace (KB/span) عامل را گلوگاه ذخیره‌سازی می‌کند — نمونه‌برداری هوشمند (100% خطا، 1% موفقیت) اجباری است | VERIFIED | HIGH |

---

## ۲) معماری‌های موجود (ارزیابی‌شده در Stage 2)

1. **A1 Tool-Calling / ReAct** — ساده‌ترین، کمترین LoC، هر مرحله یک LLM call → پرهزینه در زنجیره‌های بلند؛ بدون checkpoint پایدار نیست.
2. **A2 Planner/Executor (DAG)** — پلَن جلو + executor ارزان؛ ADaPT +28pp روی ALFWorld؛ سقف در محیط‌های پویا (replan thrash).
3. **A3 Event-Driven** — Pub/Sub سست‌پیوست، مقیاس fleet، بازپخش لاگ؛ اشکال‌یابی پراکنده، eventual consistency.
4. **A4 Graph / State-Machine** — صریح، قابل replay، time-travel، بهترین HIL (LangGraph `interrupt_before`)؛ منحنی یادگیری تند.
5. **A5 Command-Bus / Capability** — typed envelope + Registry + نردبان مجوز A-D؛ قطعی، تست‌پذیر (pure packs stdlib+pydantic)، مناسب مدیا/اپ‌های با سطوح مجوز.

*هیبریدها ترکیب این شِکل‌های خالص‌اند — نه شِکل ششم مستقل.*

---

## ۳) Trade-offها (بدون «بهترین»)

| محور | طرف A | طرف B | پیامد تصمیم |
|------|-------|-------|-------------|
| انعطاف ↔ قطعیت | ReAct/Event | Graph/Bus | اکتشافی <6 گام → ReAct؛ شاخه‌بندی/تایید → Graph/Bus |
| اشکال‌یابی | Graph (waterfall) | Event (پراکنده) | نیاز compliance → Graph |
| هزینه | Planner (۱ گران + N ارزان) | ReAct (N گران) | زنجیره ۱۰گامه → اختلاف ۳۰-۴۰% |
| پایداری | Temporal / گراف با checkpoint | Queue (at-least-once) | ساعت‌ها/روزها + جبران → Temporal منحصراً |
| مقیاس | Kafka (partition) | SQLite sidecar | Zero infra → Postgres/SQLite؛ 10k+ events/s → Kafka |

---

## ۴) ریسک‌های اصلی (خلاصه Stage 4 — ماتریس Heat)

بحرانی‌ترین باقیمانده‌ها پس از کنترل:

- **HIGH residual:** Indirect prompt injection (ورود پنهان در RAG/وثیقه/تقویم)، Memory poisoning تدریجی
- **MEDIUM-HIGH:** Tool poisoning / supply chain (رجیستری هنوز اسکن خودکار اجباری ندارد)، Data exfiltration از RAG بدون ACL
- **MEDIUM:** Untrusted tool output (XSS زنجیره‌ای)، Supply chain ترانزیتی، Privilege delegation (ASI03)
- **LOW-MED:** SSRF (اگر guard خصوصی‌رنج + DNS validation)، Credential leakage (اگر redaction allow-list)، Command injection (اگر no-shell + allow-list) — هر سه در NEXUS کنترل‌شده دیده شدند.

دو شکاف بازِ صریحِ ثبت‌شده در `docs/architecture/SECURITY.md` (READ-ONLY): **P0-8 double wiring** و **P0-9 graph memory write path** — هر دو در بورد task-124 رهگیری شده‌اند نه نادیده. [VERIFIED]

---

## ۵) فناوری‌های در حال بلوغ (Growing, قابل سرمایه‌گذاری)

- **MCP 2026-07-28 stateless + Tasks/EMA/Apps extensions + Registry** — مسیر استاندارد ابزار؛ هر پروژه جدید باید MCP-client شود. [M4]
- **LangGraph v1.0 + PostgresSaver** — اجرای پایدار state-machine برای agent؛ بهترین نگهبان production.
- **Hybrid Search + reranker (FlashRank)** — سود NDCG اندازه‌گرفتنی با هزینه کم؛ sqlite-vec + FTS5 برای monolith مناسب.
- **GGUF/Ollama/MLX** — استک آفلاین روی سه بک‌اند اصلی (Metal/CUDA/ROCm/Vulkan) پایدار شد.
- **OTel GenAI semconv + Phoenix/TruLens** — اتحاد observability: trace → score.
- **A2A (Agent-to-Agent, Google Apr 2025)** — توسعه‌پذیری fleet بدون قفل فریم‌ورک — مکمل MCP.
- **ExecuTorch 1.0** — هوش روی گوشی/میکروکنترلر برای میلیاردها دستگاه — شکاف edge را می‌بندد.

---

## ۶) فناوری‌های پرریسک (Caution)

- **GraphRAG** — اگر حجم پرس‌وجو single-hop ساده است، افزایش دقت < معنادار ولی هزینه ingest سنگین + staleness → قبل از adopt باید eval حتما سود را نشان دهد.
- **Ollama به‌عنوان سرور تولیدی همروند** — serialize می‌کند؛ برای concurrency به vLLM سوئیچ لازم است (پیتفال مستند).
- **WebGPU/WASM LLM در مرورگر** — op gaps + payload بزرگ؛ معادل native نیست — فعلاً tier دوم.
- **Swarm / Debate چندعاملی** — در گزارش زیست‌بوم 2025-26 «کمتر قابل پیش‌بینی برای enterprise» رده‌بندی شد؛ production-proven نیست.
- **«Exactly-once processing» هر broker** — بازار یابی گمراه‌کننده است؛ فقط transactional narrow scope معتبر است.

---

## ۷) شکاف‌های بازار/فناوری

| شکاف | توضیح | شواهد |
|------|-------|-------|
| G1 Model capability card | کارت استاندارد برای VRAM/quant quality/زبان‌ها (خصوصاً فارسی) وجود ندارد — هر پروژه دستی | UNKNOWN — گزارشی نیافتیم |
| G2 Hardware-aware model auto-select | محاسبه خودکار headroom شامل KV-cache برای context×concurrency — دستی با trial | PARTIALLY VERIFIED |
| G3 Memory eval benchmark | بنچمارک نگهداشت episodic چندماههٔ استاندارد نیست — تیم‌ها سفارشی | UNKNOWN/LOW maturity |
| G4 Registry latency در مقیاس | نگاه Registry زیر 1M tool benchmark نشده | UNKNOWN |
| G5 Tool-catalog TTL optimum | توازن prompt-cache stability vs freshness بدون مطالعه تجربی | UNKNOWN |
| G6 Multilingual hybrid (فارسی/عربی غنی‌ساختواژ) | BM25 vs vector برش مرفولوژی فارسی بنچمارک نشده | UNKNOWN |
| G7 Native agent policy gate | هیچ فریم‌ورکی gate سیاست بومی ندارد | VERIFIED (Cordum جدول) |

---

## ۸) فرصت‌های تحقیقاتی (برای rip آینده)

1. **Provenance-tagged RAG:** هر chunk منبع+مجوز+زمان‌مهر داشته باشد → فیلتر قبل از similarity → شکاف exfil اصلی بسته شود.
2. **Golden memory dataset:** مجموعه نگهداشت 30-روزهٔ فارسی برای اندازه‌گیری forgetting/staleness — پر کردن G3/G6.
3. **Lite workload benchmark:** تک سناریوی پشتیبانی (مسیریابی → جستجو → اسناد → پاسخ) روی هر چهار فریم‌ورک با همان مدل/ابزار/معیار (تکرار aggregate ارقام پراکندهٔ فعلی).
4. **Registry hardening POC:** اسکن خودکار + امضای بسته + AI-BOM برای MCP Registry قبل از اجباری‌شدن.
5. **Local model small-vs-large frontier ladder:** آستانه دقیق «چه task محلی کافی است vs باید به groq/gemini رفت» به‌صورت تصمیم‌درختی مستند.
6. **Cost ledger reconciliation:** خودکارسازی تطبیق estimate (`image_generation_cost` مانند) با invoice واقعی provider.

---

## ۹) سوالات حل‌نشده

- Q1: آیا GraphRAG کیفیت استخراج فارسی/عربی را به همان خوبی انگلیسی حفظ می‌کند (افت کیفی استخراج کم‌منبع)؟ [UK]
- Q2: چه نسبت دقیق sampling trace هم debut و هم هزینه را بهینه می‌کند برای 1M call/day در trace-store های گوناگون؟ [UK]
- Q3: آیا مدل‌های 1-2M context «گم‌شدن در میانه» را بدون retriever حل می‌کنند یا فقط آن را گران‌تر پنهان می‌کنند؟ [PV — افت effective recall پس از 100k مشاهده شد]
- Q4: مرز canonical بین Celery/RQ/Dramatiq و Postgres `SKIP LOCKED` از نظر توان/هزینه در workload یکسانِ agent چقدر است؟ [UK — مطالعه head-to-head نیافتیم]

---

## ۱۰) پیشنهادهای قابل بررسی (فنی، غیرسلیقه‌ای، هر کدام trade-off صریح)

> **هیچ «معماری برتر نهایی» نمی‌دهیم (دستور). به‌جای آن سه گزینه:**

### OPTION A — «سنگر Monolith» (تکامل محافظه‌کارانهٔ وضع موجود)

- **چیست:** نگه‌داشتن گراف LangGraph + bus قابلیت Nagar فعلی + queue sidecar SQLite + sqlite-vec + LiteLLM fallback + guard deny-by-default. رشد تا ~10k کاربر را با مقیاس عمودی + PG Neon + R2 پوشش ده.
- **Trade-off مثبت:** صفر broker جدید → کمترین گسل عملیاتی؛ pure packs تست‌پذیر می‌ماند؛ مسیر `nexus smoke` حفظ؛ توضیح آسان برای agentهای موازی.
- **Trade-off منفی:** سفت‌بودن تک-نویسنده SQLite؛ تاییدهای ساعت/روز یا 100k+ کاربر → بن‌بست قانون‌گذارانه (نیازمند ADR).
- **پیش‌نیاز:** بستن task-124 (ONE-OWNER wiring + memory write)، افزودن صادرکننده OTel اختیاری، تفکیک صف اولویت بین chat vs render قبل از اشباع.
- **شواهد:** Stage 7/10 — monolith تا ~1k tps بدون broker توجیه اقتصادی دارد. [VERIFIED]

### OPTION B — «گراف با مرزهای پایدار» (Monolith + هسته پایدار جداگانه برای long-running)

- **چیست:** گراف فعلی را نگه‌دار؛ فقط workflows نیازمند ساعت/روز (تایید انسانی + جبران) را به یک تکه Temporal جدا (یا گراف LangGraph با PostgresSaver پایدار) برون‌سپاری کن. بقیه روی monolith.
- **Trade-off مثبت:** بدون بازنویسی همه؛ Temporal فقط جایی که replay/history ارزش دارد؛ استرس K/F/CC را تسکین می‌دهد بی‌آنکه قرارداد monolith شکسته شود.
- **Trade-off منفی:** دو مدل اجرا (graph vs workflow)؛ deterministic sandbox Temporal؛ نیاز به collector/DB اضافی برای آن تکه.
- **پیش‌نیاز:** تعریف مرز service (کدام workflow واقعاً long) + transactional outbox بین monolith و Temporal + آستانه SLO برای مهاجرت workload.
- **شواهد:** Markaicode/SuhasBhairav — Celery برای stateless کوتاه، Temporal برای stateful بلند. [VERIFIED]

### OPTION C — «بستر event برای fleet» (آماده‌سازی برای مقیاس شبکه‌ای)

- **چیست:** لایه event (Redis Streams/Kafka) به‌عنوان bus اصلی + MCP stateless (2026-07-28 LB) + A2A برای hand-off بین agentهای ناهمگون + Temporal برای sagaهای بلند. هر agent مصرف‌کننده رویداد مستقل.
- **Trade-off مثبت:** مقیاس‌پذیرترین، replay/audit کامل event log، پیوندپذیری بین فریم‌ورک‌ها (MCP=ابزار، A2A=عامل).
- **Trade-off منفی:** پیچیدگی عملیاتی بسیار بالا، اشکال‌یابی پراکنده، به بلوغ تیم و نیاز واقعی به fleet نیاز دارد؛ برای <10k کاربر افزونه زودهنگام.
- **پیش‌نیاز:** نیازمندی اثبات‌شده به fleet (سناریو C/D/L در Stage 10) + ADR برای لغو constraint monolith + تیمی که توان نگه‌داری Kafka/Temporal را دارد.
- **شواهد:** Agentic Infra Landscape 2025-26 — supervisor/sequential تولیدی، swarm پژوهشی؛ event-driven بیشترین توان ولی نیاز به governance لایه‌ای. [VERIFIED]

**توصیه آزمون تصمیم:** فقط وقتی به Option B بروید که workloadی دارید که (a) بیش از دقایق زنده است و (b) نیازمند replay بعد از crash یا تایید انسانی با جبران است؛ فقط وقتی به Option C بروید که (a) >10k کاربر همزمان فعال و (b) fleet چندفریم‌ورکی و (c) ظرفیت نگه‌داری event mesh اثبات‌شده دارید. در غیر این صورت Option A را نگه‌دارید — زود fleet شدن گران‌تر از دیر fleet شدن است (تکرار یافته Stage 2/7).

---

## وضعیت هر 10 مرحله (QC Summary)

| مرحله | فایل | STATUS | CONFIDENCE | منبع (primary+indep) | یافته کلیدی | سوال باز |
|-------|------|--------|------------|----------------------|-------------|---------|
| 1 | 01-technology-landscape.md | COMPLETE | HIGH | 10 primary + 7 indep | 4 ستون 2026 + 12 فناوری ارزیابی‌شده | pricing provider volatile |
| 2 | 02-agent-architectures.md | COMPLETE | HIGH | 4 primary + 8 indep | 5 شِکل + ماتریس 14 بُعدی | granularity گراف / trigger replan |
| 3 | 03-capability-registry.md | COMPLETE | HIGH | 5 primary + 4 indep | MCP stateless + Registry متا + 7 پیشنهاد طراحی | WASM ext, TTL optimum |
| 4 | 04-security.md | COMPLETE | HIGH | 7 primary + 5 indep | 12 تهدید + residual HIGH برای indirect/memory | تشخیص stego |
| 5 | 05-memory-rag.md | COMPLETE | MEDIUM-HIGH | 2 primary + 6 indep | Hybrid پیش‌فرض + GraphRAG دامنه‌ای | فارسی hybrid، forgetting benchmark |
| 6 | 06-local-first-ai.md | COMPLETE | HIGH | 3 primary + 3 indep | GGUF/Ollama air-gap + fallback tiered | WebGPU vs native gap |
| 7 | 07-jobs-concurrency.md | COMPLETE | HIGH | 8 primary + 4 indep | 9 سیستم + exactly-once layers + idempotency | PG vs Temporal head-to-head نیافتیم |
| 8 | 08-observability-evaluation.md | COMPLETE | HIGH | 3 primary + 5 indep | OTel GenAI + 6 KPI + framework 4-suite | goal completion metric ذهنی |
| 9 | 09-open-source-landscape.md | COMPLETE | HIGH | 6 primary + 10 indep | 19 پروژه (15+ الزام) بی‌رتبه | stars خوداظهاری |
| 10 | 10-future-stress-test.md | COMPLETE | MEDIUM-HIGH | 3 primary + Stages 1-9 | 12 سناریو + pressure map + constitutional breakpoint | نیاز به بنچمارک سخت‌افزاری واقعی |

**سندیت و کنترل کیفیت:**
- ادعاهای بدون منبع علامت‌گذاری شد (UNKNOWN/UNVERIFIED) — نه پر شده با حدس.
- Broken sources: ندارد (هر URL در 2026-08 زنده؛ MCP gist نمونه spec آرشیوی بود ولی cross-valid با blog رسمی 2026-07-28 — علامت‌گذاری شد).
- Duplicate findings حذف: Graph ماتریس تکرار 01 و 02 یکپارچه شد در stress test.
- تناقض‌ها ثبت: `exactly-once` مارکتینگ vs docs — جدول نقض؛ GraphRAG gains دامنه‌ای — هر دو سو گزارش شد.
- Fact / Analysis / Opinion تفکیک: توصیف اسپِک = fact؛ استنباط «مقیاس تا 1k کاربر» = analysis؛ پیشنهاد Option A/B/C = opinion مستند.
- تاریخ منابع بررسی: امتیاز به منابع 2025-2026 داده شد، 2023-24 فقط برای timeline تغییرات 2025.
- Nagar لمس نشد: تمام ارجاعات NEXUS فقط READ-ONLY + تگ OBSERVATION؛ هیچ فایل `src/`، `docs/` Nagar، یا dependency تغییر نکرد.

---

## فهرست کامل deliverable

```
research/
  01-technology-landscape.md
  02-agent-architectures.md
  03-capability-registry.md
  04-security.md
  05-memory-rag.md
  06-local-first-ai.md
  07-jobs-concurrency.md
  08-observability-evaluation.md
  09-open-source-landscape.md
  10-future-stress-test.md
  tools/README.md
RESEARCH_EXECUTIVE_SUMMARY.md  (این فایل — ریشه)
```

---

## نتیجه‌گیری امانت‌دارانه

این بسته **intelligence layer مستقل** است — برای **تصمیم‌های آینده** سایر عامل‌ها، نه برای merge امروز. هر ادعا با منبع **primary + مستقل** سنجیده شد؛ هر شکاف با برچسب **unknown** روشن شد؛ هر گزینه با **trade-off** صریح نه با «برتری مطلق» آمد.

**RESEARCH COMPLETE** — 10/10 مرحله با اعتماد HIGH/MEDIUM، 40+ منبع یکتا (primary + مستقل)، بدون لمس Nagar یا branch دیگران، آماده برای استفاده معماری.

