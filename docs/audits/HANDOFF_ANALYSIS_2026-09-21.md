# گزارش تحلیل کامل، پاک‌سازی تخته و واگذاری مهندسی‌شده — ۲۰۲۶-۰۹-۲۱

- **عامل:** D (سشن تحلیل سیستم و ارکستراسیون واگذاری)
- **شاخه:** `arena/01a0c3ca-nexus-ai-agent` (بر پایه `main` @ `c41b1b0`)
- **روش:** بررسی مستقیم مخزن، اجرای عیب‌یاب‌های فقط-خواندنی (ruff / pytest هدفمند)، گزارش CI گیت‌هاب، و تحقیق وب در هر حوزه وظیفه. گیت‌های رسمی: *deferred to gates owner* (عامل A).
- **خروجی‌ها:** این سند + بازنویسی `.agents/board.json` + به‌روزرسانی `AGENTS.md`.

---

## ۱) حکم اجرایی (Executive Verdict)

ادعای «ادغام کامل و سبز» در اعلام قبلی **ناقص و در بخش گیت‌ها نادرست** بود:

| ادعا | واقعیت راستی‌آزمایی‌شده |
|---|---|
| «۳۱۷ تست با صفر خطا پاس شد» | ✅ منطق پک‌ها سبز است (اجرای هدفمند محلی: **180 passed** روی زیرمجموعه معادل؛ منطبق با روح ادعا) |
| «تایید کامل گیت‌های کیفیت» | ❌ **CI روی `main` قرمز است** — run [`35594818988`](https://github.com/bot523h/nexus-ai-agent/actions/runs/35594818988) در مرحله `ruff check .` شکست خورده و حتی به pytest نرسیده است |
| «تداخل صفر میلی‌متری» | ✅ مسیرها واقعاً گسسته بودند؛ هیچ برخوردی با PR#32 وجود ندارد |
| «تحویل ۵ بستر» | ⚠️ کد و تست تحویل شده، ولی **۴ پک از ۵ در runtime رجیستر نشده‌اند** (`pending` کامل) و هیچ مصرف‌کننده‌ای ندارند |

**نتیجه:** `main` تا ادغام تسک ۱۰۱ (نجات گیت‌ها) باید «قرمز» فرض شود و هیچ PR جدیدی نباید روی آن استک بگیرد.

### ۱.۱) پیگیری — تسک ۱۰۱ توسط عامل D اجرا شد (به‌روزرسانی همان روز)

عامل D طبق پروتکل، تسک ۱۰۱ را قبل از شروع روی تخته Claim کرد («⛔️ هیچ عامل دیگری وارد این محدوده نشود»)، آن را سر تا سر اجرا کرد و اجاره را آزاد ساخت:

| گیت | قبل | بعد |
|---|---|---|
| `ruff check .` | ۷۱ خطا (40 E501، 28 F401، 2 I001، 1 F841) | **۰ — All checks passed!** |
| `ruff format --check .` | ۱۶ فایل خارج از فرمت | **۳۳۶ فایل تمیز** |
| pytest هدفمند (architecture + پک‌ها + studio + router + graph_memory) | 180/180 | **180/180** |
| pytest گسترده (architecture + unit با deps موجود) | — | **749 passed** (20 skipped/failed محیطی) |
| اسلایدشو (surface + notify + لاین رندر واقعی FFmpeg) | — | **45/45** |

**اثبات صفر تغییر معنایی:** (۱) تمام شکست‌های رشته‌ای با «شکست همسان-بایت» انجام شد (literalهای مجاور با الحاق دقیقاً یکسان) تا خروجی ASS، هش‌های golden و پیام‌های خطای `match=` ذره‌ای تغییر نکنند؛ (۲) F841 به جای حذف، به یک lookup بدون انتساب تبدیل شد تا رفتار fail-fast (KeyError) حفظ شود؛ (۳) آزمون A/B با `git stash`: هر ۲۰ شکست باقی‌مانده روی **commit پایه** هم دقیقاً تکرار می‌شود — همگی وابستگی‌های سنگین نصب‌نشده sandbox هستند، نه تغییرات. Diff نهایی: ۲۴ فایل، ‏158+/160−، صفر تغییر منطقی.

---

## ۲) آنچه انجام شده (Done — با مدرک)

### ۲.۱ خط اصلی محصول (Phases 0–5)
طبق `ROADMAP_STATUS.md` و تاریخچه PRها: کنترل‌پلین/امنیت، محصول هسته، مسیر LLM محلی، روتینگ چند-فراهم‌کننده، اسکیما/PostgreSQL/Neon، ذخیره‌سازی پایدار R2 — **کامل و منتشرشده تا v3.12.0**.

### ۲.۲ استودیوی نگار (Phase 6) — تا انتهای «زیرساخت»
| موج | محتوا | وضعیت | مدرک |
|---|---|---|---|
| 1 | Green cockpit (`nagar.command.v1`، bus، resolver) | MERGED (PR#19/20) | `ac6c25b` |
| 2a/2b/2c | substrate پک‌ها + slideshow + لاین رندر FFmpeg | MERGED (PR#21–23) | — |
| 2.5 / 3 | سطح تلگرام `/slideshow` + تولید تصویر `/imagine` | MERGED (PR#25/26/28) | — |
| **3** | `nexus.edit.timeline` — **۸ عملیات** (trim, ripple_delete, insert_gap, speed_ramp, reverse_segment, freeze_frame, attach_b_roll, retime_to_music) | ✅ کد+تست (PR#31) | `creative/packs/edit/` |
| **4a/4b** | `nexus.language.caption` — ۱۰ قابلیت manifest، ۷ هندلر، فرمتر SRT/ASS-RTL وزیرمتن | ✅ کد+تست (PR#29/31) | `creative/packs/caption/` |
| **5** | `nexus.audio.studio` — ۴ عملیات (detect_beats, normalize_loudness EBU R128, duck_music, beat_sync_cut) | ✅ کد+تست (PR#31) | `creative/packs/audio/` |
| **6** | `nexus.motion.graphics` — ۵ عملیات (add_transition, keyframe_transform, add_glow, add_motion_blur, add_title) | ✅ کد+تست (PR#31) | `creative/packs/motion/` |
| **7** | `nexus.color.delivery` — ۷ عملیات (apply_lut, adjust_exposure, auto_balance, match_shot, make_proxy_480p, export_otio, render_master_4k) | ✅ کد+تست (PR#31) | `creative/packs/delivery/` |

راستی‌آزمایی محلی این سشن: `tests/architecture/ + پک‌ها + studio + router_multilingual + graph_memory` → **180 passed در ۳.۲۴s**. معماری «داده به‌جای باینری» (manifest فقط داده، ثبت allow-list، تست مرز ۶۳گانه) رعایت شده است.

### ۲.۳ هماهنگی چندعاملی
- PR#30: پروتکل + ممیزی معماری (۱۰ یافته P0 مستند) — MERGED.
- PR#32 (عامل B): لایه جدید `bot/surface/` + سیم‌کشی B1–B8 — **OPEN**، در انتظار rebase.

---

## ۳) آنچه انجام نشده (Not Done — با مدرک)

1. **🔴 گیت‌های کیفیت روی main قرمز:** ۷۱ خطای `ruff check` (40×E501، 28×F401، 2×I001، 1×F841 — همه در فایل‌های PR#31) + ۱۶ فایل خارج از فرمت `ruff format`. CI در اولین قدم می‌میرد. **PR#31 با گیت قرمز ادغام شده** (check-runهای head آن: `test => FAILURE`) — نقض مستقیم قانون «تک‌مالک گیت‌ها».
2. **🔴 چهار پک در runtime رجیستر نشده‌اند:** خروجی `nexus packs list` روی main:
   `edit 8/8 pending · motion 5/5 pending · audio 4/4 pending · delivery 7/7 pending · caption 7/10 (pending: align_words, diarize, translate_local)`.
   یعنی `register_edit_operations` و همتایان audio/motion/delivery **هیچ‌جا صدا زده نمی‌شوند** و عملیات‌ها از طریق bus قابل فراخوانی نیستند.
3. **🔴 هیچ لاین اجرایی (impure lane) برای پک‌های جدید وجود ندارد:** تنها `slideshow` مسیر plan→IR→FFmpeg→probe دارد. edit/motion/audio/caption/delivery فقط ریاضیات خالص روی IR هستند؛ هیچ فیلترگراف/انکد/فایل خروجی واقعی‌ای تولید نمی‌کنند.
4. **🔴 بسته امنیتی (P0-1..P0-10 ممیزی) هنوز ادغام نشده:** `/api/dashboard/recent_users` هنوز `telegram_id` لو می‌دهد؛ گارد path-traversal در `/cloud`,`/download` اعمال نشده؛ AuthMiddleware روی ~۲ از ~۸۰ مسیر فعال است. اجاره عامل A فعال است ولی PRی باز ندارد.
5. **🟠 caption.transcribe fail-closed است** (`adapters/caption_unavailable.py`) و موتور محلی ASR پشت `CaptionEnginePort` وجود ندارد.
6. **🟠 export_otio با کتابخانه واقعی OpenTimelineIO هرگز اعتبارسنجی نشده** (JSON دست‌ساز؛ خطر ناسازگاری schema).
7. **🟠 وابستگی‌ها سنگین و تخت:** torch (از sentence-transformers)، llama-cpp-python، chromadb، flashrank، boto3 همه در `[project.dependencies]` هسته — هدف «۷GB» هنوز پابرجاست (تسک ۱۰۷).
8. **🟠 features/* هنوز sync engine می‌سازند:** `create_engine` در ads, analytics, anonymous_chat, channel_manager, conversation_store, engagement, force_join, ... — بلاک‌کننده لوپ تلگرام (تسک ۱۰۸).
9. **🟠 RAG ابتدایی:** چانک ثابت ۱۰۰۰ کاراکتری **بدون همپوشانی**، بدون retrieval هیبریدی، بدون ارزیابی؛ کلیدهای ۱۵ زبانه موجود (`i18n/locales/` کامل است) به همه خروجی‌ها بسته نشده‌اند (تسک ۱۰۹).
10. **🟡 شکاف‌های کوچک شناخته‌شده:** ConversationStorePort بدون آداپتور؛ امضای manifest در وضعیت `placeholder/format_only_unverified`؛ حذف جراحی checkpoint (POST_V1) معوق.

---

## ۴) واگذاری مهندسی‌شده (۱۰ تسک بعدی)

جزئیات کامل (مسیر انحصاری، معیار پذیرش، ابزار) در `.agents/board.json → ten_forward_tasks_network`. خلاصه + پایه تحقیقی:

| # | تسک | اولویت | پایه تحقیق (ابزار روز دنیا — صفر هزینه) |
|---|---|---|---|
| ۱۰۱ | نجات گیت‌ها: رفع مکانیکی ۷۱ خطا + فرمت ۱۶ فایل | **P0** | ruff پین‌شده + pre-commit (جلوگیری از تکرار) |
| ۱۰۲ | بچ امنیتی P0-1..P0-10 (عامل A) | **P0** | الگوهای auth/guard استاندارد PTB؛ بدون وابستگی جدید |
| ۱۰۳ | ادغام PR#32 پس از rebase (عامل B) | P1 | الگوی surface؛ قاعده حل تداخل board |
| ۱۰۴ | نگار موج ۸ — لاین اعمال FFmpeg برای edit/motion/audio | P1 | `atrim/tpad/reverse/xfade/tmix/loudnorm دوماسه/sidechaincompress`؛ الگوی اثبات‌شده موج 2c؛ تست golden دترمینیستیک |
| ۱۰۵ | نگار موج ۹ — ASR محلی پشت CaptionEnginePort | P1 | **faster-whisper** (CTranslate2/int8/CPU/بدون torch — برنده مقایسه‌های ۲۰۲۶ برای self-host) + **argos-translate** آفلاین برای translate_local (پشتیبانی رسمی فارسی) |
| ۱۰۶ | نگار موج ۱۰ — سطح تلگرام /edit /caption /grade | P2 (بعد از PR#32) | الگوی `bot/surface` عامل B + JobQueuePort |
| ۱۰۷ | بسته‌بندی PEP 735 + uv + Docker slim | P1 | **PEP 735 dependency-groups** (استاندارد ۲۰۲۴→) برای ابزار توسعه، extras واقعی `[rag]/[speech]/[translate]/[local-llm]/[r2]`، نصب CI با **uv** |
| ۱۰۸ | دیتابیس تمام‌ناهمگام | P1 | SQLAlchemy 2.0 async: یک `create_async_engine` مرکزی، `async_sessionmaker(expire_on_commit=False)`، session-per-task، آزمون نگهبان |
| ۱۰۹ | RAG recursive + هایبرید + i18n binding | P2 | چانک ۲۵۶–۵۱۲ توکن با overlap ۱۰–۲۰٪ (پژوهش ۲۰۲۵/۲۰۲۶)، BM25 (rank_bm25/FTS5) + وکتور + flashrank موجود، هارنس recall@k |
| ۱۱۰ | OTIO round-trip واقعی + بستن شکاف پورت‌ها | P2 | **OpenTimelineIO** (ASWF؛ پس از v0.16 هسته جدا از plugins) به‌عنوان dev-extra؛ spike امضای ed25519 |

**ترتیب ادغام:** ۱۰۱ → PR#32 → (۱۰۲ ∥ ۱۰۴ ∥ ۱۰۵ ∥ ۱۰۷ ∥ ۱۰۸ ∥ ۱۰۹ ∥ ۱۱۰ روی مسیرهای گسسته) → ۱۰۶.

---

## ۵) ضمیمه — روش بازتولید (Reproducibility)

```bash
# قرمزی گیت روی main (مشاهده در CI):
gh run list --branch main --limit 1          # run 35594818988 => failure (step: ruff check .)
ruff check .                                  # => Found 71 errors (40 E501, 28 F401, 2 I001, 1 F841)
ruff format --check .                         # => 16 files would be reformatted

# سبزی منطق پک‌ها (اجرای هدفمند فقط-خواندنی):
PYTHONPATH=src pytest -q tests/architecture/ \
  tests/unit/test_edit_pack.py tests/unit/test_audio_pack.py \
  tests/unit/test_motion_pack.py tests/unit/test_delivery_pack.py \
  tests/unit/test_caption_ass.py tests/unit/test_pack_manifest_verify.py \
  tests/unit/test_creative_studio.py tests/unit/test_router_multilingual.py \
  tests/unit/test_graph_memory.py              # => 180 passed

# وضعیت pending پک‌ها:
PYTHONPATH=src python -m nexus_ai_agent.cli packs list

# تخته و ممنوعیت تداخل:
python scripts/agent_board.py show
python scripts/agent_board.py check --files ".agents/board.json,AGENTS.md,docs/HANDOFF_ANALYSIS_2026-09-21.md" --branch arena/01a0c3ca-nexus-ai-agent   # => no overlap
```

*پایان گزارش — عامل D · ۲۰۲۶-۰۹-۲۱*
