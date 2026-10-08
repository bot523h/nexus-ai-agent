# Dead engines behind live handlers — verification, five-option analysis, and the wiring batch

**Session:** `arena/01a0cb38-nexus-ai-agent` · 2026-09-22 · agent identity = branch (protocol v2, rule 3)
**Base:** `main` @ `192b227` (CI run `35779030942`, success)
**Scope:** the three `features/` engines that had no importer — `ads`, `channel_manager`, `onboarding` —
plus the quality-gate state and the coordination state of the repository.
**Character:** a dated record. `docs/architecture/` remains the living view; the durable rules this pass
produced live in `docs/DECISION_LOG.md` D-0009, `docs/architecture/MODULE_MAP.md` §3 (R12) and
`docs/architecture/SECURITY.md` §2 (T14).

---

## 0. خلاصه (Persian)

سه موتور `ads`, `channel_manager`, `onboarding` روی `main` **صفر مصرف‌کننده** داشتند؛ در عوض
`bot/handlers.py` سیزده فرمان را با رشته‌های ثابت پاسخ می‌داد («simulated», «Ad campaign created
successfully», «Onboarding step completed!») و کاربر را متقاعد می‌کرد که عملی رخ داده است.
این سشن ابتدا وضعیت را راستی‌آزمایی کرد (بند ۱: repo و GitHub در این سندباکس **در دسترس** هستند؛
قفل «نبود ریپو» که در سشن پیشین گزارش شده بود برطرف شده است), سپس پنج راه‌حل ممکن را روی کد واقعی
مقایسه و گزینهٔ «لایهٔ surface» را پیاده کرد: سه ماژول جدید در `bot/surface/`, حذف کامل استاب‌ها از
`handlers.py`, و گاردهای AST که جلوی بازگشت هر استاب را می‌گیرند.

سه نکتهٔ مهم:

1. **این فقط سیم‌کشی نبود:** `AdManager.pause/resume/delete` فقط شناسهٔ کمپین می‌گیرند؛ اتصال مستقیم
   آن‌ها به فرمان، یک آسیب‌پذیری IDOR بین‌گفتگویی می‌ساخت. گارد مالکیت در surface اضافه و با تست
   اثبات شد (T14 در SECURITY).
2. **یک باگ نهفتهٔ تایپ پیدا شد:** امضای `create_campaign` عدد `int` می‌خواست در حالی که ستون
   `interval_hours` در مدل `Float` است؛ mypy به‌محض وجود اولین فراخوان واقعی آن را گرفت.
3. **صادقانه، هنوز یک حلقهٔ ارسال وجود ندارد:** کمپین ثبت و زمان‌بندی می‌شود، اما هیچ tick پس‌زمینه‌ای
   `get_due_campaigns` را صدا نمی‌زند (صفر فراخوان، با مدرک). این بخش به `task-159` واگذار شد چون
   `worker.py` در PR#33 در جریان است — نه پنهان، بلکه ثبت‌شده.

---

## 1. Why this document exists

A previous session reported `REMOTE ACCESS BLOCKED` and an empty workspace, and therefore staged
governance documents without touching code. That finding was correct **at the time** and is now
**stale**: this sandbox has a real checkout, `origin` answers, and `gh` is authenticated. The first
duty of this pass was therefore not to repeat the previous conclusion but to re-measure it:

```bash
git rev-parse --short HEAD              # 192b227  (repo present, one shallow commit)
git ls-remote origin HEAD               # 192b227ae3d8cbe9428666e304844d382cd1759c
gh auth status                          # Logged in as arena-ai-coding-agent[bot]
gh run list --branch main --limit 1     # success — CI run 35779030942, 7m8s
```

Consequence: the blocked queue could be executed instead of described. Everything below is measured
in this checkout; commands are given so any later session can reproduce them.

## 2. Verification of the gap (the "audit handoff" part)

### 2.1 Zero importers, not a judgement call

```bash
for m in ads channel_manager onboarding; do
  grep -rn "features import $m\|features\.$m\|import $m\b" src/ tests/ scripts/ migrations/ \
    | grep -v "^src/nexus_ai_agent/features/$m.py"
done
```

The only hits anywhere in `src/` were two comments in `bot/handlers.py`:

| Engine | Lines | Public methods | Reference from the bot |
|---|---:|---:|---|
| `features/ads.py` | 242 | 10 (`create_campaign` … `get_stats`) | `# ad_manager = AdManager()` |
| `features/channel_manager.py` | 239 | 13 (`post_to_channel` … `run_nightly_tasks`) | `# In a real app, this would use ChannelManager` |
| `features/onboarding.py` | 122 | 3 (`is_first_time_user`, `send_onboarding`, `handle_onboarding_callback`) | none |

And the engine methods that *should* have been driven by a scheduler were equally unreferenced —
verified, not assumed:

```bash
grep -rn "get_due_campaigns\|mark_delivered\|run_nightly_tasks\|post_viral_content\|is_first_time_user\|send_onboarding" src/ \
  | grep -v "features/\(ads\|channel_manager\|onboarding\).py"
# → no results (exit 1)
```

### 2.2 What users were being told

Thirteen commands in `bot/handlers.py` returned a constant. The three worst shapes:

- **Fabricated outcome** — `/ad_create` answered `created successfully` with no row written;
  `/ad_stats` answered `5k impressions, 200 clicks` although `AdCampaign` has no impressions or
  clicks column (`storage/models.py:202`).
- **Fabricated side effect** — `/ban`, `/pin`, `/welcome`, `/schedule` answered `(simulated)` /
  `updated` / `pinned` having performed nothing; `/ban` did it for **any** caller, since a check was
  pointless when nothing was enforced.
- **Fabricated progress** — `onboarding_callback_handler` matched the whole `^onboarding_` pattern
  and rewrote the onboarding message to `Onboarding step completed!` for every payload, while the
  engine's three real hints sat translated in all 15 locale files (`onboarding.ai_hint`,
  `onboarding.image_hint`, `onboarding.explore_hint`).

### 2.3 Coordination state (so the batch lands without a collision)

| Item | Measured state | Consequence for this pass |
|---|---|---|
| PR#33 (`arena/01a0c3aa`, v3.13.0 security + wiring) | OPEN, `mergeable=CONFLICTING`, `mergeStateStatus=DIRTY`, last updated `2026-09-21T15:40:59Z`, 52 files incl. `bot/handlers.py`, `features/onboarding.py`, `worker.py`, `README.md` | `features/onboarding.py` and `worker.py` **not edited**; the `handlers.py` edit is confined to import lines + deleting stub bodies, and is recorded here and on the board as required for the highest-conflict file |
| `feature-wiring-batch` lease (`arena/01a0c34d`) | `expired` (24 h TTL elapsed) | free to work the zone; `agent_board.py gc` releases it |
| `ci-gates-steward` (`arena/01a0c460`) | `completed_released`, `gates_owner: false` | no gates owner on `main`; this session ran **local diagnostics** only (`ruff`, targeted `pytest`, `mypy`) and did not claim `--gates` |
| Board `next_work` | `task-122/123/124/126/121/128` sequenced after #32/#33 | no numbered task covered the stub commands; `stub-command-truth-batch` named in PR#33's body is **absent from the board** — created here as `task-158` |

## 3. Five implementation options, compared on the real tree

The board's own instruction for the sibling tasks is to compare strategies rather than copy a
pattern. All five were evaluated against the code that exists today (not against a hypothetical
rebuild). ✗/✓ columns are decided by measurement, not taste.

| # | Option | Shape | Reachable after merge? | Collision with PR#33 | Test cost | Falsifiability (can a stub creep back?) | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | **Inline calls inside `bot/handlers.py`** | replace each stub body with `await AdManager.…` | ✓ | ✗ high — ~200 lines of new code in the highest-conflict file, exactly where #33 rewrites the command list | low | ✗ none: handlers.py cannot be imported in a bare test job, so the wiring is only provable through `build_handlers` with PTB installed | rejected |
| 2 | **`bot/surface/` modules + import swap** *(chosen)* | pure modules in `bot/surface/`, `handlers.py` only changes imports and loses stubs | ✓ (same registry the gamification/docs surface already uses) | ✓ low — new files, and the `handlers.py` delta is deletion + one import block | medium (fake doubles exist: `tests/unit/surface_fakes.py`) | ✓✓ the existing AST contract test becomes a ratchet; extended here to all 20 commands + the callback | **accepted** |
| 3 | **Extend `bot/feature_handlers.py`** | the P0-batch engine bundle (`FeatureEngines`) | ✓ | ✗ high — that file and its `build_feature_engines` are inside #33's diff | low | ~ partial; engines are constructed twice (`P0-8` still open), so a "one owner per engine" claim could not be made truthfully | rejected |
| 4 | **DI first: single engine registry in `bot_data`, then wire** | fix `P0-8`, then attach all three engines to it | ✓ eventually | ✗ touches `bot/app.py`, `worker.py` — #33 territory | high | ✓ but only after the registry lands | deferred: the right host for the delivery tick; recorded as `task-159` |
| 5 | **Truth-only: delete the lies, answer "not wired"** | remove 13 stubs, reply with an explicit "این فرمان در این نسخه فعال نیست" | ✗ product regression for 13 commands | ✓ | low | ✓ | rejected: honest but wasteful — the engines already exist and their APIs match the commands |

**Why 2 wins on evidence, not aesthetics:** it is the only option that is simultaneously (a)
reachable from a real command, (b) free of `handlers.py` logic growth, and (c) checkable from the
test suite without importing PTB — the property `tests/unit/test_surface_registration.py` already
exists to protect. Option 4 is *also* correct in the long run, so it was not discarded but
sequenced: the surface module `manager_for()` caches the one manager in `application.bot_data`,
which is the same shape option 4 wants, without pre-empting the `P0-8` owner's refactor.

Two rules the comparison exposed, which became part of the code:

* **Authorisation cannot be inherited from a dead engine.** `AdManager.pause_campaign(id)` has no
  chat scoping. Option 1/3 would have shipped an IDOR; the surface layer is where the check can
  live *and* be unit-tested, which was itself an argument for option 2.
* **Rendering must not exceed the schema.** `/ad_stats` cannot print impressions; there is no
  column. Any option that kept the old copy would still be lying after a successful wiring.

## 4. What was delivered

| Path | Change |
|---|---|
| `bot/surface/ads.py` | new — 6 commands, `parse_create_args` / `parse_campaign_id` / `parse_status_filter`, four `format_*` renderers, `_load_owned` authorisation guard |
| `bot/surface/channel_management.py` | new — 7 commands, `manager_for` (`bot_data` memoisation), `parse_schedule_args`, `parse_target_id`, six renderers, Telegram failures surfaced instead of swallowed |
| `bot/surface/onboarding.py` | new — routes the three known `onboarding_*` payloads to the engine handler in the caller's language; unknown payloads are answered, the message is left intact |
| `bot/surface/_ptb.py` | +3 accessors: `reply_to_message_id`, `reply_to_user_id`, `user_language_code` |
| `bot/surface/__init__.py` | 13 new registry entries; `onboarding_callback_cmd` deliberately outside `COMMAND_HANDLERS` (it is a callback, not a command) |
| `bot/handlers.py` | 13 stub bodies deleted, 1 callback redirected, imports extended; the registration lines are unchanged |
| `features/ads.py` | `create_campaign(interval_hours: float = 24.0)` — the annotation contradicted the `Float` column; mypy caught it the moment a caller existed |
| `tests/unit/test_surface_ads.py` | 31 tests |
| `tests/unit/test_surface_channel_management.py` | 42 tests |
| `tests/unit/test_surface_onboarding.py` | 10 tests, incl. the bare-import probe |
| `tests/unit/test_surface_registration.py` | `EXPECTED` 7 → 20 commands, new `EXPECTED_CALLBACKS`, 13 more forbidden stub strings |
| `tests/unit/test_surface_ptb.py` | 7 tests for the new accessors, incl. malformed-update tolerance |
| `tests/unit/surface_fakes.py` | `FakeBot` records `pin/delete/ban/unban/member_count` and can raise; `FakeCallback`; quoted-message and language plumbing on `FakeUpdate` |

## 5. Evidence (commands run, results observed)

```bash
# environment: venv, `pip install --no-deps -e .` + the deps the touched paths import
# (deliberately no torch / sentence-transformers / llama-cpp / chromadb — all lazy or optional)

python -m pytest -q tests/unit tests/architecture \
  --ignore=tests/unit/test_postgres_checkpointer.py --ignore=tests/unit/test_reconciler.py
#   base commit (192b227) .............. 1634 passed, 1 skipped in 53.94s
#   this branch ........................ 1728 passed, 1 skipped in 64.70s   (+94)

ruff check .            # All checks passed!
ruff format --check .   # 446 files already formatted
python -m mypy src      # 1 error: features/rag.py:187 chromadb.utils import-not-found
```

The single `mypy` error is **not** from this change: `git stash -u && python -m mypy src` on the base
commit emits the identical line (228 files checked there vs 231 here). It is this sandbox lacking
`chromadb`, a core dependency CI installs; `mypy_path`/`ignore_missing_imports` for `chromadb` cover
the package but not the `chromadb.utils` submodule in this partial install.

### 5.1 Negative controls (a guard that cannot fail is decoration)

Three deliberate breaks, then a restore:

| Break injected | Tests that turned red |
|---|---|
| `/ad_list` re-pointed at a lambda replying `"📢 Active Ads: 2, Paused: 1."` | `test_every_command_is_registered_once_with_the_surface_handler`, `test_no_stub_string_survives_in_handlers`, `test_no_stub_is_ever_replied_to_a_user` |
| the `chat_id` check removed from `_load_owned` | `test_pausing_another_chats_campaign_is_refused_and_changes_nothing` |
| `"welcome": welcome_cmd` dropped from `COMMAND_HANDLERS` | `test_the_surface_package_exports_exactly_these_commands` |

`5 failed` on the mutants; `120 passed` after restoring. Full-suite comparison: no new failures,
no new skips.

## 6. Residuals — recorded, not hidden

| # | Residual | Why not here | Follow-up |
|---|---|---|---|
| 1 | No ad **delivery tick**: `get_due_campaigns` / `mark_delivered` still have no caller, so a campaign is persisted and scheduled but never posted | the natural host is `worker.py` / the job queue, both inside PR#33's diff (`P0-8` too) | `task-159` on the board, with acceptance criteria incl. the naive-`DateTime` caveat: `ChannelSchedule.scheduled_at` round-trips **without** a tz marker from SQLite, so a reconciler must treat it as UTC |
| 2 | Onboarding **first-run** flow still unreached: `send_onboarding` + `is_first_time_user` have no caller | `/start` is the highest-traffic handler and PR#33 is changing the same engine file (`AsyncSession.exec()` misuse) | `task-159`'s sibling; needs the DB-path fix landed first |
| 3 | Onboarding language comes from Telegram's `language_code`, not the `/language` row | the async `db_session_factory` lives in the composition root and is not in `bot_data` (same root cause as `P0-8`) | fold into `P0-8` / `task-124` |
| 4 | Moderation commands are **owner-only**, not admin-aware | the repo has no "is administrator of this chat" helper; inventing a looser privilege inside a wiring PR is out of scope | `core-security` zone |
| 5 | `features/*` still build a sync engine per call (`_sync_engine()`), and `bot/handlers.py` is still ~1,900 lines | `core-database` (`task-128`) and `handlers-decomposition-batch` own those files | unchanged board sequencing |
| 6 | `README.md` command-status table not updated | the file is in PR#33's diff (already `CONFLICTING`); touching it doubles the rebase cost | whoever rebases #33 onto this |

## 7. Reproduction

```bash
python -m venv .venv && . .venv/bin/activate
pip install --no-deps -e .
pip install "python-telegram-bot>=21" "sqlmodel==0.0.42" "sqlalchemy==2.0.54" \
            "pydantic-settings>=2" structlog typer httpx pytest pytest-asyncio ruff mypy
python -m pytest -q tests/unit/test_surface_ads.py tests/unit/test_surface_channel_management.py \
                    tests/unit/test_surface_onboarding.py tests/unit/test_surface_registration.py
python scripts/agent_board.py show      # the task-158 claim record
```
