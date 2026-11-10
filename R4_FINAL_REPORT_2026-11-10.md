# NEXUS — PR #325 F5/R4 Architectural Closure — گزارش نهایی تحویل

**وضعیت تحویل: `VERIFIED_WITH_LIMITATIONS`**

> این گزارش **مجوز ادغام نیست**. ادغام منوط به بازبینی مستقل و عبور از Gate است (طبق بخش ۸ مأموریت).
> شاخه: `fix/binding-ownership-fence-20261107` · مخزن: `bot523h/nexus-ai-agent` · PR: #325
> تاریخ گزارش: 2026-11-10 · Task board: `task-282-slice-2-artifact-assetversion-binding` (claim فعال، `generation: 3`)

---

## ۱) SHA اولیه و نهایی

| مورد | SHA |
|---|---|
| SHA اولیهٔ مشاهده‌شده (HEAD ورودی مأموریت) | `1d878375986644023292ca88d92906abf914ce4e` |
| کامیت اصلاح F5/R4 | `200553af` — «fix(task-282): per-job artifact binding identity closes F5/R4» |
| کامیت رفع رگرسیون golden (CI) | `a50b2c77fd942ca0473c26c9d4e1f1995979cc10` — «repin the PostgreSQL checkpoint golden for head 20261108perjobbind» |
| **SHA نهایی تحویل** | **`a50b2c77fd942ca0473c26c9d4e1f1995979cc10`** |
| `main` مبنا | `5fe9451228ad6e4d5ac067aaa4cbd2df8c633cd4` |
| هم‌گرایی با main | merge-base(HEAD, main) = `5fe94512…` — شاخه **۰ عقب / ۱۹ جلو** (fully converged) |

هیچ Force-Push، هیچ نوشتن روی `main`، و هیچ تغییر مستقیم روی شاخهٔ PR #327 انجام نشد.

---

## ۲) Root Cause بازتولیدشده

**جدول `projectartifactbinding` کلید سراسری روی محتوای باینری داشت، نه روی Provenance هر Job.**

- کلید اصلی قدیمی: `PRIMARY KEY(artifact_id)` + `UNIQUE(project_id, artifact_id)`.
- پس دو Job که مصنوع **بایت‌یکسان** (همان Content Address / همان SHA-256) تولید می‌کنند، **یک ردیف Provenance مشترک** داشتند.
- `retract(artifact_id, job_id, attempt)` آن ردیف مشترک را حذف می‌کرد؛ اگر Job A در CAS پایانی `COMPLETED` شکست می‌خورد و retract اجرا می‌شد، **Provenance متعلق به Job B (که COMPLETED شده بود) نابود می‌شد.**

De-duplication باینری (SHA-256) در Asset Library **صحیح** است و حفظ شد؛ چیزی که باید مستقل می‌شد، **رکورد Provenance هر Job تولیدکننده** بود.

### بازتولید قطعی (Baseline Failure قبل از Fix)

با بازگرداندن موقت `store.py` و `models.py` به وضعیت پیش‌از‌فیکس (`f5b81939`) و اجرای همان تست‌های جدید روی کد قدیمی:

```
5 failed, 45 deselected

FAILED test_owner_retract_after_successor_completed_keeps_successor_binding
       AssertionError: assert 'job-A' == 'job-B'      ← ردیف Job B پس از retract فقط 'job-A' را resolve می‌کند
FAILED test_cross_job_adoption_race_keeps_both_jobs
       AssertionError: assert {'job-A'} == {'job-A', 'job-B'}   ← ردیف Job B ناپدید شد
FAILED test_exact_job_and_attempt_fence_each_row_independently
       TypeError: SqlArtifactBindingStore.resolve() got an unexpected keyword argument 'job_id'
FAILED test_many_jobs_sharing_identical_bytes_each_resolve
       AssertionError: assert 'j1' == 'j2'
FAILED test_same_artifact_conflicting_revisions_are_per_job
       ArtifactBindingConflict: artifact already bound to a different revision
```

پس از بازگرداندن Fix و اجرای همین ۵ تست: **`5 passed`**.
فایل‌های `store.py` و `models.py` بعد از آزمایش **byte-identical** به HEAD بازگردانده شدند (`diff -q` = تأیید).

---

## ۳) Migration و تغییرات Schema

مهاجرت جدید (new head): `migrations/versions/20261108_artifact_binding_per_job.py`
- `revision = 20261108perjobbind` ، `down_revision = 20261107bindfence`

تغییرات دقیقاً روی یک جدول — `projectartifactbinding`:

| قبل | بعد |
|---|---|
| `PRIMARY KEY(artifact_id)` | `PRIMARY KEY(artifact_id, job_id)` |
| `UNIQUE(project_id, artifact_id)` | حذف شد (مانع ردیف دومِ Job دوم بود) |
| `ix_projectartifactbinding_job_id` | حذف شد (`job_id` اکنون بخشی از PK است) |

سازگاری دادهٔ قدیمی:
- **همهٔ ردیف‌های موجود verbatim حفظ می‌شوند**؛ هر ردیف قدیمی از قبل `job_id` دارد (`""` برای فراخوانی خارج از صف) و صرفاً به ردیف همان Job تبدیل می‌شود — هیچ داده‌ای حذف/بازنویسی نمی‌شود.
- بازسازی جدول (`rename` → `create` → `INSERT … SELECT` → `drop`) انجام می‌شود چون تغییر PK روی SQLite با `ALTER` درجا ممکن نیست؛ شکل نهایی روی SQLite و PostgreSQL یکسان است.
- `downgrade()` صریحاً **lossy** است: کلید قدیمی فقط یک ردیف در هر Content Address را می‌پذیرد، بنابراین قدیمی‌ترین (`created_at`, tie-break: `job_id`) نگه داشته و بقیه حذف می‌شوند (مستند و آگاهانه، نه fail).

زنجیرهٔ head: `20261108perjobbind ← 20261107bindfence ← 20261106artbind ← 20261104dpstate ← d4e1c0a79b21 ← … ← f4a9c2e71b08`.

اثر جانبی روی Golden: چون fingerprint چک‌پوینت PostgreSQL شامل `migration_head` است، جلو رفتن head، golden را منسوخ کرد → بازتولید با
`nexus checkpoints golden update --backend postgres --yes` (human-only):
`d6457c71da11…` → `1eb9a990b845bf049726ceef2c05d26b497616b2be54b40f9d63646442e53bd2` (کامیت `a50b2c77`).

---

## ۴) فایل‌های تغییرکرده (محدودهٔ F5/R4)

**کامیت `200553af` (۱۴ فایل، +۹۶۷/−۱۴۵):**

| فایل | نقش |
|---|---|
| `migrations/versions/20261108_artifact_binding_per_job.py` | مهاجرت PK مرکب `(artifact_id, job_id)` |
| `src/nexus_ai_agent/storage/models.py` | مدل: `job_id` بخشی از PK، حذف UniqueConstraint |
| `src/nexus_ai_agent/creative/project_state/store.py` | `bind/resolve/retract` آگاه به هویت Job |
| `tests/integration/test_durable_project_revision_pinning.py` | ۸ تست اجباری F5/R4 (failure-first) |
| `tests/integration/test_artifact_binding_postgres_concurrency.py` | هم‌روندی چندپروسه‌ای PG |
| `tests/unit/test_migrations.py` | ارتقا/سازگاری ردیف‌های موجود |
| `scripts/durable_project_state_mutations.py` | ۳ mutation جدید (توکن‌محور/Job‌محور) |
| `.github/workflows/ci.yml` | اجرای کمپین mutation در CI |
| `tests/unit/test_mutation_harness_anchors.py` | لنگرهای هارنس mutation |
| ۵ فایل تست دیگر | هم‌راستاسازی با head/API جدید |

**کامیت `a50b2c77` (۲ فایل):** `src/nexus_ai_agent/storage/golden/postgres.langgraph.json` + `docs/architecture/DATA_AND_STORAGE.md`.

**مدل Binding (حداقل هویت مستقل):** `project_id`, `artifact_id`, `revision_id`, `job_id`, `attempt`, `artifact_version_id` — همه در PK/ستون‌ها حاضرند.

**سازگاری API:** `bind(..., job_id="", attempt=0)` و `resolve(..., job_id=None)` — **backward-compatible** برای مصرف‌کنندگان موجود.

---

## ۵) تست‌های Failure-first و نتایج

پوشش ۱۱ دستهٔ الزامی مأموریت:

| # | سناریوی الزامی | تست | Baseline (بدون Fix) | Post-Fix |
|---|---|---|---|---|
| 1 | Cross-job adoption race | `test_cross_job_adoption_race_keeps_both_jobs` | RED (`{'job-A'}`) | PASS |
| 2 | Owner retract after successor COMPLETED | `test_owner_retract_after_successor_completed_keeps_successor_binding` | RED (`'job-A' != 'job-B'`) | PASS |
| 3 | Exact job/attempt fencing | `test_exact_job_and_attempt_fence_each_row_independently` | RED (TypeError) | PASS |
| 4 | Stale attempt cannot delete successor | `test_stale_attempt_cannot_delete_the_successors_row` | RED | PASS |
| 5 | Multiple jobs sharing identical bytes | `test_many_jobs_sharing_identical_bytes_each_resolve` | RED (`'j1' != 'j2'`) | PASS |
| 6 | Same artifact, conflicting revisions | `test_same_artifact_conflicting_revisions_are_per_job` | RED (Conflict) | PASS |
| 7 | Cross-project isolation | `test_cross_project_isolation_holds_for_every_job` | RED | PASS |
| 8 | Recovery and replay | `test_restart_replay_rebinds_per_job_after_recovery` | RED | PASS |
| 9 | PostgreSQL multi-process concurrency | `test_artifact_binding_postgres_concurrency.py` (۴ تست، ۱ تست real-subprocess) | — | PASS |
| 10 | Migration upgrade + existing-row compat | `tests/unit/test_migrations.py` (۹ تست) | RED (schema/head) | PASS |
| 11 | Mutation tests | `scripts/durable_project_state_mutations.py` | — | ۳۴/۳۴ killed |

اجرای تازهٔ ۸ تست F5/R4 روی HEAD نهایی (`a50b2c77`): **`8 passed`**.
اجرای همان ۵ تست پس از بازگردانی Fix: **`5 passed`** (مقابل `5 failed` در baseline).

---

## ۶) نتایج PostgreSQL Concurrency

محیط: PostgreSQL 16.15 روی `127.0.0.1:5432` (سازگار با image CI: `pgvector/pgvector:pg16`)، DB `nexus_f5`، `NEXUS_PG_TEST_DESTRUCTIVE=1`.

```
NEXUS_DATABASE_URL=postgresql://nexus:nexus@127.0.0.1:5432/nexus_f5
python -m pytest tests/integration/test_artifact_binding_postgres_concurrency.py
→ 4 passed in 13.59s
```

شامل: ردیف مستقل هر Job و retract فقط همان Job؛ سناریوی owner-retract-بعد-از-COMPLETED؛ جلوگیری از retract بین‌پروسه‌ای توسط attempt کهنه (real subprocess)؛ و «هرگز ردیف با توکن پایین‌تر باقی نماند» در مسابقهٔ ارتقای توکن.

Migration chain روی PostgreSQL واقعی: `alembic upgrade head` تا `20261108perjobbind` اجرا شد و job `migrate-postgres` در CI (روی `a50b2c77`) **pass** است.

---

## ۷) Mutation Campaign

- هارنس: `scripts/durable_project_state_mutations.py` — هر mutation یک guard را **in-place** بازنویسی می‌کند، suite‌ها را اجرا می‌کند، و باید RED شود؛ فایل‌ها بعد از هر mutation byte-for-byte restore می‌شوند.
- **اجرا شده: ۳۴ · کشته‌شده: ۳۴ (34/34 killed)** — baseline قبل و بعد GREEN، working tree پس از کمپین بدون تغییر (`git diff --exit-code`).
- شامل ۳ mutation کلیدی F5/R4: `retraction_not_job_scoped`, `retraction_not_token_scoped`, `resolve_ignores_job_identity` (به‌همراه `rebind_advance_not_job_scoped`, `stale_row_fallback_on_vanished_binding`).
- همین هارنس در CI داخل job **`trust-mutations`** اجرا می‌شود (`ci.yml:391`) و روی SHA `a50b2c77` **pass** است.

---

## ۸) وضعیت CI روی SHA دقیق (`a50b2c77`)

GitHub Actions — run `42815690272`:

| نتیجه | تعداد |
|---|---|
| pass | **۲۵** |
| skipping (CodeRabbit, Sourcery) | ۲ |
| fail | ۰ (فقط یک status خارجی؛ زیر) |

چک‌های کلیدی و همه pass: `test`, `lint`, `lint-fast`, `migrate-postgres`, `python-parity` (۳.۱۰/۳.۱۱/۳.۱۲), `continuum-evidence` (۳.۱۰/۳.۱۱/۳.۱۲), `governance-mutations`, `trust-mutations`, `execution-core-mutations`, `api-gate-mutations`, `temporal-mutations`, `remote-key-mutations`, `b2-blob-tier-mutations`, `queue-ownership-mutations` (workflow جداگانه), `extras-matrix` (core/pdf/speech/translate), `dependency-audit`, `merge-base-guard`, `release-lineage`.

- `mergeable: MERGEABLE` · `mergeStateStatus: UNSTABLE`.
- **`UNSTABLE` فقط به‌خاطر `CircleCI Pipeline` است**: پیام آن «No configuration was found in your project» — یک integration بدون config که Gate واقعی نیست (روی `main` هم حضور ندارد). هیچ چک الزامیِ GitHub قرمز نیست.
- **رگرسیونِ کشف‌شده و رفع‌شده:** `migrate-postgres` پیش‌تر روی merge-SHA شکست خورده بود (fingerprint mismatch: `d6457c71…` vs `1eb9a99…`) — پیش از fix تأیید شد که `main` @ `5fe94512` سالم است ⇒ رگرسیون. با بازتولید مستقل در یک venv جداگانه (`langgraph-checkpoint-postgres==3.1.0`, DB تازه) مقدار جدید = دقیقاً مقدار CI اثبات و golden بازتولید شد.

### گیت‌های محلی (روی `a50b2c77`)
- `ruff check` → All checks passed · `ruff format --check` → 971 files already formatted.
- `mypy` → ۱۳ خطا، همگی optional-extras (chromadb/llama_index/litellm/pypdf/uvicorn) و **pre-existing** (خارج از override list).
- Suite اصلی (pinning + migrations + docs integrity + board): **302 passed**.
- Regression (Asset Library / Queue / Recovery / Durable / golden): **315 passed, 3 skipped, 3 failed** — هر ۳ شکست **محیطی** (پایین).

---

## ۹) اثبات حفظ Binding متعلق به Job موفق

سناریوی دقیق F5/R4 (Job A artifact می‌سازد و bind می‌کند → Job B همان Artifact/Revision را adopt و COMPLETED می‌شود → Job A در CAS پایانی شکست می‌خورد → Job A retract می‌کند):

- **قبل از Fix (baseline):** `test_owner_retract_after_successor_completed_keeps_successor_binding` → `assert 'job-A' == 'job-B'` ، یعنی پس از retract، ردیف باقی‌مانده فقط `job-A` است و **Provenance موفق (Job B) از دست رفته** — بازتولید قطعیِ باگ.
- **بعد از Fix:** همان تست **PASS** و `resolve(project_id, artifact_id, job_id="job-B")` ردیف مستقل Job B را برمی‌گرداند؛ Build `retract` با triple `(artifact_id, job_id, attempt)` صرفاً ردیف Job A را حذف می‌کند.
- تست مکمل `test_exact_job_and_attempt_fence_each_row_independently` (PASS پس از Fix) نشان می‌دهد هر Job مستقل resolve/retract می‌شود و **هیچ Jobی نمی‌تواند Provenance دیگری را حذف کند**.

---

## ۱۰) موارد باقی‌مانده و تصمیم‌های لازم از مالک محصول

**۱. تأثیر روی PR #327 (Composition) — نیازمند اقدام مالک آن شاخه.**
`src/nexus_ai_agent/creative/composition/job.py` (head `d5d5ead3`) در فراخوانی bind فقط `job_id` می‌فرستد و **`attempt` نمی‌فرستد**:

```python
job_id = str(result.get("_queue_job_id") or "")
...
await store.bind(
    project_id=..., revision_id=..., artifact_id=measured,
    job_id=job_id, artifact_kind=..., size_bytes=..., artifact_version_id=version_id,
)
```

API ما backward-compatible است (attempt پیش‌فرض 0) پس این کد **کار می‌کند**، اما Composition زیر fence کامل نمی‌نویسد. توصیه: خواندن `_queue_attempt` (که صف در `in_process_job_queue.py` تزریق می‌کند) و فوروارد آن:
`attempt=int(result.get("_queue_attempt") or 0)`. **این تغییر باید توسط مالک PR #327 انجام شود**؛ این شاخه هیچ لمس مستقیمی روی #327 نکرد.

**۲. محدودیت‌های محیطی سندباکس (بدون اثر بر CI):**
- `langgraph-checkpoint-sqlite` نصب‌شده `3.0.3` در برابر pin `3.1.2` (mirror سندباکس نسخهٔ pin را ندارد) → ۲ شکست `test_checkpoint_dependency_pin.py` صرفاً محیطی. CI با pin صحیح نصب می‌کند و pass است.
- نبود `arabic_reshaper` → ۱ شکست `test_in_process_job_queue.py` (TimeoutError)، pre-existing.
- `mypy` ۱۳ خطای optional-extras (خارج از override list)، pre-existing.

**۳. به‌روزرسانی‌های مؤخر بر اثبات (per §7 مأموریت):**
- PR description و audit مربوط به R4 فقط **پس از این اثبات** به‌روزرسانی شوند.
- Truth Matrix و OPERATION_TRUTH باید **پس از هم‌گرایی با main مجدداً** تولید شوند. هم‌گرایی انجام شد (merge-base = `5fe94512`)، اما بازتولید این دو آرتیفکت به‌عنوان گام جداگانه باقی است تا با SHA همگرای نهایی مهر شود.

**۴. تصمیم ادغام با مالک محصول است** — این گزارش صرفاً آماده‌سازی تحویل است و مجوز ادغام/Force-Push/تغییر main نیست.

---

### جمع‌بندی وضعیت: `VERIFIED_WITH_LIMITATIONS`

Fix F5/R4 به‌صورت failure-first بازتولید و اثبات شد؛ ۸ تست F5/R4 + PG concurrency (۴) + migrations (۹) سبز؛ ۳۴/۳۴ mutation کشته شد؛ و **تمام چک‌های الزامی GitHub روی SHA دقیق `a50b2c77` سبز است**. محدودیت‌ها: (الف) یکپارچه‌سازی `attempt` در PR #327 به مالک آن واگذار شد، (ب) چند شکست صرفاً محیطیِ سندباکس، (ج) بازتولید Truth Matrix/OPERATION_TRUTH به‌عنوان گام مؤخر باقی است.
