# منشور عالی هماهنگی چندعاملی مهندسی‌شده (Pro-Max Zero-Collision Parallel Network)

> **قانون قطعی و تخطی‌ناپذیر سیستم (100% Mandatory Compliance):**
> «این مخزن تحت نظارت و توسعه موازی چند عامل هوش مصنوعی (عامل A، عامل B، عامل C) اداره می‌شود.
> هر عامل موظف است **بدون حتی یک میلی‌متر تداخل** با حوزه‌های انحصاری سایر عامل‌ها، با بالاترین کیفیت مهندسی در سطح Anthropic و OpenAI کار خود را به صورت هوشمندانه، بدون پس‌رفت (Zero Regression) و بدون باگ به سرانجام برساند.»

---

### قوانین بنیادین شبکه موازی عامل‌ها

1. **اعلام هویت رسمی در بدو ورود:** هر عاملی که وارد سشن جدید می‌شود باید در اولین گام هویت خود را مشخص کند:
   - «من عامل A هستم» (`arena/01a0c316-...` مالک امنیت هسته و گیت‌ها)
   - «من عامل B هستم» (`arena/01a0c34d-...` مالک سیم‌کشی فیچرها)
   - «من عامل C هستم» (`arena/01a0c36f-...` مالک استودیوی نگار — سشن تکمیل و آزاد شد)
   - «من عامل D هستم» (`arena/01a0c3ca-...` تحلیل سیستم، پاک‌سازی تخته و واگذاری مهندسی‌شده — سشن تکمیل و آزاد شد)
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
| **عامل A** | `P0-security-batch` | core-security | active (gates_owner) — PR #34 open | `arena/01a0c316-nexus-ai-agent` |
| **عامل B** | `feature-wiring-batch` | feature-wiring | active — PR #32 open, needs rebase | `arena/01a0c34d-nexus-ai-agent` |
| **عامل C** | `nagar-wave3-timeline-edit-delivery` | nagar-creative-edit | ✅ completed — PR #31 merged (`c41b1b0`), lease released | `arena/01a0c36f-nexus-ai-agent` |
| **عامل D** | `board-gc-engineered-handoff` | coordination | ✅ completed — board cleanup + 10-task engineered handoff | `arena/01a0c3ca-nexus-ai-agent` |
| **عامل E** | `task-107` (packaging) + `task-110` (delivery-interop) | core-packaging, delivery-interop | active — PR #33 open (v3.13.0 security code + feature wiring) | `arena/01a0c3aa-nexus-ai-agent` |

**Closing a claim.** `release` is not enough on its own: write what was actually
delivered into the claim's `delivered` list and point `evidence` at the tests that
prove it. PR#30 merged with an active lease whose declared scope was never
committed, and the next agent had to rediscover that by reading the code.

**Orphaned-lease rule (learned 2026-09-21).** A claim whose branch has already been
merged to `main` is finished even if its lease has not expired — the owning sandbox
is gone and nobody will ever run `release`. If `show` reports an `active` lease whose
`agent_branch` is merged, the next agent may reclaim it, but MUST record the takeover
in `.agents/board.json → takeover_log` with the merge commit as evidence. `gc_expired()`
cannot catch these because TTL is measured from `claimed_at`, not from the merge.

**Overlapping PRs are a board failure, not a git failure.** PR #32 (عامل B) and
PR #33 (عامل E) both wired the same feature engines because the board was rewritten
between the two claims. When two open PRs touch the same zone, the *owner* picks one
and the other is rebased onto it — never silently merged in sequence.

> **⚠️ INCIDENT (2026-09-21) — RESOLVED by عامل D ✅:** PR #31 was merged to `main` with a red
> lint gate (71 ruff errors + 16 unformatted files; CI run
> [35594818988](https://github.com/bot523h/nexus-ai-agent/actions/runs/35594818988) died at `ruff check .`).
> عامل D claimed **task-101**, fixed all 71 errors mechanically (zero semantic change — proven by
> 180/180 targeted tests, 749 suite passes, and an A/B `git stash` reproduction of the 20
> pre-existing env failures on the base commit), and opened the restore PR from
> `arena/01a0c3ca-nexus-ai-agent`. Full evidence: `docs/HANDOFF_ANALYSIS_2026-09-21.md`.
> Until that PR merges, treat `main` as RED.

---

## شبکه ۱۰ وظیفه کلیدی بعدی (10 Forward Tasks Network — نسخه مهندسی‌شده ۲۰۲۶-۰۹-۲۱)

تسک‌های تکمیل‌شده قبلی (موج ۳ تدوین، موج ۶ موشن، موج ۵ صدا، موج ۷ رنگ/تحویل) از تخته حذف شدند.
۱۰ وظیفه بعدی با معیار پذیرش، مسیر انحصاری گسسته و ابزارهای تحقیق‌شده رایگان در
`.agents/board.json` مستند شده‌اند؛ خلاصه اولویت‌بندی شده:

1. **[P0 — ✅ انجام شد توسط عامل D] تسک ۱۰۱ — نجات گیت‌های کیفیت (`ci-restore`):** ۷۱ خطای ruff → ۰ (۲۸×F401 خودکار، ۲×I001، ۴۰×E501 با شکست رشته‌های همسان-بایت، ۱×F841 به fail-fast بدون انتساب) + فرمت ۱۶ فایل. اثبات صفر-رگرسیون: 180/180 هدفمند + 749 گسترده + بازتولید A/B شکست‌های محیطی روی commit پایه. PR از شاخه سشن عامل D باز است.
2. **[P0 — عامل A] تسک ۱۰۲ — بچ امنیتی (`P0-security-batch`):** بستن P0-1 تا P0-10 ممیزی؛ معیار پذیرش: AuthMiddleware روی تمام مسیرهای خصوصی، حذف PII از `/api/dashboard/recent_users`، گارد Path Traversal با `resolve()+is_relative_to`، احراز واقعی فورس‌جوین، گیت رضایت خروجی LLM.
3. **[P1 — عامل B] تسک ۱۰۳ — ادغام PR#32 (`feature-wiring`):** rebase روی main سبز، حل تداخل `board.json` به سود آخرین وضعیت واگذاری، هماهنگی `handlers.py` با عامل A.
4. **[P1 — عامل آزاد] تسک ۱۰۴ — نگار موج ۸، «لاین اعمال» (`nagar-render-lane`):** از IR خالص پک‌ها تا یک انکد واقعی FFmpeg — الگوی موج 2c: plan → IR → filtergraph → argv → یک پروسه → probe اثبات‌شده؛ نگاشت فنی هر عملیات (atrim/xfade/loudnorm دوماسه/sidechaincompress/tpad/tmix) در تخته.
5. **[P1 — عامل آزاد] تسک ۱۰۵ — نگار موج ۹، موتور محلی گفتار (`nagar-caption-engine`):** faster-whisper (CTranslate2، int8، بدون torch) پشت `CaptionEnginePort` به‌عنوان extra `[speech]`؛ پیاده‌سازی align_words/diarize/translate_local (argos-translate آفلاین)؛ `pending` کپشن از ۳ به ۰.
6. **[P1 — عامل آزاد] تسک ۱۰۷ — بسته‌بندی مدرن (`core-packaging`):** تفکیک با PEP 735 (`[dependency-groups]` برای ابزار توسعه) + extras واقعی `[rag]/[speech]/[translate]/[local-llm]/[r2]`، نصب CI با uv، Docker چندمرحله‌ای slim؛ هدف: هسته <۲۵۰MB بدون torch/chroma/llama.
7. **[P1 — عامل آزاد] تسک ۱۰۸ — دیتابیس تمام‌ناهمگام (`core-database`):** حذف sync `create_engine` از features/* به سود یک `create_async_engine` مرکزی با session-per-task + آزمون نگهبان معماری.
8. **[P2 — عامل آزاد] تسک ۱۰۹ — RAG و i18n (`features-rag-i18n`):** چانکینگ بازگشتی ۲۵۶–۵۱۲ توکن با همپوشانی ۱۰–۲۰٪، بازیابی هایبرید BM25+وکتور+rerank، هارنس recall@k، اتصال کلیدهای ۱۵ زبانه به همه خروجی‌ها + آزمون برابری کلیدها.
9. **[P2 — عامل آزاد] تسک ۱۱۰ — درون‌سازی OTIO و بدهی پورت‌ها (`delivery-interop`):** تست round-trip خروجی export_otio با کتابخانه واقعی OpenTimelineIO (dev-extra)، بستن شکاف ConversationStorePort (آداپتور یا ADR)، spike امضای ed25519 برای manifest.
10. **[P2 — عامل آزاد، پس از PR#32] تسک ۱۰۶ — نگار موج ۱۰، سطح تلگرام (`creative-surface`):** فرمان‌های /edit و /caption و /grade روی فایل جدید `bot/creative_surface.py` (بدون لمس handlers.py) از مسیر JobQueuePort و لاین رندر.

Full details + acceptance criteria: `.agents/board.json` · Analysis: `docs/HANDOFF_ANALYSIS_2026-09-21.md` · Protocol: `docs/MULTI_AGENT_PROTOCOL.md` (فارسی: `docs/MULTI_AGENT_PROTOCOL.fa.md`)

## Merge order

1. **task-101** (lint restore — ✅ delivered by عامل D, PR open from `arena/01a0c3ca-nexus-ai-agent`) merges first — everything else rebases on a green `main`.
2. **PR #32** rebases, resolves the `.agents/board.json` conflict in favor of *this* handoff state, then merges (handlers.py coordination with عامل A).
3. task-102 / 104 / 105 / 107 / 108 / 109 / 110 proceed **in parallel on disjoint paths**.
4. **task-106** (creative surface) lands after PR #32's `bot/surface` pattern is on `main`.

`src/nexus_ai_agent/bot/handlers.py` remains the single highest-conflict file. It is locked to
عامل A's security batch until that PR merges; عامل B's surface layer deliberately avoids it.
