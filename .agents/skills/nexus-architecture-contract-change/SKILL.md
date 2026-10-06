---
name: nexus-architecture-contract-change
description: This skill should be used when changing a system boundary, a documented architecture claim, an ADR, the DECISION_LOG, i18n strings, or the release version in nexus-ai-agent, when asked to "update the architecture docs", "write an ADR", "add a boundary law", "why does test_docs_integrity fail", "bump the version", or "add a locale key". It encodes docs-as-code enforcement, the rule-to-test register, the MADR template, and the lockstep guards.
---

# NEXUS Architecture & Contract Change

## Purpose

In this repository, documentation and structure are **executable**. A boundary rule that is not
enforced by a test is a defect; a document that is not indexed is a defect; a release whose version
disagrees across three files is a defect. This skill makes those guarantees work for you instead of
against you.

## The three guarantees

1. **Every rule names its test.** `docs/architecture/MODULE_MAP.md` §3 is the register: each law R1–R16
   points at the fitness function that enforces it. The forward direction is itself enforced —
   `tests/architecture/test_module_map_law_coverage.py` (law R15) fails if a law names a test file or a
   `::symbol` that no longer exists. A rule stated in prose with no guard is still a defect by
   convention, but the reverse ("a test with no rule") is **not** a violation: helper and
   infrastructure tests under `tests/architecture/` legitimately have no law.
2. **Every document is indexed and links resolve.** `tests/unit/test_docs_integrity.py` fails on an
   unindexed file under `docs/`, a broken relative link, an unbalanced/empty Mermaid fence, leftover
   `TODO`/`FIXME`, and version drift between `VERSION`, `pyproject.toml`, and the `CHANGELOG` head.
3. **Living vs dated.** `docs/architecture/*` are living views, updated in the same PR as the code
   that invalidated them. `docs/audits/` and `docs/history/` are dated, immutable records — never
   sources of truth. `docs/DECISION_LOG.md` wins when any summary disagrees with it.

## Procedure by change type

### Change a system boundary (behaviour)

1. Add or update the law in `docs/architecture/MODULE_MAP.md` §3 **and** its enforcing test in
   `tests/architecture/`. Both, in the same PR.
2. If it changes **system behaviour**, record it in `docs/DECISION_LOG.md`.
3. If it is about the **documentation/board layer**, write an ADR under `docs/architecture/adr/`.

### Write an ADR

Use `docs/architecture/adr/template.md` (MADR 4.0-lite). The **Confirmation** section is mandatory —
it names the test or command that keeps the decision true. Number it `NNNN-slug.md` (four digits;
`test_docs_integrity.py` checks that the index lists exactly the existing records) and add it to
`docs/architecture/adr/README.md` and `docs/README.md`.

### Change a documented claim

Update the page in place, then run `python -m pytest -q tests/unit/test_docs_integrity.py`. If you
quote a count, include the command that regenerates it (`TESTING.md` §6).

### Add a locale key

`src/nexus_ai_agent/i18n/locales/` — 15 locales × 79 keys. `tests/unit/test_i18n_parity.py` fails on
a missing key in any locale. Add the key to all 15. (Re-derive the count with
`python3 -c "import json;print(len(json.load(open('src/nexus_ai_agent/i18n/locales/en.json'))))"`.)

### Bump the version (a release)

`VERSION` == `pyproject.toml [project].version` == the latest `CHANGELOG.md` heading, all in lockstep:

```bash
python scripts/check_version_lockstep.py
python -m pytest -q tests/unit/test_docs_integrity.py
```

### Retire a module

Delete it, delete its `tests/architecture/legacy_baseline.json` entry if present, update
`MODULE_MAP.md` §2 and `OVERVIEW.md` §8. `test_docs_integrity.py` fails if a linked path disappears.

## Adding a new document

Add it to the correct table in `docs/README.md` **in the same PR that adds the file**. English is
canonical for architecture/CI/PR text; Persian summaries defer to the English text.

## Additional Resources

- **`references/docs-governance.md`** — the docs-integrity assertion table, the ADR/DECISION_LOG
  split, the full rule→test register (R1–R16), the i18n parity rule, and the version-lockstep chain.

## Common mistakes

- Adding a `docs/` file without indexing it in `docs/README.md` — docs integrity goes red.
- Stating a boundary in prose without an enforcing test — that is a documented defect.
- Putting a behaviour decision in an ADR instead of `DECISION_LOG.md` (or vice versa).
- Bumping `VERSION` without the `CHANGELOG` heading and `pyproject.toml` — the lockstep guard fails.
- Leaving a `TODO` in an architecture page — the docs gate rejects placeholder tokens.
