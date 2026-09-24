"""Nagar operation truth — the executable evidence gate (Gate 2.2).

This package owns **one** job: to answer, mechanically and reproducibly, the
question *"what does Nagar actually have?"* — and to make that answer
impossible to change without CI noticing.

The rule this package exists to enforce (ADR
``docs/architecture/adr/0005-three-layer-operation-truth.md``):

    Product Definition → Runtime Registration → Executable Surface
      → Runtime Proof → Artifact / Result Evidence

Each arrow is a **separate measurement**.  ``registered`` is never inferred
from ``defined``; ``surface_reachable`` is never inferred from ``registered``;
``runtime_proven`` is never inferred from ``surface_reachable``.  A document
saying so is not evidence.

Three sources are allowed (see :mod:`nexus_ai_agent.nagar.sources`):

1. the **Product Catalog** — the seven pack tables of
   ``docs/NAGAR_70_OPERATIONS_TDD.md``;
2. the **Live Runtime Registry** — ``build_runtime_registry()``;
3. the **Live Executable Surface** — the surface mapper's allow-list, the
   worker's closed dispatch map, and the entrypoints that reach them.

Everything else — the matrix, the reconciliation, the maturity table, the
documentation — is a *projection*: derived from those three and never
consulted as an authority.

Design notes (why this is small and dependency-free):

* stdlib only, plus imports of the repository's own packages.  No in-toto, no
  SLSA, no import-linter, no new runtime dependency — the *concepts* of those
  frameworks (link metadata, provenance subject digest, fitness functions) are
  used, not their packages.
* The projection is written by ``python -m nexus_ai_agent.nagar`` and carries a
  generation header so a reader can tell what was measured, when, from which
  revision, and which fields are non-deterministic.
* The gate never upgrades evidence.  A missing measurement is recorded as
  ``NOT_MEASURED`` / ``NOT_AVAILABLE`` and stays that way.
"""

from __future__ import annotations

__all__ = ["sources", "truth"]
