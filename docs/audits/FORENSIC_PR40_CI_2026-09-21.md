# گزارش جنایی CI — قرمزی job «test» در PR#40 (سشن `01a0c4bb`) — ۲۰۲۶-۰۹-۲۱

- **عامل:** I (سشن `arena/01a0c506-nexus-ai-agent`) — طبق قاعده گیت‌ها: «عیب‌یابی فقط-خواندنی همیشه مجاز است»؛ **صفر خط به شاخه مالک (`01a0c4bb`) نوشته نشد.**
- **موضوع:** سه run متوالی CI روی PR#40 در job `test (pytest -m "not slow")` قرمز می‌شوند؛ `lint` و `migrate-postgres` سبزند؛ مالک محلاً «1161 passed» گزارش کرده بود.
- **نتیجه:** **ریشه پیدا، بازتولید و رفعِ آن راستی‌آزمایی شد.** عیب ۲ سطر است؛ شواهد کامل زیر.

---

## ۱) حکم اجرایی

**علت ریشه‌ای:** دو تست بنچمارک جدید (`tests/bench/test_render_bench.py`) داخل خودِ تابع `from scripts.bench_render import ...` می‌کنند. پوشهٔ `scripts/` نه `__init__.py` دارد، نه پکیج نصب‌شده است (src-layout؛ فقط `nexus_ai_agent` نصب می‌شود). بنابراین این import فقط وقتی کار می‌کند که **ریشهٔ مخزن روی `sys.path`** باشد:

| نحوه اجرا | `sys.path` شامل ریشه مخزن؟ | نتیجه |
|---|---|---|
| `python -m pytest` (اجرای محلی مالک) | ✅ بله — `python -m` مسیر CWD را در `sys.path[0]` می‌گذارد | **1161 passed** — سبزیِ صادقانهٔ مالک |
| `pytest` اسکریپت کنسول (دستور دقیق CI) | ❌ خیر — فقط `venv/bin` و مسیرهای نصب | `ModuleNotFoundError: No module named 'scripts'` → **2 FAILED** → job قرمز |

این دقیقاً توضیح می‌دهد چرا (۱) هر سه run با همان الگو قرمز شدند، (۲) دو job دیگر سبز ماندند (`lint` فقط `test_version_command.py -k lockstep` را اجرا می‌کند که `scripts` را import نمی‌کند؛ `migrate-postgres` فقط سه فایل integration را — هیچ‌کدام `scripts.*` را import نمی‌کنند)، (۳) تلاش دیباگ مالک (verbose + artifact) هنوز به جواب نرسیده است.

## ۲) زنجیرهٔ اثبات (A/B روی همان commit `67535f4`)

محیط: venv تمیز، Python 3.11.2، نصب `pip install -e . --no-deps` + مجموعه وابستگی‌های سبک/متوسط (سنگین‌ها: torch/chroma/llama/flashrank/boto3 نصب نبودند — در تحلیل «بخش ۵» مستثنا شده‌اند).

```text
$ python -m pytest -q -m "not slow"          →  1161 passed, 20 skipped, 4 warnings   (54.5s)
$ pytest -q tests/bench/test_render_bench.py →  2 failed, 1 passed
     test_render_bench_within_tolerance  → ModuleNotFoundError: No module named 'scripts'
     test_caption_bench_within_tolerance → ModuleNotFoundError: No module named 'scripts'
$ pytest -q -o "pythonpath=." -m "not slow"  →  1161 passed, 20 skipped, 4 warnings   (54.4s)  ← همان suite با فیکس یک‌سطری، از طریق کنسول اسکریپت
```

- `grep -rn "from scripts\|import scripts" tests/ --include="*.py"` → **فقط همان ۲ سطر** در `tests/bench/test_render_bench.py:28,44`. هیچ تست دیگری این کلاس خطا ندارد.
- شمارش‌ها عیناً با ادعای مالک (1161/20) می‌خوانند — ادعای سبزیِ محلی او درست بود؛ فقط مکانیزم اجرا متفاوت بوده است.
- زمان job قرمز CI (~۶.۵ دقیقه) با «اجرای کامل suite و قرمزی در انتها» سازگار است (bench تست‌ها fail می‌شوند ولی `-x` وجود ندارد و suite ادامه می‌یابد).

## ۳) رفع — دو گزینه (هر دو راستی‌آزمایی‌شده/طراحی‌شده)

### گزینه A — فوری، یک سطر (برای سبز شدن PR#40)
در `pyproject.toml` بخش موجود:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
pythonpath = ["."]   # ← اضافه شود (pytest>=7؛ بدون وابستگی جدید)
```

مستند در بخش ۲: با این سطر، `pytest` کنسول‌اسکریپت هم کل suite را 1161/20 سبز اجرا می‌کند.

⚠️ **هماهنگی:** `pyproject.toml` زون انحصاری PR#33 (`core-packaging`) هم هست؛ PR#40 خودش امروز این فایل را تغییر داده (override‌های mypy) پس درون PR#40 مشکلی نیست، فقط در توضیح merge-order قید شود که هر دو PR به `pyproject.toml` می‌رسند.

### گزینه B — معماری (توصیه‌شده به‌عنوان گام بعد، داخل همین PR#40 یا بلافاصله پس از آن)
منطق بنچ را به داخل پکیج نصب‌شده منتقل کن؛ اسکریپت‌ها فقط CLI لاغر بمانند:

```text
scripts/bench_render.py  → از nexus_ai_agent.creative.rendering.bench import کند
src/nexus_ai_agent/creative/rendering/bench.py       (bench_render_ir_compile)
src/nexus_ai_agent/creative/caption/bench.py         (bench_caption_format)
tests/bench/test_render_bench.py          → import از پکیج (بدون هیچ path-hack)
```

فایده: منطق قابل‌اندازه‌گیری جزو آرتیفکت نصب‌شده می‌شود (قابل استفاده در عملیات واقعی)، و کلاس خطای «import از مخزن نصب‌نشده» برای همیشه بسته می‌شود.

### گزینه‌های ردشده و دلیل
- `sys.path.insert` در خود تست: کار می‌کند ولی path-magic تکرارشونده است.
- `conftest.py` در ریشه که ریشه را به sys.path اضافه کند: جادوی پنهان، برای خوانندهٔ بعدی نامفهوم.
- پکیج‌کردن `scripts/`: مسیر اشتباه — اسکریپت‌ها کتابخانه نیستند.

### پاشنه — آزمون نگهبان (تا این کلاس خطا بازنگردد)
```python
# tests/architecture/test_scripts_import_boundary.py
"""No test may import the unpackaged `scripts` namespace — CI runs the
console-script pytest where the repo root is not on sys.path."""
from pathlib import Path

def test_no_test_imports_scripts_namespace() -> None:
    offenders = [
        p for p in Path(__file__).parents[1].rglob("*.py")
        if "from scripts" in p.read_text(encoding="utf-8")
        or "\nimport scripts" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"tests importing unpackaged scripts/: {offenders}"
```
(همچنین job `migrate-postgres` هم `pytest` کنسول‌اسکریپت را روی integrationها اجرا می‌کند — امروز بی‌خطر است، ولی همین نگهبان آن را برای همیشه بی‌خطر نگه می‌دارد.)

## ۴) یافتهٔ هماهنگی دوم — ریسک دو-delivery تسک ۱۱۱ (قفل نسخه)

هر دو PR باز، گیت VERSION↔pyproject↔CHANGELOG را پیاده کرده‌اند:

| | PR#39 (`test_version_lockstep.py`، فقط-تست) | PR#40 (`test_version_command.py` + job lint + pre-commit) |
|---|---|---|
| لایه اجرا | فقط pytest | pytest + **گیت واقعی CI** + pre-commit |
| معناشناسی `[Unreleased]` | صریح: «Unreleased هیچ‌وقت راضی‌کننده نیست» | هر heading نیم‌ورژن؛ Unreleased ذاتاً match نمی‌شود |
| بررسی اضافه | metadata توزیع نصب‌شده (skip محسوس) | `running_version` هندلر `/version` |

**توصیه dedupe:** گاردِ #40 را نگه دار (اجرا در CI + pre-commit دارد)؛ در rebase/merge بعدی، دو معنای برتر #39 به گارد بازمانده منتقل شود: (۱) رفتار سخت‌گیرانهٔ `[Unreleased]`، (۲) چک metadata توزیع. سپس فایل `test_version_lockstep.py` حذف/ادغام شود تا دو گارد واگرا روی main نسازمیم.

## ۵) محدوده اطمینان این تحلیل
- لاگ خام CI از این sandbox قابل دریافت نبود (خروجی به blob storage با خطای SSL مسدود شد؛ سپس نصب GitHub App معلق شد). جبران شد با **بازتولید کامل محلی دو-طرفه** (شکست بدون فیکس با اجرای CI-سبک، سبزی با فیکس) که قوی‌تر از خواندن لاگ است.
- سنگین‌ها (torch/chromadb/flashrank/boto3/llama) در محیط بازتولید نصب نبودند؛ اما (۱) مجموعه تست‌ها با همان شمارش 1181 collect و 1161/20 اجرا شد، (۲) هیچ‌یک از ۲ تستِ خطاخورده به آن‌ها وابسته نیست، (۳) grep نشان داد فقط `test_r2_provider` و `test_local_server_provider` به boto3/llama نزدیک‌اند و هر دو سبز جمع‌آوری/اجرای محلی بودند. بنابراین قرمزی CI با این تحلیل کاملاً تبیین می‌شود؛ اگر پس از فیکس A باز هم قرمزی ماند (انتظار نمی‌رود)، artifact `pytest-log` که مالک در run بعدی می‌گیرد مرجع است.

## ۶) اقدام پیشنهادی برای مالک `01a0c4bb` (حداقل → کامل)
1. سطر `pythonpath = ["."]` در `[tool.pytest.ini_options]` → پوش → هر دو ref (push + pull_request) سبز می‌شود.
2. آزمون نگهبان بخش ۳ را اضافه کن (۳ خط، کلاس خطا را می‌بندد).
3. گام بعدی session: انتقال منطق بنچ به پکیج (گزینه B) + dedupe تسک ۱۱۱ با #39 طبق بخش ۴.
