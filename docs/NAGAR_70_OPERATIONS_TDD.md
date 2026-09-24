# Nagar — سند طراحی فنی کاتالوگ ۷۰ عملیات

**وضعیت:** Proposed Technical Design Document  
**تاریخ:** ۲۰ سپتامبر ۲۰۲۶  
**دامنه:** استودیوی Local-First، Agent-Operable و Non-destructive برای ویدئو، صدا و تصویر  
**اصل بنیادین:** Agent فقط Typed Command صادر می‌کند؛ هرگز مختصات UI را پیدا نمی‌کند و کلیک شبیه‌سازی‌شده انجام نمی‌دهد.

> این سند معماری هدف است، نه ادعای پیاده‌سازی‌شدن همه‌ی ۷۰ موتور. موج Green Cockpit باید ابتدا قرارداد، Registry، State و یک Executor کوچک و قابل‌تست را تثبیت کند؛ سپس Packها به‌صورت lazy و قابل‌حذف اضافه شوند.

---

## ۰. خلاصه تصمیم‌های معماری

1. **زمان مرجع، عدد صحیح میکروثانیه است:** `timecode_us` مرجع ذخیره‌سازی است. `frame_number` یک فیلد مشتق‌شده از `TimeBase` یا نقشه‌ی PTS/VFR است، نه جایگزین زمان.
2. **Timeline شبیه OTIO است، اما مدل Nagar canonical است:** شناسه‌های پایدار، `AssetRef`، `Clip`های non-destructive و Stateهای hash‌شده در هسته‌ی Nagar قرار می‌گیرند. OpenTimelineIO برای ساختار و interchange الهام‌بخش و Adapter خروجی است؛ منبع حقیقت داخلی نیست.
3. **Pack، کد دلخواه نیست:** Manifest فقط Executorهای declarative، مدل‌ها، hash، مجوزها و محدودیت منابع را معرفی می‌کند. هیچ `post_install`، shell command یا entrypoint آزاد مجاز نیست.
4. **سه مسیر اجرا وجود دارد:**
   - `Preview`: WebCodecs + WebGPU در Worker، با رزولوشن ۴۸۰p/۷۲۰p.
   - `Analysis`: ONNX Runtime Web برای مدل‌های سبک؛ مدل‌های سنگین در Runtime محلی.
   - `Master`: FFmpeg/native یا Runtime محلی، با Filter IR تولیدشده از Typed Command؛ نه با رشته‌ی shell که Agent ساخته باشد.
5. **Cloud در مسیر اصلی نیست:** دریافت اختیاری Pack یا به‌روزرسانی مدل می‌تواند شبکه‌ای باشد، اما ویدئو، صدای خام و اجرای Operation به‌صورت local انجام می‌شود.
6. **Undo روی revision و snapshot است:** State اصلی overwrite نمی‌شود. هر Transaction به parent revision، `previous_state_hash`، `new_state_hash` و patch معکوس متصل است.

---

# پروتکل تحقیق و تحلیل

## ۱. کالبدشکافی مسئله: چرا الگوی رایج ۷۰ ابزار برای Agent مناسب نیست؟

این نقد به معنی «بد بودن» CapCut، Descript یا Premiere نیست؛ هر سه برای کاربر انسانی و مدل تجاری خود بهینه شده‌اند. شکست مورد بحث، شکست **قابل‌عملیات‌بودن توسط Agent** است، نه شکست محصول برای انسان.

| مسئله | ادیتورهای UI-محور | پیامد برای Agent |
|---|---|---|
| فضای نام ضمنی | ابزار بین پنل، منو، context، plugin و keyboard shortcut پخش است | Agent باید از UI حدس بزند که کدام state فعال است؛ این شکننده و غیرقابل‌تست است |
| وضعیت پنهان | انتخاب clip، track، playhead، focus و mode در UI نگهداری می‌شود | یک فرمان طبیعی ممکن است روی target اشتباه اجرا شود |
| API ناپایدار | نام، محل و رفتار دکمه‌ها با نسخه و layout عوض می‌شود | automation مبتنی بر click، contract ندارد |
| مدل ابری یا account-bound | بعضی مدل‌ها، assetها، sync یا قابلیت‌های AI به account/network وابسته‌اند | privacy، latency و offline-first از بین می‌رود؛ حتی اگر خود ویرایشگر کاملاً cloud-based نباشد |
| مرز مبهم بین preview و export | preview گاهی با renderer نهایی یا plugin نهایی یکسان نیست | Agent نمی‌تواند تضمین کند چیزی که دیده همان چیزی است که export می‌شود |
| نبودن reference قفل‌شده | «اینجا»، «همین لحظه» و «وقتی چهره دیده شد» معمولاً به یک timestamp immutable تبدیل نمی‌شوند | اجرای دیرتر فرمان روی نقطه‌ی دیگری اتفاق می‌افتد |
| کشف‌پذیری ضعیف برای AI | UI توضیحی درباره‌ی schema، precondition، confidence و permission ندارد | LLM ممکن است ابزار درست را با پارامتر نادرست صدا بزند |
| Undo غیرقابل‌اعتماد | بعضی effects، cacheها و pluginها بیرون از تاریخچه‌ی قابل‌مشاهده‌اند | Agent نمی‌تواند atomicity یا rollback را اثبات کند |

راه‌حل Nagar «یک UI بهتر» نیست؛ **UI فقط projection از State است**. Agent به catalog تایپ‌دار، referenceهای semantic، precondition و نتیجه‌ی قابل‌اعتبارسنجی دسترسی دارد.

## ۲. سنتز بین‌رشته‌ای

### ۲.۱ معماری بازی و سیستم دانلود Pack

از بازی‌ها چهار ایده گرفته می‌شود:

- **Base runtime کوچک:** فقط Command Bus، decoder پایه، State و Registry اولیه در نصب اصلی.
- **Content-addressed Pack:** فایل بر اساس `sha256` ذخیره می‌شود؛ نصب مجدد یا دو پروژه‌ی مشترک duplicate ندارد.
- **Lazy loading و eviction:** فقط Pack فعال و مدل مورد نیاز در حافظه است؛ Pack غیر فعال unload می‌شود.
- **Compatibility gate:** `min_app_version`، ABI، GPU features، مدل و license قبل از نصب بررسی می‌شوند.

اما بر خلاف pluginهای سنتی، Pack Nagar نمی‌تواند binary دلخواه اجرا کند. Adapterهای مجاز از پیش در runtime ثبت شده‌اند و Manifest فقط انتخاب آن‌ها و داده‌های مدل را اعلام می‌کند.

### ۲.۲ کامپایلر: Text → AST → Typed IR → Reducer/Executor

فرمان طبیعی مستقیماً اجرا نمی‌شود:

```text
User text
  ↓
Intent parser / local language model
  ↓
Candidate Intent AST
  ↓  (schema + capability + permission + confidence)
Canonical Typed Command
  ↓  (reference capture + precondition)
Batch Transaction
  ↓
Pure State Reducer  +  Preview Executor / Final Executor
  ↓
State revision + derived asset + diagnostics
```

این همان جداسازی parser، semantic analysis، type checking و code generation در کامپایلر است. رشته‌ی FFmpeg، shader یا Python هرگز AST ورودی Agent نیست و فقط در adapter داخلی از IR معتبر ساخته می‌شود.

### ۲.۳ OpenTimelineIO و FFmpeg

ساختار canonical در OpenTimelineIO از `Timeline → Stack/Tracks → Clips/Transitions` شکل می‌گیرد و Clip به Media Reference و Source Range متصل است؛ این دقیقاً برای جداسازی «رسانه‌ی اصلی» از «برش editorial» مفید است [1](https://github.com/AcademySoftwareFoundation/OpenTimelineIO/blob/main/docs/tutorials/architecture.md). Nagar همین ایده را با `AssetRef`، hash و revision تقویت می‌کند.

FFmpeg filtergraph برای renderer نهایی مناسب است، نه برای ورودی Agent. FFmpeg فهرست بزرگی از filterها، timeline editing و commandهای runtime دارد [10](https://ffmpeg.org/ffmpeg-filters.html)، اما Nagar تنها subset تایپ‌دار و allow-listed آن را تولید می‌کند.

## ۳. اعتبارسنجی فنی: WebAssembly، WebGPU، ONNX و Native

راهنمای ONNX Runtime Web صریحاً WASM را مسیر CPU برای مدل‌های کوچک یا دستگاه بدون GPU و WebGPU را مسیر GPU برای دستگاه مناسب معرفی می‌کند؛ multi-threading در WASM نیز به `crossOriginIsolated` وابسته است [1](https://onnxruntime.ai/docs/tutorials/web/performance-diagnosis.html). WebGPU علاوه بر render، compute pipeline دارد و WGSL زبان shader آن است [1](https://developer.mozilla.org/en-US/docs/Web/API/WebGPU_API) [3](https://www.w3.org/TR/WGSL/).

| Pack | WASM | WebGPU | ONNX سبک | Native local | تصمیم معماری |
|---|---:|---:|---:|---:|---|
| `nexus.edit.timeline` | ✅ | △ برای preview | ✖ | ✅ برای master | عملیات زمان‌بندی و فیلترهای ساده در WASM؛ export نهایی native |
| `nexus.vision.portrait` | △ مدل‌های کوچک | ✅ | ✅ | ✅ | WebGPU برای inference، WASM fallback، کیفیت بالا در native |
| `nexus.vision.scene` | △ decode/tracking سبک | ✅ | ✅ | ✅ | segmentation و tracking در WebGPU؛ inpainting سنگین native |
| `nexus.motion.graphics` | ✅ | ✅ اصلی | △ | ✅ | shaderهای WGSL برای preview، فیلتر native برای master |
| `nexus.audio.studio` | ✅ DSP | △ | ✅ برای VAD/denoise | ✅ | AudioWorklet/WASM برای real-time، مدل سنگین در local worker |
| `nexus.language.caption` | △ tiny profile | △ محدود | △ small ASR/VAD | ✅ اصلی | WhisperX کامل در native local؛ browser فقط profile کوچک و optional |
| `nexus.color.delivery` | ✅ LUT/DSP | ✅ | △ | ✅ اصلی | preview GPU، color-managed master native |

**نکته‌ی مهم درباره‌ی WhisperX:** WhisperX خود یک JavaScript/WASM package نیست؛ pipeline آن بر faster-whisper، forced alignment و diarization متکی است. Repository رسمی آن به word-level alignment، batching، VAD و diarization اشاره می‌کند و محدودیت‌هایی مانند زبان‌محور بودن aligner و ضعف در overlapping speech را مستند کرده است [1](https://github.com/m-bain/whisperX). بنابراین طراحی درست، اجرای کامل WhisperX در یک browser tab نیست؛ اجرای local در worker/process و ارائه‌ی یک small ONNX fallback است.

## ۴. استیل‌منینگ: قوی‌ترین مخالفت و پاسخ معماری

### مخالفت ۱: «۷۰ ابزار در browser حافظه را منفجر می‌کند»

این نگرانی واقعی است. `VideoFrame` منابع بزرگ GPU دارد و باید صریحاً بسته شود؛ مستندات WebCodecs هشدار می‌دهند که frameهای بسته‌نشده می‌توانند برنامه را با کمتر از صد frame فعال دچار crash کنند. همچنین پردازش callbackها بهتر است در Worker انجام شود و با `encodeQueueSize` backpressure اعمال شود [1](https://developer.chrome.com/docs/web-platform/best-practices/webcodecs).

**پاسخ:** ۷۰ ابزار هرگز هم‌زمان load نمی‌شوند. Base runtime کوچک، یک Pack فعال، یک مدل سنگین، ring buffer محدود، Worker، `VideoFrame.close()` در `finally`، memory watchdog و fallback به native داریم. 4K master در browser هدف نیست.

### مخالفت ۲: «WebGPU روی همه‌ی دستگاه‌ها نیست»

**پاسخ:** Registry قبل از اجرا capability negotiation می‌کند: `webgpu`, `wasm_simd`, `native_worker`, RAM و codec profile. هر Operation یک implementation ladder دارد:

```text
WebGPU → WASM SIMD → Native local → reject with actionable diagnostic
```

Fallback کیفیت و زمان را تغییر می‌دهد، نه معنا و schema را.

### مخالفت ۳: «مدل‌های local سنگین، دیر load و از نظر license خطرناک‌اند»

**پاسخ:** مدل‌ها Pack جدا، hash‌شده، signed و دارای license/provenance هستند. first run با مدل کوچک انجام می‌شود؛ مدل بزرگ فقط با opt-in دانلود می‌شود. Prompt یا media به provider مدل ارسال نمی‌شود. عملیات حساس confidence و `model_digest` را در نتیجه ثبت می‌کنند.

### مخالفت ۴: «Agent در انتخاب ابزار hallucinate می‌کند»

**پاسخ:** Agent فقط capability IDهای Registry را می‌بیند؛ ورودی با Pydantic/JSON Schema اعتبارسنجی می‌شود، precondition hash لازم است، Operation ناشناخته رد می‌شود، و فرمان low-confidence به confirmation تبدیل می‌شود. Agent نمی‌تواند `ffmpeg -i ...` یا WASM bytecode بسازد.

## ۵. قوهای سیاه پیش‌بینی‌شده

1. **VFR و drift زمانی:** رابطه‌ی ساده‌ی `frame = time × fps` برای ویدئوی variable-frame-rate غلط است. PTS map، timebase و `timecode_us` نگهداری می‌شوند؛ frame number برای VFR از decoder map می‌آید.
2. **RTL ترکیبی:** فارسی، انگلیسی، URL، emoji و اعداد فارسی در یک خط باعث جابه‌جایی علامت سؤال، شماره و پرانتز می‌شوند. متن logical، bidi runs، glyph shaping، فونت fallback و snapshot تصویری باید تست شوند.
3. **چهره‌ی مشابه یا occlusion:** «چهره‌ی من» ممکن است با شخص دیگری اشتباه شود. `SubjectRef` با confidence، face embedding محلی، continuity track و confirmation زیر آستانه‌ی مشخص لازم است.
4. **GPU device lost یا background tab:** WebGPU ممکن است device را از دست بدهد. Executor باید checkpoint داشته باشد و command را با همان revision در native/WASM ادامه دهد، نه این‌که دوباره از ابتدا و بدون idempotency اجرا کند.
5. **آلودگی Pack:** Manifest مخرب یا مدل جایگزین می‌تواند data exfiltration کند. امضای Ed25519، allow-list runtime، بدون network permission پیش‌فرض و hash هر artifact لازم است.
6. **تغییر مدل و عدم تکرارپذیری:** version و digest مدل، seed، provider و precision داخل Transaction ذخیره می‌شوند؛ replay با مدل متفاوت باید explicit باشد.
7. **ترجمه‌ی معنای زمان:** «پنج ثانیه قبلش را حذف کن» بعد از ripple delete به مختصات تازه تبدیل می‌شود. Command باید reference اصلی و mapping بعد از edit را هم ثبت کند.
8. **HDR/SDR و رنگ:** preview رنگی ممکن است با master متفاوت باشد. color space، transfer function و range در MediaRef و RenderProfile الزاماً explicit هستند.

---

# بخش ۱: معماری کاتالوگ هوشمند

## ۱.۱ قرارداد مشترک Operation

Registry به‌صورت زیر دیده می‌شود:

```text
Domain
 └── Capability
      └── OperationSpec
           ├── operation_id
           ├── input_schema
           ├── output_schema
           ├── permission_level
           ├── preview_executor
           ├── final_executor
           ├── required_packs
           └── deterministic / model_policy
```

### Command Envelope

```json
{
  "protocol_version": "nagar.command.v1",
  "command_id": "cmd_01J...",
  "session_id": "session_01J...",
  "operation": "timeline.split_at_playhead",
  "target": {
    "project_id": "project_01",
    "track_id": "video_01",
    "clip_id": "clip_01"
  },
  "input": {
    "at": {
      "timecode_us": 12500000,
      "frame_number": 375,
      "timebase": {"numerator": 30, "denominator": 1},
      "captured_at_command": true
    }
  },
  "preconditions": {
    "state_revision": 41,
    "state_hash": "sha256:..."
  },
  "idempotency_key": "client-request-42"
}
```

### انواع مشترک

- `MediaRef`: `{asset_id, content_sha256, media_kind, duration_us, timebase, color_space}`
- `TimeRangeUS`: `{start_us, end_us}` با شرط `0 <= start_us < end_us`
- `PlayheadRef`: `{timecode_us, frame_number, timebase, captured_at_command:true}`
- `ClipRef`: `{project_id, track_id, clip_id, source_asset_id, range:TimeRangeUS}`
- `SubjectRef`: `{subject_id, track_id, confidence, evidence_range:TimeRangeUS}`
- `MaskRef`: `{mask_id, coordinate_space, resolution, temporal_range, confidence}`
- `EffectLayerRef`: `{layer_id, operation, parameters_hash, range, reversible:true}`
- `DerivedAsset`: `{asset_id, parent_asset_ids, content_sha256, provenance}`
- `StateDelta`: `{from_revision, to_revision, added_ids, removed_ids, changed_paths}`

هر Operation خروجی استاندارد زیر را نیز دارد:

```json
{
  "transaction_id": "tx_01J...",
  "status": "applied",
  "state_revision": 42,
  "state_hash": "sha256:...",
  "output": {},
  "diagnostics": {
    "executor": "webgpu|wasm|native",
    "model_digest": "sha256:...",
    "confidence": 0.98
  },
  "undo_available": true
}
```

سطح مجوز:

- **A — Immediate:** خواندن/تحلیل/کنترل playback؛ بدون تغییر مخرب.
- **B — Reversible:** تغییر timeline یا effect با Transaction و Undo.
- **C — Confirmation:** export سنگین، تغییر هویتی/چهره، حذف مبهم یا عملیاتی که confirmation صریح می‌خواهد.
- **D — Denied:** shell، network upload خام، اجرای کد، نوشتن روی source asset یا Operation ثبت‌نشده.

حجم‌های زیر تقریبی و compressed هستند؛ shared runtime، cache موجود و زبان‌های غیرضروری شمرده نشده‌اند.

## ۱.۲ Pack اول: `nexus.edit.timeline`

- **حجم پایه:** حدود ۲۰ MB
- **موتور:** Pure timeline reducer، WebCodecs برای preview، FFmpeg.wasm برای fallback و FFmpeg/native برای master
- **هدف:** زمان‌بندی و تدوین بدون تغییر MediaRef اصلی

| Operation | Input → Output | موتور اصلی | سطح |
|---|---|---|---:|
| `timeline.split_at_playhead` | `ClipRef + PlayheadRef` → `SplitResult{left,right,source_unchanged}` | reducer + trim/concat | B |
| `timeline.trim` | `ClipRef + TimeRangeUS` → `TimelinePatch` | reducer | B |
| `timeline.ripple_delete` | `TrackRef + TimeRangeUS` → `TimelinePatch + TimeMap` | reducer + final trim | B |
| `timeline.insert_gap` | `TrackRef + PlayheadRef + duration_us` → `TimelinePatch` | reducer | B |
| `timeline.speed_ramp` | `ClipRef + RateCurve` → `TimeMap + EffectLayerRef` | WebGPU preview/native timebase | B |
| `timeline.reverse_segment` | `ClipRef + TimeRangeUS` → `DerivedClipRef` | FFmpeg `reverse`/native | B |
| `timeline.freeze_frame` | `ClipRef + PlayheadRef + duration_us` → `DerivedClipRef` | frame extraction + loop | B |
| `timeline.attach_b_roll` | `BaseRange + MediaRef + fit_policy` → `TrackPatch` | reducer + overlay | B |
| `timeline.sync_multicam` | `ClipSet + anchor_policy` → `OffsetMap + confidence` | audio/PTS analysis | A |
| `timeline.retime_to_music` | `ClipSet + BeatGrid` → `RateCurve + CutPlan` | beat grid + reducer | B |

### نمونه فرمان

```json
{
  "protocol_version": "nagar.command.v1",
  "command_id": "cmd_split_01",
  "session_id": "session_01",
  "operation": "timeline.split_at_playhead",
  "target": {"project_id": "p1", "track_id": "video_01", "clip_id": "clip_01"},
  "input": {
    "at": {
      "timecode_us": 12500000,
      "frame_number": 375,
      "timebase": {"numerator": 30, "denominator": 1},
      "captured_at_command": true
    }
  },
  "preconditions": {"state_revision": 41, "state_hash": "sha256:timeline-before"},
  "idempotency_key": "split-p1-clip01-12500000"
}
```

## ۱.۳ Pack دوم: `nexus.vision.portrait`

- **حجم پایه:** حدود ۱۸۰ MB؛ profile با کیفیت بالا تا حدود ۳۵۰ MB
- **موتور:** ONNX Runtime Web + WebGPU، WASM fallback، native ONNX برای کیفیت/رزولوشن بالا
- **اصل ایمنی:** عملیات چهره، به‌خصوص gaze و تغییر هویت بصری، confidence و provenance را ثبت می‌کند.

| Operation | Input → Output | موتور اصلی | سطح |
|---|---|---|---:|
| `portrait.detect_landmarks` | `ClipRef + sample_policy` → `FaceTrackSet` | ONNX/WebGPU | A |
| `portrait.smooth_skin` | `FaceTrackSet/MaskRef + strength` → `EffectLayerRef` | segmentation + temporal filter | B |
| `portrait.retouch_blemish` | `FaceMask + blemish_policy` → `EffectLayerRef` | inpaint local | B |
| `portrait.relight_face` | `FaceTrackSet + LightModel` → `EffectLayerRef` | ONNX/WebGPU shader | B |
| `portrait.whiten_teeth` | `TeethMask + intensity` → `EffectLayerRef` | segmentation + color | B |
| `portrait.correct_gaze` | `FaceTrackSet + gaze_target` → `EffectLayerRef` | face warp model | C |
| `portrait.enhance_eyes` | `FaceTrackSet + clarity/red_eye` → `EffectLayerRef` | local enhancement | B |
| `portrait.mask_hair` | `FaceTrackSet + edge_quality` → `MaskRef` | matting ONNX | A |
| `portrait.background_blur` | `SubjectMask + blur_profile` → `EffectLayerRef` | mask + WebGPU blur | B |
| `portrait.stabilize_face` | `FaceTrackSet + stabilization_policy` → `TransformCurve` | tracker/Kalman | B |

### نمونه فرمان

```json
{
  "protocol_version": "nagar.command.v1",
  "command_id": "cmd_skin_01",
  "session_id": "session_01",
  "operation": "portrait.smooth_skin",
  "target": {"clip_id": "clip_01", "range": {"start_us": 0, "end_us": 18000000}},
  "input": {
    "subject": {"subject_id": "face_01", "track_id": "face-track-7", "confidence": 0.97},
    "strength": 0.22,
    "temporal_stability": "high"
  },
  "preconditions": {"state_revision": 42, "state_hash": "sha256:..."}
}
```

## ۱.۴ Pack سوم: `nexus.vision.scene`

- **حجم پایه:** حدود ۲۶۰ MB؛ profile دارای inpainting تا حدود ۵۰۰ MB
- **موتور:** lightweight segmentation/detection ONNX روی WebGPU، tracker در WASM، inpainting با native local برای فریم‌های بزرگ
- **هدف:** فهم صحنه با semantic reference، نه مختصات پیکسلی ساختگی

| Operation | Input → Output | موتور اصلی | سطح |
|---|---|---|---:|
| `scene.segment_subject` | `ClipRef + semantic_query` → `MaskRef` | segmentation ONNX | A |
| `scene.remove_object` | `ObjectTrack + fill_policy` → `EffectLayerRef + confidence` | temporal inpaint/native | B |
| `scene.replace_sky` | `ClipRef + sky_asset + horizon_policy` → `EffectLayerRef` | sky segmentation | B |
| `scene.remove_background` | `SubjectMask + alpha_policy` → `AlphaLayerRef` | matting ONNX | B |
| `scene.track_object` | `ClipRef + semantic_seed` → `ObjectTrack` | detector + optical flow | A |
| `scene.track_face` | `ClipRef + SubjectRef` → `FaceTrackSet` | face detector/tracker | A |
| `scene.detect_shot_boundaries` | `ClipRef + threshold` → `ShotBoundarySet` | histogram/embedding | A |
| `scene.find_subject_moment` | `ClipRef + SubjectQuery + event` → `CandidateMomentSet` | face/object/audio fusion | A |
| `scene.remove_logo` | `LogoMask + legal_policy` → `EffectLayerRef` | mask + inpaint | C |
| `scene.auto_reframe_subject` | `SubjectTrack + aspect + safe_area` → `TransformCurve` | tracker + reducer | B |

### نمونه فرمان

```json
{
  "protocol_version": "nagar.command.v1",
  "command_id": "cmd_wire_01",
  "session_id": "session_01",
  "operation": "scene.remove_object",
  "target": {"clip_id": "clip_01", "range": {"start_us": 4000000, "end_us": 13000000}},
  "input": {
    "object_track_id": "object-track-wire-2",
    "fill_policy": "temporal_inpaint",
    "preserve_camera_motion": true,
    "minimum_confidence": 0.9
  },
  "preconditions": {"state_revision": 44, "state_hash": "sha256:..."},
  "confirmation": {"confirmed_by_user": true}
}
```

## ۱.۵ Pack چهارم: `nexus.motion.graphics`

- **حجم پایه:** حدود ۳۰ MB
- **موتور:** WebGPU/WGSL برای preview، WASM برای math و fallback، native filter adapter برای master
- **اصل:** پارامترها keyframe و typed هستند؛ shader خام از Agent پذیرفته نمی‌شود.

| Operation | Input → Output | موتور اصلی | سطح |
|---|---|---|---:|
| `motion.add_transition` | `ClipPair + TransitionSpec` → `EffectLayerRef` | WebGPU/FFmpeg xfade | B |
| `motion.keyframe_transform` | `ClipRef + TransformCurve` → `TransformLayer` | WebGPU | B |
| `motion.add_parallax` | `LayerSet + depth_policy` → `DepthEffectLayer` | depth ONNX/WebGPU | B |
| `motion.apply_mask` | `ClipRef + MaskRef + feather` → `MaskedLayer` | shader | B |
| `motion.add_glow` | `ClipRef + GlowSpec` → `EffectLayerRef` | blur/composite shader | B |
| `motion.add_motion_blur` | `ClipRef + shutter_angle` → `EffectLayerRef` | temporal shader | B |
| `motion.stabilize` | `ClipRef + StabilizationSpec` → `TransformCurve` | feature tracker | B |
| `motion.warp` | `ClipRef + MeshCurve` → `EffectLayerRef` | WebGPU mesh | B |
| `motion.add_title` | `TitleSpec + TimeRangeUS` → `GraphicLayerRef` | glyph atlas/libass | B |
| `motion.add_particles` | `EmitterSpec + TimeRangeUS` → `ParticleLayerRef` | compute shader | B |

### نمونه فرمان

```json
{
  "protocol_version": "nagar.command.v1",
  "command_id": "cmd_transition_01",
  "session_id": "session_01",
  "operation": "motion.add_transition",
  "target": {"left_clip_id": "clip_a", "right_clip_id": "clip_b"},
  "input": {"kind": "crossfade", "duration_us": 500000, "easing": "smoothstep"},
  "preconditions": {"state_revision": 45, "state_hash": "sha256:..."}
}
```

## ۱.۶ Pack پنجم: `nexus.audio.studio`

- **حجم پایه:** حدود ۸۰ MB
- **موتور:** AudioWorklet، WASM SIMD DSP، ONNX برای VAD/denoise، native worker برای مدل‌های سنگین
- **اصل:** waveform و beat grid محلی cache می‌شوند؛ صدای خام از دستگاه خارج نمی‌شود.

| Operation | Input → Output | موتور اصلی | سطح |
|---|---|---|---:|
| `audio.detect_beats` | `AudioRef + tempo_policy` → `BeatGrid` | DSP/ONNX onset | A |
| `audio.beat_sync_cut` | `TimelineRange + BeatGrid + cut_policy` → `CutPlan` | reducer | B |
| `audio.remove_noise` | `AudioRef + noise_profile` → `DerivedAudioRef` | RNNoise/ONNX | B |
| `audio.remove_vocal` | `AudioRef + stem_policy` → `StemSet` | source separation ONNX | B |
| `audio.duck_music` | `DialogueRef + MusicRef + DuckCurve` → `AutomationLayer` | compressor/sidechain | B |
| `audio.normalize_loudness` | `AudioRef + target_lufs + true_peak` → `GainMap` | EBU-R128 analysis | B |
| `audio.eq_voice` | `AudioRef + VoiceEQProfile` → `AudioEffectLayer` | biquad DSP | B |
| `audio.deess` | `AudioRef + threshold` → `AudioEffectLayer` | dynamic DSP | B |
| `audio.align_music` | `TimelineRange + MusicRef + BeatGrid` → `OffsetMap` | beat matching | B |
| `audio.time_stretch` | `AudioRef + ratio + preserve_pitch` → `DerivedAudioRef` | WSOLA/phase vocoder | B |

### نمونه فرمان

```json
{
  "protocol_version": "nagar.command.v1",
  "command_id": "cmd_beat_01",
  "session_id": "session_01",
  "operation": "audio.beat_sync_cut",
  "target": {"track_id": "video_01", "range": {"start_us": 0, "end_us": 30000000}},
  "input": {
    "beat_grid_id": "beats_01",
    "cut_policy": {"prefer_key_beats": true, "minimum_clip_us": 900000}
  },
  "preconditions": {"state_revision": 46, "state_hash": "sha256:..."}
}
```

## ۱.۷ Pack ششم: `nexus.language.caption`

- **حجم small profile:** حدود ۳۸۰ MB
- **حجم full local profile:** حدود ۱.۲ تا ۱.۸ GB، بسته به زبان، Whisper و aligner
- **موتور:** WhisperX native local برای مسیر کامل؛ ONNX/WASM کوچک برای live preview؛ pyannote local برای diarization؛ HarfBuzz/FriBidi/libass برای RTL
- **هشدار:** diarization رسمی pyannote به مدل و شرایط license/token مربوط است و اجرای pipeline محلی مستند شده است [1](https://github.com/pyannote/pyannote-audio/blob/develop/README.md). این token فقط برای دریافت مجاز مدل است، نه ارسال رسانه به cloud.

| Operation | Input → Output | موتور اصلی | سطح |
|---|---|---|---:|
| `caption.transcribe` | `AudioRef + language_policy` → `TranscriptRef{segments,words?}` | WhisperX/faster-whisper | A |
| `caption.align_words` | `TranscriptRef + AudioRef + language` → `WordTimingSet` | wav2vec2/CTC aligner | A |
| `caption.diarize` | `AudioRef + speaker_bounds` → `SpeakerTurnSet` | pyannote local | A |
| `caption.translate_local` | `TranscriptRef + target_language` → `TranslatedTranscriptRef` | local seq2seq/LLM pack | B |
| `caption.generate_srt` | `WordTimingSet + line_policy` → `DerivedAsset(.srt)` | deterministic formatter | B |
| `caption.generate_ass_rtl` | `TranscriptRef + RTLStyle` → `DerivedAsset(.ass)` | bidi/shaping formatter | B |
| `caption.style_vazirmatn` | `CaptionAsset + VazirmatnStyle` → `StyledCaptionAsset` | HarfBuzz/libass | B |
| `caption.highlight_words` | `WordTimingSet + highlight_policy` → `CaptionLayerRef` | timeline layer | B |
| `caption.search_transcript` | `TranscriptRef + query` → `MatchSet{ranges,confidence}` | indexed text | A |
| `caption.burn_in` | `ClipRef + StyledCaptionAsset` → `DerivedAsset(video)` | WebGPU preview/native FFmpeg | B |

### نمونه فرمان

```json
{
  "protocol_version": "nagar.command.v1",
  "command_id": "cmd_fa_caption_01",
  "session_id": "session_01",
  "operation": "caption.generate_ass_rtl",
  "target": {"clip_id": "clip_01", "range": {"start_us": 0, "end_us": 30000000}},
  "input": {
    "transcript_id": "transcript_fa_01",
    "language": "fa-IR",
    "style": {
      "font_family": "Vazirmatn",
      "font_file_ref": "pack://nexus.language.caption/fonts/Vazirmatn-Regular.ttf",
      "direction": "rtl",
      "font_size": 48,
      "safe_margin": {"left": 96, "right": 96, "bottom": 84},
      "outline_px": 3
    }
  },
  "preconditions": {"state_revision": 48, "state_hash": "sha256:..."}
}
```

## ۱.۸ Pack هفتم: `nexus.color.delivery`

- **حجم پایه:** حدود ۴۵ MB
- **موتور:** WebGPU color kernels، LUTهای content-addressed، FFmpeg/native برای encode و OTIO export
- **اصل:** color management بخشی از schema است؛ صرفاً فیلتر RGB نیست.

| Operation | Input → Output | موتور اصلی | سطح |
|---|---|---|---:|
| `color.auto_balance` | `ClipRef + ColorProfile` → `ColorLayerRef` | histogram/WebGPU | B |
| `color.adjust_exposure` | `ClipRef + ExposureEVCurve` → `ColorLayerRef` | shader | B |
| `color.white_balance` | `ClipRef + TempTintCurve` → `ColorLayerRef` | shader | B |
| `color.match_shot` | `SourceClip + ReferenceClip` → `LookTransform` | feature/color analysis | B |
| `color.apply_lut` | `ClipRef + LUTRef + color_space` → `ColorLayerRef` | 3D LUT | B |
| `color.hdr_tonemap` | `ClipRef + HDRProfile + target_space` → `ColorLayerRef` | native/GPU | B |
| `color.deband_denoise` | `ClipRef + quality_profile` → `DerivedAsset` | temporal filter | B |
| `delivery.make_proxy_480p` | `ProjectRef + ProxyProfile` → `DerivedAsset(.mp4)` | WebCodecs/native | A |
| `delivery.render_master_4k` | `ProjectRef + RenderProfile` → `RenderArtifact` | FFmpeg/native | C |
| `delivery.export_otio` | `ProjectRef + OTIOProfile` → `DerivedAsset(.otio)` | deterministic adapter | B |

### نمونه فرمان

```json
{
  "protocol_version": "nagar.command.v1",
  "command_id": "cmd_master_01",
  "session_id": "session_01",
  "operation": "delivery.render_master_4k",
  "target": {"project_id": "p1"},
  "input": {
    "profile": {
      "width": 3840,
      "height": 2160,
      "codec": "h264",
      "container": "mp4",
      "color_space": "bt709",
      "audio": {"codec": "aac", "target_lufs": -14}
    },
    "output_policy": "new_derived_asset"
  },
  "preconditions": {"state_revision": 52, "state_hash": "sha256:..."},
  "confirmation": {"confirmed_by_user": true}
}
```

---

# بخش ۲: ماتریس نوآوری فنی

## ۲.۱ زیرنویس و صدا: WhisperX، diarization و RTL به‌صورت local و real-time

### Pipeline

```text
Local demux
  → 16 kHz mono PCM ring buffer
  → VAD (Silero/ONNX یا native)
  → 1–5 second partial windows
  → WhisperX/faster-whisper local ASR
  → language-specific forced alignment
  → pyannote local diarization
  → Persian normalization + bidi runs
  → Transcript/Caption State
```

- **Real-time strategy:** ASR partial است اما edit commit watermark دارد. مثلاً تا زمانی که پنجره‌ی بعدی نیامده، آخرین ۱.۵ ثانیه قابل اصلاح است؛ بخش قبل از watermark immutable می‌شود. این مانع پرش مداوم زیرنویس در UI می‌شود.
- **Persian alignment:** WhisperX به aligner زبان‌محور نیاز دارد. اگر aligner فارسی معتبر در Pack موجود نباشد، خروجی به‌جای جعل دقت، `alignment_quality="interpolated"` و confidence پایین می‌دهد. برای نسخه‌ی دقیق، یک CTC/phoneme aligner فارسی با داده‌ی آزمایشی و معیار word boundary لازم است.
- **Diarization:** speaker turn با word time overlap join می‌شود. overlapping speech به‌عنوان چند speaker احتمالی نگهداری می‌شود، نه این‌که Agent یک speaker قطعی hallucinate کند.
- **RTL rendering:** متن در State به شکل logical UTF-8 باقی می‌ماند؛ در render، Unicode normalization، bidi segmentation، Arabic shaping و glyph fallback انجام می‌شود. در متن ترکیبی، URL، کد و عدد با isolateهای LTR داخل پاراگراف RTL قرار می‌گیرند.
- **Vazirmatn:** فونت به‌صورت artifact داخل Pack و با license/hash ثبت می‌شود. برای master می‌توان ASS/libass را استفاده کرد؛ FFmpeg subtitles filter گزینه‌هایی برای `fontsdir`، `original_size` و line wrapping دارد [10](https://ffmpeg.org/ffmpeg-filters.html). مستندات رسمی Vazirmatn آن را فونت فارسی/عربی معرفی می‌کنند [1](https://github.com/rastikerdar/vazirmatn/blob/master/DESCRIPTION.en_us.html).

## ۲.۲ ویرایش معنایی ویدئو بدون ارسال ویدئو

برای فرمان «سیم را از آسمان حذف کن» مسیر این است:

1. WebCodecs/native decoder فقط فریم‌های نمونه و keyframeهای لازم را محلی decode می‌کند.
2. Frame به ۵۱۲ یا ۷۶۸ پیکسل downsample می‌شود؛ مدل segmentation/line detector در ONNX Runtime WebGPU اجرا می‌شود.
3. `scene.track_object` mask سیم را در زمان دنبال می‌کند؛ optical flow و confidence در هر frame ثبت می‌شود.
4. `scene.remove_object` mask را به temporal inpainting می‌دهد: از فریم‌های قبل/بعد و motion field برای fill استفاده می‌کند.
5. Preview با WebGPU نشان داده می‌شود؛ master با مدل native یا filtergraph allow-listed رندر می‌شود.

برای سوژه‌ی باریک مانند سیم، segmentation عمومی کافی نیست؛ detector خط، edge continuity و temporal consistency لازم است. اگر confidence افت کند، Operation به `needs_review` تبدیل می‌شود و خودش حذف را commit نمی‌کند.

«برش روی بیت» نیز با ارسال ویدئو به سرور حل نمی‌شود: `audio.detect_beats` waveform محلی را به `BeatGrid` تبدیل می‌کند و `audio.beat_sync_cut` آن را به `CutPlan` و Transaction timeline تبدیل می‌کند.

## ۲.۳ مدیریت State، Undo/Redo و OpenTimelineIO-like data structure

### مدل داخلی

```text
Project
 ├── AssetRegistry (immutable MediaRef)
 └── Timeline
      ├── Tracks
      │    ├── Clip / Gap / Transition
      │    └── Effect Layers
      ├── Markers
      └── Playhead
```

هر edit یک reducer خالص دارد:

```text
new_state = reducer(old_state, canonical_command)
transaction = {
  command_ast,
  parent_revision,
  previous_state_hash,
  new_state_hash,
  forward_patch,
  inverse_patch,
  timestamp,
  executor_version,
  model_digests
}
```

- **Undo:** head revision را به parent می‌برد و inverse patch یا snapshot معتبر را اعمال می‌کند؛ source asset تغییر نکرده است.
- **Redo:** Transaction undone در redo branch قرار می‌گیرد. اگر edit تازه‌ای بعد از Undo انجام شود، branch قبلی حذف نمی‌شود؛ فقط دیگر branch پیش‌فرض نیست.
- **Atomic batch:** چند Operation مثل `pause + mark` یا دو split در یک `BatchTransaction` ثبت می‌شوند. یا همه commit می‌شوند یا هیچ‌کدام.
- **Optimistic concurrency:** command شامل `state_revision` و `state_hash` است. اگر UI یا Agent قدیمی باشد، Bus آن را رد می‌کند و State جدید را برمی‌گرداند؛ merge حدسی انجام نمی‌شود.
- **Time:** `RationalTime` ایده‌ی OTIO را حفظ می‌کنیم، اما serialization اصلی `timecode_us` + `TimeBase` است. برای VFR، `PTSMap` نگهداری می‌شود.
- **Persistence:** snapshotها با content hash و log کوتاه در SQLite محلی نگهداری می‌شوند. هنگام compaction، checkpoint باقی می‌ماند و audit trail از بین نمی‌رود.

## ۲.۴ Dual Rendering

### Preview lane

```text
Encoded source
  → WebCodecs VideoDecoder در Worker
  → WebGPU texture/effect graph
  → 480p VideoEncoder یا canvas presentation
```

قواعد:

- هر `VideoFrame` دقیقاً یک owner دارد و در پایان `close()` می‌شود.
- اگر `encodeQueueSize` از سقف بیشتر شد، frame غیرکلیدی drop می‌شود؛ timeline/timecode drop نمی‌شود.
- cache key برابر است با `state_hash + range + preview_profile + pack_digests`.
- preview ممکن است quality پایین یا effect approximate داشته باشد، اما باید diagnostics آن را اعلام کند.
- WebCodecs برای low-level frame/chunk access مناسب editor است و handling آن بهتر است در Worker باشد [1](https://developer.chrome.com/docs/web-platform/best-practices/webcodecs).

### Master lane

```text
Canonical State
  → Render IR
  → allow-listed native adapter
  → FFmpeg filtergraph + codec/muxer
  → new DerivedAsset
```

- هیچ source path با `-y` overwrite نمی‌شود؛ خروجی موقت در staging و سپس atomic rename به asset جدید می‌رود.
- Filtergraph از `Render IR` ساخته می‌شود؛ Agent به FFmpeg syntax دسترسی ندارد.
- preview و master باید از یک command/state hash تغذیه شوند؛ اگر engine تفاوت دارد، آن تفاوت در `diagnostics` ثبت می‌شود.

## ۲.۵ طراحی کامل JSON Manifest برای Pack

Manifest پیشنهادی:

```json
{
  "manifest_schema": "nexus.capability-pack.v1",
  "package_id": "nexus.language.caption",
  "version": "1.0.0",
  "display_name": "Local Persian and multilingual captions",
  "download_size_mb": 384,
  "capabilities": [
    "caption.transcribe",
    "caption.align_words",
    "caption.diarize",
    "caption.translate_local",
    "caption.generate_srt",
    "caption.generate_ass_rtl",
    "caption.style_vazirmatn",
    "caption.highlight_words",
    "caption.search_transcript",
    "caption.burn_in"
  ],
  "runtime": {
    "browser": {
      "preferred": "onnx_webgpu",
      "fallback": "onnx_wasm_simd",
      "worker_required": true
    },
    "native": {
      "adapter_id": "nagar.local.whisperx.caption.v1",
      "sandbox": "process_isolated",
      "network_required": false
    }
  },
  "permissions": [
    "read_project_audio",
    "read_project_video_frames",
    "write_transcript_assets",
    "write_derived_caption_assets",
    "write_timeline_layers"
  ],
  "network_policy": {
    "upload_media": false,
    "runtime_network": false,
    "pack_update": "optional_signed_download"
  },
  "hardware_requirements": {
    "minimum_ram_mb": 4096,
    "recommended_ram_mb": 8192,
    "minimum_cpu_threads": 4,
    "gpu": {
      "optional": true,
      "webgpu": true,
      "features": ["shader-f16"]
    },
    "disk_free_mb": 1200
  },
  "artifacts": [
    {
      "artifact_id": "whisper-small-int8",
      "kind": "onnx_model",
      "path": "models/whisper-small-int8.ort",
      "size_mb": 142,
      "sha256": "sha256:replace-with-real-digest",
      "license": "model-license-reference"
    },
    {
      "artifact_id": "persian-aligner",
      "kind": "onnx_model",
      "path": "models/fa-aligner.ort",
      "size_mb": 68,
      "sha256": "sha256:replace-with-real-digest",
      "license": "model-license-reference"
    },
    {
      "artifact_id": "vazirmatn-regular",
      "kind": "font",
      "path": "fonts/Vazirmatn-Regular.ttf",
      "size_mb": 1.2,
      "sha256": "sha256:replace-with-real-digest",
      "license": "OFL-1.1"
    }
  ],
  "resource_budget": {
    "max_resident_model_mb": 900,
    "max_audio_minutes_in_memory": 2,
    "max_preview_resolution": "1280x720"
  },
  "compatibility": {
    "min_nagar_version": "3.10.0",
    "protocol_version": "nagar.command.v1",
    "state_schema": "nagar.state.v1"
  },
  "security": {
    "signature_algorithm": "ed25519",
    "signature": "base64:replace-with-signed-manifest",
    "trusted_publisher": "nagar-core",
    "allow_arbitrary_native_code": false,
    "allow_arbitrary_wasm_imports": false
  }
}
```

Manifest باید با JSON Schema معتبر شود. `capabilities` فقط Operationهایی را می‌تواند معرفی کند که runtime اصلی قبلاً شناخته است؛ Pack نمی‌تواند Operation جدیدی را با اسم دلخواه register کند.

---

# بخش ۳: الگوی تعامل Agent و کاربر — سناریوی کامل

فرمان کاربر:

> «این ویدیو را پخش کن، وقتی به چهره‌ی من رسید نگه دار، ۵ ثانیه‌ی قبلش را حذف کن و یک زیرنویس فارسی با فونت وزیر به آن اضافه کن.»

فرض کنیم MediaRef ویدئو قبلاً local و Project در revision `100` است.

## گام ۱: تشخیص نیت و حل Reference

Agent ابتدا فرمان را به intentهای زیر تبدیل می‌کند:

```text
play(media)
stop_when(subject=me, event=visible)
ripple_delete(range=[face_time - 5s, face_time])
caption(language=fa-IR, font=Vazirmatn)
```

برای «چهره‌ی من» Agent مختصات پیدا نمی‌کند. ابتدا Operation تحلیلی زیر اجرا می‌شود:

```json
{
  "operation": "scene.find_subject_moment",
  "target": {"clip_id": "clip_01"},
  "input": {
    "subject_query": {"kind": "speaker_face", "speaker_ref": "user:me"},
    "event": "first_visible_face",
    "minimum_confidence": 0.92
  },
  "preconditions": {"state_revision": 100, "state_hash": "sha256:rev100"}
}
```

خروجی فرضی:

```json
{
  "candidate_id": "moment_01",
  "timecode_us": 12345678,
  "frame_number": 371,
  "captured_at_command": true,
  "confidence": 0.96,
  "evidence_range": {"start_us": 12290000, "end_us": 12410000},
  "subject_id": "person_me_01"
}
```

Resolver محاسبه می‌کند:

```text
face_timecode_us = 12,345,678
remove_start_us  =  7,345,678
remove_end_us    = 12,345,678
```

هر دو reference در Command قفل می‌شوند. بعد از ripple delete، face به زمان جدیدی منتقل می‌شود؛ این mapping با `TimeMap` ثبت می‌گردد و Agent دوباره «همین‌جا» را resolve نمی‌کند.

اگر confidence کمتر از ۰.۹۲ باشد، اجرای بخش حذف متوقف و confirmation انسانی درخواست می‌شود. بازیابی preview می‌تواند ادامه داشته باشد، اما edit commit نمی‌شود.

## گام ۲: تولید زنجیره‌ی Typed Commands و Batch Transaction

این یک Plan چندمرحله‌ای است؛ barrier زمان‌مند Operation جدیدی نیست. تمام اعضای Plan همچنان Operationهای Registry هستند:

### مرحله‌ی تعاملی

```json
{
  "operation": "media.play",
  "target": {"clip_id": "clip_01"},
  "input": {
    "stop_at": {
      "timecode_us": 12345678,
      "frame_number": 371,
      "captured_at_command": true
    },
    "stop_reason": "subject_visible:person_me_01"
  },
  "preconditions": {"state_revision": 100, "state_hash": "sha256:rev100"}
}
```

Playback به `playing` می‌رود. Playback clock در همان PTS دقیق به `paused` transition می‌کند و event `playhead_reached` تولید می‌شود. UI فقط State و event را render می‌کند.

### مرحله‌ی ویرایش اتمیک بعد از barrier

```json
{
  "kind": "batch_transaction",
  "batch_id": "batch_01",
  "atomic": true,
  "source_revision": 101,
  "commands": [
    {
      "operation": "timeline.ripple_delete",
      "target": {"track_id": "video_01"},
      "input": {
        "range": {"start_us": 7345678, "end_us": 12345678},
        "reference_origin": {
          "event": "subject_visible:person_me_01",
          "captured_at_command": true
        }
      }
    },
    {
      "operation": "caption.transcribe",
      "target": {"clip_id": "clip_01"},
      "input": {"language_policy": "auto", "word_timestamps": true}
    },
    {
      "operation": "caption.translate_local",
      "target": {"transcript_id": "transcript_01"},
      "input": {"target_language": "fa-IR"}
    },
    {
      "operation": "caption.align_words",
      "target": {"audio_asset_id": "asset_audio_01"},
      "input": {"transcript_id": "translated_transcript_01", "language": "fa"}
    },
    {
      "operation": "caption.generate_ass_rtl",
      "target": {"clip_id": "clip_01"},
      "input": {
        "language": "fa-IR",
        "font_family": "Vazirmatn",
        "direction": "rtl",
        "safe_margin": {"left": 96, "right": 96, "bottom": 84}
      }
    },
    {
      "operation": "caption.style_vazirmatn",
      "target": {"caption_asset_id": "caption_ass_01"},
      "input": {"weight": 500, "outline_px": 3, "background": "semi_transparent_box"}
    }
  ],
  "preconditions": {"state_hash": "sha256:after-pause"}
}
```

اگر کاربر منظورش «زیرنویس فارسی از گفتار فارسی» باشد، `caption.translate_local` حذف می‌شود. این تفاوت از language detection و intent clarification می‌آید، نه حدس خام.

## گام ۳: Registry، Schema و Permission

| مرحله | Operation | Permission | شرط |
|---|---|---:|---|
| تشخیص چهره/لحظه | `scene.find_subject_moment` | A | confidence و local model digest |
| پخش و توقف | `media.play` | A | stop PTS قفل‌شده |
| حذف پنج ثانیه | `timeline.ripple_delete` | B | reversible و batch atomic |
| ASR/alignment | `caption.transcribe`, `caption.align_words` | A | مدل/زبان موجود |
| ترجمه | `caption.translate_local` | B | local model و confidence |
| ساخت ASS | `caption.generate_ass_rtl` | B | Vazirmatn artifact و bidi validation |
| style | `caption.style_vazirmatn` | B | caption asset موجود |

Command Bus موارد زیر را رد می‌کند:

- `operation` ناشناخته یا خارج از Pack فعال؛
- `timecode_us` بدون `captured_at_command` در جایی که reference لازم است؛
- `state_hash` قدیمی؛
- source overwrite یا path دلخواه؛
- model/Pack با hash نامعتبر؛
- confidence زیر threshold بدون confirmation؛
- هر فیلد شبیه shell، filtergraph خام یا کد.

## گام ۴: State و انعکاس آنی در UI

Eventهای قابل مشاهده برای UI:

```text
command.accepted       {command_id, operation, state_revision}
analysis.progress      {operation, processed_us, total_us}
playhead.changed       {timecode_us, frame_number, playback_state}
playhead.reached       {reference_id, captured_at_command:true}
transaction.preview    {batch_id, diff, confidence}
transaction.committed  {transaction_id, state_revision, state_hash}
render.progress        {derived_asset_id, percent}
```

UI بر اساس State مرکزی، marker، split، caption layer و وضعیت playback را خودش render می‌کند. هیچ eventی از UI حاوی «روی x=742,y=391 کلیک کن» نیست.

---

# بخش ۴: پیش‌مرگ — دقیقاً سه دلیل فنی شکست در مقیاس بزرگ

## ۱. Memory leak و ناپایداری GPU/WASM

**نشانه:** بعد از چند دقیقه preview، tab crash می‌کند، frame drop شدید می‌شود یا WebGPU device lost رخ می‌دهد. علت معمول، نگه‌داشتن `VideoFrame`/texture، queue بی‌سقف یا load هم‌زمان چند model است.

**Mitigation:**

- Worker جدا برای decode/encode و `try/finally { frame.close() }`؛
- ring buffer و سقف سخت `encodeQueueSize`؛
- فقط یک مدل سنگین resident؛ LRU eviction برای Pack؛
- memory watchdog با abort امن و checkpoint؛
- 4K master خارج از browser و fallback native؛
- test طولانی ۳۰ دقیقه‌ای با معیار رشد حافظه، نه فقط unit test.

## ۲. latency، ناسازگاری و license مدل‌ها

**نشانه:** اولین caption چند دقیقه طول می‌کشد، Persian alignment کیفیت ناپایدار دارد، GPU بعضی دستگاه‌ها مدل را اجرا نمی‌کند یا Pack به‌علت license قابل توزیع نیست.

**Mitigation:**

- small/base/full profile و preflight سخت‌افزار؛
- warm worker و cache مدل content-addressed؛
- fallback `WebGPU → WASM → native` با diagnostics؛
- ثبت `model_digest`, `alignment_quality` و confidence؛
- Pack signed با license/provenance و عدم نصب مدل بدون مجوز؛
- benchmark جدا برای فارسی، اعداد، نام‌ها، code-switching و overlapping speech؛
- هر نتیجه‌ی approximate به‌صراحت approximate باقی می‌ماند و به‌عنوان حقیقت قطعی وارد Transaction نمی‌شود.

## ۳. خطای معنایی/زمانی Agent در Stateهای پیچیده

**نشانه:** در VFR، نقطه‌ی «اینجا» چند frame جابه‌جا می‌شود؛ بعد از delete، subtitle روی زمان قدیمی می‌ماند؛ Agent روی چهره‌ی اشتباه edit می‌کند یا retry شبکه‌ای یک Operation را دوبار اعمال می‌کند.

**Mitigation:**

- canonical `timecode_us` + PTS map و frame map؛
- immutable `state_revision`، `state_hash` و precondition؛
- idempotency key و transaction atomic؛
- SubjectRef/MaskRef با evidence و confidence؛
- reference capture در لحظه‌ی دریافت فرمان، نه در زمان اجرای دیرتر؛
- snapshot/inverse patch برای Undo؛
- acceptance test با VFR، RTL ترکیبی، retry، model version change و duplicate command؛
- هر ambiguity به `needs_confirmation` تبدیل می‌شود، نه به اجرای خوش‌بینانه.

---

# جمع‌بندی اجرایی

معماری Nagar برای مدیریت ۷۰ ابزار، ۷۰ دکمه‌ی بیشتر نمی‌سازد. آن‌ها را به ۷ فضای نام مستقل، Packهای قابل‌دانلود و Operationهای schema-first تبدیل می‌کند. Agent به semantic target و typed input دسترسی دارد؛ renderer به State و IR؛ و UI فقط projection است.

ترتیب اجرای پیشنهادی:

1. تثبیت `Command Envelope`، `timecode_us`، Registry، State revision و Undo برای Green Cockpit؛
2. اضافه‌کردن Manifest verifier و Pack cache بدون اجرای کد دلخواه؛
3. فعال‌سازی timeline/audio/caption سبک؛
4. فعال‌سازی ONNX/WebGPU برای preview و تحلیل؛
5. اضافه‌کردن native local worker برای WhisperX، inpainting و master 4K؛
6. سپس فعال‌سازی تدریجی ۷۰ Operation با contract test مستقل برای هر Operation.

معیار موفقیت Nagar این نیست که همه‌ی قابلیت‌ها روی یک دستگاه ضعیف هم‌زمان اجرا شوند؛ معیار این است که هر قابلیت **قابل‌کشف، قابل‌اعتبارسنجی، قابل‌Undo، قابل‌تشخیص از نظر confidence، local-first و مستقل از مختصات UI** باشد.

---

# وضعیت پیاده‌سازی (Implementation status) — ۲۴ سپتامبر ۲۰۲۶

> نمای زنده‌ی وضعیت در [`architecture/CREATIVE_STUDIO.md`](architecture/CREATIVE_STUDIO.md) و [`ops/PACK_RUNTIME.md`](ops/PACK_RUNTIME.md) نگهداری می‌شود؛ این پیوست فقط تصویر اندازه‌گیری‌شده در زمان تحویل است.

- **ثبت‌شده و پیاده‌سازی‌شده: ۶۷ از ۷۰ شناسه‌ی این کاتالوگ** (به‌علاوه‌ی `system.undo`). Composition = **۷۷ عملیات در ۸ پک** (`packs/runtime.py::COMPOSITION`: slideshow, caption, edit, motion, audio, delivery, portrait, scene)؛ `composition_issues() == ()`، `stale_capabilities() == {}`.
- **پک‌های `nexus.vision.portrait` و `nexus.vision.scene`** (شناسه‌های §۱.۳ و §۱.۴) پیاده‌سازی و ثبت شدند (task-152/153)؛ عملیاتِ دارای mask/track به primitives مشترک `creative/packs/vision_common.py` متصل‌اند.
- **سه شناسه‌ی باقی‌مانده:** `color.white_balance`, `color.hdr_tonemap`, `color.deband_denoise` (§۱.۸) — محل فرود آن‌ها `packs/delivery/` است که توسط PR#33 قفل شده (BLOCKED_SHARED_CONTRACT).
- **قرارداد زمان:** `timecode_us` صحیح (میکروثانیه) مرجع است؛ پل OTIO (`creative/otio/`) و پل render-plan (`creative/rendering/plan.py`) نیز همان واحد را نگه می‌دارند.
- طبقه‌بندی صادقانه‌ی اجرا: عملیات پک‌ها **State-Only** هستند (AssetRecord/EffectLayerRef با hash قطعی)؛ تنها مسیر تولید رسانه‌ی واقعی، lane رندر (`creative/rendering/`) است.
