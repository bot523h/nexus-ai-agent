# Nagar Chronos physics contracts

Chronos is a deterministic CPU kernel layer: it performs no network access and uses SI seconds/meters unless a parameter explicitly says mm, pixels, samples, or ticks. Integer PTS plus a positive `Fraction` time base is authoritative; conversion to another clock is allowed only when exact. Floating-point wall-clock accumulation is forbidden.

## Kernel and units

* **Timeline:** `RationalTimestamp.pts` is integer ticks; `time_base` is seconds/tick. `concat_copy` requires absolute paths, probes all inputs, compares stream signatures, checks packet PTS/DTS monotonicity and packet-count conservation, full-decodes the staged output, decodes each join window, checks duration within 1 ms, then hashes and atomically publishes. Failures clean staging and refuse publication. Its receipt claims `NO_ADDITIONAL_REENCODE_LOSS_AT_ASSEMBLY` only after those checks; this is a stream-copy statement, not a perceptual codec-quality guarantee. PCM sample-count equality is not yet independently verified.
* **Geometry:** world coordinates are meters, optics are mm, sensor dimensions mm, image coordinates pixels. Character scale is a world-space invariant; focal length changes projection, not rig dimensions. `K[R|t]P` requires a proper SO(3) rotation and positive camera depth. Distortion coefficients follow Brown-Conrady order `(k1,k2,k3,p1,p2)`. Thin-lens CoC returns pixels. The depth Jacobian is analytical for a centered upright height model.
* **Color:** sRGB decode/encode use IEC 61966-2-1 breakpoints; BT.709 decode uses its OETF inverse. Bradford adaptation operates on caller-provided XYZ whites. Structure-preserving relighting works on log-luminance arrays and regularizes the scalar variance transport.
* **Acoustics:** coordinates/dimensions are meters, speed is 343 m/s, delays are fractional samples. The shoebox model uses image sources with distance attenuation and an absorption power approximation. Sabine/Eyring estimates assume diffuse fields; neither is a room measurement. Cepstral biometric identification is intentionally not claimed by this kernel; any future vocal-tract consistency metric must be labeled `ACOUSTIC_TIMBRE_CONSISTENCY`, not speaker identity.
* **Jaw:** inputs are ordered rational timestamps and normalized aperture targets. The solver integrates the critically damped second-order model with deterministic bounded substeps.

## Ground-truth error and uncertainty matrix

| Quantity | Reference | Error/uncertainty contract |
|---|---|---|
| rational time | exact Fraction | zero quantization unless rescale is unrepresentable, then fail |
| pinhole pixel | analytic K[R|t] | floating-point matrix error; propagate depth sigma with `abs(dH/dZ) sigma_Z` |
| CoC | thin-lens equation | input uncertainty in f, N, focus, subject distance, and pixel pitch must be propagated by caller |
| RT60 | Sabine/Eyring | model uncertainty dominates; not calibrated without measured decay |
| RIR | image-source model | finite reflection order and fractional-sinc truncation bound accuracy |
| color | transfer/adaptation equations | clipping and display encoding are not scene-linear operations |
| structural check | gradient correlation | not a semantic identity metric; threshold is a postcondition only |

## CPU/memory benchmark protocol (N >= 3)

The compact `test_chronos_benchmarks_n3` test measures three repetitions per kernel in this sandbox (environment-specific, no warm-up): timeline median/max 0.011/0.049 ms, geometry 0.123/0.307 ms, color 0.030/0.087 ms, acoustics 0.202/0.362 ms; process peak RSS was 76,344 KiB and estimated staging bytes 2,780. This microbenchmark does not include FFmpeg assembly, which needs external binaries and was not run here. Production benchmarks should record CPU model, NumPy version, dimensions, warm-up, median and peak RSS.

## CommandBus integration handoff

Send an immutable `ChronosCommand` with operation name, ordered input artifact references, string-valued canonical parameters, `deterministic=true`, and `external_api_allowed=false`. The caller owns artifact resolution, authorization, cancellation, resource limits, and persistence. Outputs should include content hashes, rational time bases, kernel version, and verifier outcomes. Do not pass open file handles or mutable arrays through the bus; do not mark a task successful when any verifier is `UNKNOWN`.
