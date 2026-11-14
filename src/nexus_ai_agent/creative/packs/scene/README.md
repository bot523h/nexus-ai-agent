# scene.imaging pack (AETHEL Wave 3) — declarative registrar

This directory is the **declarative** half of the `nexus.scene.imaging` pack:
typed input models (`models.py`), typed errors (`errors.py`) and the additive
registrar (`operations.py`). It contains **no numpy and no I/O** — the pack
purity architecture gate (`tests/architecture/test_slideshow_adapter_boundary.py`)
forbids heavy imports inside `creative/packs`.

The NumPy kernels live in the composition layer, in this agent's own zone:
`nexus_ai_agent.creative.composition.scene_imaging`. `operations.py` imports
them there; the pack stays data plus a thin adapter.

**Integration handoff (out of this agent's zone):** to expose the pack through
`nexus packs list`/`activate`, a later integration PR must (1) add a
`pack.manifest.json` here and (2) register `scene` in
`nexus_ai_agent.creative.packs.runtime.COMPOSITION` with
`build_scene_registry`. Both edits touch shared files and are deliberately left
for the authorized integration phase. Until then the ten operations are usable
additively via `build_scene_registry()` / `register_scene_operations(registry)`.
