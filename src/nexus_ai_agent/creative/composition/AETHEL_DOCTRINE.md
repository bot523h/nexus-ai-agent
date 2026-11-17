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

## 8. Rhythmic montage optimizer (Wave 5)

`composition/montage_optimizer.py` adds a bounded Viterbi-style dynamic program
that assigns a *duration* to each candidate clip to hit a declared beat grid and
pace target, minimising a sum of exact-`Fraction` penalties:

| Term | Meaning |
|---|---|
| beat | `beat_weight * |cut - nearest_anchor|` (0 when no beat grid is declared) |
| pace | `pace_weight * |duration - pace_target|` |
| continuity | `continuity_weight * jump_cost[i]` for every `i > 0` |

`optimize_rhythmic_montage(request) -> MontageSchedule` is deterministic; ties
break on `(cost, duration, index)`. It returns `PROVEN_OPTIMAL` only when the
complete finite search finished inside the state/transition budget, and
`FEASIBLE_NOT_PROVEN_OPTIMAL` (with a partial schedule) when the budget is hit.
`compile_montage_to_timeline_dag(schedule)` turns an optimal/bounded schedule
into a linear `v1` `TimelineDAG`.

## 9. Adversarial and mutation evidence (Wave 6)

- **61 adversarial / refusal probes** total: 20 `scene.*`, 20 `portrait.*`, and
  21 cross-module probes in
  `tests/creative/director/test_cross_module_adversarial.py` spanning
  `Fountain -> StoryDocument -> CausalEngine -> TemporalNetwork -> ShotPlanner ->
  MontageOptimizer -> TimelineDAG`. These assert typed refusals or honest
  `UNKNOWN`/`NEEDS_REVIEW`/`FAIL` statuses — never a silent PASS. They are
  **input/refusal tests, not mutation tests**.
- **A real 32-mutant code-mutation campaign** lives in
  `tests/creative/composition/test_aethel_mutation_campaign.py`. Each mutant
  injects one real source change into an isolated temp copy (never the real tree)
  and runs the targeted test in a subprocess; a mutant is KILLED only when a test
  actually fails. Current result: **32/32 killed**. The campaign also asserts
  every mutation anchor is unique in the real tree.
- The first campaign run exposed **11 weak tests** (Porter-Duff alpha-symmetry,
  the Bellman-Ford zero-weight witness, a non-zero first jump cost, the skin L*
  locus, the fog sign, the DR gain, the 30-degree threshold, the planner evidence
  `AND`, and the causal `UNKNOWN` branch). Those tests were strengthened until
  every mutant died; this is recorded honestly rather than reported as a
  first-pass 100%.

## 10. Scale, benchmarks and gates (Waves 7-8)

- **Stress E2E** (`tests/creative/episode_causality_lab/test_episodic_e2e_stress.py`):
  a 10-episode / 1000-scene / 2000-event story runs through the causal engine, the
  exact-integer temporal network, the incremental engine (checked against a full
  recompute oracle), a real-disk report export reopened and digest-verified, and
  the DAG compilers. Conflicts are injected at scene 1, 501 and 999 and found at
  every depth.
- **Benchmarks** (`tests/creative/episode_causality_lab/test_episodic_benchmarks.py`):
  N=3 wall-clock samples with a `tracemalloc` peak around the call for parse,
  causal, temporal, incremental-vs-full and export at small/medium/large sizes.
  These are samples, not production throughput or tail-latency claims.
- **Architecture gates** (`tests/creative/composition/test_aethel_architecture_gates.py`):
  AST-based, so prose that merely mentions `eval` cannot false-positive. They
  assert (1) determinism, (2) no heavy/optional imports (`scipy`, `cv2`, `torch`,
  `moviepy`, `PIL`, `requests`, `httpx`, `urllib`, `socket`, `subprocess`,
  `ctypes`, `pickle`, cloud-AI SDKs, `telegram`, `sqlalchemy`) in the reasoning
  cores, (3) no `eval`/`exec`/`__import__`/`compile`/`.cwd()`, (4) no
  re-defined shared authority (`CommandBus`, `AssetVersion`, `Passport`,
  `Revision`), and (5) bounded parsers.

## 11. Forensic self-audit (Wave 8)

| # | Audit item | Evidence |
|---|---|---|
| 1 | No duplicated engine/DB/passport/audio/Chronos | `test_aethel_architecture_gates.py::test_no_shared_authority_is_redefined`; no Alpha/Beta path touched |
| 2 | No missing input becomes PASS | 61 adversarial probes + causal `UNKNOWN`/`NEEDS_REVIEW` tests |
| 3 | Temporal math correct | `test_temporal.py` oracle + `test_temporal_edge_directions_are_documented` |
| 4 | Causal logic honest | `test_causal.py` closed/open-world, flashback, unknown-state tests |
| 5 | Optimization claims honest | `test_montage_optimizer.py` budget + `test_plan.py` resource-limit tests |
| 6 | Incremental == full | `test_incremental.py` + `test_episodic_e2e_stress.py` 1000-scene oracle |
| 7 | Security (bounded, no unsafe deserialization) | `test_aethel_architecture_gates.py` AST gates; `loads_story` stdlib-`json` only |
| 8 | No bypass of policy/CommandBus/staging | handoff is inert (`build_handoff` sets `authority: advisory`); export uses `WorkspaceFilesystem`/`AttemptStaging` |
| 9 | Artifacts reopened and verified | `export_report` re-reads and digest-checks; `test_episodic_e2e_stress.py` |
| 10 | Docs match code | this document; `tests/creative/**` |

## 12. Wave 9 — radiative-transfer relighting (transcendental computational cinematography)

Wave 9 adds `creative/composition/radiative_relighting.py`: an order-2
**spherical-harmonics** lighting solver, a **Frankot–Chellappa** integrable
normal-field reconstructor, and a **Cook–Torrance** GGX specular term, all in
pure NumPy over scene-linear float radiance. It exposes two pack operations
(`scene.harmonize_radiative_transfer`, `portrait.relight_spherical_harmonics`)
that are *additive* to the ten-operation catalogue and do not change it.

### 12.1 Spherical-harmonics irradiance

Real SH basis up to degree 2 (`Y_00..Y_22`), orthonormal on the sphere:

```
Y00  = 0.2820947917738781            A0 = pi
Y1x  = 0.4886025119029199 * {y, z, x}  A1 = 2*pi/3
Y2x  = {1.0925484305920792, 0.3153915652525200, 0.5462742152960396} (mixed)  A2 = pi/4
```

Lambertian irradiance is `E_c(n) = sum_lm A_l L_lm Y_lm(n)`. The transfer
weights `(pi, 2 pi/3, pi/4)` are the *exact* cosine-lobe convolution coefficients,
so a constant radiance `c` gives `E = pi c` (verified to `2e-3` by an
equirectangular quadrature whose solid-angle weights are normalised to exactly
`4 pi`). The equivalent 4x4 per-band matrix `M_c` satisfies
`n~^T M_c n~ == E_c` to `< 1e-9`. Key-light direction is the normalised `l = 1`
dipole; a zero dipole (ambient-only) returns `(0, 0, 1)`.

### 12.2 Integrable normal-field reconstruction (Frankot–Chellappa)

Given gradients `(p, q)`, the surface is reconstructed in the Fourier domain,
projecting onto the integrable subspace; the residual discrete curl
`d(dz/dx)/dy - d(dz/dy)/dx` is `< 1e-10` on an integrable field. The resulting
normal map is unit length by construction. A non-integrable input is projected,
not rejected (the projection is real and finite).

### 12.3 Relighting

`relight_subject_to_environment` performs an intrinsic split
(`shading = luminance`, `albedo = subject / shading`, so `albedo * shading ==
subject` exactly), scales by the target/source irradiance ratio, adds an optional
Cook–Torrance GGX highlight driven by the target key-light direction, and blends
through the mask. An identical probe with `specular_strength = 0` is a numerical
identity. All inputs are bounded (`MAX_RELIGHT_PIXELS = 4 194 304`) and validated;
NaN/Inf, non-unit normals, degenerate masks, out-of-range roughness/specular and
singular normal-fit systems all raise typed errors.

### 12.4 Evidence

- `tests/creative/composition/test_radiative_relighting.py`: 47 real-computation
  and adversarial tests (Monte-Carlo orthonormality, closed-form ambient
  irradiance, matrix-vs-sum equivalence, discrete-curl integrability, refusal
  cases).
- Registry tests in `tests/creative/packs/{scene,portrait}/`.
- Coverage: `radiative_relighting.py`, `scene_ops.py`, `portrait_ops.py` at
  **100% line + branch** (measured with the `coverage` CLI under `--timid`, since
  the in-process `pytest-cov` tracer cannot reload NumPy in this environment).

### 12.5 Scope limits (honest)

- The SH fit is a **Tikhonov-regularised least squares** over the supplied
  normals, not a physically-unique decomposition; albedo and illumination are
  not separable in general.
- The relighting result is an *editorial* lighting match, not a calibrated
  radiometric measurement; no GUM uncertainty budget is claimed for it.
- No scipy/cv2/torch/cloud-AI dependency is used; everything is NumPy + stdlib.


---

## 13. Wave 10 — Optical-flow computational cinematography

Module: `src/nexus_ai_agent/creative/composition/computational_cinematography.py`
(import alias `cine` / `cc`). Deterministic, offline, NumPy + stdlib only; no
network, no cloud AI, no project state. Registry operation:
`scene.retime_optical_flow` (additive, outside the ten catalogue operations, same
pattern as Wave 9's `scene.harmonize_radiative_transfer`).

### 13.1 Sign convention

`flow[p]` is the displacement that carries the point at `p` in `img_a` to its
position in `img_b`, i.e. `a(p) ~ b(p + flow(p))`. `warp_by_flow(img, flow)`
samples `img(x + flow_x, y + flow_y)`, so aligning `b` onto `a` is
`warp_by_flow(b, flow_ab)` and aligning `a` onto `b` is `warp_by_flow(a, -flow_ab)`.

### 13.2 Coarse-to-fine Lucas–Kanade

At each pyramid level the windowed structure tensor `G` and residual
`[Σ Ix It, Σ Iy It]` give the per-pixel increment
`df = -(G + ridge·I)^{-1} [Σ Ix It, Σ Iy It]` with
`ridge = 0.05 · trace(G)/2`. Two robustness devices keep the estimate stable:
a relative Levenberg–Marquardt ridge so a near-flat window is damped rather than
amplified, and a per-increment magnitude cap of one pixel so a bad linearisation
cannot overshoot. A 3×3 median filter after each level removes isolated outliers.
The estimator is deterministic and local; it is **not** a global-optimality claim.
Pyramid depth is bounded so each level is at least 16 px on its short side.

### 13.3 Horn–Schunck and consistency

`horn_schunck_flow` is the classic global-smoothness iterative scheme
(`u = ū − Ix·(Ix·ū + Iy·v̄ + It)/(α² + Ix² + Iy²)`, symmetric for `v`). It is
included as an independent estimator; Lucas–Kanade is the higher-accuracy path.

`forward_backward_consistency(flow_ab, flow_ba, threshold)` marks a pixel
consistent when the forward–backward endpoint error `|d_ab(p) + d_ba(p + d_ab)|`
is `<= threshold`. An occlusion (a hole in the backward flow) is marked
inconsistent; a constant, agreeing field is fully consistent.

### 13.4 Flow-based frame interpolation

`interpolate_frame(a, b, t)` warps `a` backwards along `-t·flow_ab` and `b`
forwards along `-(1−t)·flow_ba`, then blends with occlusion-aware weights
(`(1−t)·consistent_ab`, `t·consistent_ba`), falling back to a linear mix where
neither direction is consistent, and clips to `[0, 1]`. On a synthetic
translation the interpolated frame matches the analytic ground truth to
`MAE < 0.02`.

### 13.5 Bounds and refusals

`MAX_FLOW_PIXELS = 4 194 304` (2048×2048) bounds every entry point; NaN/Inf,
wrong channel count, shape mismatch, non-odd window `< 3`, `iters < 1`,
`levels < 1`, `alpha <= 0`, non-positive consistency threshold and `t` outside
`[0, 1]` all raise `FlowError`.

### 13.6 Evidence

- `tests/creative/composition/test_computational_cinematography.py`: 42
  real-computation and adversarial tests (analytic-translation recovery,
  ramp/texture single-level exactness, warp alignment, FBC occlusion, EPE
  closed-form value, endpoint exactness, bounded output, every refusal path).
- `tests/creative/packs/scene/test_scene_registry.py`: Wave-10 registry tests.
- Coverage: `computational_cinematography.py` at **100% line + branch**
  (measured with the `coverage` CLI, since the in-process `pytest-cov` tracer
  cannot reload NumPy in this environment).

### 13.7 Scope limits (honest)

- The flow field is a local, brightness-constancy estimator: large motion,
  occlusion, specular highlights and brightness changes degrade it.
- The retimed frame is an *editorial* interpolation, not a recovered physical
  sample; no radiometric or GUM uncertainty budget is claimed.
- No learned optical-flow model, no scipy/cv2/torch, no cloud service is used.

---

## 14. Wave 11 — HDR tone mapping and colour science

Module: `src/nexus_ai_agent/creative/composition/hdr_tonemapping.py` (import alias
`hdr`). Registry operation: `scene.tonemap_hdr` (additive, outside the ten
catalogue operations). Pure NumPy + stdlib, offline and deterministic.

### 14.1 Tone curves

Scene-linear radiance may exceed 1.0; four monotone, `f(0)=0`, `[0,1]`-bounded
curves map it to display range, all with a shared `exposure` scale `x = e·L`:

- **Extended Reinhard** `L_d = x (1 + x/white²) / (1 + x)`; `white` is the input
  that maps to 1.0, `white = 1` gives plain Reinhard.
- **ACES (Narkowicz 2015 fit)** `(2.51 x² + 0.03 x) / (2.43 x² + 0.59 x + 0.14)`.
- **Hejl–Burgess** on `max(0, x − 0.004)`: `(6.2 x² + 0.5 x) / (6.2 x² + 1.7 x + 0.06)`.
- **Uncharted 2 (Hable)** `f(x)/f(11.2)` with the published constants.

### 14.2 Exposure and transfer

`auto_exposure_scale` is Reinhard's log-average key:
`key / exp(mean(log(Y + ε)))`, with `ε` a floor so a black frame stays finite.
`gamma_encode`/`gamma_decode` are the pure power law `x^(1/γ)` / `x^γ`; the exact
IEC 61966-2-1 sRGB pair already lives in `compositor_2d` and is not duplicated.

### 14.3 White balance

`kelvin_to_xy` is the Kim et al. (2002) cubic fit of the Planckian locus, valid
over 1667–25000 K (outside that range it refuses rather than extrapolating).
`bradford_adapt` converts linear-sRGB → XYZ, applies the Bradford cone-response
adaptation `diag(cone_dst/cone_src)` between the source and destination white
points, and converts back; `src == dst` is a numerical no-op. A source white on
the Bradford cone null raises a typed error instead of dividing by ~0.

### 14.4 Bounds and refusals

`MAX_TONEMAP_PIXELS = 4 194 304`; NaN/Inf, negative radiance, wrong channel
count, empty input, over-budget size, non-positive exposure/`white`/`gamma`/`key`/
`epsilon`, unknown temperature out of range, and a degenerate adaptation white
all raise `TonemapError` (with `DegenerateToneCurveError` as a subclass).

### 14.5 Evidence

- `tests/creative/composition/test_hdr_tonemapping.py`: closed-form curve values,
  monotonicity/boundedness, exposure key identity, Kelvin↔xy range and direction,
  Bradford identity and warm-shift, sRGB↔XYZ round-trip, vignette identity and
  corner darkening, and every refusal path.
- `tests/creative/packs/scene/test_scene_registry.py`: Wave-11 registry tests.
- Coverage: `hdr_tonemapping.py` at **100% line + branch** (via the `coverage` CLI).

### 14.6 Scope limits (honest)

- These are *editorial* tone and white-balance operators, not a calibrated
  display transform; no GUM uncertainty budget or reference-monitor claim.
- No ACES CTL / OCIO, no scipy/cv2/torch, no cloud service is used.

---

## 15. Wave 12 — photochemical film emulation and objective metrics

Module: `src/nexus_ai_agent/creative/composition/film_emulation.py` (import
alias `film`). Registry operation: `scene.film_emulation` (additive, outside the
ten catalogue operations). Pure NumPy + stdlib, offline, seeded and bounded.

### 15.1 Halation

Light that passes the emulsion scatters off the anti-halation backing and
re-exposes the layer; red light penetrates furthest, so the glow is a
large-radius, red-weighted bleed. Pixels above `threshold` are isolated,
Gaussian-blurred (separable, edge-clamped) and added back with the red channel
scaled by `red_bias`. `strength = 0` is an exact identity.

### 15.2 Film grain

Luminance-dependent, seeded Gaussian grain. The weight is a triangular mid-tone
function `1 − |2·clamp(Y) − 1|` (zero at pure black and white) and the RMS is
`sigma / sqrt(area)`, so a larger `area` gives finer grain. Identical
`(seed, shape)` always produce identical grain; `sigma = 0` is an identity.

### 15.3 Gate weave

Deterministic sub-pixel projector-gate jitter: a smooth low-frequency drift
(`sin(2π f/23)`, `sin(2π f/31 + 1)`) plus a small seeded high-frequency term,
resampled bilinearly with edge clamp. `amplitude = 0` is an exact identity.

### 15.4 Objective metrics

- **PSNR** `10 log10(peak² / MSE)`, `inf` for identical inputs.
- **SSIM** (Wang et al. 2004) on the Rec.709 luma with a Gaussian window,
  `C1 = (0.01·peak)²`, `C2 = (0.03·peak)²`; the mean of the local SSIM map.
  Identical inputs give exactly `1.0`. These metrics are the honest verification
  for the other Waves' operators (they quantify signal destruction, they do not
  certify artistic quality).

### 15.5 Bounds and refusals

`MAX_FILM_PIXELS = 4 194 304`; NaN/Inf, negative radiance, wrong channel count,
empty input, over-budget size, non-positive `sigma`/`truncate`/`peak`, negative
`strength`/`seed`/`frame_index`/`amplitude`, `area < 1` and mismatched shapes
all raise `FilmEmulationError`.

### 15.6 Evidence

- `tests/creative/composition/test_film_emulation.py`: determinism, identity
  cases, the `1/sqrt(area)` grain law, mid-tone grain weighting, halation
  red-dominance, the closed-form PSNR value, SSIM identity/monotonicity/symmetry,
  and every refusal path.
- `tests/creative/packs/scene/test_scene_registry.py`: Wave-12 registry tests.
- Coverage: `film_emulation.py` at **100% line + branch** (via the `coverage` CLI).

### 15.7 Scope limits (honest)

- These are *editorial* artefacts, not a physical emulsion simulation; no
  density-curve or granularity (Selwyn) model and no GUM budget are claimed.
- PSNR/SSIM are full-reference signal metrics; they do not measure perceptual or
  artistic quality.
- No OpenCV/scipy/torch, no film-scan LUT, no cloud service is used.
