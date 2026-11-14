# AETHEL scene.imaging — doctrine (Wave 3)

**Status:** EXPERIMENTAL local pack. Pure NumPy; no cloud call; no state.

## Operations (10)

| Operation | Math |
|---|---|
| `scene.insert_object_with_shadow` | rolled alpha + Gaussian penumbra (σ=4) compositing |
| `scene.remove_object_inpaint` | multiscale harmonic PDE (downsample → Laplace → upsample) |
| `scene.outpaint_canvas_expand` | mirror-pad border extension |
| `scene.synthesize_depth_of_field` | depth-driven circle-of-confusion defocus |
| `scene.add_volumetric_fog_haze` | Koschmieder scattering + Henyey-Greenstein phase |
| `scene.lens_distortion_correct` | Brown-Conrady radial undistortion |
| `scene.chromatic_aberration_control` | per-channel radial magnification |
| `scene.perspective_horizon_level` | structure-tensor horizon tilt, folds into [-90, 90) |
| `scene.sky_replacement_relit` | sky replace + warm/cool foreground gain |
| `scene.reflection_water_glass_synth` | flat mirror reflection + Fresnel + Gerstner ripples |

Kernels also expose `horizon_stabilization`, `homography_perspective_rectification`
(DLT + SVD, singular-refusing), `perlin_camera_shake` (fractal 1/f field),
`fraunhofer_lens_flare` (|FFT(aperture)|²), `wire_and_rig_removal`,
`dynamic_range_expansion`, `optical_shadow_synthesis`,
`defocus_depth_map_estimation`, `sky_replacement_harmonization`, and
`motion_vector_flow` (block matching).

## Contract

Every kernel takes float32 linear-radiance `(H, W, 3)` and validates shape,
finiteness, and masks. Malformed input raises `InvalidImageError` /
`InvalidMaskError`; a singular homography raises `DegenerateGeometryError`.

The registrar and NumPy kernels live in the composition layer
(`creative/composition/scene_ops.py`, `scene_imaging.py`) because the pack
purity gate forbids heavy imports inside `creative/packs`. Operations are
registered additively on a caller-supplied `CapabilityRegistry`; the caller
supplies an in-memory `_images` store in the operation context. No `AssetVersion` is minted and no command is dispatched.
