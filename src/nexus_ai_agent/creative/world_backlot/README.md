# NAGAR World Backlot — phase 3

پکیج مستقلِ مولد شهرک آرشیو جهان؛ بدون اتصال به بات، سرویس ذخیره‌سازی،
مدل زبانی، رندرر خارجی یا شبکه. تمام داده‌ها در لحظه در حافظه ساخته می‌شوند.
تنها وابستگی زمان اجرا **NumPy** است که از قبل در وابستگی‌های مخزن وجود دارد.
این پکیج عمداً به مسیر اجرایی برنامه سیم‌کشی نشده است؛ API پایتون زیر نقطهٔ ورود آن است.

## Quick start

```python
from nexus_ai_agent.creative.world_backlot import (
    CanonicalWorldBacklotCatalog,
    Observer,
    RecipeQuality,
    resolve_director_scene_recipe,
)

catalog = CanonicalWorldBacklotCatalog()
assert len(catalog.archives) == 12
assert len(catalog) == 122_880
matches = catalog.search("کاروانسرا", archive="architecture", limit=3)
assert matches == catalog.search("caravanserai", archive="architecture", limit=3)
assert catalog.get(matches[0].entry_id).sha256 == matches[0].sha256

recipe = resolve_director_scene_recipe(
    "نیمه‌شب کویری ایران با آسمان پرستاره، باد و گردوغبار ملایم، "
    "معماری کاروانسرای سنتی و نور آبی مهتاب",
    seed="episode-01-location-03",
    catalog=catalog,
)
assert recipe.verify()
assert recipe.layout.verify()  # SceneDNASeal
cube_text = recipe.cube_text  # complete, valid linear-RGB .cube text, red-fastest
wav_bytes = recipe.wav_bytes  # complete RIFF/WAVE, mono PCM16, little endian, 48 kHz
manifest = recipe.manifest()  # small JSON-serializable provenance/equations manifest

# Stream instead of keeping every frame in memory. The geometry/DNA never changes.
for frame in recipe.iter_frames(48, fps=24):
    assert frame.scene_dna_sha256 == recipe.layout.seal.sha256
    # frame.camera, frame.star_directions, frame.star_flux, frame.particles
    # Meshes are also lazy: recipe.layout.objects[0].mesh()

# Explicit civil time/location always overrides the description's time label.
observational = resolve_director_scene_recipe(
    "غروب ایران، باد ملایم",
    seed="episode-01-location-03",
    observer=Observer(
        latitude_deg=32.6546,
        day_of_year=172,
        hour=19.1,
        year=2026,
        longitude_deg=51.668,
        utc_offset_hours=3.5,
    ),
    quality=RecipeQuality(star_count=4096, particle_count=4096, cube_size=17, audio_seconds=1.0),
)
```

No generator writes any file. The caller may persist `cube_text`, `wav_bytes`,
meshes or the manifest **outside Git** in its own storage/output layer.
The default WAV is an interactive **0.25-second preview**, not an eight-second
soundtrack. Request up to five seconds per clip explicitly. The camera defaults
to eight seconds; simulation frames and meshes are generated on demand.

## Modules and mathematical contracts

| Module | Implemented model / output |
|---|---|
| `_determinism.py` | Versioned, type- and length-framed SHA-256 seed derivation; domain-separated SHA-256 counter streams; Box–Muller Gaussian noise; canonical JSON hashes; immutable, explicit-endian buffers. |
| `celestial_spacetime_sky.py` | NOAA/Meeus low-order Sun, perturbed elliptic Moon, oblate-Earth observer and topocentric parallax; lunar illumination/limb angle and Lambertian terminator; solar/lunar disc-overlap eclipses; spherical exponential atmosphere with Earth shadow, both optical-depth paths, Rayleigh λ⁻⁴ and normalized Henyey–Greenstein Mie phase; 81 visible spectral bands and analytic CIE-to-linear-sRGB conversion; Planck-temperature stars, magnitude distribution, Milky Way density, rotation, extinction and scintillation; 3D `.cube`. |
| `soil_air_weather_physics.py` | Analytic curl of a time-varying Fourier vector potential (divergence-free, spatially periodic); exact constant-wind OU velocity **and correlated integrated-position noise**, including zero/tiny/high-drag limits; periodic optional particle recycling and resumable streams; six soil BRDF responses with exact unpolarized dielectric Fresnel/TIR; filtered aeroacoustic wind, resonant rain impacts, rustle/crackle and propagation-delayed thunder in real PCM/WAV. |
| `architecture_decor_grammar.py` | Normalized split grammar, five style productions, explicit on-demand box/arch/dome/column/lattice/ring meshes, symmetric girih graph, decor, four depth layers, immutable geometry and `SceneDNASeal`; smooth deterministic camera keyframes and inverse-depth projection. |
| `canonical_backlot_catalog.py` | Lazy family/tag index, deterministic IDs and profiles, bounded bilingual AND search and pagination; explicit bilingual director lexicon; eager bounded preview materialization; recipe seal over all dependencies; streaming film frames. |

### Units and conventions

- Astronomy: azimuth clockwise from north, geometric elevation in degrees,
  local **east/north/up** unit vectors. `Observer.hour` is fractional **civil**
  time in `[0, 24)` with explicit year/day/longitude/UTC offset; never machine
  timezone or implicit local solar time. Leap days and year rollover are handled.
- Atmosphere: wavelengths in **nm**, coefficients in **m⁻¹**, scale heights in
  **metres**. `planck_radiance` returns SI spectral radiance; the sky's solar
  normalization and `SkySpectrum.radiance` are **relative**, not calibrated
  absolute photometry. LUT input/output is linear RGB, not gamma-encoded sRGB.
- Particles/wind: **SI**, gravity along **negative Z** (hot sparks use positive
  buoyancy). `noise_n_sqrt_s` is the force-noise coefficient in the stated SDE.
  Wind is frozen during each integration step; integration is exact for a
  constant field, first-order for a spatially/time-varying field. The default
  initial field is at `t=0`; `iter_frames` advances it without a time-offset bug.
- Architecture: camera-local **X right / Y up / Z forward**, in metres. This is
  deliberately separate from the atmospheric ENU basis. Pixel displacement
  obeys `u' = u + focal_px * delta_X / Z`, with delta_X defined as scene-relative
  displacement. Focal length is in **pixels**, not millimetres. This package
  supplies geometry/projection, not a rasterizer, occlusion engine or bokeh renderer.
- Layers by object-centre depth: `Foreground < 8 m`, `Midground < 35 m`,
  `Background < 500 m`, otherwise `FarSky`; the default sky plane is 10,000 m.
  Every generated scene contains all four layers.

### Accuracy and semantic limits (important)

These are **cinematic procedural approximations**, not a navigation ephemeris,
weather forecast or full multiple-scattering renderer. Coordinates are bounded
to 1900–2100, but no uniform, certified angular-error bound is claimed. Tests
anchor the Sun to the published NREL SPA example and Moon/eclipses to selected
known 2024/2022 dates; eclipse contact timings are not authoritative. Eclipses
omit refraction, limb darkening and terrain; local solar obscuration is zero
when the Sun is completely below the geometric horizon. Lunar shadow fractions
are geocentric. The atmosphere omits clouds/ozone/multiple scattering; a vacuum
requires both pressure and aerosol turbidity zero. Stellar RGB interpolates a
256-temperature Planck/CIE table, with out-of-gamut negatives clipped.

Director matching is a deterministic, whole-word Persian/English **lexicon**,
with local negation handling, explicit priorities and fallback warnings; it is
not general natural-language understanding. Description labels map to documented
representative civil hours: dawn 05:00, morning 08:00, noon 12:00, afternoon
15:00, sunset 18:00, twilight 19:30, midnight/lunar/starfield labels 00:00.
Default location/date is Isfahan (32.6546 N, 51.668 E, UTC+3:30), day 172 of
2026. These clock labels do **not** solve sunrise/sunset for arbitrary seasons
or polar latitudes and do not override the physically computed lunar phase.
Supply an `Observer` for observationally specified scenes. In particular, a
“full moon” catalogue tag does not force the actual date to a full moon.

Multiple recognized weather kinds share a **total** particle budget; storm
subsumes rain. Ambient audio mixes wind with the primary weather layer, not
arbitrarily many independent soundtracks. Rain/snow/dust/ash/leaves/fog/sparks
have distinct mass/drag/noise/gravity presets. Sound synthesis is physically
inspired, not a field recording, speech synthesizer or room-acoustics solver.

### Twelve archives, not 122,880 stored files

Each archive has **10 named bilingual families × 1,024 lazy parameter variants**.
The total is 122,880 searchable distinct profiles, and the cross-archive recipe
space has `10_240**12` combinations. Searching touches 120 family records and
materializes only the requested page (maximum 100 entries); it never loads the
virtual archive into memory.

1. Face — proportions and facial controls.
2. Body — dimensions and limb ratios.
3. Clothing — weave/drape/roughness/colour controls.
4. Motion — cycle/stride/joint-swing controls.
5. Voice and tone — fundamental/formant/breath/intonation controls.
6. Spoken language — language/direction/cadence controls.
7. Architecture — five styles / ten building families.
8. Decor and props — dimensions/material/depth controls and scene geometry.
9. Spacetime and sky — observer/astronomical controls and sky/star models.
10. Soil, air and weather — physical presets and simulation/audio.
11. Lighting and colour — exposure/grade controls and `.cube` generation.
12. Direction and camera — ten sealed camera-curve templates.

The first six archives intentionally expose **control profiles**, not rendered
people, garments, animation rigs, voices or translated speech. They can be
consumed by later adapters without importing or modifying other packages. There
is no claim that an isolated world generator implements the previous character
or speech rendering phases.

## Reproducibility, seals and licensing

- Seed material is **cryptographically derived**, not encrypted or secret. A
  type-tagged SHA-256 digest is expanded with counter-mode SHA-256, with a
  different namespace for every subsystem/field/step. Never `hash()`, a global
  RNG, wall-clock seed, network call or provider output.
- Identical seed + parameters + schema + numerical stack yield identical arrays,
  IDs, `.cube`, WAV and recipe hashes. Named streams are prefix-stable. Geometry
  is independent of time, weather and preview resolution. Its seal includes
  dimensions, object IDs, primitives, materials, depth assignments and seed.
- Frozen dataclasses and **bytes-backed** NumPy buffers prevent re-enabling the
  array write flag. Recipe verification covers sky/atmosphere, assets, wind,
  particles, surface, LUT/text, WAV and camera as well as the scene-DNA seal.
  Seals prove content integrity, **not** identity, authorization or a signature.
- JSON hashing is package-local canonicalization, not a claim of RFC 8785/JCS.
  Arrays serialize in explicit little endian. Transcendentals/FFTs/BLAS can vary
  across libm/NumPy/CPU versions: no cross-stack bit-for-bit promise is made.
  Bump `SCHEMA` and model version when changing the procedural contract.
- Newly generated procedural data carries the requested **`CC0-Procedural`**
  label, meaning CC0 1.0 dedication for these generated assets. This is a
  package metadata label, not an SPDX identifier. No external photos, recorded
  voices or third-party binary assets are bundled. Package source remains under
  the repository's existing licence; NumPy retains its own licence.

## Resource bounds and measured performance

Hard per-request limits are checked **before allocation**:

| Output/control | Default | Maximum |
|---|---:|---:|
| Stars | 2,048 | 32,768 |
| Total particles | 1,024 | 32,768 |
| LUT edge | 9 (729 rows) | 33 (35,937 rows) |
| Mono PCM16, 48 kHz | 0.25 seconds | 5 seconds |
| Fourier wind modes | 12 | 64 |
| Sky view/light samples | 24 / 12 | 128 / 64 |
| Architecture floors/bays | 3 / 5 | 30 / 20 |
| Camera duration/keyframes | 8 seconds / 3 | 3,600 seconds / 64 |

Work/memory scales with active numerical buffers, not catalogue size. Star and
particle storage is O(N); curl evaluation is O(N × modes), spectral integration
O(81 × view samples × light samples), LUT O(edge³), audio O(samples log samples).
Meshes and frames are lazy, with no unbounded cache. A caller retaining an
unbounded number of recipes/frames defeats any per-request memory budget.

Measured in the Arena Linux sandbox on 2026-11-14, Python 3.11.2 / NumPy 2.4.6,
`OPENBLAS_NUM_THREADS=1` (imports excluded, **catalogue construction + all preview
artifacts + seals included**, no warm recipe cache):

| Benchmark | Resolution latency | Peak process RSS |
|---|---:|---:|
| Default Persian scene, 9 independent seeds | median **14.76 ms**, max **24.87 ms** | **36.81 MiB** |
| All recipe caps, storm scene, 1 seed, also two streamed frames/individual meshes | **289.35 ms** resolution | **72.14 MiB** |

The **<50 ms target applies to the interactive default**, not maximum-resolution
clips, long films, cold imports or arbitrary hardware. The maximum case remains
below 100 MB but deliberately makes no 50 ms claim. RSS is the whole standalone
Python/NumPy process, not just Python-heap allocations. On Linux the benchmark
reads `/proc/self/status` **VmHWM** because `getrusage().ru_maxrss` can inherit the
parent test runner's peak across fork/exec. It does not subtract a fabricated
baseline. A surrounding application may already consume additional memory.

```sh
PYTHONPATH=src OPENBLAS_NUM_THREADS=1 python \
  tests/creative/world_backlot/benchmark_world_backlot.py --runs 9
PYTHONPATH=src OPENBLAS_NUM_THREADS=1 python \
  tests/creative/world_backlot/benchmark_world_backlot.py --maximum --runs 1
```

## Tests and quality gates

Install the repository's development dependencies and `pytest-cov` in an isolated
virtual environment. No project config, dependency manifest or external source
file was changed for this package. Keep coverage **outside the repository**:

```sh
export COVERAGE_FILE=/tmp/.coverage_world_backlot
export PYTHONPATH=src
export OPENBLAS_NUM_THREADS=1

pytest -q tests/creative/world_backlot \
  --cov=src/nexus_ai_agent/creative/world_backlot --cov-branch \
  --cov-report=term-missing --cov-fail-under=95
ruff check .
ruff format --check .
mypy src
mypy --strict src/nexus_ai_agent/creative/world_backlot tests/creative/world_backlot
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 pytest -q
```

Evidence from this implementation run: **212 focused tests passed; 99.88% combined
line/branch coverage** (only the defensive unreachable camera-time assertion is
uncovered); full repository suite **7,584 passed / 45 skipped**. Full-tree Ruff
check/format and `mypy src` passed. Tooling: Ruff 0.16.8 (repository CI pin),
Mypy 2.4.1, pytest 9.2.0 and pytest-cov 7.1.0. Final commit identity is reported in
the engineering handoff, rather than embedded recursively in its own contents.

Tests include independent NREL geometry, known eclipse dates, phase-disc areas,
phase-function normalization, SI Planck constants, noon/sunset/night spectra,
starfield density/rotation/scintillation, finite-difference curl/divergence,
analytic OU drift and independently quadrature-integrated joint-noise moments,
all weather presets and Fresnel/TIR, real WAV decoding and spectra, causal thunder,
all style meshes/split tiling/girih symmetries, inverse-depth projection and
continuity, every one of the 122,880 catalogue IDs, bounded search, negation,
all artifact seals, cross-process/timezone/hash-seed reproducibility, forbidden
I/O, and subprocess latency/maximum-RSS budgets.
