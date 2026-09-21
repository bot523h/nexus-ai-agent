# منشور عالی هماهنگی چندعاملی مهندسی‌شده (Pro-Max Zero-Collision Parallel Network)

> **قانون قطعی و تخطی‌ناپذیر سیستم (100% Mandatory Compliance):**
> «این مخزن تحت نظارت و توسعه موازی چند عامل هوش مصنوعی اداره می‌شود.
> هر عامل موظف است **بدون حتی یک میلی‌متر تداخل** با حوزه‌های انحصاری سایر عامل‌ها، با بالاترین کیفیت مهندسی در سطح Anthropic و OpenAI کار خود را به صورت هوشمندانه، بدون پس‌رفت (Zero Regression) و بدون باگ به سرانجام برساند.»

---

# AGENTS.md — Multi-Agent Coordination Contract (protocol v2)

Several agents work on this repository **in parallel, from separate sandboxes**. The only shared
medium between sandboxes is **this git repository**, so coordination is file-based: a claim board
committed and pushed through git, backed by a zero-dependency CLI
(`scripts/agent_board.py`) whose behaviour and schema are themselves tested
(`tests/unit/test_agent_board.py`).

The board is `.agents/board.json` (**schema 2**: `protocol`, `zones`, `claims`, `deferred_log`,
`takeover_log`, `next_work`, `history`). Structure is enforced, not requested.

## 1. The five rules

1. **CLAIM BEFORE YOU CODE.** `python scripts/agent_board.py show` → `... next --branch <your-branch>`
   → `... claim <task> --branch <your-branch>`. Commit **and push the board change immediately**:
   an unpushed claim does not exist for the other sandbox. Finish → `release`; blocked → `defer`
   (the Persian note template is built into the CLI).
2. **ONE OWNER PER FILE-ZONE.** Claims carry `exclusive_paths`. Before pushing, run
   `python scripts/agent_board.py check --files <changed,files> --branch <you>`; exit 1 means overlap
   with another agent's live lease — do not push that work, pick another task or defer.
3. **BRANCH NAME IS THE CANONICAL IDENTITY.** Letters (A/B/C/…) are convenience labels only.
   A newcomer must state its identity as *its branch*, after checking `git ls-remote origin "arena/*"`,
   the open PRs, and this board. (The rule exists because three sessions once declared "agent E".)
4. **ONE GATES OWNER.** Exactly one agent (the holder of `gates_owner: true`) runs the full gates on
   main-bound work. Everyone else writes "deferred to gates owner" in the PR body. Local *diagnostic*
   checks (`ruff check`, targeted `pytest`) are always allowed and encouraged — they are not the gate.
5. **LEASES EXPIRE (24 h TTL).** `show`/`next`/`gc` auto-release stale leases. Renew by re-running
   `claim` (heartbeat; refreshes the clock). `gates_owner` is exclusive: taking it clears any other.

## 2. Evidence rules (new in v2)

- Every claimable task carries **`acceptance_criteria`** and **`evidence_required`** — a task without
  them cannot be claimed, and `tests/unit/test_agent_board.py` fails the board if one appears.
- A PR body states: task id, zone, the acceptance criteria met, the exact commands run, and their
  observed result. "Tests pass" without a command is not evidence.
- Honest status beats optimistic status: if a capability is implemented but not reachable, say so and
  record the reproduction (see `task-126` in the board as the worked example).
- Numbers in documentation must be reproducible from the tree
  (`docs/architecture/TESTING.md` §6).

## 3. Local gate reproduction (the one gotcha)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # editable install matters: 5 tests spawn `python -m nexus_ai_agent.cli`
make lint && make types && make test
```

With a bare `PYTHONPATH=src` (no editable install) four migration-race tests and one
distribution-version test fail for purely environmental reasons — documented in
`docs/architecture/TESTING.md` §5. CI installs the package, so it never sees this.

## 4. Board state (schema 2 — summary; always verify with `show`)

Verified at `2026-09-21T16:45Z`, `main` @ `7573249` (v3.13.0).

| Task | Zone | Owner (branch) | Status |
|---|---|---|---|
| `task-131` documentation & architecture suite + board rewrite | `docs-architecture` | `arena/01a0c4c1-…` | **active** (this contract's author) |
| `task-111` release version lock-step guard (tests-only) | `ci-quality` | `arena/01a0c4c1-…` | **active** |
| `ci-gates-steward` interim gates stewardship | `ci-quality` | `arena/01a0c460-…` | **active** (`gates_owner: true`) |
| `feature-wiring-batch` PR#32 | `feature-wiring` | `arena/01a0c34d-…` | **active** — needs rebase (see `task-122`) |
| `pr33-in-review` PR#33 (legacy record, no path claim) | `packaging+interop` | `arena/01a0c3aa-…` | active_in_review — needs slim-down (`task-123`) |
| `task-126` unified pack registry (4 packs pending) | `cli-packs-registry` | free | `available_sequenced_post_33` — **P0** |
| `task-124` P0-8 single wiring + P0-9 graph memory | `audit-remainder` | free | `available_sequenced_post_32_33` |
| `task-122` PR#32 rebase + dedupe | `feature-wiring` | عامل B | `assigned_to_B` |
| `task-123` PR#33 slim-down | `packaging+interop` | شاخه 3aa | `assigned_to_E_pr33` |
| `task-121` OTIO markers · `task-128` async DB · `task-106` studio surface · `task-127` real RAG | — | free / B | sequenced (prerequisites in the board) |
| `task-132` extras CI matrix · `task-134` portrait slice | `ci-quality` / `nagar-portrait` | free | `queued` |

Closed waves, merged PRs (#19–#38) and the four recorded incidents live in
`history` inside `.agents/board.json`; the durable narrative is
`docs/architecture/` plus `docs/audits/`.

## 5. شبکه ۱۰ وظیفه کلیدی بعدی (wave 4 — full detail: `.agents/board.json → next_work`)

1. **`task-126` [P0] رجیستری یکپارچهٔ پک‌ها** — `nexus packs list` باید `pending=0` بدهد؛ امروز ۸/۷/۸/۷ است و `activate` شکست می‌خورد. وابسته به آزاد شدن `cli.py` (پس از `task-123`).
2. **`task-124` [P0] بستن P0-8 و P0-9** — تست ONE-OWNER برای سیم‌کشی موتورها + مسیر نوشتن حافظهٔ گراف با round-trip.
3. **`task-122` [P0] ریبیس و رفع دوگانگی PR#32** — مالک: عامل B.
4. **`task-123` [P0] کوچک‌سازی PR#33** — مالک: شاخه 3aa؛ قفل `cli.py` را آزاد می‌کند.
5. **`task-121` [P1] مارکرهای OTIO** — تنها شکاف باقی‌ماندهٔ ۱۱۰.
6. **`task-128` [P1] فاز ۱ دیتابیس ناهمگام** — انجین مرکزی + ۲ پایلوت + گارد معماری.
7. **`task-106` [P1] فرمان‌های استودیو در تلگرام** — `/edit /caption /grade` روی `bot/creative_surface.py`.
8. **`task-132` [P1] ماتریس دود اختیاری‌ها در CI** — مسیرهای `[speech]/[translate]/[pdf]/[local-llm]`.
9. **`task-134` [P2] اسلایس خانوادهٔ پرتره** — ۵ عملیات از ۲۹ شناسهٔ پیاده‌نشدهٔ TDD.
10. **`task-127` [P2] RAG واقعی** — چانکینگ بازگشتی + بازیابی هایبرید + هارنس recall@k.

Deferred improvements (toolchain adoption triggers) are recorded in
`protocol.deferred_improvements` so they are not forgotten.

## 6. Merge order

1. **`task-123`** (PR#33 slim-down) — frees `cli.py`, largest conflict surface.
2. **`task-122`** (PR#32 rebase + dedupe against the merged #34).
3. **`task-126`** (unified pack registry) — the first visibly user-facing win after #33.
4. **`task-124` / `task-121` / `task-128` / `task-106`** in parallel on disjoint zones.
5. **`task-132` / `task-134` / `task-127`** in the following wave.

`src/nexus_ai_agent/bot/handlers.py` remains the highest-conflict file in the repository: any agent
touching it must coordinate explicitly in the board note before pushing. The rule for
`.agents/board.json` or `AGENTS.md` conflicts is **newest state wins, and schema 2 must still pass
`tests/unit/test_agent_board.py`**.

## 7. Documentation contract (new in v2)

- `docs/architecture/*` are **living views** — update them in the same PR as the code that invalidated
  them; `docs/audits/` and `docs/history/` are dated records, never sources of truth.
- Every document must be indexed in `docs/README.md`, every relative link must resolve, and every
  diagram fence must be balanced: `pytest -q tests/unit/test_docs_integrity.py`.
- Every boundary rule must name its enforcing test in `docs/architecture/MODULE_MAP.md` §3.
- Architecture decisions that change **system behaviour** go to `docs/DECISION_LOG.md`; decisions about
  the **documentation/board layer** go to `docs/architecture/adr/`.
