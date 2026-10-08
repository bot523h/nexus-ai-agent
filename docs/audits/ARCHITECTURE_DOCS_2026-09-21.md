# Architecture & Documentation Pass — 2026-09-21 (task-131)

**Session:** `arena/01a0c4c1-nexus-ai-agent` (identity by branch, per the AGENTS.md identity rule)
**Base:** `main` @ `7573249` (v3.13.0) · **Board tasks:** `task-131` (docs/architecture, coordinator) + `task-111` (release lock-step, tests-only)
**Scope of change:** `docs/`, `AGENTS.md`, `.agents/board.json`, and three new test files. **Zero** `src/` lines changed (proven: `git diff origin/main --stat -- src/` is empty).

> This is a dated, immutable record of what was written, what was verified, and what was found. Living documents are under [`../architecture/`](../architecture/); this file is history.

---

## 1. What the pass produced

| Deliverable | Lines | Purpose |
|---|---:|---|
| [`../architecture/OVERVIEW.md`](../architecture/OVERVIEW.md) | ~230 | C4 context + container views, stakeholders, five quality attributes, frozen constraints, deployment topologies |
| [`../architecture/MODULE_MAP.md`](../architecture/MODULE_MAP.md) | ~150 | layer diagram, package inventory, 11 boundary laws → the test that enforces each, extension recipes |
| [`../architecture/RUNTIME_FLOWS.md`](../architecture/RUNTIME_FLOWS.md) | ~200 | seven flows (message, slideshow, apply lane, checkpoint lifecycle, migration, webhook, image gen) each with a failure contract |
| [`../architecture/CREATIVE_STUDIO.md`](../architecture/CREATIVE_STUDIO.md) | ~180 | Nagar capability model, permission ladder, pack inventory, activation gap, TDD coverage ledger, render lane |
| [`../architecture/DATA_AND_STORAGE.md`](../architecture/DATA_AND_STORAGE.md) | ~110 | store map, Alembic chain, entity ownership, deletion rules, portability, "never stored" list |
| [`../architecture/SECURITY.md`](../architecture/SECURITY.md) | ~140 | trust boundaries, 13-row STRIDE → control → evidence table, P0 follow-through, review checklist |
| [`../architecture/OBSERVABILITY.md`](../architecture/OBSERVABILITY.md) | ~120 | events, metric policy, health semantics, inspection commands, the short alert list |
| [`../architecture/TESTING.md`](../architecture/TESTING.md) | ~140 | taxonomy, gates, determinism, local baseline, honest gaps |
| [`../architecture/PORTS.md`](../architecture/PORTS.md) | ~60 | all six ports (was an incomplete list of five) with method surfaces and gate mapping |
| [`../architecture/REFERENCES.md`](../architecture/REFERENCES.md) | ~80 | standards followed (C4, arc42, MADR 4.0, fitness functions, docs-as-code, STRIDE) and what was not adopted |
| [`../architecture/OVERVIEW.fa.md`](../architecture/OVERVIEW.fa.md) | ~80 | Persian navigation summary (English wins on conflict) |
| [`../architecture/adr/`](../architecture/adr/README.md) | 4 records + template + index | doc-layer decisions 0001–0004 |
| [`../architecture.md`](../architecture.md) | ~70 | rewritten front door; the v2.0.0 page moved to [`../history/architecture-v2.0.0.md`](../history/architecture-v2.0.0.md) with a banner |
| [`../README.md`](../README.md) | ~110 | single index; every file under `docs/` listed |
| [`tests/unit/test_docs_integrity.py`](../../tests/unit/test_docs_integrity.py) | ~230 | the doc gate: index, links, Mermaid structure, placeholders, ADR index, H1 discipline |
| [`tests/unit/test_agent_board.py`](../../tests/unit/test_agent_board.py) | ~270 | board schema-2 integrity + CLI behaviour (claim/release/defer/check/gc) |
| [`tests/unit/test_version_lockstep.py`](../../tests/unit/test_version_lockstep.py) | ~130 | task-111 guard, proven red on a mismatched fixture |
| [`.agents/board.json`](../../.agents/board.json) | rewritten | schema 2: protocol block, 21 zones, 18 claims, 10 forward tasks, history (waves/PRs/incidents) |
| [`AGENTS.md`](../../AGENTS.md) | rewritten board section | protocol v2, identity rule, verification rule, and the honest board summary |

## 2. Verified findings (each reproducible)

| # | Finding | Evidence | Consequence |
|---|---|---|---|
| F1 | **Four of six packs cannot be activated.** The CLI registry knows 21 of the 51 implemented operation ids. | `nexus packs list` → pending `8/0/7/8/7/0`; `nexus packs activate nexus.audio.studio` → "the runtime does not know audio.detect_beats, …" (8 ids) | `task-126` promoted to P0 with the evidence recorded; the earlier board claim "pending is 0" is corrected in `history.incidents["pack-pending-correction"]` |
| F2 | **The published architecture page was four releases stale** (v2.0.0: Dropbox/MEGA cloud tiers, 6-section inline menu, Gemini-only AI). | the archived page + `ROADMAP_STATUS.md` wave anchors | page archived with a banner; replaced by a suite verified against `7573249` |
| F3 | **The test suite was green only after an editable install.** With `PYTHONPATH=src` alone, 5 tests fail for environmental reasons (4 spawn `python -m nexus_ai_agent.cli` subprocesses; 1 needs distribution metadata). | local A/B run: 1101 passed / 5 failed → `pip install -e . --no-deps` → 1106 passed / 0 failed | documented in [`../architecture/TESTING.md`](../architecture/TESTING.md) §5 (a documentation fix; CI was never affected) |
| F4 | **`mypy src` locally reported 5 errors that CI does not** — the cause was `sqlmodel 0.0.44` instead of the pinned `0.0.42`, plus a missing `pypdf`/`chromadb` in this sandbox. After pinning: 2 remaining errors in `features/rag.py`, both caused by the absent `chromadb` extra; `rag.py` is byte-identical to `main`. | `diff <(git show origin/main:src/…/rag.py) src/…/rag.py` | recorded so the next session does not chase a phantom regression |
| F5 | **TDD coverage ledger (measured, not estimated):** 71 catalogued ids, 51 implemented and registered, 42 catalogued ids implemented, **29 remaining** (portrait 10, scene 10, motion 3, color 3, audio 2, timeline 1). | union of the seven registry builders minus the TDD id list | `task-134` (portrait slice) added to the forward network |
| F6 | **Reference drift risk:** `docs/README.md` indexed 12 of 27 documents. | index test on the pre-change tree | one index, machine-checked |
| F7 | The coordination CLI had **no tests** and the board had three drifted "ten forward tasks" arrays with inconsistent fields. | `tests/unit/test_agent_board.py` (new), board diff | schema 2 (ADR 0004); structure is now a failing test, not a convention |

## 3. Cost of the change (honest)

- Documentation is now ~1 900 new lines of Markdown to keep true. Mitigation: the index/link/fence gate plus the rule "update the living view in the same PR as the code that invalidated it".
- One target of the change is a **conflict surface**: `AGENTS.md` and `.agents/board.json` are also touched by open PRs #32/#33. Resolution rule for those conflicts: newest state wins, then `tests/unit/test_agent_board.py` must pass — the board cannot be merged in a shape that violates schema 2.
- The board rewrite is a content change for other agents: `status` values `available_sequenced_post_32`, `assigned_to_B_next`, etc. survive, so no existing claim changed hands; only structure and evidence were added.

## 4. Gate results (local, at the time of writing)

| Gate | Command | Result |
|---|---|---|
| Lint | `ruff check .` | **All checks passed** (385 files) |
| Format | `ruff format --check .` | **385 files already formatted** |
| Types | `mypy src` | 2 errors, both environmental (`chromadb` extra absent); `src/` untouched by this PR |
| Tests | `pytest -q -m "not slow"` | **1179 passed, 20 skipped, 0 failed** (baseline before the pass: 1106 passed / 23 skipped — the difference is the 73 new tests in the three files above) |
| Docs gate | `pytest tests/unit/test_docs_integrity.py` | green — 47 tests (index · links · Mermaid · placeholders · ADR index · H1 discipline) |
| Board gate | `pytest tests/unit/test_agent_board.py` | green — 18 tests; board = 18 claims, 21 zones, 10 forward tasks |

## 5. Follow-ups created by this pass

| Item | Where |
|---|---|
| Unified runtime pack registry (F1) | board `task-126` (P0, sequenced after PR#33) |
| Extras smoke matrix — optional paths are outside the default CI job | board `task-132` |
| Portrait family slice (F5) | board `task-134` |
| Docs toolchain adoption (markdownlint/Lychee/mermaid-lint) | `protocol.deferred_improvements` + [ADR 0002](../architecture/adr/0002-docs-as-code-enforcement.md) |
