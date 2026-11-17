# portrait.refine pack (AETHEL Wave 4) — declarative contracts

Declarative half of the `nexus.portrait.refine` pack: typed input models
(`models.py`) and typed errors (`errors.py`). No numpy, no I/O — the pack purity
gate forbids heavy imports inside `creative/packs`.

The NumPy kernels and the registrar live in the composition layer:
`nexus_ai_agent.creative.composition.portrait_imaging` and `...portrait_ops`.

**Integrated:** `pack.manifest.json` is on disk and `portrait` is registered in
`creative/packs/runtime.py` (after `nexus.color.delivery`). `operations.py` is
the declarative registrar: it declares `PORTRAIT_SPECS` (the operation table the
composition kernels import) and registers fail-closed delegates that dispatch to
an injected `context.portrait_kernel` — the audio-pack engine pattern. The
native implementation (`NativePortraitKernel`) lives in `creative/composition/
portrait_ops.py`; without it, every `portrait.*` operation refuses honestly
(code `PORTRAIT_KERNEL_REQUIRED`). The declared `OPERATION_*` symbols are
re-exported from `models.py` (single source of truth).
