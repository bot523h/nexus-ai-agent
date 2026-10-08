# References & Standards

**Status:** Living document
**Scope:** the external standards this documentation set follows, what we adopted, what we deliberately did not
**Verified against:** 2026-09-21

Documentation that cites nothing is opinion. This page records *which* industry standard each part of the architecture documentation implements, so a future maintainer can tell the difference between a rule we chose and a rule we inherited by accident.

---

## 1. How the views are structured

| Standard | What it gives us | Where it is applied |
|---|---|---|
| **C4 model** (Brown) — Context → Container → Component → Code | one vocabulary for zoom levels; each level serves a different audience | [`OVERVIEW.md`](OVERVIEW.md) §5–6 (Context/Container). Component detail is intentionally replaced by the package tables, because a component diagram that mirrors `ls` rots faster than the code ([C4 FAQ](https://c4model.com/faq): Context/Container/Component map onto arc42 §3/§5.1/§5.2) |
| **arc42** (Starke & Hruschka) | a checklist that prevents whole concerns from being forgotten (goals, constraints, context, building blocks, runtime, deployment, cross-cutting, decisions, quality, risks) | section coverage: purpose→§1, stakeholders→§2, quality→§3, constraints→§4, context→§5, containers→§6, deployment→§7, runtime→[`RUNTIME_FLOWS.md`](RUNTIME_FLOWS.md), building blocks→[`MODULE_MAP.md`](MODULE_MAP.md), cross-cutting→[`SECURITY.md`](SECURITY.md)/[`OBSERVABILITY.md`](OBSERVABILITY.md)/[`TESTING.md`](TESTING.md), decisions→[`../DECISION_LOG.md`](../DECISION_LOG.md)+[`adr/`](adr/README.md) |
| **Mermaid-in-Markdown, flowchart syntax** | diagrams that render on GitHub, in the repo, in PR diffs — no external tool, no binary asset to drift | every diagram in this suite. The C4 *concept* is expressed with `flowchart` labels rather than `C4Context` because GitHub's Mermaid build support for C4 diagrams is narrower than for flowcharts (decision recorded in [`adr/0003`](adr/0003-mermaid-flowchart-for-c4-views.md)) |

## 2. How decisions are recorded

| Standard | What it gives us | Where |
|---|---|---|
| **Nygard ADR** (2011) | context / decision / consequences, in the repository, one file per decision | [`adr/`](adr/README.md) |
| **MADR 4.0** (adr.github.io/madr, 2024-09-17) | decision drivers, considered options with pros/cons, **confirmation** section (how compliance is validated) | [`adr/template.md`](adr/template.md) |
| Repository history | the authoritative, dated record of *all* architecture decisions, including rejected alternatives | [`../DECISION_LOG.md`](../DECISION_LOG.md) — when a summary disagrees with it, the log wins |

**Boundary between the two**: `DECISION_LOG.md` is the project's historical record (what we chose and why, across every phase). `docs/architecture/adr/` holds *doc-layer* decisions — how the architecture documentation itself is produced, enforced, and versioned. A decision that changes system behaviour belongs in the log; a decision that changes how the documentation works belongs in an ADR. [`adr/0001`](adr/0001-docs-as-code-layout.md) records that split.

## 3. How the rules are enforced

| Practice | Source | Where applied here |
|---|---|---|
| **Architecture fitness functions** — executable guards for structural intent | Ford/Parsons/Kua, *Building Evolutionary Architectures*; [InfoQ](https://www.infoq.com/articles/fitness-functions-architecture/); [pattern entry](https://aipatternbook.com/architecture-fitness-function) | `tests/architecture/` (15 files) ↔ [`MODULE_MAP.md`](MODULE_MAP.md) §3 |
| **Import-boundary testing in Python** — AST/import rules instead of hope | the same practice as `import-linter` / `pytest-archon` ([pytest-archon](https://deepwiki.com/jwbargsten/pytest-archon)) | implemented dependency-free with `ast`, which keeps CI Python-only |
| **Docs as code** — docs versioned, reviewed, and checked like source | [docs-as-code practice roundup](https://converter.brightcoding.dev/blog/guide-to-writing-technical-docs-with-markdown-in-2025-7-game-changing-practices-that-will-10x-your-documentation-quality) | `tests/unit/test_docs_integrity.py` (index, links, fences, no placeholders) |
| **Link checking / Mermaid linting / prose linting** | GitLab's documented docs pipeline (Vale + markdownlint + Lychee + mermaidlint, [docs testing](https://docs.gitlab.com/development/documentation/testing/)), [mermaid-lint](https://github.com/jasonworden/mermaid-lint) | **not adopted yet** — trade-off recorded in [`adr/0002`](adr/0002-docs-as-code-enforcement.md): the checks that need Node/Chromium wait until `docs/` is large enough to justify them |

## 4. Security and reliability references

| Practice | Source | Where |
|---|---|---|
| **STRIDE** threat enumeration | Microsoft threat-modelling tradition | [`SECURITY.md`](SECURITY.md) §2 (threat → control → evidence → status) |
| **Fail-closed / least privilege defaults** | general security engineering | deny-by-default access gate, empty allow-list ⇒ nobody, missing extra ⇒ typed error |
| **Evidence-based operations** (measure, don't assume) | SRE practice popularised by *Site Reliability Engineering* (Google) | render lane probing + sha256, `healthz` semantics, queue status as source of truth ([`OBSERVABILITY.md`](OBSERVABILITY.md)) |
| **Bounded work** instead of latency promises | SRE (SLI/SLO discipline: don't promise what you cannot influence) | one process timeout, bounded retries, typed failures — see [`OBSERVABILITY.md`](OBSERVABILITY.md) §6 |

## 5. Domain references

| Topic | Source | Where |
|---|---|---|
| Timeline interchange | OpenTimelineIO (`Timeline → Stack/Tracks → Clips/Transitions`) | `creative/packs/delivery/operations.py` (`delivery.export_otio`), [`../NAGAR_70_OPERATIONS_TDD.md`](../NAGAR_70_OPERATIONS_TDD.md) §1 |
| Media operations catalogue | the Phase-6 TDD (71 operation ids) | [`CREATIVE_STUDIO.md`](CREATIVE_STUDIO.md) §5 coverage ledger |
| Local speech-to-text | faster-whisper / CTranslate2 (int8, no torch) | `adapters/whisper_local.py`, extra `[speech]` |
| Offline translation | argos-translate | extra `[translate]`, `caption.translate_local` |
| Multi-provider LLM routing | litellm `Router` fallbacks + cooldowns | [`LLM_PROVIDERS.md`](LLM_PROVIDERS.md) |

## 6. Multi-agent coordination

| Practice | Where |
|---|---|
| Claim-before-code with leases, one owner per file-zone, one gates owner | [`../../AGENTS.md`](../../AGENTS.md), `.agents/board.json`, `scripts/agent_board.py` |
| Identity by branch, not by letter (collision-proof) | `AGENTS.md` identity rule (introduced after the 2026-09-21 triple-E / double-F collisions) |
| Board restructured to declare prerequisites and acceptance criteria | `.agents/board.json` schema 2 ([`adr/0004`](adr/0004-board-schema-2.md)) |
