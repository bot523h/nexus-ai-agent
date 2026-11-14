# AETHEL portrait.refine — doctrine (Wave 4)

**Status:** EXPERIMENTAL local pack. Pure NumPy; no cloud call; no state.

## Operations (10)

| Operation | Math |
|---|---|
| `portrait.gaze_redirection` | iris-disc shift bounded by the eye mask |
| `portrait.eyeblink_synthesis` | sin² ease closure over a 150 ms window |
| `portrait.facial_relighting_gradient` | Lambertian shading from a gradient normal |
| `portrait.skintone_harmonization` | CIE L*a*b* skin-locus clip + palette bias |
| `portrait.micro_expression_morph` | clamped thin-plate-spline warp with a Jacobian fold guard |
| `portrait.teeth_whitening` | adaptive desaturation inside the teeth mask |
| `portrait.facial_hair_continuity` | Gabor-filtered stubble at a fixed density |
| `portrait.head_repose_3d` | weak-perspective cylindrical re-pose (±15°) |
| `portrait.depth_of_field_bokeh` | depth-driven polygonal-aperture bokeh |
| `portrait.aging_deaging_geometry` | nasolabial-fold / jaw-line metric displacement |

## Contract

sRGB/linear `[0, 1]` float32 `(H, W, 3)`. Malformed input raises
`InvalidImageError`; a bad mask or out-of-range parameter raises
`InvalidRegionError`; a non-invertible warp raises `DegenerateWarpError`. The
skin-locus constants are the classic literature bounds
(`L* ∈ [30, 85], a* ∈ [8, 25], b* ∈ [10, 30]`).

The registrar and kernels live in the composition layer because the pack purity
gate forbids heavy imports inside `creative/packs`. Operations are registered
additively on a caller-supplied `CapabilityRegistry`; the caller supplies an
in-memory `_images` store. No `AssetVersion` is minted and no command is
dispatched.
