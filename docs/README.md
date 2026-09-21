# Documentation Map

Single index for everything under `docs/`. If a document is not listed here,
it either lives in `docs/history/` (archived, superseded) or it should.

## Authoritative

| Document | What it is |
|---|---|
| [DECISION_LOG.md](DECISION_LOG.md) | **The** architecture decision log. When any summary disagrees with it, this file wins. |
| [../ROADMAP_STATUS.md](../ROADMAP_STATUS.md) | Living roadmap status with per-wave commit anchors. |
| [../REQUIREMENTS_LEDGER.md](../REQUIREMENTS_LEDGER.md) | PR1/PR2/PR3 checkpoint-lifecycle requirements ledger. |
| [../AGENTS.md](../AGENTS.md) | Multi-agent coordination contract + current board summary. |

## Architecture

| Document | What it is |
|---|---|
| [architecture.md](architecture.md) | Current system architecture overview. |
| [architecture/DATA_LIFECYCLE.md](architecture/DATA_LIFECYCLE.md) | Data lifecycle (stages, retention, forbidden journal table). |
| [architecture/LLM_PROVIDERS.md](architecture/LLM_PROVIDERS.md) | LLM provider chain and routing. |
| [architecture/PORTS.md](architecture/PORTS.md) | Hexagonal ports inventory. |
| [architecture/RETENTION_DECISION.md](architecture/RETENTION_DECISION.md) | Retention policy decision record. |
| [NAGAR_70_OPERATIONS_TDD.md](NAGAR_70_OPERATIONS_TDD.md) | Phase 6 (Nagar creative studio) technical design baseline. |
| [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md) | End-user guide: bring any NEXUS database to the Alembic head. |

## Audits (dated, immutable records)

| Document | What it is |
|---|---|
| [audits/AUDIT_REPORT_2026-09-21.md](audits/AUDIT_REPORT_2026-09-21.md) | Full architecture audit (P0 findings + command honesty matrix). |
| [audits/HANDOFF_ANALYSIS_2026-09-21.md](audits/HANDOFF_ANALYSIS_2026-09-21.md) | Board GC + engineered handoff analysis (agent D session). |

## Protocols

| Document | What it is |
|---|---|
| [MULTI_AGENT_PROTOCOL.md](MULTI_AGENT_PROTOCOL.md) | Multi-agent coordination protocol (English). |
| [MULTI_AGENT_PROTOCOL.fa.md](MULTI_AGENT_PROTOCOL.fa.md) | همان پروتکل به فارسی. |

## Operations

| Document | What it is |
|---|---|
| [ops/deployment-koyeb.md](ops/deployment-koyeb.md) | Koyeb scale-to-zero deployment guide. |
| [ops/NEON_LIFECYCLE_RUNBOOK.md](ops/NEON_LIFECYCLE_RUNBOOK.md) | Neon/PostgreSQL lifecycle runbook. |
| [ops/DEPLOY_RUNBOOK.md](ops/DEPLOY_RUNBOOK.md) | Koyeb deploy runbook: preflight → deploy → smoke → rollback → incidents. |
| [ops/r2-storage.md](ops/r2-storage.md) | Cloudflare R2 blob tier: setup + maintenance secrets. |
| [ops/COLOR_LANE.md](ops/COLOR_LANE.md) | Color/exposure lane runbook: EV→filter mapping, hard contracts, FFmpeg-free validation, troubleshooting. |

## History (archived — read-only, never a source of truth)

| Document | What it is |
|---|---|
| [history/PHASE_D_ARCHITECTURE.md](history/PHASE_D_ARCHITECTURE.md) | Phase D (schema management) engineering record; superseded by DECISION_LOG. |
| [history/v2-roadmap-strategy.md](history/v2-roadmap-strategy.md) | Pre-v2 growth strategy (June 2025); delivered items landed, rest superseded by the seven-phase roadmap. |
| [history/todo-v1.2.0.md](history/todo-v1.2.0.md) … [history/todo-v2.1.0.md](history/todo-v2.1.0.md) | Session checklists from the v1.2→v2.1 era. |
