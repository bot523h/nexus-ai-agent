# NAGAR OLYMPUS — Computational-Cinematography Doctrine (Waves 21-30)

Status: **living** (updated with the code in the same commit).
Owner zone: `src/nexus_ai_agent/creative/atlas/**`, `tests/creative/atlas/**`.
Authority: **advisory**. OLYMPUS owns no project state, mints no passport,
dispatches no `CommandBus` command, and claims no registered production
capability.

> Placement note: this doctrine lives inside the OLYMPUS source zone rather than
> `docs/architecture/` because the shared documentation index (`docs/README.md`)
> is off-limits in this mission. The handoff to move it under `docs/architecture/`
> and index it there is recorded in §8.

---

## 1. Scope and honesty contract

OLYMPUS is an offline, deterministic, bounded set of computational-cinematography
operators plus one real media boundary. It is **not** an inference engine for
biometric identity, **not** an autonomous film generator, and **not** a media
quality certificate.

| Status token | Meaning in OLYMPUS |
|---|---|
| `COMPUTED` | The operator ran and (where it wrote bytes) the artifact was re-opened and verified. |
| `BLOCKED` | A required external binary/dependency is absent; nothing was faked. |
| `SKIPPED` | No bounded operator fixture exists for this pass; nothing was faked. |
| `PROVEN_OPTIMAL` | The complete applicable finite search ran (declared problem only). |
| `UNKNOWN` / `NEEDS_REVIEW` | The input does not determine the answer. |

Absence of data never becomes `PASS`. A caller-supplied `verified=True` without
a non-empty trusted evidence reference is treated as unverified. `ru_maxrss` is a
process high-water mark, not per-operation memory.

Every module in this wave set raises a module-specific `ValueError` subclass for
malformed input, bounds all sizes and work, uses seeded generators for any
randomness, and never uses `eval`, dynamic code execution, or ambient
`Path.cwd()` defaults.

---

## 2. Wave 21 — analytical SDF global illumination (`sdf_global_illumination.py`)

* Exact primitives with analytic Euclidean distance: sphere `|p−c|−r`, box
  (Quilez closed form `|max(q,0)| + min(max(q),0)` with `q=|p−c|−h`), plane
  half-space `n·p−d`, torus.
* CSG: smooth (polynomial) union/intersection via `smin(a,b,k)`; ray marching with
  a sphere-trace step.
* Cone-traced ambient occlusion (Quilez): `AO = 1 − occ`, sampling along the
  normal with `falloff^i` weighting; `AO ∈ [0,1]`, `1` = fully open. A concave
  two-sphere union yields `AO < 1` (measured 0.892).
* Soft shadows via the standard `h = min(h, k·d/t)` smooth-step formulation.

## 3. Wave 22 — spectral ocean / shallow-water equations (`spectral_ocean_swe.py`)

* Deep-water dispersion `ω = √(g k)`.
* JONSWAP directional spectrum over `(kx, ky)` with a Pierson-Moskowitz base
  `S(k) ∝ α/k⁴ · exp(−1.25 (k_p/k)²)`, peak `k_p = g/(0.8772² U²)`, `cos²`
  directional spread, and the PM normalisation `H_s = 0.21 U²/g`. Verified:
  `H_s = 2.143 m` at `U = 10 m/s` (analytic 2.143).
* 1-D/2-D shallow-water equations with a Rusanov (local Lax-Friedrichs) flux and
  reflective / zero-gradient boundaries that preserve grid shape and interior
  mass.

## 4. Wave 23 — buoyant pyro / blackbody (`buoyant_pyro_blackbody.py`)

* Exact blackbody physics: Planck spectral radiance
  `B_λ = 2hc²/λ⁵ · 1/(exp(hc/λkT)−1)`, Wien displacement `λ_max T = b`,
  Stefan-Boltzmann flux `σT⁴`, and an sRGB chromaticity conversion.
* Pyro CFD with semi-Lagrangian advection and a Jacobi pressure projection using
  a periodic forward-difference / backward-gradient pair (converges; residual
  ~7.5e-05).

## 5. Wave 24 — PGS-LCP rigid multi-contact (`rigid_contact_pgs_lcp.py`)

* Projected Gauss-Seidel linear complementarity solver for normal + Coulomb
  friction contacts, with a Baumgarte bias term. Effective mass
  `k = 1/(m⁻¹ + (r×n)ᵀI⁻¹(r×n))`. Bounce verified `vy 5 → −4.0` for `e = 0.8`.

## 6. Wave 25 — botanical modal wind (`botanical_modal_wind.py`)

* Euler-Bernoulli cantilever eigenmodes: characteristic roots `β_n L`
  (1.8751, 4.6941, 7.8548, …), `ω_n = (β_n L)²√(EI/(μL⁴))`; verified
  `ω₁ = 10.5061` and `ω₂/ω₁ = 6.2669` (analytic 6.2673).
* Mode shapes normalised to unit tip value; static tip deflection
  `F L³/(3EI)` reproduced.
* A Davenport-like gust field (`f^(−5/3)` slope, seeded, target turbulence
  intensity) and aerodynamic drag `½ ρ C_d A U²`. Two stable integrators:
  substepped symplectic Euler and a closed-form exponential integrator for the
  damped oscillator under constant modal force; a divergence guard flags
  unphysical blow-up instead of returning it.

## 7. Wave 26 — ACES / HALD film optic (`aces_hald_film_optic.py`)

* Published ACES colour matrices: AP0→XYZ(D60), XYZ→AP1, their composition and
  exact inverse. Verified white-point preservation (row sums = 1) and round-trip
  identity.
* Narkowicz ACES tone-map fit `(x(ax+b))/(x(cx+d)+e)`: monotonic, `f(0)=0`,
  bounded `[0,1]`, `f(1) ≈ 0.8038`.
* Identity HALD CLUTs with trilinear interpolation (identity reproduces the image
  to `2.2e-16`), red-biased halation convolution, seeded band-limited film grain.

## 8. Wave 27 — diegetic modal foley (`diegetic_modal_foley.py`)

* Modal impact synthesis: `Σ_n A_n exp(−t/τ_n) sin(2π f_n t + φ_n)` with
  published free-free-bar / plate mode ratios per material and per-mode damping.
* Diegetic placement ("waveweide"): events delayed by `d / c_air`
  (`c = 343 m/s`); events outside the timeline are honestly *not placed*.
* Exponential-decay reverb IR (unit energy, −60 dB at `rt60`) and full
  convolution.

## 9. Wave 28 — multi-episode showrunner bible (`showrunner_bible.py`)

* Characters with per-episode arcs, uniquely-owned props, episode-ordered facts
  with provenance.
* Three-valued `Truth` (`TRUE`/`FALSE`/`UNKNOWN`): an undeclared fact is
  `UNKNOWN`, never a fabricated `FALSE`.
* Consistency engine: `dead_receives_prop`, `dead_declares_fact`, `double_death`,
  `duplicate_fact`, `redundant_transfer`.

## 10. Wave 29 — real FFmpeg media sine (`media_sine.py`)

* The OLYMPUS zone's **single** external-binary boundary. `ffmpeg` is invoked
  with an argument list (never `shell=True`), a bounded timeout, and paths that
  resolve inside a caller-supplied `WorkspaceFilesystem` root.
* The synthesised H.264/mpeg4 test-pattern clip is re-opened with `ffprobe`
  (`-count_frames`) and its codec/width/height/frame-count/pix_fmt are asserted
  against the request.
* A missing binary raises `MediaSineUnavailable` (→ `BLOCKED`), never a fake
  success.

## 11. Wave 30 — Olympus master render (`olympus_master.py`)

The integration wave. `render_master` wires continuity analysis, SDF ambient
occlusion, the JONSWAP ocean spectrum, blackbody radiance, diegetic modal foley
and ACES grading into one deterministic pipeline, writes a real 16-bit mono WAV
through `WorkspaceFilesystem`, encodes a real H.264 clip with FFmpeg and
re-probes it, and persists a canonical, digest-protected ledger. Each stage
carries an explicit `COMPUTED`/`BLOCKED` status; FFmpeg absence yields `BLOCKED`
while the CPU-only stages still run and are reported honestly.

### 11.1 Integration handoff

The only out-of-zone integration points needed for a later authorised PR are:

1. move this doctrine to `docs/architecture/NAGAR_OLYMPUS_DOCTRINE.md` and index
   it in `docs/README.md`;
2. register the master-render operators in the canonical operation catalogue
   (`OPERATION_TRUTH.json`) — **not done here**, OLYMPUS claims no registered
   capability;
3. optionally route `render_master` through the existing `CommandBus` / policy
   flow so a master render is an authorised command rather than a library call.

---

## 12. Verification summary (see the mission final report for commands)

| Wave | Module | Tests | Coverage |
|---|---|---|---|
| 21 | `sdf_global_illumination.py` | 37 | 100% |
| 22 | `spectral_ocean_swe.py` | 52 | 100% |
| 23 | `buoyant_pyro_blackbody.py` | 46 | 100% |
| 24 | `rigid_contact_pgs_lcp.py` | 31 | 99% |
| 25 | `botanical_modal_wind.py` | 53 | 99% |
| 26 | `aces_hald_film_optic.py` | 46 | 100% |
| 27 | `diegetic_modal_foley.py` | 48 | 100% |
| 28 | `showrunner_bible.py` | 36 | 100% |
| 29 | `media_sine.py` | 21 | 100% |
| 30 | `olympus_master.py` | 16 | 100% |

Full `tests/creative/atlas` suite: **701 passed**, 99% aggregate branch coverage.
Repository architecture suite: **367 passed**.
