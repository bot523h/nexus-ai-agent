# NAGAR AETHEL - episodic composition & director doctrine (Waves 1-4)

**Status:** EXPERIMENTAL, local, deterministic. No cloud API. No state ownership.
This document lives inside the AETHEL author's zone; it is intentionally not a
`docs/` page because the repository's documentation-index rule
(`tests/unit/test_docs_integrity.py`) would require an edit to the shared
`docs/README.md`, which is outside this agent's zone.

## 1. Scope and boundaries

AETHEL adds four deterministic subsystems on top of the NAGAR creative core:

| Wave | Package | Purpose |
|---|---|---|
| 1 | `creative/director/` | Fountain -> typed AST -> ATLAS `StoryDocument`; 180/30-degree/eyeline shot planner |
| 2 | `creative/composition/` | exact rational time, TimelineDAG, Bezier time warp, J/L cuts, Porter-Duff |
| 3 | `creative/packs/scene/` + `composition/scene_imaging.py` | ten `scene.*` catalogue image operations |
| 4 | `creative/packs/portrait/` + `composition/portrait_imaging.py` | ten `portrait.*` catalogue image operations |

AETHEL is a proposal and analysis layer. It never mints an `AssetVersion`, a
passport, or a project database, never dispatches a `CommandBus` command, and
never owns project state.

## 2. Pack purity (why kernels are not inside packs/)

Two architecture gates constrain the layout:

1. `tests/architecture/test_slideshow_adapter_boundary.py` - `creative/packs/**`
   and `creative/studio/**` may not import `numpy`, `PIL`, `os`, `socket`,
   `subprocess`, `wave`, `httpx` (heavy imports belong behind an adapter).
2. `tests/architecture/test_pack_manifest_is_data_only.py::
   test_pack_substrate_does_not_cross_package_boundaries` - a pack may only
   import `creative.packs`, `creative.studio`, `creative.temporal`.

Therefore the NumPy kernels and the registrars live in the composition zone
(`composition/{scene,portrait}_{imaging,ops}.py`) and each pack directory holds
only pydantic input models and typed errors.

## 3. Temporal algebra (Wave 2)

`RationalTime(value, time_base=Fraction)` (OTIO convention) with exact
`TimeRange` arithmetic and `common_rate` conversion between 23.976, 24, 25,
29.97, 30, 59.94 and the audio rates 48000/96000. `TimelineDAG` gives a Kahn
topological sort that reports the offending cycle nodes. `TimeWarp` is a
monotone cubic-Bezier curve inverted with Newton-Raphson plus a bisection
fallback (tolerance `WARP_TOLERANCE`). `compositor_2d` implements the twelve
Porter-Duff operators in premultiplied linear-radiance float, with the
`PLUS`/`MULTIPLY`/`SCREEN` additions.

## 4. Scene-imaging kernels (Wave 3)

Ten catalogue operations plus supporting primitives:

- Geometry: `homography_perspective_rectification` (8-point DLT + SVD,
  singular-refusing), `horizon_stabilization` / `perspective_horizon_level`
  (structure tensor; the gradient is perpendicular to the horizon; tilt folds
  into [-90, 90) and above 45 degrees is refused), `lens_distortion_correct`
  (Brown-Conrady), `chromatic_aberration_control` (per-channel radial scale).
- Lighting/physics: `add_volumetric_fog_haze` (Koschmieder transmittance +
  Henyey-Greenstein phase), `fraunhofer_lens_flare` (FFT of the aperture),
  `dynamic_range_expansion`, `optical_shadow_synthesis`,
  `reflection_water_glass_synth` (Fresnel-weighted mirror).
- Inpainting/restoration: `remove_object_inpaint` and
  `wire_and_rig_removal` (multiscale harmonic PDE), `outpaint_canvas_expand`,
  `defocus_depth_map_estimation`, `sky_replacement_harmonization`.
- Motion: `perlin_camera_shake` (fractal 1/f field),
  `motion_vector_flow` (forward block matching).

## 5. Portrait-imaging kernels (Wave 4)

- `gaze_redirection`, `eyeblink_synthesis` (sin-squared ease over a 150 ms
  window), `facial_relighting_gradient` (Lambertian shading from a gradient
  normal).
- `skintone_harmonization` - CIE L*a*b* (D65) with the literature skin locus
  `L* in [30, 85]`, `a* in [8, 25]`, `b* in [10, 30]`, blended at the requested
  strength.
- `micro_expression_morph` - clamped thin-plate-spline warp; the unclipped
  sampling map is checked for a positive Jacobian determinant and a fold-over
  raises `DegenerateWarpError`.
- `teeth_whitening`, `facial_hair_continuity` (Gabor stubble), `head_repose_3d`
  (weak-perspective cylindrical, +/-15 degrees), `depth_of_field_bokeh`
  (polygonal aperture), `aging_deaging_geometry` (nasolabial-fold/jaw shading).

## 6. Uncertainty and honesty

- A kernel refuses malformed input with a typed error
  (`InvalidImageError`/`InvalidMaskError`/`InvalidRegionError`/
  `DegenerateGeometryError`/`DegenerateWarpError`); it never returns a
  fabricated success.
- The kernels are deterministic: identical input yields identical output
  (asserted).
- Benchmarks are N=3 wall-clock samples with a `tracemalloc` peak on the call;
  `ru_maxrss` is not used because it is a whole-process high-water mark.
- Adversarial tests check refusals; they are NOT code-mutation tests, and no
  mutation campaign is claimed here.

## 7. Integration handoff (out of this agent's zone)

To expose the two packs through `nexus packs list` / `activate`:

1. add `src/nexus_ai_agent/creative/packs/scene/pack.manifest.json` and
   `.../portrait/pack.manifest.json`; and
2. register `scene` -> `build_scene_registry` and `portrait` ->
   `build_portrait_registry` in `nexus_ai_agent.creative.packs.runtime.COMPOSITION`.

Both are shared-file edits and are deliberately left for the authorized
integration PR. Until then the twenty operations are usable additively via
`build_scene_registry()` / `build_portrait_registry()` /
`register_*_operations(registry)`.
