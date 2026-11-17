# Hyperion Phase 2 Speech-Cinema Doctrine

These modules are pure, deterministic building blocks for offline speech cinema. They accept PCM arrays or standard WAV input, avoid service calls and mutable global state, and use NumPy only for signal math.

* **Voice detection** reports frame masks and exact `Fraction`-based segment boundaries; thresholds are explicit in `VADConfig` and SNR is estimated against a low-percentile noise floor.
* **Alignment** is a reproducible baseline rather than a language model: tokenization, grapheme phonemization, viseme IDs, aperture weights, and monotonic banded DTW have stable tie-breaking.
* **Dubbing** guarantees the requested frame count, including empty and multichannel inputs. The baseline uses deterministic band-limited linear interpolation and exposes a result object for auditability.
* **Subtitles** are data-first: aligned words become cues via duration, character, gap, and shot-cut constraints, and codecs emit stable SRT, WebVTT, and ASS. SHA-256 fingerprints are over exact serialized bytes.

Callers should preserve sample rate metadata and treat times as seconds unless documented otherwise. Inputs are never modified in place.
