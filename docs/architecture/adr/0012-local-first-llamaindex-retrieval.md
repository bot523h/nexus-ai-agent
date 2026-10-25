---
status: accepted
date: 2026-10-23
deciders: agent session arena/01c872e0-nexus-ai-agent (board task-273-llamaindex-retrieval)
consulted: AGENTS.md protocol v2, docs/architecture/MODULE_MAP.md
informed: future per-project knowledge scoping work
---

# 0012. Knowledge-retrieval documentation is a living view enforced by a named fitness test, not prose

## Context and Problem Statement

The knowledge-retrieval capability (task-273) introduced a new subsystem whose
*behaviour* decisions (adapter confinement, embedding provider, authoritative
store, evidence semantics) belong in `docs/DECISION_LOG.md` per the coding
guidelines and this directory's own rule 3 ("an ADR here must not restate a
DECISION_LOG decision; it links to it"). What is still undecided here is the
**documentation-enforcement** question: where the capability's living
documentation lives, and how the repo keeps it true as the code changes.

## Decision Drivers

- Every boundary rule must name its enforcing test (AGENTS.md §7, MODULE_MAP).
- One index, living views, no duplicate authoritative records.
- Drift between docs and code must fail a gate, not a review comment.

## Considered Options

1. Prose-only documentation in one page, enforced by review discipline.
2. **A single living architecture view plus a MODULE_MAP law row whose
   enforcing test is an executable fitness function** (chosen).
3. Fold the capability docs into existing pages (OVERVIEW/MODULE_MAP) only.

## Decision Outcome

Chosen option: **2**, because it makes both the docs and their claims
mechanically checkable:

- `docs/architecture/KNOWLEDGE_RETRIEVAL.md` is the living view (indexed in
  `docs/README.md`, with `truth:` witness markers the truth doctor verifies).
- MODULE_MAP law **R22** names `tests/architecture/test_knowledge_retrieval_boundaries.py`
  as its enforcing test; that test (plus the docs-integrity suite) is the
  confirmation that the written boundaries and commands stay executable.

Behaviour decisions themselves are **not** recorded here — they live in
[`../../DECISION_LOG.md`](../../DECISION_LOG.md) entry **D-0030**
(implementation status stated there honestly; the D-0029 slot remains
reserved for the app-first product frame proposed by PR #240, pending
explicit owner acceptance).

### Consequences

- Positive: docs claims resolve to tests (`truth:` markers, R22), one
  authoritative behaviour record (D-0030), one docs-enforcement record (here).
- Negative: adding another enforcement surface means updating both the law row
  and this record's Confirmation.

## Confirmation

- `tests/architecture/test_knowledge_retrieval_boundaries.py` (R22: adapter
  confinement, core-package isolation, authority imports, resolver self-tests).
- `tests/unit/test_docs_integrity.py` (index/link/H1/ADR-index agreement —
  this record is listed in `adr/README.md` and `docs/README.md`).
- `python -m nexus_ai_agent.diagnostics.truth` (law-test resolution + claim
  witnesses across the living view).

## Links

- Living view: [`../KNOWLEDGE_RETRIEVAL.md`](../KNOWLEDGE_RETRIEVAL.md)
- Behaviour record: [`../../DECISION_LOG.md`](../../DECISION_LOG.md) entry D-0030
- Board claim: `.agents/board.json` → `task-273-llamaindex-retrieval`
