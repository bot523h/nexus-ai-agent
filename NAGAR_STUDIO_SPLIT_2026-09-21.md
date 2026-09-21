# تقسیم دو نیمه‌ی استودیوی نگار — تخته‌ی سیستم‌قید دوعاملی (A / B)

> **مالک سند:** عامل A — شاخه‌ی `arena/01a0c625-nexus-ai-agent` (نام شاخه = هویت قطعی، طبق منشور `AGENTS.md`).
> **قید مالک (Binding constraint):** «من عامل A هستم؛ عامل دیگر B است؛ هرکس وظایف خودش را به بهترین شکل ممکن به اتمام برساند.»
> **وضعیت:** روی تخته (`.agents/board.json`) ثبت شد — کارت‌های `nagar-half-a-semantics` (فعال، مالِ A) و
> `nagar-half-b-surface` (صندوق رزرو برای B). قانون ۱ تخته رعایت شد: **claim پیش از اولین خط کد، کامیت و پوش شد.**
> **تاریخ:** ۲۰۲۶-۰۹-۲۱ · **منشور حاکم:** `AGENTS.md` (Pro-Max Zero-Collision Parallel Network)

---

## ۰. چرا این تقسیم؟ (بیان مسئله)

«کارهای نیمه‌تمام و زیرساخت استودیوی نگار» (فاز ۶ نقشه‌ی راه) باید **به‌طور کامل و بدون حتی یک میلی‌متر تداخل** بین دو عامل
تقسیم شود. کالبدشکافی فورنزیک مخزن در ۲۰۲۶-۰۹-۲۱ نشان داد استودیوی نگار این وضعیت را دارد:

| ردیف | قلم (TDD نگار) | وضعیت واقعی روی `main` (`b422512`) |
|---|---|---|
| ۱ | هسته‌ی سبز (فرمان‌باس، رجیستری، ارجاعات زمانی، undo) | ✅ موج ۱–۲c، ۲.۵، ۳ (۵۱ عملیات ثبت‌شده) |
| ۲ | پک slideshow / edit / audio / motion / caption / delivery | ✅ فعال؛ ۶-op شکاف TDD با `task-125` بسته شد |
| ۳ | رجیستری runtime یکپارچه (همه‌ی پک‌ها `pending=0`) | ⏳ **در پرواز:** PR#46 (MERGEABLE، CI سبز) |
| ۴ | لاین رندر/نوردهی | ⏳ **در پرواز:** PR#45 |
| ۵ | گیت اجاره‌ی `active_in_review` | ⏳ **در پرواز:** PR#44 |
| ۶ | سخت‌سازی storage | ⏳ **در پرواز:** PR#43 |
| ۷ | **`nexus.vision.scene` (۱۰ op — TDD §۱.۴)** | ❌ **اصلاً وجود ندارد** |
| ۸ | **`nexus.vision.portrait` (۱۰ op — TDD §۱.۳)** | ❌ **اصلاً وجود ندارد** |
| ۹ | **ارجاعات معنایی (SubjectRef/MaskRef/… — TDD «انواع مشترک»)** | ❌ **زیرساخت مفقود** |
| ۱۰ | مارکرهای OTIO (`task-121`) | ❌ نیمه‌تمام (تولد `include_markers` بدون emission) |
| ۱۱ | سطح خلاقیت تلگرام (`task-106-rescope`) | ❌ نیمه‌تمام (mapper هست؛ dispatch نهایی نه) |
| ۱۲ | ران‌بوک عملیات (موج۴-قدم۸) | ❌ نیمه‌تمام (۱۰۱ خط؛ ناقص) |
| ۱۳ | دودکش E2E (موج۴-قدم۹) | ❌ نیمه‌تمام (stub؛ نیمه‌ی compose توالی‌یافته) |

**نتیجه:** استودیو «مغزِ فرمان‌محور» را دارد اما «چشم» (بینایی/معنا) و «دستِ تحویل‌دهنده» (سطح/OTIO/عملیات) نیمه‌کاره‌اند.
همین دوگانگی — که خود TDD در بخش «سه مسیر اجرا: Preview / Analysis / Master» بر آن تأیید می‌زند — **خط گسلِ تقسیم** است.

---

## ۱. خط گسل معماری (Fault Line)

```text
                    ┌────────────── استودیوی نگار ──────────────┐
                    │                                          │
   نیمه‌ی A ────────┤   خطِ خالص / معنایی (Analysis)             │
   «عقل و چشم»     │   مدل‌های تایپ‌شده · reducerهای دترمینیستیک │
                    │   ارجاعات معنایی · صفر I/O · stdlib+pydantic│
                    │   studio/semantic.py + packs/{scene,portrait}
                    │                                          │
   نیمه‌ی B ────────┤   خطِ ناخالص / اجرایی (Master & Surface)  │
   «دست و تحویل»   │   کتابخانه‌ها · انکدرها · تلگرام · OTIO     │
                    │   Docker · ران‌بوک · smoke                 │
                    │   packs/delivery + bot/creative_surface    │
                    │   + scripts/smoke_e2e + docs/ops           │
                    └──────────────────────────────────────────┘
```

هر دو نیمه **یک اسلایس عمودی کامل**‌اند: هرکدام هم «کار نیمه‌تمام» دارد هم «زیرساخت». اما:

1. **هیچ فایلی مالک دوتایی ندارد** (جدول §۳ را ببینید — اشتراک مجموعه‌ی مسیرها ∅ است).
2. **هیچ ایمپورتی بین دو نیمه برقرار نیست** — نه A چیزی از B می‌خواند، نه B از A. تنها رابط مشترک،
   زیرساختِ منجمدِ `creative/studio` و `creative/packs` (manifest/verify/registry) است که **هر دو فقط می‌خوانند**.
3. **فایل مشترک تنها** `.agents/board.json` است — رسانه‌ی هماهنگی همه — که با قاعده‌ی `AGENTS.md`
   («آخرین وضعیت فورنزیک برنده است») حل تعارض می‌شود و blob بازاعمال آن در §۹ همین سند است.

---

## ۲. صندلی‌ها و هویت

| صندلی | هویت (برچسب راحتی) | شاخه‌ی قانونی | کارت تخته | وضعیت |
|---|---|---|---|---|
| **A** | عامل A — نیمه‌ی خالص/معنایی | `arena/01a0c625-nexus-ai-agent` | `nagar-half-a-semantics` | **فعال — در حال اجرا** |
| **B** | عامل B — نیمه‌ی ناخالص/اجرایی | هنگام نشستن اعلام می‌شود | `nagar-half-b-surface` | **رزروشده — claim هنگام شروع** |

**پروتکل نشستنِ عامل B (ترتیب اجباری):**
```bash
python scripts/agent_board.py show
python scripts/agent_board.py claim nagar-half-b-surface --branch <arena/…-nexus-ai-agent>
git add .agents/board.json && git commit -m "board: seat B claims nagar-half-b-surface" && git push  # بلافاصله
python scripts/agent_board.py check --files <changed> --branch <your-branch>   # پیش از هر push؛ خروجی ۰
```

---

## ۳. نقشه‌ی فایلی — مالکیت انحصاری (صفر اشتراک)

### نیمه‌ی A — `nagar-half-a-semantics` (عامل A)

| مسیر انحصاری | نقش |
|---|---|
| `src/nexus_ai_agent/creative/studio/semantic.py` | **زیرساخت:** ارجاعات معنایی TDD (SubjectRef, MaskRef, ClipRef, StateDelta, AlphaLayerRef, FaceTrackSet, ObjectTrack, ShotBoundarySet, CandidateMomentSet, TransformCurve) |
| `src/nexus_ai_agent/creative/packs/scene/` | پک `nexus.vision.scene` — ۱۰ op خالص (models + operations + manifest) |
| `src/nexus_ai_agent/creative/packs/portrait/` | پک `nexus.vision.portrait` — ۱۰ op خالص با ایمنی هویت |
| `src/nexus_ai_agent/creative/packs/vision_composition.py` | سازنده‌های رجیستری + قلاب یکپارچه‌سازی H1 |
| `tests/unit/test_semantic_refs.py` | تست‌های مرزی زیرساخت معنایی |
| `tests/unit/test_scene_pack.py` | تست‌های خالص ۱۰ op صحنه |
| `tests/unit/test_portrait_pack.py` | تست‌های خالص ۱۰ op پرتره + ایمنی هویت |
| `tests/unit/test_vision_composition.py` | لِیونِس manifest↔registry + فعال‌سازی |
| `tests/architecture/test_vision_pack_boundary.py` | گارد معماری (بدون وابستگی سنگین، بدون عبور بسته) |
| `NAGAR_STUDIO_SPLIT_2026-09-21.md` | همین سند |
| `NAGAR_HALF_A_REASONING_FA.md` | سند استدلال خط‌به‌خط نیمه‌ی A |

### نیمه‌ی B — `nagar-half-b-surface` (عامل B)

| مسیر انحصاری | نقش |
|---|---|
| `src/nexus_ai_agent/creative/packs/delivery/` | تسک ۱۲۱: مدل `OtioMarker` + emission مارکرها در `export_otio` |
| `src/nexus_ai_agent/bot/creative_surface.py` | تسک ۱۰۶-rescope: تکمیل dispatch سطح خلاقیت |
| `tests/unit/test_creative_surface.py` | تست‌های سطح |
| `tests/unit/test_otio_interop.py` | round-trip واقعی OTIO برای مارکرها |
| `tests/unit/test_smoke_e2e_contract.py` | تست قرارداد دودکش |
| `scripts/smoke_e2e.py` | تکمیل دودکش E2E (نیمه‌ی compose توالی‌یافته) |
| `docs/ops/RUNBOOK_HARDENING.md` | تکمیل ران‌بوک عملیات |
| `docs/ops/NAGAR_OPERATIONS_FA.md` | ران‌بوک فارسی عملیات استودیو |
| `NAGAR_HALF_B_REASONING_FA.md` | سند استدلال خط‌به‌خط نیمه‌ی B |

### مشترکِ پروتکلی (تنها نقطه‌ی نوشتنِ مشترک)

| فایل | قاعده‌ی حل تعارض |
|---|---|
| `.agents/board.json` | «آخرین وضعیت فورنزیک برنده است» (`AGENTS.md`) + بازاعمال blob بند §۹ |

### منجمد برای هر دو نیمه (فقط خواندنی)

`creative/studio/{models,capabilities,bus,references}.py` · `creative/packs/{manifest,verify,registry}.py` ·
هر فایلی که اجاره‌ی فعالِ سایر پری‌ها دارد (§۴).

---

## ۴. حصار کارهای در پرواز (Visibility — درسِ void-claims)

ادعای push‌نشده وجود ندارد؛ اما **PRِ دیده‌نشده نیز تصادف آینده است** (ماجرای `arena/ifk3hadg`). برای همین چهار کارت
ثبتی-حصاری (`active` + مسیرهای انحصاری) روی تخته نوشته شد تا `agent_board check` مکانیکی هر دو نیمه را از فایل‌های
در پرواز دور نگه دارد:

| کارت ثبتی | شاخه | فایل‌های حصارشده | آزادسازی |
|---|---|---|---|
| `pr46-in-review-wave5` | `arena/01a0c5da` | `packs/runtime.py`, `packs/registry.py`, `packs/{audio,edit,motion}/`, `cli.py`, `continuum/pack_coverage.py`, `scripts/pack_coverage.py`, تست‌ها و داک‌هایش | پس از merge #46 |
| `pr45-in-review-color-lane` | `arena/01a0c58e` | `creative/rendering/`, تست‌ها و `docs/ops/COLOR_LANE.md` | پس از merge #45 |
| `pr44-in-review-board-lease` | `arena/01a0c593` | `scripts/agent_board.py`, تستش | پس از merge #44 |
| `pr43-in-review-storage` | `arena/01a0c58a` | `storage/resilience.py`, تستش | پس از merge #43 |

کارت‌های قدیمی `feature-wiring-batch` (PR#32)، `pr33-in-review` (PR#33) و `pr39-in-review-docs` (PR#39) نیز همچنان
برقرارند؛ PR#33 مالکِ معنوی بسته‌بندی/OTIO است → قید توالی H2 (§۵).

---

## ۵. هانک‌های یکپارچه‌سازیِ توالی‌یافته (تنها تماس‌های آینده با فایل‌های داغ)

| هانک | مالک | چه زمانی | دقیقاً چه چیزی | سقف |
|---|---|---|---|---|
| **H1** | A | پس از merge شدن **PR#46** | افزودن دو ورودی به `COMPOSITION` در `creative/packs/runtime.py` با فراخوانی `register_scene_operations` / `register_portrait_operations` | ≤ ۱۲ خط؛ اثبات `merge-tree`؛ سپس `nexus packs list` → ۸ پک، `pending=0` |
| **H2** | B | پس از merge شدن **PR#33** | emission مارکرها در `export_otio` (فیلد `include_markers`) + نیمه‌ی compose در `scripts/smoke_e2e.py` | ناحیه‌ای؛ طبق `task-121` و `task-130` |

تا قبل از این دو merge، نتیجه‌ی صادقانه این است که `nexus packs list` دو پکِ بینایی را با `pending` نشان دهد —
این **رفتار درستِ** مدلِ فعال‌سازی است (رجیستری manifest اول، فعال‌سازی بعد از شناخت runtime)، نه باگ.

---

## ۶. قیدهای سیستم‌قید (S1–S12) — الزامی برای هر دو نیمه

| # | قید |
|---|---|
| **S1** | نام شاخه هویت قطعی است. A = `arena/01a0c625-nexus-ai-agent`؛ B = صندلی‌ای که `nagar-half-b-surface` را claim کند. |
| **S2** | **صفر میلی‌متر تداخل:** هیچ ویرایشی در `exclusive_paths` طرف مقابل. `python scripts/agent_board.py check --files … --branch …` پیش از **هر** push باید خروجی ۰ دهد. |
| **S3** | **انجماد قرارداد رابط:** سطح عمومی §۷ ثابت است؛ هیچ نیمه‌ای ماژول نیمه‌ی دیگر را import نمی‌کند. |
| **S4** | فایل‌های در پرواز §۴ تا merge منجمد؛ فقط هانک‌های ناحیه‌ای H1/H2 با توالی صریح. |
| **S5** | **تک‌مالک گیت‌ها:** `gates_owner` همچنان `ci-gates-steward` (`01a0c460`). دو نیمه فقط diagnostics (ruff/mypy/pytest هدفمند) اجرا می‌کنند و در بدنه‌ی PR می‌نویسند: *deferred to gates owner*. |
| **S6** | اجاره ۲۴ ساعته؛ تمدید با re-claim (heartbeat)؛ پایان زودهنگام = `release`؛ مسدود = `defer` با یادداشت فارسی. |
| **S7** | **صفر رگرسیون:** مجموعه‌ی تست موجود سبز می‌ماند؛ هیچ تغییر رفتاری در API پک‌های فعال. |
| **S8** | بدون وابستگی runtime جدید. نیمه‌ی خالص: فقط stdlib+pydantic. نیمه‌ی ناخالص: فقط کتابخانه‌های موجود (OTIO در dev-extra). |
| **S9** | **ایمنی هویت (TDD §۱.۳):** `portrait.correct_gaze` و `scene.remove_logo` سطح C‌اند (بدون `confirmed` → رد). هر op چهره `confidence` و `provenance` ثبت می‌کند. |
| **S10** | مانیفست‌ها data-only (`nexus.capability-pack.v1`)؛ capability داخل namespace بسته (`nexus.vision.portrait` → `portrait.*`، `nexus.vision.scene` → `scene.*`)؛ بدون کلید اجرایی. |
| **S11** | هر handler خالص و دترمینیستیک؛ خروجیِ مشتق content-addressed (`sha256` از والد + پارامترها)؛ منبع اصلی هرگز mutate نمی‌شود. |
| **S12** | پس از تکمیل هر نیمه: ۱۰ کار بعدی روی تخته + شواهد اندازه‌گیری‌شده (نه ادعایی). |

---

## ۷. قرارداد رابط مشترک (منجمد — تنها درز بین دو نیمه)

هر دو نیمه زیرساخت زیر را **فقط می‌خوانند** (ویرایش ممنوع):
`creative.studio.{models,capabilities,bus,references}` · `creative.packs.{manifest,verify,registry}`.

**سطح عمومی نیمه‌ی A (برای B منجمد — B هرگز به آن‌ها import نمی‌زند):**
```text
nexus_ai_agent.creative.studio.semantic          → SubjectRef, MaskRef, ClipRef, StateDelta, AlphaLayerRef,
                                                   FaceObservation, FaceTrack, FaceTrackSet,
                                                   ObjectTrackPoint, ObjectTrack,
                                                   ShotBoundary, ShotBoundarySet,
                                                   CandidateMoment, CandidateMomentSet,
                                                   TransformKey, TransformCurve, LightModel
nexus_ai_agent.creative.packs.scene.operations   → build_scene_registry(),  register_scene_operations()
nexus_ai_agent.creative.packs.portrait.operations → build_portrait_registry(), register_portrait_operations()
nexus_ai_agent.creative.packs.vision_composition → build_vision_registry(), register_vision_operations()
```

**سطح عمومی نیمه‌ی B (برای A منجمد — A هرگز به آن‌ها import نمی‌زند):**
```text
creative/packs/delivery            → OtioMarker + قرارداد emission مارکرها در export_otio
bot/creative_surface               → CreativeSurfaceMapper.map(), .job_payload(), build_creative_handlers()
scripts/smoke_e2e                  → offline_checks()
```

الگوی سازگار با بقیه‌ی پک‌ها (که هر دو نیمه باید رعایت کنند): `build_<pack>_registry() -> CapabilityRegistry`
(شامل `build_wave1_registry()` + `register_<pack>_operations(registry)`).

---

## ۸. وظایف و معیارهای پذیرش

### نیمه‌ی A (۶ وظیفه)

| # | وظیفه | معیار پذیرش |
|---|---|---|
| **A1** | زیرساخت `semantic.py` | مدل‌های «انواع مشترک» TDD + خروجی‌های بینایی؛ فقط stdlib+pydantic؛ تست مرزی confidence و بازه |
| **A2** | پک `nexus.vision.scene` (۱۰ op) | ثبت ۱۰ op دترمینیستیک؛ digest محتوایی؛ `scene.remove_logo` سطح C (تست رد بدون تأیید) |
| **A3** | پک `nexus.vision.portrait` (۱۰ op) | ثبت ۱۰ op؛ confidence+provenance در همه؛ `correct_gaze` سطح C؛ تست ریاضی smoothing |
| **A4** | `vision_composition.py` + قلاب H1 | اجتماع دقیق ۲۰ op بدون تداخل؛ فعال‌سازی واقعی با `PackRegistry`؛ H1 مستند/توالی‌یافته |
| **A5** | ۵ فایل تست + گارد معماری | سبز؛ گارد boundary؛ لِیونِس manifest↔registry |
| **A6** | سند تقسیم + استدلال خط‌به‌خط | فارسی کامل؛ هر تصمیم استدلال‌شده؛ شواهد صفر تداخل |

### نیمه‌ی B (۵ وظیفه)

| # | وظیفه | معیار پذیرش |
|---|---|---|
| **B1** | مارکرهای OTIO (`task-121`) | `include_markers=False` → صفر مارکر (تست)؛ round-trip واقعی نام/فریم/رنگ؛ صفر رگرسیون |
| **B2** | تکمیل سطح خلاقیت (`task-106-rescope`) | `/edit` `/caption` `/grade` از `JobQueuePort`؛ شکست‌های تایپ‌شده؛ صفر لمس `handlers.py` |
| **B3** | تکمیل ران‌بوک (موج۴-قدم۸) | fa/en کامل؛ smoke با exit-code |
| **B4** | تکمیل دودکش E2E (موج۴-قدم۹) | نیمه‌ی offline سبز؛ نیمه‌ی compose توالی‌یافته H2؛ تست قرارداد |
| **B5** | سند استدلال خط‌به‌خط | فارسی، سطح خط، شواهد اندازه‌گیری‌شده |

---

## ۹. پروتکل اثبات «صفر میلی‌متر» (اندازه‌گیری، نه ادعا)

پیش از هر push و در پایان هر نیمه، هر چهار سطر باید اجرا و خروجی‌شان ثبت شود:

```bash
# ۱) داور تخته — مثبت: با شاخه‌ی خودت → خروجی ۰
python scripts/agent_board.py check --files <changed,comma,separated> --branch <you>

# ۲) کنترل منفی — بدون شاخه → باید خروجی ۱ (یعنی ناحیه واقعاً حصار دارد)
python scripts/agent_board.py check --files <changed> --branch ""

# ۳) ادغام سه‌طرفه‌ی واقعی بین دو نیمه → بدون تعارض
git merge-tree --write-tree <half-a-tip> <half-b-tip>

# ۴) اشتراک مجموعه‌ی مسیرهای انحصاری → ∅ (خود تخته مرجع است)
```

### بازاعمال blob برای `.agents/board.json` (قاعده‌ی حل تعارض «آخرین وضعیت فورنزیک»)

اگر تعارض board.json رخ داد، این دو claim باید در نسخه‌ی ادغام‌شده زنده بمانند (از `nagar_two_half_split` و
`claims` همین تاریخ). خلاصه‌ی بازاعمال: `nagar-half-a-semantics` (فعال، `arena/01a0c625-nexus-ai-agent`،
۱۱ مسیر انحصاری) و `nagar-half-b-surface` (صندوق رزرو، ۹ مسیر انحصاری) به‌همراه چهار claim ثبتی
`pr4x-in-review-*`. نسخه‌ی کامل ماشینی در شیء `nagar_two_half_split` همان commit است.

---

## ۱۰. شبکه‌ی ۱۰ کار بعدی (پس از این تقسیم)

1. **H1 (A، پس از #46):** قلاب `COMPOSITION` دو پک بینایی → `nexus packs list` ۸ پک `pending=0`.
2. **H2 (B، پس از #33):** emission مارکرهای OTIO + نیمه‌ی compose دودکش.
3. **پس از H1:** `nexus vision explain` (کاتالوگ کشف‌پذیر برای agent — خروجی JSON از OperationSpecها).
4. **فاز اجرایی بینایی:** آداپتورهای ONNX پشت پورت‌های تحلیلی (الگوی `CaptionEnginePort`) — `scene.segment_subject` و `portrait.detect_landmarks` اول.
5. **لاین اعمال بینایی:** نگاشت `EffectLayerRef`های پرتره/صحنه به filtergraph (پس از #45).
6. **Intent AST (TDD §۲.۲):** متن → Candidate Intent → Typed Command با schema+confidence — زیرساخت جامانده‌ی اصلی.
7. **سازگاری پک:** gate نصب (`min_nagar_version`, ABI) با امضای ed25519 مانیفست (ادامه‌ی `delivery/signing.py`).
8. **کاورژ mutation سبک** روی reducerهای جدید بینایی (الگوی `pack_coverage.py` پس از #46).
9. **۱۰-op باقی‌مانده‌ی TDD** (بسته به شمارش نهایی §۱.۵–۱.۷) — اسلایس خالص بعدی.
10. **Preview lane (WebCodecs/WebGPU)** — مسیر سوم TDD؛ پس از تثبیت Analysis و Master.

---

## ۱۱. ترتیب ادغام پیشنهادی

1. ادغام PRهای کوچک سبز (#43, #44, #45) — هرکدام مستقل.
2. ادغام PR#46 (draft → آماده پس از ثبت gates owner) — مبنای مشترک دو نیمه.
3. **نیمه‌ی A** (این سشن) روی main جدید: H1 کوچک، سپس PR.
4. **نیمه‌ی B** پس از claim: B1–B5؛ H2 پس از ادغام/تعیین تکلیف PR#33.
5. هرگز #32 و #33 را بدون rebase پشت‌سرهم ادغام نکن (قاعده‌ی E#1 — PRهای هم‌پوشان).

---

*سند مهندسی‌شده توسط عامل A (`arena/01a0c625-nexus-ai-agent`) — ۲۰۲۶-۰۹-۲۱.
همراه این سند: `NAGAR_HALF_A_REASONING_FA.md` (استدلال خط‌به‌خط پیاده‌سازی نیمه‌ی A).*
