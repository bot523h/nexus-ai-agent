# Documentation Map

Single index for everything under `docs/`. If a document is not listed here it either lives in
`docs/history/` (archived, superseded) or it is a defect — `tests/unit/test_docs_integrity.py`
fails on an unindexed file, a broken relative link, an unbalanced Mermaid fence, or leftover
placeholder text.

**Front door:** [`architecture.md`](architecture.md). **How this map works:**
[ADR 0001](architecture/adr/0001-docs-as-code-layout.md) (layout) ·
[ADR 0002](architecture/adr/0002-docs-as-code-enforcement.md) (what is enforced, and what waits).

---

## Architecture (living views — updated in place)

| Document | What it is |
|---|---|
| [architecture.md](architecture.md) | Front door: the system in one picture + the reading order. |
| [architecture/OVERVIEW.md](architecture/OVERVIEW.md) | C4 context & container views, stakeholders, the five quality attributes, frozen constraints, deployment topologies. |
| [architecture/OVERVIEW.fa.md](architecture/OVERVIEW.fa.md) | خلاصهٔ فارسی و نقشهٔ مسیر مطالعه (navigation summary; English wins on conflict). |
| [architecture/MODULE_MAP.md](architecture/MODULE_MAP.md) | Layer diagram, package inventory, boundary laws → the tests that enforce them, extension recipes. |
| [architecture/RUNTIME_FLOWS.md](architecture/RUNTIME_FLOWS.md) | Message, slideshow, render-lane, checkpoint, migration, and webhook flows — each with its failure contract. |
| [architecture/CREATIVE_STUDIO.md](architecture/CREATIVE_STUDIO.md) | Nagar: capability model, permission ladder, packs, activation gap, TDD coverage ledger, render lane. |
| [architecture/DATA_AND_STORAGE.md](architecture/DATA_AND_STORAGE.md) | Every store, ownership, Alembic chain, retention, portability, and what never enters a store. |
| [architecture/SECURITY.md](architecture/SECURITY.md) | Trust boundaries, STRIDE threat → control → evidence table, P0 audit follow-through, review checklist. |
| [architecture/OBSERVABILITY.md](architecture/OBSERVABILITY.md) | Structured events, metric policy, health semantics, inspection commands, the short alert list. |
| [architecture/TESTING.md](architecture/TESTING.md) | Test taxonomy, the four gates, determinism rules, baseline numbers, honest gaps. |
| [architecture/PORTS.md](architecture/PORTS.md) | The six hexagonal ports, their invariants and their signature tests. |
| [architecture/REFERENCES.md](architecture/REFERENCES.md) | The external standards followed (C4, arc42, MADR 4.0, fitness functions, docs-as-code, STRIDE) and what was deliberately not adopted. |
| [architecture/DATA_LIFECYCLE.md](architecture/DATA_LIFECYCLE.md) | Data lifecycle contract: stages, retention, checkpoint schemas, the forbidden operation journal. |
| [architecture/RETENTION_DECISION.md](architecture/RETENTION_DECISION.md) | Retention policy decision record (30-day resumability window, circuit breaker). |
| [architecture/LLM_PROVIDERS.md](architecture/LLM_PROVIDERS.md) | Provider chain, cooldowns, anti-retry-storm rule, strict-privacy flag. |
| [architecture/adr/README.md](architecture/adr/README.md) | Doc-layer decision index + rules. |
| [architecture/adr/template.md](architecture/adr/template.md) | MADR 4.0-lite template (Confirmation section mandatory). |
| [architecture/adr/0001-docs-as-code-layout.md](architecture/adr/0001-docs-as-code-layout.md) | Documentation layout: living views + one index + language policy. |
| [architecture/adr/0002-docs-as-code-enforcement.md](architecture/adr/0002-docs-as-code-enforcement.md) | Enforce documentation in Python now; Node toolchain on a recorded trigger. |
| [architecture/adr/0003-mermaid-flowchart-for-c4-views.md](architecture/adr/0003-mermaid-flowchart-for-c4-views.md) | Express C4 views with Mermaid `flowchart`. |
| [architecture/adr/0004-board-schema-2.md](architecture/adr/0004-board-schema-2.md) | Coordination board schema 2: prerequisites, acceptance criteria, history split. |

## Authoritative records

| Document | What it is |
|---|---|
| [DECISION_LOG.md](DECISION_LOG.md) | **The** architecture decision log. When any summary disagrees with it, this file wins. |
| [../ROADMAP_STATUS.md](../ROADMAP_STATUS.md) | Living roadmap status with per-wave commit anchors. |
| [../REQUIREMENTS_LEDGER.md](../REQUIREMENTS_LEDGER.md) | PR1/PR2/PR3 checkpoint-lifecycle requirements ledger. |
| [../AGENTS.md](../AGENTS.md) | Multi-agent coordination contract + current board summary. |
| [NAGAR_70_OPERATIONS_TDD.md](NAGAR_70_OPERATIONS_TDD.md) | Phase 6 (Nagar creative studio) technical design baseline — 71 operation ids. |
| [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md) | End-user guide: bring any NEXUS database to the Alembic head. |

## Protocols

| Document | What it is |
|---|---|
| [MULTI_AGENT_PROTOCOL.md](MULTI_AGENT_PROTOCOL.md) | Multi-agent coordination protocol (English). |
| [MULTI_AGENT_PROTOCOL.fa.md](MULTI_AGENT_PROTOCOL.fa.md) | همان پروتکل به فارسی. |

## Operations

| Document | What it is |
|---|---|
| [ops/DEPLOY_RUNBOOK.md](ops/DEPLOY_RUNBOOK.md) | Koyeb deploy runbook: preflight → deploy → smoke → rollback → incidents. |
| [ops/deployment-koyeb.md](ops/deployment-koyeb.md) | Koyeb scale-to-zero deployment guide. |
| [ops/NEON_LIFECYCLE_RUNBOOK.md](ops/NEON_LIFECYCLE_RUNBOOK.md) | Neon/PostgreSQL lifecycle runbook. |
| [ops/r2-storage.md](ops/r2-storage.md) | Cloudflare R2 blob tier: setup + maintenance secrets. |
| [ops/COLOR_LANE.md](ops/COLOR_LANE.md) | Color/exposure lane runbook: EV→filter mapping, hard contracts, FFmpeg-free validation, troubleshooting. |
| [ops/PACK_RUNTIME.md](ops/PACK_RUNTIME.md) | Unified pack runtime (wave 5): composition, activation gate, op-gap ledger, composition checklist for new packs. |
| [ops/RUNBOOK_HARDENING.md](ops/RUNBOOK_HARDENING.md) | Runbook hardening pass (wave-4 step 8): boundary conditions, failure modes, operator knobs. |

## Audits (dated, immutable records)

| Document | What it is |
|---|---|
| [audits/AUDIT_REPORT_2026-09-21.md](audits/AUDIT_REPORT_2026-09-21.md) | Full architecture audit (P0 findings + command honesty matrix). |
| [audits/HANDOFF_ANALYSIS_2026-09-21.md](audits/HANDOFF_ANALYSIS_2026-09-21.md) | Board GC + engineered handoff analysis (agent D session). |
| [audits/REPO_HYGIENE_REPORT_2026-09-21.md](audits/REPO_HYGIENE_REPORT_2026-09-21.md) | Repository hygiene pass: docs reorganisation, release alignment, branch janitorial work. |
| [audits/ARCHITECTURE_DOCS_2026-09-21.md](audits/ARCHITECTURE_DOCS_2026-09-21.md) | Architecture-documentation pass (task-131): what was written, verified, and enforced. |
| [audits/WAVE5_ACTIVATION_2026-09-21.md](audits/WAVE5_ACTIVATION_2026-09-21.md) | Wave-5 activation audit: unified runtime, six packs active, coverage harness, honest 10-task forward network. |
| [audits/FORENSIC_PR40_CI_2026-09-21.md](audits/FORENSIC_PR40_CI_2026-09-21.md) | Forensic CI analysis of the red PR#40 wave-4 batch (root cause + A/B proof). |
| [audits/SESSION_PLAN_2026-09-21_01a0c506.md](audits/SESSION_PLAN_2026-09-21_01a0c506.md) | Session plan record (agent 01a0c506): wave-4 hardening batch planning. |
| [audits/PR32_TRIAGE_2026-09-21.md](audits/PR32_TRIAGE_2026-09-21.md) | Forensic per-file triage of PR#32 vs merged PR#34 (15 SUPERSEDED / 4 DROP / 3 PORT / 4 ADAPT) — basis of the supersession. |
| [audits/P0_STABILIZATION_2026-09-24.md](audits/P0_STABILIZATION_2026-09-24.md) | Owner-directed P0 day (task-165/166/167): legacy `/creative/*` lane disposition, studio surface wiring, verifiable backups — repro/evidence-first report. |
| [audits/DEAD_ENGINES_2026-09-22.md](audits/DEAD_ENGINES_2026-09-22.md) | Dead-engine verification (zero importers, measured gates), five-option wiring analysis, negative controls, and the recorded residuals. |

## History (archived — read-only, never a source of truth)

| Document | What it is |
|---|---|
| [history/architecture-v2.0.0.md](history/architecture-v2.0.0.md) | The v2.0.0 architecture diagram, archived with a banner when the suite replaced it. |
| [history/PHASE_D_ARCHITECTURE.md](history/PHASE_D_ARCHITECTURE.md) | Phase D (schema management) engineering record; superseded by DECISION_LOG. |
| [history/v2-roadmap-strategy.md](history/v2-roadmap-strategy.md) | Pre-v2 growth strategy (June 2025); delivered items landed, rest superseded by the seven-phase roadmap. |
| [history/todo-v1.2.0.md](history/todo-v1.2.0.md) | Session checklist, v1.2.0 era. |
| [history/todo-v1.3.0.md](history/todo-v1.3.0.md) | Session checklist, v1.3.0 era. |
| [history/todo-v2.0.0.md](history/todo-v2.0.0.md) | Session checklist, v2.0.0 era. |
| [history/todo-v2.1.0.md](history/todo-v2.1.0.md) | Session checklist, v2.1.0 era. |
| [history/todo-step3-pending.md](history/todo-step3-pending.md) | Unfinished items from the v1.x step-3 plan. |

---

## Rules of this map

1. **One index.** A new document is added to the right table above in the same PR that adds the file.
2. **Living vs dated.** `architecture/` is updated in place; `audits/` and `history/` are dated and immutable.
3. **Every structural claim has an enforcer.** If a page states a boundary or invariant, it names the test that guards it ([`MODULE_MAP.md`](architecture/MODULE_MAP.md) §3 is the register).
4. **Numbers are reproducible.** Documents that quote counts (files, operations, tests) include the command that regenerates them ([`TESTING.md`](architecture/TESTING.md) §6).
5. **Language.** English is canonical for architecture, CI, and PR text; Persian summaries exist for the owner and defer to the English text. The board and AGENTS.md remain bilingual by design.
6. **Enforcement.** `pytest -q tests/unit/test_docs_integrity.py` — run it before pushing a documentation change.
