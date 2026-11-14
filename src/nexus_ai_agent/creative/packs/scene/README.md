# scene.imaging pack (AETHEL Wave 3) — declarative registrar

This directory is the **declarative** half of the `nexus.scene.imaging` pack:
typed input models (`models.py`), typed errors (`errors.py`) and the additive
registrar (`operations.py`). It contains **no numpy and no I/O** — the pack
purity architecture gate (`tests/architecture/test_slideshow_adapter_boundary.py`)
forbids heavy imports inside `creative/packs`.

The NumPy kernels live in the composition layer, in this agent's own zone:
`nexus_ai_agent.creative.composition.scene_imaging`. `operations.py` imports
them there; the pack stays data plus a thin adapter.

**Integrated:** `pack.manifest.json` is on disk and `scene` is registered in
`creative/packs/runtime.py` (after `portrait`). `operations.py` is the
declarative registrar: it declares `SCENE_SPECS` (the operation table the
composition kernels import) and registers fail-closed delegates that dispatch to
an injected `context.scene_kernel` — the audio-pack engine pattern. The native
implementation (`NativeSceneKernel`) lives in `creative/composition/scene_ops.py`;
without it, every `scene.*` operation refuses honestly (code
`SCENE_KERNEL_REQUIRED`). The declared `OPERATION_*` symbols are re-exported
from `models.py` (single source of truth).
