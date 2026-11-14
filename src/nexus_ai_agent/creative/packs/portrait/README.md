# portrait.refine pack (AETHEL Wave 4) — declarative contracts

Declarative half of the `nexus.portrait.refine` pack: typed input models
(`models.py`) and typed errors (`errors.py`). No numpy, no I/O — the pack purity
gate forbids heavy imports inside `creative/packs`.

The NumPy kernels and the registrar live in the composition layer:
`nexus_ai_agent.creative.composition.portrait_imaging` and `...portrait_ops`.

**Integration handoff:** expose via `nexus packs list`/`activate` by adding a
`pack.manifest.json` here and registering `portrait` in
`creative/packs/runtime.py` — both are shared-file edits left for the authorized
integration PR. Until then use `build_portrait_registry()` /
`register_portrait_operations(registry)`.
