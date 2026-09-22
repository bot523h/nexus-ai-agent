# گزارش موج بهداشت مخزن — ۲۰۲۶-۰۹-۲۲ (task-162)

- **عامل:** سشن `arena/01a0cb43-nexus-ai-agent`
- **مجری به دستور:** مالک مخزن («سازماندهی مهندسی‌شدهٔ کل ریپو + پاک‌سازی شاخه‌های مرده + اقدامات ضروری، بدون اختلال در دو عامل درحال‌کار»)
- **پایه:** `main @ a997aab5` (پس از ادغام PR#55) · **تخته:** claim `task-162-repo-hygiene-2026-09-22`، zone `repo-hygiene`، بدون gates (واگذار به gates owner)
- **روش:** فقط‌خواندنیِ تشخیص + تغییرات حداقلیِ هم‌مرز با حصارهای زنده؛ هر عدد با دستور بازتولیدش آورده شده است.

> English summary: janitorial wave ordered by the repo owner — 15 merged/superseded remote branches
> deleted with per-PR evidence, three dated root reports archived to `docs/audits/` (history-preserving
> `git mv`, zero content change), docs index updated, and zero-drift diagnostics recorded (ruff 0
> findings / 447 formatted, guard tests green, version lock-step ok). The two live agents
> (`arena/01a0c992`, `arena/01a0ca9c`), both open PR branches (#33, #55), and every brand-new session
> branch were left untouched. Full reproduction commands in §7.

---

## ۱) حکم اجرایی

| موضوع | وضعیت راستی‌آزمایی‌شده |
|---|---|
| شاخه‌های ریموت | **۲۴ → ۹ ref** — ۱۵ شاخهٔ مردهٔ ادغام‌شده/جایگزین‌شده حذف شد؛ هیچ شاخهٔ زنده‌ای لمس نشد (§۳) |
| ریشهٔ مخزن | ۳ سند مورخهٔ سشنی با `git mv` به `docs/audits/` منتقل و در `docs/README.md` ایندکس شد — **تغییر محتوا: صفر بایت** |
| کیفیت (تشخیص فقط‌خواندنی) | `ruff check` ✅ ۰ خطا · `ruff format --check` ✅ ۴۴۷ فایل تمیز (ruff پین‌شدهٔ 0.16.8، هم‌تراز با CI) |
| تست‌های نگهبان | board (۳ فایل، 39 تست) ✅ · docs-integrity ✅ · version-lockstep ✅ · `scripts/check_version_lockstep.py` ✅ |
| عدم‌تداخل | `agent_board.py check --files …` → **no overlap**؛ صفر فایل از فهرست ۵۲تایی PR#33؛ صفر فایل از حصارهای زنده |

---

## ۲) قواعد عدم‌اختلال (چه کسانی محافظت شدند)

در لحظهٔ اجرا این اجاره‌ها/مسیرها **زنده** بودند و هیچ‌کدام لمس نشده‌اند:

| مالک | اجارهٔ زنده | انقضا | حصار |
|---|---|---|---|
| `arena/01a0c992-nexus-ai-agent` | `task-145-ci-gates-handoff-refresh` (gates owner) | 2026-09-23T15:45Z | `.github/workflows/` |
| `arena/01a0ca9c-nexus-ai-agent` | `pr47-feature-wiring-continuation` (`active_in_review`) | 2026-09-23T17:30Z | `bot/surface/`, `handlers.py`, `features/{gamification,rag,rag_core}.py`, تست‌های surface/rag، `CHANGELOG.md`, `docs/audits/PR32_TRIAGE_2026-09-21.md` |
| PR#33 (باز) | branch `arena/01a0c3aa` — ۵۲ فایل در دست (شامل `README.md`, `AGENTS.md`, `CHANGELOG.md`, `VERSION`, `.nexus/continuum.json`, `pyproject.toml` …) | — | فهرست کامل: `gh pr view 33 --json files` |

نکتهٔ آگاهانه: بنر `README.md` («Version: v3.12.0» در برابر 3.13.0 فعلی) **عمداً دست‌نخورده** مانده است —
مالکیتش با PR#33 باز است و بقایای lockstep قبلاً به‌صورت `task-135`/`task-142-lockstep-residue-post-pr33`
روی تخته ثبت شده و مرحله‌بندی‌اش «پس از PR#33» است. (ورود هم‌اکنون = اختلال در کار عامل دیگر.)

## ۳) تصمیم شاخه‌به‌شاخه

### ۳.۱ حذف‌شده — ۱۵ شاخهٔ مرده (محتوایشان به main رسیده است)

ملاک: PR زده و **MERGED** (یا بسته‌شدهٔ رسماً جایگزین‌شده) + بدون اجارهٔ زنده + بدون PR باز.
قرارداد مخزن: شاخه‌های PRهای #۳ تا #۳۱ قبلاً پس از ادغام حذف شده بودند؛ این موج همان قرارداد را ادامه می‌دهد.

| شاخه | شاهد |
|---|---|
| `arena/01a0c316` | PR#30 MERGED (2026-09-21T09:02Z) |
| `arena/01a0c34d` | PR#32 CLOSED — رسماً superseded: ارزش یکتا در PR#47 پورت شد (handoff گیت‌ها) |
| `arena/01a0c460` | PR#36, PR#38 MERGED |
| `arena/01a0c484` | PR#37 MERGED |
| `arena/01a0c4bb` | PR#40 CLOSED — جایگزین: PR#41 MERGED (`docs/audits/FORENSIC_PR40_CI_2026-09-21.md`) |
| `arena/01a0c4c1` | PR#39 MERGED |
| `arena/01a0c506` | PR#41 MERGED |
| `arena/01a0c54f` | PR#42 MERGED |
| `arena/01a0c58a` | PR#43 MERGED |
| `arena/01a0c58e` | PR#45 MERGED |
| `arena/01a0c593` | PR#44 MERGED |
| `arena/01a0c5da` | PR#46 MERGED |
| `arena/01a0c634` | PR#47 MERGED |
| `arena/01a0c649` | PR#48 MERGED |
| `arena/01a0c920` | PR#49 MERGED |

### ۳.۲ نگه‌داشته‌شده — ۸ شاخه + main

| شاخه | چرا؟ |
|---|---|
| `arena/01a0c3aa` | **PR#33 باز است** — حذف = بستن اجباری PR و اختلال مستقیم |
| `arena/01a0c992` | اجارهٔ زندهٔ gates owner (task-145)؛ یکی از «دو عامل درحال‌کار» |
| `arena/01a0ca9c` | اجارهٔ زندهٔ `active_in_review`؛ عامل دومِ درحال‌کار |
| `arena/01a0cb38` | سشن تازه (PR#54/#55 هر دو امروز ادغام شدند)؛ احتمال ادامهٔ فعالیت |
| `arena/01a0cb3f` | سشن تازهٔ امروز (claimهای task-159/160/161 روی شاخه) — کار باز |
| `arena/01a0cb4d` | سشن تازهٔ امروز (بستهٔ اطلاعاتی تحقیقاتی) — کار باز |
| `arena/01a0c625` | **بدون PR**؛ اسنپ‌شات هماهنگی ۰۲۵ که بخش اعظمش روی تخته ثبت شده — تصمیم نهایی با مالک (§۶) |
| `arena/01a0cb43` | شاخهٔ همین سشن |

## ۴) سازماندهی ریشه

سه سند مورخهٔ سشنی از ریشه به آرشیو رسمی منتقل شدند (کنوانسیون موجود: اسناد مورخه در `docs/audits/`،
منبع حقیقت نیستند؛ جاروی مشابه دیروز در PR#37 انجام شده بود و این سه سند باقی‌ماندهٔ بعد از آن بودند):

| قبل | بعد | تغییر محتوا |
|---|---|---|
| `COORDINATION_2026-09-21.md` | `docs/audits/COORDINATION_2026-09-21.md` | ۰ بایت (rename خالص) |
| `WAVE5_COORDINATION_01a0c5da.md` | `docs/audits/WAVE5_COORDINATION_01a0c5da.md` | ۰ بایت (rename خالص) |
| `STORAGE_RESILIENCE_CHECKUP_2026-09-21.md` | `docs/audits/STORAGE_RESILIENCE_CHECKUP_2026-09-21.md` | ۰ بایت (rename خالص) |

- ارجاع متنی `docs/audits/WAVE5_ACTIVATION_2026-09-21.md` به `WAVE5_COORDINATION_01a0c5da.md` حالا **واقعاً** به همسایهٔ خودش اشاره می‌کند (قبلاً به ریشه).
- ریشهٔ نهایی: `README · CHANGELOG · CONTRIBUTING · LICENSE · AGENTS · ROADMAP_STATUS · REQUIREMENTS_LEDGER · VERSION` + کانفیگ‌ها — اسناد زنده (ردایف `../ROADMAP_STATUS.md` و `../REQUIREMENTS_LEDGER.md` در خودِ ایندکس docs به ریشه اشاره می‌کنند و عمداً در ریشه می‌مانند).
- `docs/README.md` — چهار ردیف به جدول Audits اضافه شد (۳ سند منتقل‌شده + همین گزارش)؛ گیت `test_docs_integrity.py` روی ایندکس/لینک‌ها سبز است.

## ۵) تشخیص‌های فقط‌خواندنی (شواهد صفر-دریفت)

```
ruff 0.16.8            → All checks passed!           (exit 0)
ruff format --check .   → 447 files already formatted   (exit 0)
pytest -q tests/unit/test_agent_board.py tests/unit/test_agent_board_active_in_review.py \
          tests/unit/test_agent_board_pr_visibility.py tests/unit/test_version_lockstep.py
                        → 39 passed
pytest -q tests/unit/test_docs_integrity.py
                        → 47 passed
python3 scripts/check_version_lockstep.py
                        → version lock-step ok: VERSION == pyproject == CHANGELOG == 3.13.0
python3 scripts/agent_board.py check --files <8 مسیر تغییرکردهٔ این موج> --branch arena/01a0cb43-…
                        → no overlap — safe to proceed. (exit 0)
```

گیت‌های کامل (`make lint/types/test`) طبق قرارداد به gates owner واگذار می‌شود؛ موارد بالا همان
«diagnostic checks» مجازِ قانون ۴ هستند.

## ۶) بدهی/تصمیم باقی‌مانده برای مالک

1. **`arena/01a0c625`** — تنها شاخهٔ بدون PR؛ اسنپ‌شات پینِ تختهٔ «دو نیمهٔ نگار» (محتوای عملیاتی‌اش در
   تخته و PRهای #۴۵/#۴۶ تحویل شده). اگر تأیید شود که دیگر لازم نیست، یک دستور حذف کافی است:
   `git push origin --delete arena/01a0c625-nexus-ai-agent`
2. **`arena/01a0c992` / `arena/01a0ca9c`** — پس از پایان اجاره‌ها (فردا) طبق همان قرارداد
   «حذف پس از ادغام» قابل پاک‌سازی‌اند؛ هم‌اکنون حذفشان «اختلال در دو عامل درحال‌کار» بود.
3. **بنر نسخهٔ README (v3.12.0)** — مرحله‌بندی‌شده برای پس از PR#33 (`task-142-lockstep-residue-post-pr33`).

## ۷) بازتولید (Reproduction)

```bash
git ls-remote --heads origin                                   # فهرست زندهٔ refها (اکنون 9)
gh pr list --state all --limit 60 --json number,state,headRefName,mergedAt
grep -n "audits/" docs/README.md                               # چهار ردیف جدید جدول Audits
git log --follow --oneline -- docs/audits/COORDINATION_2026-09-21.md   # تاریخچهٔ rename
PYTHONPATH=src pytest -q tests/unit/test_docs_integrity.py     # 47 passed
python3 scripts/agent_board.py show | grep -A4 task-162        # اجارهٔ این موج
```

---

*Record type: dated audit (immutable). Living truth stays in `docs/architecture/*` and the board.*
