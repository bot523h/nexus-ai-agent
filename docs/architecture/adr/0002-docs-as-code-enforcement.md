---
status: accepted
date: 2026-09-21
deciders: session arena/01a0c4c1 (task-131)
informed: all agents working on this repository
---

# 0002. Enforce documentation in Python (no Node toolchain yet)

## Context and Problem Statement

If documentation is not checked, it becomes decoration. The industry toolchain for docs-as-code is mature — Vale for prose, `markdownlint-cli2` for structure, Lychee for links, `mermaid-lint` for diagrams, as GitLab documents in its own [docs testing pipeline](https://docs.gitlab.com/development/documentation/testing/) and as the [mermaid-lint](https://github.com/jasonworden/mermaid-lint) project demonstrates for diagram parsing. All of it, however, is Node/Chromium-based, while this repository's CI is a single Python job (`ruff`, `mypy`, `pytest`) plus a PostgreSQL job.

## Decision Drivers

- Momentum and access: adding a second toolchain slows every PR (install time, cache, version pinning) and creates a second failure surface.
- The genuinely dangerous documentation failures here are *broken links*, *unindexed files*, *placeholder text left behind*, *unbalanced/empty diagrams*, and *version drift* — all checkable with `ast`/`re`/`pathlib`.
- The repo's mission constraint: core stays dependency-light; docs must not be the reason a build is heavy.

## Considered Options

1. Adopt the full Node toolchain now (Vale + markdownlint + Lychee + mermaid-lint) in a second CI job.
2. Enforce nothing; rely on review.
3. Enforce the high-value subset in Python now, and record the adoption trigger for the rest.

## Decision Outcome

Chosen option: **3**. `tests/unit/test_docs_integrity.py` (stdlib only, runs in the existing `pytest` job) enforces:

| Check | Failure it prevents |
|---|---|
| every `docs/**` file indexed once in `docs/README.md` | orphaned documents nobody reads |
| every relative Markdown link resolves on disk | dead links (the #1 way docs become fiction) |
| every ```mermaid fence is balanced, non-empty, and starts with a known diagram type | empty/copy-pasted diagram blocks |
| no `TODO`/`FIXME`/`XXX`/`<placeholder>` in architecture docs | unfinished text shipped as authority |
| `VERSION` == `pyproject.toml` == latest `CHANGELOG` heading | release drift (pairs with board task-111) |

**Adoption trigger for the heavier toolchain** (recorded so it is not lost): when `docs/` exceeds ~30 Markdown files or when a non-English documentation set is added, adopt `markdownlint-cli2` + Lychee in a separate CI job, and `mermaid-lint` for real diagram parsing. Until then, the Python guard covers the failure modes that actually occurred in this repository's history.

### Consequences

- (+) Zero new dependencies; the checks run in the job everyone already runs.
- (+) Fast: milliseconds, no network.
- (−) Mermaid *syntax* is not fully validated — a structurally plausible but syntactically invalid diagram can pass. Mitigation: diagrams are kept small and flowchart-based ([`0003`](0003-mermaid-flowchart-for-c4-views.md)), and a syntax error is visually obvious in a PR diff.
- (−) Prose style (Vale's territory) is unenforced; review owns it.

## Confirmation

`pytest -q tests/unit/test_docs_integrity.py`; the file itself is listed in [`../TESTING.md`](../TESTING.md) §4, and every check above names its failure mode so removing one is a visible decision.

## Pros and Cons of the Options

### Option 1 — Node toolchain now

- (+) strongest guarantees, industry-standard tooling
- (−) second toolchain in CI for < 20 Markdown files; Chromium download for Mermaid parsing; pins and caches to maintain

### Option 2 — nothing

- (−) the repository's own history shows exactly what happens: a v2.0.0 architecture page surviving four releases

### Option 3 — Python subset now, trigger recorded (chosen)

- (+) enforced where it matters today, cheap, no new dependency
- (−) honest coverage gap, documented in [`../TESTING.md`](../TESTING.md) §7

## More Information

- [`0001-docs-as-code-layout.md`](0001-docs-as-code-layout.md), [`../REFERENCES.md`](../REFERENCES.md) §3
- Board task **task-111** (release lock-step guard) shares the version assertion.
