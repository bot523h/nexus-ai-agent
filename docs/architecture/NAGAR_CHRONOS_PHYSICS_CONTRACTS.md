# Nagar Chronos physics contracts

Chronos is a deterministic CPU kernel layer: it performs no network access and uses SI seconds/meters unless a parameter explicitly says mm, pixels, samples, or ticks. Integer PTS plus a positive `Fraction` time base is authoritative; conversion to another clock is allowed only when exact. Floating-point wall-clock accumulation is forbidden.

## Kernel and units

* **Timeline:** `RationalTimestamp.pts` is integer ticks; `time_base` is seconds/tick. `concat_copy` probes, checks signatures and packets, full-decodes and boundary-decodes staged media, checks packet counts and duration within 1 ms, then hashes and atomically publishes. `assemble_pcm_wav_stream_copy` concatenates raw PCM frames with stdlib `wave`; its separate on-disk verifier proves exact frame and payload-byte conservation, exact `Fraction(nframes, sample_rate)` duration, splice boundary sample readability, and post-publish hash. Both receipts use `NO_ADDITIONAL_REENCODE_LOSS_AT_ASSEMBLY`; this is an assembly-mode claim, not perceptual quality.
* **Geometry:** world coordinates are meters, optics are mm, sensor dimensions mm, image coordinates pixels. `WorldScaleReceipt` labels invariant height as `WORLD_SPACE_CHARACTER_SCALE` and projected pixel height as `IMAGE_SPACE_PROJECTED_SCALE`. `K[R|t]P` requires SO(3) and positive camera depth. Brown–Conrady coefficients are `(k1,k2,k3,p1,p2)`; `undistort_normalized_point` iteratively inverts them and reports residual. `backproject_character_height_m` reports recovered scale and pixel-derived Type-A sensitivity. Thin-lens CoC returns pixels; the centered-height depth Jacobian is analytical.
* **Color:** sRGB and BT.709 have piecewise encode/decode functions. `cct_kelvin_to_xyz` uses the standard cubic Planckian-locus approximation for CIE xy from 2000–15000 K (not a daylight-locus model), then normalizes Y to 1. Bradford adaptation accepts caller-provided XYZ whites. `match_linear_rgb_gaussian_ot` transports full RGB Gaussian means/covariances with symmetric `eigh` matrix powers and epsilon regularization; `preserve_structure` is a scalar log-luminance variance transform. PNG verification independently reloads filter-zero RGB8 bytes, checks CIEDE2000 convergence, and enforces Sobel-gradient correlation >= 0.985.
* **Acoustics:** coordinates/dimensions are meters, speed is 343 m/s, delays are fractional samples. The shoebox image-source model uses distance attenuation and absorption power; `synthesize_stereo_room_ir` reports geometric DRR and theoretical Sabine RT60, with optional measured fields where the Schroeder fit succeeds. `measure_rir_acoustics` measures direct/reverberant energy and backward-integrated Schroeder decay (T20/T30 range selection). Sabine/Eyring remains theoretical. Timbre comparison is `ACOUSTIC_TIMBRE_CONSISTENCY`, never biometric ID. Jaw peak lag is a discrete sample-index measure from `solve_jaw_trajectory`; it is not guaranteed below half a frame for arbitrary drives.
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

The compact `test_chronos_benchmarks_n3` test measures three repetitions (environment-specific, no warm-up): real-disk WAV median/max 0.440/0.544 ms, real-disk PNG verification 1.962/2.338 ms, rational timeline 0.009/0.027 ms, geometry 0.105/0.199 ms, color transfer 0.030/0.031 ms, and acoustics RIR 0.206/0.281 ms. Process peak RSS was 68,612 KiB and estimated staging bytes 3,528. This benchmark does not include FFmpeg assembly, which requires external binaries unavailable in this environment. Production benchmarks should record CPU model, NumPy version, dimensions, warm-up, median and peak RSS.

## CommandBus integration handoff

`ChronosPort.execute` / `execute_chronos_command` accepts immutable `ChronosCommand` with operation, ordered input references, string parameters, deterministic=true, external_api_allowed=false. It allowlists WAV/FFmpeg assembly, scale back-projection, PNG verification, spatial rendering, and timbre/jaw operation identifiers, then invokes an explicitly injected `ChronosHandler`; it validates the returned operation and nonempty SHA-256. Concrete handlers are not wired into the Studio package because architecture gates prohibit Studio imports of NumPy and `creative.chronos`; composition-root binding remains a Phase 2 integration task. Inputs, authorization, cancellation, resource limits, and AssetVersion passports remain caller responsibilities. UNKNOWN continuity is rejected.
