# منشور عالی هماهنگی چندعاملی مهندسی‌شده (Pro-Max Zero-Collision Parallel Network)

> **قانون قطعی و تخطی‌ناپذیر سیستم (100% Mandatory Compliance):**
> «این مخزن تحت نظارت و توسعه موازی چند عامل هوش مصنوعی (عامل A، عامل B، عامل C) اداره می‌شود.
> هر عامل موظف است **بدون حتی یک میلی‌متر تداخل** با حوزه‌های انحصاری سایر عامل‌ها، با بالاترین کیفیت مهندسی در سطح Anthropic و OpenAI کار خود را به صورت هوشمندانه، بدون پس‌رفت (Zero Regression) و بدون باگ به سرانجام برساند.»

---

### قوانین بنیادین شبکه موازی عامل‌ها

1. **اعلام هویت رسمی در بدو ورود:** هر عاملی که وارد سشن جدید می‌شود باید در اولین گام هویت خود را مشخص کند:
   - «من عامل A هستم» (`arena/01a0c316-...` مالک امنیت هسته و گیت‌ها)
   - «من عامل B هستم» (`arena/01a0c34d-...` مالک سیم‌کشی فیچرها)
   - «من عامل C هستم» (`arena/01a0c36f-...` مالک استودیوی نگار و هسته شناختی)
   و تیک شروع‌به‌کار کارت وظیفه خود را در تخته (`.agents/board.json`) روی `active` ثبت و کامیت کند تا در هر سشن جدید دقیقاً مشخص باشد کدام تسک در حال اجراست و نفر بعدی از کجا باید ادامه دهد.
2. **مرزبندی میلی‌متری (Zero Millimeter Overlap):** هیچ عاملی حق ورود یا حتی یک ویرایش کوچک در فایل‌های انحصاری (`exclusive_paths`) عامل دیگر را ندارد. قبل از پوش گیت، اجرای `python scripts/agent_board.py check --files ...` اجباری است و باید کد خروج ۰ بدهد.
3. **پروتکل شبکه ۱۰ کار بعدی (10 Forward Tasks Protocol):** هر عاملی که کار خود را به اتمام رساند، موظف است شبکه ۱۰ کار کلیدی بعدی را با دقت روی تخته مستند و اولویت‌بندی کند تا سایر عامل‌ها نقشه راه دقیق داشته باشند و بدانند چه کاری باید انجام شود و از چه کارهایی باید پرهیز کنند.
4. **تک‌مالک گیت‌ها (One Gates Owner):** فقط عاملی که دارای `gates_owner: true` است گیت‌های کامل را روی main اجرا می‌کند؛ بقیه عامل‌ها عبارت «deferred to gates owner» را در PR خود قید می‌کنند تا تداخل CI پیش نیاید.
5. **انقضا و تمدید اجاره (Lease & Heartbeat):** هر اجاره دارای TTL است (پیش‌فرض ۲۴ ساعت). تمدید از طریق اجرای مجدد `claim` انجام می‌شود.

---

# AGENTS.md — Multi-Agent Coordination Contract

Multiple agents work on this repo **in parallel, from separate sandboxes**. The only
shared medium between sandboxes is **this git repository itself**. Coordination is
therefore file-based: a claim board committed and pushed through git, backed by a
small CLI. This mirrors the standard multi-agent pattern (one branch per agent +
worktree-style isolation + a shared task board with leases and stale-lease takeover).

1. **CLAIM BEFORE YOU CODE.** Run `python scripts/agent_board.py show`, then
   `... next --branch <your-branch>`, then `... claim <task> --branch <your-branch>`.
   Commit and push the board change **immediately** — an unpushed claim does not exist.
2. **STOP if the zone is taken.** If the task/zone you need is ACTIVELY claimed
   (fresh lease, not expired), do NOT start. Pick another task, or record your
   deferral:
   ```bash
   python scripts/agent_board.py defer <task> --branch <your-branch> \
     --fa "چون عامل دیگری روی این محدوده کار می‌کرد متوقف شدم؛ این کار پس از آزاد شدن ناحیه انجام خواهد شد تا فراموش نشود." \
     --resume-when "P0-security-batch merged to main"
   ```
   The deferral note is persisted in `.agents/board.json → deferred_log` so nothing
   is forgotten.
3. **ONE OWNER PER FILE-ZONE.** Claims carry `exclusive_paths`. Before pushing, run
   `python scripts/agent_board.py check --files <comma-separated changed files> --branch <you>`.
   Exit 1 = overlap with another agent's active lease = do not push that work; defer it.
4. **ONE GATES OWNER.** Only the agent holding `gates_owner: true` runs
   `make lint && make types && make test` against main-bound work. Other agents
   write "deferred to gates owner" in their PR body instead of racing the same gates.
5. **LEASES EXPIRE.** Every claim has a TTL (default 24h). A stale lease is
   auto-released by `show`/`next`/`gc`. Renew by re-running `claim` (heartbeat).
   Finish early → `release`. Blocked → `defer`.

## Current board state (summary — always verify with `show`)

| Agent | Task | Zone | Status | Owner Branch |
|---|---|---|---|---|
| **عامل A** | `P0-security-batch` | core-security | active | `arena/01a0c316-nexus-ai-agent` |
| **عامل B** | `feature-wiring-batch` | feature-wiring | active (PR #32 open) | `arena/01a0c34d-nexus-ai-agent` |
| **عامل C** | `nagar-wave6-motion-graphics-delivery` | nagar-creative-motion | active | `arena/01a0c36f-nexus-ai-agent` |

---

## شبکه ۱۰ وظیفه کلیدی بعدی (10 Forward Tasks Network)

برای ایجاد هماهنگی بی‌نقص موازی در سشن‌های آینده، ۱۰ کار اولویت‌دار بعدی به تفکیک حوزه و مسیرهای انحصاری به‌روزرسانی شده‌اند:

1. **[عامل A] تسک ۱ — سخت‌سازی امنیت سراسری (`P0-security-batch`):** پیاده‌سازی Global Auth Middleware در `bot/middleware.py` و رفع PII در `/api/dashboard` (مسیرهای اختصاصی هسته و امنیت).
2. **[عامل B] تسک ۲ — سیم‌کشی ابزارهای تلگرام (`feature-tools-wiring`):** اتصال موتورهای `features/tools.py` (ماشین‌حساب، یادآور، مترجم) به هندلرهای تلگرام.
3. **[عامل B] تسک ۳ — احیای بازی‌ها و ریفرال (`feature-games-referral-wiring`):** اتصال WordleFA، نظرسنجی و فعال‌سازی متد `ReferralEngine.process_referral` و تزریق بات به فورس‌جوین.
4. **[عامل C] تسک ۴ — استودیوی موشن گرافیک و ترنزیشن (`nagar-wave6-motion-graphics`):** پیاده‌سازی پکیج `nexus.motion.graphics` (ترنزیشن‌های xfade، کی‌فریم، موشن بلور، درخشش و تایتل متحرک).
5. **[عامل C] تسک ۵ — استودیوی صدای نگار (`nagar-wave5-audio-studio`):** پیاده‌سازی پکیج `nexus.audio.studio` (تحلیل تمپو، نرمال‌سازی EBU R128، منحنی Ducking موزیک) — [تکمیل شد ✅].
6. **[عامل C] تسک ۶ — استودیوی رنگ و تحویل استاندارد سینمایی (`nagar-color-delivery-pack`):** پیاده‌سازی پکیج `nexus.color.delivery` (3D LUTs، اکسپورت OpenTimelineIO، پروکسی و رندر 4K) — [تکمیل شد ✅].
7. **[عامل بعدی] تسک ۷ — لاغرسازی حیاتی بسته‌های پایتون (`core-packaging-slimming`):** تفکیک extras در `pyproject.toml` به `[creative]`, `[rag]`, `[speech]` جهت کاهش حجم ایمیج از ۷GB به زیر ۲۰۰MB.
8. **[عامل بعدی] تسک ۸ — همگام‌سازی ناهمگام دیتابیس (`async-db-harmonization`):** حذف sync engineها از ماژول‌های فیچر و انتقال به `core/async_db.py` جهت رفع بلاک شدن لوپ تلگرام.
9. **[عامل بعدی] تسک ۹ — بهینه‌سازی و ضدبرخورد RAG (`rag-chroma-sanitization`):** اصلاح تقسیم‌بندی اسناد با overlap و شناسه یکتا جهت جلوگیری از کرش پایگاه داده برداری.
10. **[عامل بعدی] تسک ۱۰ — اتصال چندزبانه کامل پاسخ‌های بات (`i18n-handler-binding`):** اتصال فرهنگ لغت ۱۵ زبانه به تمامی خروجی‌های کاربری بات تلگرام به جای رشته‌های ثابت انگلیسی.

Full details: `.agents/board.json` · Protocol: `docs/MULTI_AGENT_PROTOCOL.md`
(فارسی: `docs/MULTI_AGENT_PROTOCOL.fa.md`)

## Merge order & shared files

`src/nexus_ai_agent/bot/handlers.py` is the single highest-conflict file in the repo.
It is listed in **both** zones' `exclusive_paths` on purpose: whoever holds the active
lease owns it; the other agent must not touch it until the lease is released and the
PR is merged. After a merge to `main`, rebase your branch on `main` before continuing.
