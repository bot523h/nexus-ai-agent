---
status: accepted
date: 2026-09-21
deciders: session arena/01a0c4c1 (task-131)
consulted: AGENTS.md multi-agent protocol
informed: all agents working on this repository
---

# 0001. Documentation layout: living views, one index, one language

## Context and Problem Statement

Before this change, the architecture documentation was a single 329-line page written for v2.0.0 (it still described Dropbox/MEGA cloud tiers and a 6-section inline menu) sitting next to four small `docs/architecture/*.md` files and a dated audit. `docs/README.md` indexed some files, `README.md` indexed others, and `docs/decisions/` did not exist while `DECISION_LOG.md` held everything. Two failure modes followed: **stale content that still looked authoritative**, and **no rule about where a new document goes** — so parallel agents wrote into whichever file they found first.

## Decision Drivers

- Parallel agents must be able to add documentation without colliding (the repo's single highest-conflict file is a Python handler, not a doc — keep it that way).
- A reader must be able to tell a *living view* from a *dated immutable record* at a glance.
- The repository's primary audience is bilingual (owner instructions in Persian, code and CI in English).
- The documentation must be checkable by a machine, or it will drift.

## Considered Options

1. Keep one big `architecture.md`, extended in place.
2. Adopt an external docs site generator (MkDocs/Zensical/Docusaurus) as the source of truth.
3. Split into a small set of **view documents** with one index, a dated-record area, and an ADR layer — all inside the repo, all Markdown.

## Decision Outcome

Chosen option: **3**, because the repository is the only shared medium between parallel sandboxes and CI is Python-only; a docs site adds a build toolchain without adding a single enforced rule.

Structure now in force:

| Area | Contract |
|---|---|
| `docs/architecture/*.md` | living views — updated in place; a stale statement is a bug fixed in the same PR as the code that invalidated it |
| `docs/architecture/adr/` | doc-layer decisions (this file) with MADR-lite format |
| `docs/audits/`, `docs/history/` | dated / superseded records — append-only in spirit, never the source of truth |
| `docs/README.md` | the **single** index; every file under `docs/` appears exactly once |
| `docs/architecture.md` | the front door: a short landing page that routes to the suite and to the archived v2.0.0 diagram |
| Language | English is canonical for architecture and CI; one Persian navigation summary (`OVERVIEW.fa.md`) exists for the owner, and it defers to the English text on conflict |

### Consequences

- (+) A reader gets the same structure every time; parallel agents have an obvious destination per concern.
- (+) The old v2.0.0 diagram is preserved as history instead of being deleted or silently rewritten.
- (−) More files to keep consistent — mitigated by the index test (below).
- (~) Persian content stays a *summary*; duplicating every page in two languages would double the drift surface.

## Confirmation

`tests/unit/test_docs_integrity.py` asserts: every file under `docs/` is indexed exactly once in `docs/README.md`; every relative link resolves; the Persian summary exists and links back to the English views; ADR index ↔ files agree.

## Pros and Cons of the Options

### Option 1 — one big page

- (+) nothing to index
- (−) no place for per-concern ownership; every edit conflicts; it already went stale for four releases

### Option 2 — external docs site

- (+) search, theming, versioned builds
- (−) adds Node/Chromium to a Python-only CI, splits truth between the site and the repo, and cannot be validated by the Python gates that already exist

### Option 3 — in-repo views + index + ADRs (chosen)

- (+) zero new toolchain, reviewable in PR diffs, checkable by pytest
- (−) no built-in search; acceptable below ~30 documents, revisit at that size

## More Information

- [`0002-docs-as-code-enforcement.md`](0002-docs-as-code-enforcement.md) — what is enforced and what waits
- [`../OVERVIEW.md`](../OVERVIEW.md), [`../MODULE_MAP.md`](../MODULE_MAP.md), [`../RUNTIME_FLOWS.md`](../RUNTIME_FLOWS.md)
- Archived predecessor: [`../../../history/architecture-v2.0.0.md`](../../history/architecture-v2.0.0.md)
