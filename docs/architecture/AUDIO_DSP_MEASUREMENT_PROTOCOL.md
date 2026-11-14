# DSP measurement protocol — OMEGA audio engine

**Forward task 1 of the OMEGA STAGE-A ledger.** The doctrine (§9, *measurement
debt is declared, not hidden*) requires a stated protocol for every DSP claim.
This is that protocol: what window, what overlap, what band edges, and which
claims the choice invalidates.

Every constant below is read from the code, not restated from memory. If a
number here disagrees with the source, the source wins and this file is wrong.

---

## 1. Loudness — BS.1770-5

| parameter | value | where |
|---|---|---|
| gating block | `BLOCK_SECONDS = 0.4` | `loudness_bs1770.py:65` |
| block hop | `BLOCK_HOP_SECONDS = 0.100` | `loudness_bs1770.py:66` |
| absolute gate | `ABSOLUTE_GATE_LUFS = -70.0` | `loudness_bs1770.py` |
| relative gate | `RELATIVE_GATE_LU = -10.0` | `loudness_bs1770.py` |
| short-term window | `SHORT_TERM_SECONDS = 3.0` | `loudness_bs1770.py` |
| LRA relative gate | `LRA_RELATIVE_GATE_LU = 20.0` | `loudness_bs1770.py` |
| true-peak oversampling | `OVERSAMPLE_FACTOR = 4` | `loudness_bs1770.py` |

**K-weighting is the standard's own filter, not an RBJ approximation.** The RBJ
high shelf is a different filter with a different response; substituting it
produces a plausible-looking number that is simply not BS.1770. This was a real
defect and is the reason the cross-check below exists.

**Channel weights are 1.0 for L/R/C and 1.41 for surround.** A mono programme is
measured as one channel at weight 1.0 — *not* as a dual-mono pair. The difference
is 3.0103 dB, and it is the single most common way a crest comparison goes wrong.

**LRA comes from sliding 3 s windows, not from the 400 ms gating blocks.** Using
the gating blocks directly produced 0.003 LU on a synthetic ramp, which is
arithmetically fine and physically meaningless.

**The claim this protocol supports:** the integrated loudness is correct to
within 0.1 LU of FFmpeg's `ebur128`. That is verified, not assumed — see §5.

---

## 2. STFT / overlap-add (the denoiser)

| parameter | value | where |
|---|---|---|
| frame length | `DEFAULT_FRAME = 1024` | `wiener_denoise.py:55` |
| overlap | `DEFAULT_OVERLAP = 0.75` | `wiener_denoise.py:63` |
| window | periodic Hann, `np.hanning(N + 1)[:-1]` | `wiener_denoise.py:107` |
| noise floor | `NOISE_FLOOR_POWER = 1e-20` | `wiener_denoise.py` |

**The window must be the *periodic* Hann.** `np.hanning(N)` is the symmetric form:
it repeats the endpoint, so its squared overlap-add sum is not constant across the
hop and unity gain does not round-trip. `np.hanning(N + 1)[:-1]` is periodic and
does.

**Normalisation divides by Σw² and nothing else.** Multiplying a WOLA result by
`Σw / hop` is a common and wrong "correction"; with a periodic Hann at overlap
0.75 it introduces a gain error that looks like a denoising improvement.

**Unity gain must be an exact identity.** Measured round-trip error at unity gain:
`4.4e-16` — 10 orders inside the 1e-6 bound. This is asserted, not observed once.

**The noise floor is `1e-20`, not `float64.tiny`.** `np.finfo(float64).tiny` is
2.2e-308: non-zero, so it reads as a floor, but any real signal divided by it
overflows to infinity and the overlap-add returns NaN. This produced a real NaN
bug on sub-window inputs before it was fixed.

**The noise estimate is the minimum of a time-percentile across a spectral
neighbourhood.** A running minimum and a plain time percentile were both tried
and both fail on sustained music, because the same tone returns in every frame
and never falls below its own percentile. Noise is broadband; programme is
narrowband; the neighbourhood minimum is what separates them.

**The claim this protocol supports:** SNR gain of +10.37 dB at 16 kHz and
+11.68 dB at 48 kHz against a known clean reference, with 4.4e-16 reconstruction
error.

---

## 3. HPSS (harmonic/percussive separation) and M/S

**Both median kernels must be odd.** An even-length median averages two central
values, which smooths differently in the two passes and breaks the symmetry the
method depends on. The code refuses an even or negative kernel rather than
silently degrading.

**Masks are complementary by construction and that is asserted**: mask sum is
`0.0` exactly, and reconstruction error is `3.33e-16`. A soft mask pair that does
not sum to one leaks or double-counts energy, and the leak is inaudible — which
is why it is measured rather than listened for.

**M/S round trip is lossless to `5.55e-17`.** A sign flip in the side channel is
the classic defect: it inverts the stereo image, which sounds *almost* right on
mono-compatible material.

---

## 4. High-frequency work — the de-esser

| parameter | value |
|---|---|
| sibilance band | `SIBILANCE_LOW_HZ = 4500.0` … `SIBILANCE_HIGH_HZ = 9500.0` |

**Band edges are stated, and the effect is measured inside and outside them.**
Measured: 12.00 dB of de-ess inside the band, −111.5 dB of change in the low band,
sibilance ratio 0.0669. A de-esser that acts on the voice fundamental is a
wideband compressor wearing a different name, and only an out-of-band measurement
can tell the difference.

**A per-sample gain curve cannot multiply a spectrum.** The gain must be applied
to the time-domain band or it is being applied to the wrong quantity. This was a
real implementation error.

---

## 5. The cross-check, and why it is not optional

The engine measures integrated loudness twice, with two independent
implementations:

1. FFmpeg's `ebur128` filter, via `measure_loudness`;
2. `measure_bs1770_numpy`, written from the standard.

They must agree to within `LOUDNESS_CROSS_CHECK_TOLERANCE_LU = 0.1`. If they do
not, the integrated loudness is not a fact and the artifact is refused —
`CODE_LOUDNESS_METER_DISAGREEMENT`.

The cross-check is proven *reached* by inverting the tolerance to `-1.0` in a
test and asserting the refusal appears. A cross-check that is never reached is
indistinguishable from one that always passes.

---

## 6. What this protocol does **not** cover

Stated explicitly, per §9.

1. **`afftdn` limits.** FFmpeg's denoiser is not used for the measured path, so
   its noise-profile behaviour is not characterised here. Nothing in the engine
   depends on it.
2. **Resampling.** The engine never resamples before metering, because resampling
   changes the measured loudness. Every claim above is at the file's native rate.
3. **Beyond 2 channels.** Channel weights for 5.1 are implemented (1.41 for
   surround) but no test drives a real 5.1 programme through the meter; the
   weights are asserted directly.
4. **Codec-transparent loudness.** FLAC is lossless; the engine does not claim
   that a lossy encode preserves these measurements, and does not produce one.
5. **Perceptual claims.** Nothing here says a render *sounds* better. SNR, crest
   reduction and band ratios are physical measurements, and that is all they are
   claimed to be.

---

## 7. How to re-measure

```bash
# loudness cross-check and the meters
.venv/bin/python -m pytest tests/unit/test_omega_wave2_bs1770.py -q
.venv/bin/python -m pytest tests/unit/test_omega_wave2_verify_e2e.py -q

# denoiser, HPSS, de-esser
.venv/bin/python -m pytest tests/unit/test_omega_wave2_wiener_denoise.py -q
.venv/bin/python -m pytest tests/unit/test_omega_wave2_stereo_forensic.py -q

# per-module coverage of the engine
.venv/bin/python -m coverage run \
  --source=src/nexus_ai_agent/creative/audio_engine \
  -m pytest tests/unit/test_omega_wave2_*.py -q
.venv/bin/python -m coverage report -m

# the probes that hold every constant above in place
.venv/bin/python scripts/mutate_omega_audio.py
```
