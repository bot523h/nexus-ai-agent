# Architecture Decision Records (doc layer)

This directory holds **decisions about how the architecture documentation and its enforcement work** — layout, language, diagrams, gates, coordination-board schema.

| Log | Owns | Authority |
|---|---|---|
| [`../../DECISION_LOG.md`](../../DECISION_LOG.md) | every decision that changes **system behaviour**, with dated historical revisions and rejected alternatives | authoritative when summaries disagree |
| this directory | decisions that change **how the documentation/boards themselves work** | authoritative for the docs layer |
| [`../../MULTI_AGENT_PROTOCOL.md`](../../MULTI_AGENT_PROTOCOL.md) | the operating protocol between parallel agents | authoritative for coordination procedure |

## Index

| # | Title | Status | Date |
|---|---|---|---|
| [0001](0001-docs-as-code-layout.md) | Documentation layout: living views + one index + language policy | accepted | 2026-09-21 |
| [0002](0002-docs-as-code-enforcement.md) | Enforce documentation in Python (no Node toolchain yet) | accepted | 2026-09-21 |
| [0003](0003-mermaid-flowchart-for-c4-views.md) | Express C4 views with Mermaid `flowchart` | accepted | 2026-09-21 |
| [0004](0004-board-schema-2.md) | Coordination board schema 2: declared prerequisites and acceptance criteria | accepted | 2026-09-21 |
| [0005](0005-three-layer-operation-truth.md) | Decoupling Product Catalog, Runtime Registry, and Executable Surface | accepted | 2026-09-24 |

## Rules

1. **One decision per file**, numbered, never renumbered. Superseding is done by a new record that names the old one; the old file stays.
2. Format: MADR 4.0-lite — see [`template.md`](template.md). The **Confirmation** section is mandatory: it names the test, command, or review step that keeps the decision true. A decision without a confirmation is a wish.
3. An ADR here must not restate a `DECISION_LOG.md` decision; it links to it.
4. `tests/unit/test_docs_integrity.py` fails if an ADR file exists that is not in the index above, or if an index entry points at a missing file.
5. Statuses: `proposed` → `accepted` → (`deprecated` | `superseded by ADR-XXXX`).
