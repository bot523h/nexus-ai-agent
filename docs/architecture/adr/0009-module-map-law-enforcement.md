---
status: accepted
date: 2026-10-05
deciders: session arena/skills-suite (task-234)
informed: all agents working on this repository
---

# 0009. Enforce the boundary-law → guard register (every law names a live test)

## Context and Problem Statement

`AGENTS.md` §7 states that "every boundary rule must name its enforcing test in `docs/architecture/MODULE_MAP.md` §3", and `docs/README.md` rule 3 repeats it ("every structural claim has an enforcer"). The register existed but nothing checked it: `grep` for `MODULE_MAP` across `tests/` returned no test, so the table was maintained by hand. A guard renamed, moved, or deleted would leave its law pointing at a phantom, and the documentation contract — the repository's core discipline — would decay into decoration exactly as ADR [`0002`](0002-docs-as-code-enforcement.md) warns.

## Decision Drivers

- The register is load-bearing: it is how a newcomer (human or agent) finds the executable proof behind a claimed boundary.
- The failure mode is silent and slow (a renamed test), which is precisely what a fitness function is for.
- Zero new dependencies and milliseconds of runtime, consistent with the Python-only gate decision in ADR `0002`.
- It must not make the register harder to extend: a new law with a real guard should stay a one-line addition.

## Considered Options

1. Leave it to review (status quo).
2. A prose lint or an external doc toolchain (Vale/markdownlint) that cannot understand `file::symbol` references.
3. A stdlib fitness function in `tests/architecture/` that parses §3 and resolves every named reference against the tree.

## Decision Outcome

Chosen option: **3**. `tests/architecture/test_module_map_law_coverage.py` reads `MODULE_MAP.md` §3, keys rows by their `R<n>` id, extracts each `test_*.py` / `test_*.py::symbol` reference from the row's code spans, resolves the file under `tests/`, and checks the `::symbol` against the file's AST. It fails naming the offending law. A vacuity guard fails if fewer than ten laws parse, and a positive control proves a dangling reference is detected.

### Consequences

- (+) A renamed or deleted guard now turns the documentation contract red with a precise message.
- (+) Adding a law is still one table row; the gate simply checks that its named guard is real.
- (−) It verifies *presence*, not *relevance*: a law could name a test that exists but no longer enforces it. Relevance remains a review responsibility (the same honest limit recorded for the structural gates in `MODULE_MAP.md` §4).
- (~) Rows are keyed by `R<n>`, so a formatting change cannot silently drop a law.

## Confirmation

`pytest -q tests/architecture/test_module_map_law_coverage.py`. The gate names itself as law **R15** in `MODULE_MAP.md` §3 and is referenced from [`../TESTING.md`](../TESTING.md) §4. The positive control in the file (`test_the_gate_recognises_a_dangling_reference`) is the mutation proof: pointing a law at `test_glossary_liveness.py::test_never_defined_zzz` fails the symbol check.

## Pros and Cons of the Options

### Option 1 — review only

- (+) no new code
- (−) the repository's own history shows the decay this allows; a hand-maintained register drifts

### Option 2 — external doc toolchain

- (+) richer prose checking
- (−) cannot resolve `file::symbol`; adds a second toolchain for a problem `ast`/`re`/`pathlib` solve

### Option 3 — stdlib fitness function (chosen)

- (+) precise, dependency-free, fast, consistent with ADR `0002`
- (−) presence-only, stated above

## More Information

- [`0002-docs-as-code-enforcement.md`](0002-docs-as-code-enforcement.md) (the enforcement precedent)
- `docs/README.md` rule 3, `AGENTS.md` §7, `MODULE_MAP.md` §3 (law R15) and §4
- Board task **task-234** (`task-234-module-map-law-enforcement`)
