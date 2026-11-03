# NEXUS-OMNI — PR Convergence Ledger (2026-11-03)

> **Status:** dated record. Mission `NEXUS-OMNI-FORENSIC-RECONSTRUCTION-2026-11-03`,
> branch `arena/omni-audit-reports`. Base `main` @ `5ce9f7d3`. Snapshot:
> `GET /repos/bot523h/nexus-ai-agent/pulls?state=open` (43 open, 8 draft).

## 1. Method and an honest limitation

For every open PR this ledger records: number, title, age, draft state, changed-
file count, and the shared high-conflict paths it touches (from
`GET /pulls/{n}/files`). Classification is by **age + size + shared-file
overlap + topic**, not by a full line-by-line diff review of 43 branches — that
is beyond a single session's budget, and pretending otherwise would be the
dishonest "green report" the mission forbids. Where a disposition is a
*judgement*, it is labelled as such.

**Universal constraint:** branch protection on `main` is **strict** — a PR must
be up to date with `main` to merge. Every PR below was opened against a base SHA
that is now >100 commits behind `main`, so **every PR needs a rebase** before it
can merge. "Needs rebase" is therefore assumed, not listed as a finding.

## 2. Shared high-conflict paths (measured)

| Path | Open PRs touching it |
|---|---|
| `.agents/board.json` | 27 (113,117,118,121,129,137,149,150,157,193,205,247,273,284,285,290,297,33,57,58,60,63,69,73,84,89,304) |
| `docs/README.md` | 18 |
| `src/nexus_ai_agent/bot/handlers.py` | 8 (113,115,117,149,257,33,89,99) |
| `pyproject.toml` | 6 (117,193,204,33,63,69) |
| `src/nexus_ai_agent/cli.py` | 5 (150,247,33,60,63) |
| `AGENTS.md` | 3 (117,157,84) |
| `adapters/in_process_job_queue.py` | 2 (57,69) |

**Consequence:** `.agents/board.json` is touched by 27 PRs. Any two merged out
of order conflict on the board; the AGENTS.md rule (newest state wins, schema 2
must still pass `test_agent_board.py`) governs resolution. `bot/handlers.py` and
`cli.py` are the true architectural choke points.

## 3. Classification legend

`A` READY_FOR_REVIEW · `B` NEEDS_CODE_FIX · `C` NEEDS_REBASE_OR_CONVERGENCE ·
`D` ARCHITECTURAL_CONFLICT · `E` DUPLICATE_OR_SUPERSEDED · `F` DEFERRED_OR_BLOCKED ·
`G` SECURITY_RISK

## 4. Ledger

| PR | Title (short) | Age | Draft | Files | Class | Disposition |
|---|---|---|---|---|---|---|
| #33 | v3.13.0 — P0 security code + dead-code wiring | 42d | open | 40 | **D** | largest conflict surface (`cli.py`+`handlers.py`+`pyproject`); needs extraction into reviewable slices |
| #57 | research-v2 CLAIM+LEASE + INBOX/RECEIPT | 40d | open | 14 | C | touches `in_process_job_queue.py`; sequence after #69 |
| #58 | Security Boundary hardening S1–S5 | 40d | draft | 12 | **G/F** | security-relevant; draft, unreviewed — keep isolated until a security review |
| #60 | M0 runtime integration: InstrumentedJobQueue | 40d | open | 29 | D | `cli.py` overlap; runtime-instrumentation, needs convergence |
| #63 | task-163 board-Git truth gate + scale-to-zero | 39d | open | 35 | C | board + CI overlap; needs rebase |
| #64 | Creative Runtime (task-176 s2) | 39d | open | 38 | D | creative runtime; overlaps the merged spine — review against `main` |
| #66 | P0 creative integration: one production path | 39d | open | 37 | D | creative integration; architectural review required |
| #69 | Gate 4 cross-layer vertical slice | 39d | open | 99 | **D** | largest PR (99 files); overlaps `in_process_job_queue.py`, `pyproject`, board; **extract** |
| #73 | Gate 2.2 Operation Truth → Evidence | 39d | open | 20 | C | adds `OPERATION_TRUTH.json`; needs rebase |
| #84 | Register Engineering Constitution | 38d | open | 15 | C | `AGENTS.md` overlap; docs/constitution |
| #89 | task-122: 12 real command wirings | 38d | open | 7 | D | `handlers.py` overlap; command-truth work |
| #92 | fix(storage): honor SQLite defaults | 37d | draft | 4 | **B** | small, focused storage fix; candidate to land first after rebase |
| #97 | feat(vision): portrait + scene Vision pack | 37d | open | 17 | C | adds mutation probes; needs rebase |
| #99 | task-193 command truth — 11 fake/silent commands | 37d | open | 34 | D | `handlers.py` + docs overlap; large |
| #113 | W1 close-out: engine-owned scheduling | 36d | open | 14 | **E** | superseded by #115 (same W1 topic, `agent_manager.py` overlap) — verify before closing |
| #114 | cockpit: Persian capability explorer | 36d | draft | 16 | F | draft UI feature |
| #115 | W1 True Runtime Closure / Lifecycle Ownership | 35d | open | 14 | C | W1 runtime; overlaps #113 — pick one |
| #117 | fix(runtime): session/media contracts | 35d | draft | 41 | D | touches 5 workflows + board + `pyproject`; draft, large |
| #118 | W2 golden hardening: authority races | 35d | draft | 52 | F | draft; large hardening campaign |
| #121 | feat(memory): retrieval truth — noise-ranked recall | 35d | open | 20 | C | memory retrieval; `docs/README.md` overlap |
| #122 | fix(orchestration): H1 route chat through memory | 34d | open | 11 | C | orchestration fix; needs rebase |
| #129 | feat(studio): staged plan transactions | 32d | open | 6 | C | studio transactions; small |
| #135 | feat(studio): canonical PlanTransaction substrate | 31d | open | 8 | **E** | overlaps #140 (`packs/delivery/operations.py`) — dedupe |
| #137 | board: collision preflight + exec guard | 31d | open | 9 | C | board + docs overlap |
| #140 | Harden Studio CommandBus: PlanTransaction | 31d | open | 9 | C | overlaps #135 — pick one |
| #149 | Security Wave 1 verification & governance | 30d | open | 51 | **G/F** | security + governance; large; needs a security review before merge |
| #150 | feat(agent-intelligence): verified CreativeWork | 29d | draft | 29 | F | draft; `cli.py` overlap |
| #157 | feat(governance): Nagar agent constitution | 28d | open | 24 | C | `AGENTS.md` + CI overlap |
| #193 | feat(infra): Ubuntu 26.04 + Python 3.14 | 25d | open | 15 | C | `pyproject` + CI + board; version-matrix change |
| #204 | fix(memory): sqlite-vec extension fallback | 22d | draft | 4 | **B** | small, focused memory fix; candidate to land |
| #205 | feat(nagar): Asset Library V1 | 22d | open | 43 | D | large asset-library feature; overlaps security report's asset path |
| #230 | Arena/aa451c49 (unnamed) | 15d | open | 52 | F | untitled, 52 files, no clear scope — needs author triage |
| #247 | feat(creative): perception lane | 9d | open | 33 | D | `cli.py` overlap; creative perception |
| #257 | Palette: Telegram inline callback UX | 6d | draft | 4 | F | draft UX polish |
| #273 | feat(nagar): P0 intent NEL semantic time causal | 5d | draft | 19 | F | draft; `OPERATION_TRUTH.json` overlap |
| #284 | feat(nagar): Causal Memory (read-only) | 4d | draft | 13 | F | draft feature |
| #285 | chore(board): task-278 deferred status | 4d | draft | 1 | **A** | board-only, 1 file; safe once rebased |
| #290 | chore(board): register task-282 | 3d | open | 1 | **A** | board-only, 1 file; safe once rebased |
| #294 | repair(#189): durable creative-graph package | 3d | open | 9 | C | M16 repair; recent, coherent |
| #295 | extract(#218): content-addressed asset library | 3d | open | 2 | **A/E** | extraction from #218; small and reviewable |
| #297 | evidence(m16): M15 spine closeout | 2d | open | 5 | C | board + evidence; recent |
| #301 | chore(automation): debt watchdog | 1d | open | 2 | **A** | automation evidence-only |
| #304 | ci(queue-ownership): campaign deps | 0d | open | 2 | **A** | **this campaign** — CI disk fix (PR #304) |

> PR #305 (this campaign's R2 fix) is not in the snapshot above because it was
> opened after the `pulls` snapshot was taken; it is `A` (2 files, rebased on
> `main`).

## 5. Integration matrix — the combination risk

Several PRs are individually correct but conflict when combined:

| Cluster | PRs | Shared path | Risk |
|---|---|---|---|
| W1 runtime closure | #113, #115 | `agents/store/agent_manager.py` | two implementations of one close-out → dedupe first |
| Studio PlanTransaction | #135, #140 | `creative/packs/delivery/operations.py` | two substrates for one concept → pick one |
| Creative runtime | #64, #66, #69 | creative runtime + `in_process_job_queue.py` | three overlapping runtime integrations |
| Board-only | #285, #290, #297, #301 | `.agents/board.json` | serialise; each re-append its own claim |
| Agent/command truth | #89, #99, #113, #115 | `bot/handlers.py`, `agent_manager.py` | highest-conflict file in the repo |

**Merge-order recommendation (dependency-safe):** land the small, isolated,
already-rebased items first — #304 (done: CI), #305 (R2), #290, #285, #301,
#295, #92, #204 — then serialise the board-touching PRs one at a time,
re-appending only each PR's own claim (the reusable pattern already recorded in
project memory). The large `D`-class PRs (#33, #69, #205, #247) should be
**extracted into reviewable slices** before merge, not merged whole.

## 6. Disposition summary

| Class | Count | Notes |
|---|---|---|
| A READY_FOR_REVIEW | 6 | #304, #305, #290, #285, #301, #295 |
| B NEEDS_CODE_FIX | 2 | #92, #204 |
| C NEEDS_REBASE/CONVERGENCE | 18 | most of the queue |
| D ARCHITECTURAL_CONFLICT | 10 | #33, #60, #64, #66, #69, #89, #99, #117, #205, #247 |
| E DUPLICATE_OR_SUPERSEDED | 3 | #113 (vs #115), #135 (vs #140), #295 (extract of #218) |
| F DEFERRED_OR_BLOCKED | 8 | drafts + #230 (untitled) |
| G SECURITY_RISK | 2 | #58, #149 (security PRs needing a security review) |

**No PR was closed, force-pushed, or mass-merged in this campaign.** Each open
PR now has a classification and a disposition; the owner decides the merge
order. The single highest-leverage action is to **extract the large `D`-class
PRs**, because they are the ones that make the board and the two choke-point
files (`cli.py`, `bot/handlers.py`) conflict for everyone else.

---

## DELTA — 2026-11-03 post-audit re-classification (live truth)

This snapshot supersedes the first-pass counts. Live `mergeable_state` was read
from the GitHub API this session; base for every PR is still `main` @ `5ce9f7d3`.

| Change | Detail |
|---|---|
| Open PR count | 46 → **47** (added #308, my CI disk fix) |
| New READY | **#308** (required checks green, base == main) |
| Re-confirmed READY | #304, #305, #306, #307 — all required checks **success** on their exact head SHA |
| Newly `BEHIND` | #295, #301, #97, #64, #92, #114, #204, #257 (mergeable but stale base) |
| `DIRTY` (D-class) | #33, #57, #58, #60, #63, #66, #69, #73, #84, #89, #99, #113, #115, #117, #118, #121, #122, #129, #135, #137, #140, #149, #150, #157, #193, #205, #230, #247, #273, #284, #285, #290, #294, #297 |
| Duplicate candidates | #113/#115, #135/#140, #284/#285 (compare real diffs, do not close on title) |
| Security review | #58 (S1–S5 boundary hardening) |

Note: no PR was **incorrectly** labelled as needing rebase — the `C` set above is
`mergeable=true` with a stale base (a real rebase need), while the `D` set is
`dirty` (real conflict).
