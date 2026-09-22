---
status: accepted
date: 2026-09-21
deciders: session arena/01a0c4c1 (task-131)
---

# 0003. Express C4 views with Mermaid `flowchart`

## Context and Problem Statement

The architecture views follow the C4 model (Context, Container). Mermaid has a dedicated `C4Context`/`C4Container` syntax, but its rendering depends on the consumer's Mermaid version and build (GitHub's Markdown pipeline is the primary consumer here, plus editors and IDE previews). A diagram that renders in one place and shows as a syntax error in another is worse than a slightly more explicit diagram that renders everywhere.

## Decision Drivers

- The documentation must render **in the repository host, in PR diffs, and in any Markdown preview** without a plugin.
- Diagrams must survive review as text (diffable, reviewable line by line).
- No new tooling: diagrams are validated only structurally today ([`0002`](0002-docs-as-code-enforcement.md)).

## Considered Options

1. `C4Context` / `C4Container` Mermaid syntax (semantic C4 rendering).
2. `flowchart` with C4-aligned labels and level-annotated subgraphs (the chosen option).
3. PlantUML/C4-PlantUML rendered to images (checked-in binaries).
4. Structurizr DSL as the model source with generated views.

## Decision Outcome

Chosen option: **2**. Every diagram is:

- a `flowchart` (occasionally `sequenceDiagram` where a runtime interaction is the subject),
- labelled with its C4 level in the caption and the document (`C4 level 1 — system context`),
- restricted to `flowchart`/`sequenceDiagram` constructs supported broadly by Mermaid 10+ on GitHub,
- free of external image assets (nothing to regenerate, nothing to drift).

The C4 *concepts* are preserved: one box per deployable container, one box per external system, explicit relationship labels, and a trust-boundary note wherever a boundary is crossed ([`../OVERVIEW.md`](../OVERVIEW.md) §5–6).

### Consequences

- (+) Renders on GitHub, in editors, and in PR diffs; diffable; no assets.
- (+) Semantic C4 is still understood — the level is stated in the caption and the surrounding prose.
- (−) No automatic C4 layout/notation polish; a reader looking for the canonical C4 shapes will see flowchart boxes instead.
- (~) If a future reader needs true C4 semantics, option 4 (Structurizr → generated views) remains the upgrade path; it requires a build step and is therefore out of scope while the docs are in-repo.

## Confirmation

Read-time: every diagram in `docs/architecture/` states its level. Machine-time: `tests/unit/test_docs_integrity.py` verifies each ```mermaid block is non-empty, balanced, and uses a known diagram type, so a broken fence or an accidental image-syntax block cannot land.

## Pros and Cons of the Options

### Option 1 — `C4Context` syntax

- (+) renders as canonical C4 with icons
- (−) support varies by Mermaid version/host; a syntax error is silent for many readers

### Option 2 — `flowchart` with C4 labels (chosen)

- (+) universal rendering, diffable, no tooling
- (−) not visually "C4-shaped"

### Option 3 — rendered images

- (+) pixel-perfect
- (−) binary assets in git, regenerate-on-change friction, no diff review

### Option 4 — Structurizr DSL

- (+) single model, multiple generated views, true C4
- (−) new toolchain and a generation step for < 10 diagrams

## More Information

- [`../OVERVIEW.md`](../OVERVIEW.md) §5–6, [`../RUNTIME_FLOWS.md`](../RUNTIME_FLOWS.md)
- [C4 FAQ on combining C4 with arc42](https://c4model.com/faq)
