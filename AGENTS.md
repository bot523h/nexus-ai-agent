# منشور عالی هماهنگی چندعاملی مهندسی‌شده (Pro-Max Zero-Collision Parallel Network)

> **قانون قطعی و تخطی‌ناپذیر سیستم (100% Mandatory Compliance):**
> «این مخزن تحت نظارت و توسعه موازی چند عامل هوش مصنوعی اداره می‌شود.
> هر عامل موظف است **بدون حتی یک میلی‌متر تداخل** با حوزه‌های انحصاری سایر عامل‌ها، با بالاترین کیفیت مهندسی در سطح Anthropic و OpenAI کار خود را به صورت هوشمندانه، بدون پس‌رفت (Zero Regression) و بدون باگ به سرانجام برساند.»

---

# AGENTS.md — Multi-Agent Coordination Contract (protocol v2)

Several agents work on this repository **in parallel, from separate sandboxes**. The only shared
medium between sandboxes is **this git repository**, so coordination is git-based: a claim board
that lives on its **own coordination ref**, backed by a zero-dependency CLI
(`scripts/agent_board.py`) whose behaviour and schema are themselves tested
(`tests/unit/test_agent_board.py`, `tests/unit/test_agent_board_ref.py`).

The live board is **`refs/nexus/board`** on `origin` (file `board.json` in that ref's tree;
**schema 2**: `protocol`, `zones`, `claims`, `deferred_log`, `takeover_log`, `next_work`,
`history`). Every `claim`/`release`/`defer` is one commit on that ref, published by the CLI with a
compare-and-swap push (`--force-with-lease`), so a lost race is redone from a fresh read and never
overwrites another sandbox's lease. The tree file `.agents/board.json` is a **frozen archive** of the
last tree-resident board (audit A-04, ADR 0012): it is pinned by sha256, CI fails any pull request
that modifies it, and a code PR must never carry board state again. Structure is enforced, not
requested. (`--store tree` / `NEXUS_BOARD_STORE=tree` runs the CLI against the tree file for
offline fixtures and tests only — it can never write the authority.)

## 1. The five rules

1. **CLAIM BEFORE YOU CODE.** `python scripts/agent_board.py show` → `... next --branch <your-branch>`
   → `... claim <task> --branch <your-branch>`. The CLI fetches the ref, applies your claim and pushes
   it in the same command — **do not** `git add .agents/board.json`; the claim exists for every other
   sandbox the moment the command prints `published refs/nexus/board`. Finish → `release`; blocked
   → `defer` (the Persian note template is built into the CLI). Renew with `claim … --expected-generation <n>`.
2. **ONE OWNER PER FILE-ZONE.** Claims carry `exclusive_paths`. Before pushing, run
   `python scripts/agent_board.py check --files <changed,files> --branch <you>`; exit `1` means a
   proven overlap with another agent's live lease on the ref (or a modified board archive in your
   tree) — do not push that work, pick another task or defer. Exit `2` means the authority
   (`refs/nexus/board`) was unreadable or inconsistent (no single live gates owner, malformed claim)
   — that is **not** a pass; retry once it is reachable. Only exit `0` means "the authority was
   readable and disjoint". Code trees — yours, `main`, any PR branch — are **not** sources.
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

Verified at `2026-10-15T15:00Z`, `main` @ `6b39856` (after #212 pinned `langgraph-checkpoint-sqlite==3.1.1`). The live
record is `python scripts/agent_board.py show`; this table is a snapshot, not an authority.

| Task | Zone | Owner (branch) | Status |
|---|---|---|---|
| `task-270-full-audit-remediation` audit A-01…A-19 remediation + closeout (schema authority, time contract, updater contract, PR triage) | `full-audit-remediation` | `arena/26f7d958-nexus-ai-agent` (generation 3, 24 h lease, `gates_owner`) | `active` — draft PR #218 |
| `task-186` intent-first creative spine + `task-207` referee / `task-219` lease fencing / `task-220` gc-note | `nagar-creative-graph` / `coordination-referee` | merged | `done` (PR#126) |
| `task-221` spine plan atomicity · `task-222` transaction-scoped rollback · `task-223` identity-aware `system.undo` | `nagar-creative-graph` / `nagar-contract-gate` | merged | `done` (#126 / #183 / #128) |
| `task-190` SQLite WAL init race | `storage-observability` | merged | `done` (#132 → #174) |
| `task-253` owner identity gate fails closed when unconfigured | `features-owner-control` | merged | `done` (PR#184) |
| `task-131` documentation & architecture suite + board rewrite | `docs-architecture` | — | `done` |
| `ci-gates-steward` interim gates stewardship | `ci-quality` | — | `completed_released` (the live `gates_owner` is task-270 while its lease is valid) |
| `task-122` PR#32 rebase + dedupe | `feature-wiring` | عامل B | `assigned_to_B` (stale lease) |
| `task-123` PR#33 slim-down | `packaging+interop` | شاخه 3aa | `assigned_to_E_pr33` (stale lease) |
| `task-124` P0-8 single wiring + P0-9 graph memory | `audit-remainder` | free | `available_sequenced_post_32_33` — **P0** |
| `task-126` unified pack registry (4 packs pending) | `cli-packs-registry` | free | `available_sequenced_post_33` — **P0** |
| `task-121` OTIO markers · `task-128` async DB · `task-127` real RAG | — | free / B | sequenced (prerequisites in the board) |
| `task-134` portrait slice | `nagar-portrait` | free | `queued` |
| `task-106` studio surface · `task-132` extras CI matrix | — | — | `done` |

Closed waves, merged PRs (#19–#38 and the spine stack) and the four recorded incidents live in
`history` inside `.agents/board.json`; the durable narrative is
`docs/architecture/` plus `docs/audits/`. A stale `gates_owner: true` field is **not** proof of a live
gates owner — check the branch, generation and `expires` of the claim (`show`), as rule 2 requires.

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

Landed (do not re-order): the creative-spine stack **#126 → #183 → #128** (task-186/-207/-219/-220,
task-221, task-222, task-223) merged into `main` @ `090f35d`.

Remaining main-bound order:

1. **`task-123`** (PR#33 slim-down) — frees `cli.py`, largest conflict surface.
2. **`task-122`** (PR#32 rebase + dedupe against the merged #34).
3. **`task-126`** (unified pack registry) — the first visibly user-facing win after #33.
4. **`task-124` / `task-121` / `task-128` / `task-106`** in parallel on disjoint zones.
5. **`task-132` / `task-134` / `task-127`** in the following wave.

`src/nexus_ai_agent/bot/handlers.py` remains the highest-conflict file in the repository: any agent
touching it must coordinate explicitly in the board note before pushing. The rule for
`AGENTS.md` conflicts is **newest state wins**. `.agents/board.json` can no longer conflict: it is a
frozen archive (`python scripts/agent_board.py guard` must pass), and the live board on
`refs/nexus/board` is serialised by compare-and-swap, with schema 2 still enforced by
`tests/unit/test_agent_board.py`.

## 7. Documentation contract (new in v2)

- `docs/architecture/*` are **living views** — update them in the same PR as the code that invalidated
  them; `docs/audits/` and `docs/history/` are dated records, never sources of truth.
- Every document must be indexed in `docs/README.md`, every relative link must resolve, and every
  diagram fence must be balanced: `pytest -q tests/unit/test_docs_integrity.py`.
- Every boundary rule must name its enforcing test in `docs/architecture/MODULE_MAP.md` §3.
- Architecture decisions that change **system behaviour** go to `docs/DECISION_LOG.md`; decisions about
  the **documentation/board layer** go to `docs/architecture/adr/`.
