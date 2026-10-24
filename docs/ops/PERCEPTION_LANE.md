# Perception lane — measured media evidence (`nexus.analysis.perception`)

Status: shipped · Pack: `nexus.analysis.perception` 1.0.0 · Adapter: `creative/perception/` ·
CLI: `nexus analyze` · Cost: **free** (no API key, no network, no new dependency).

## 1. Problem

The studio could *edit* (`timeline.*`), *transform* (`audio.*`, `motion.*`, `color.*`) and
*caption*, but nothing in the repository **measured** media. The most visible symptom:
`audio.detect_beats` derives a fixed 120 BPM grid from the asset's duration (see
`creative/packs/audio/operations.py::_detect_beats`) — an honest skeleton, not an analysis.
Operations that should be driven by what is *in* the file (cut the silences, mark the scenes,
normalise to a target) had no trustworthy input.

## 2. Options considered

| # | Option | Cost | Power | Trade-off |
|---|---|---|---|---|
| A | **FFmpeg measurement filters** (`silencedetect`, `select=scene`, `ebur128`) | free, already a hard dependency (render lane) | standard, reference-quality for R128 and silence; scene score is a classic heuristic | text on stderr must be parsed; no beat/ML features |
| B | `librosa` / `scipy` / `torch` | free | strongest DSP/ML (beats, onsets) | heavy dependencies; the audio pack's boundary test forbids them; large images |
| C | Cloud analysis APIs | paid / keyed | strong | violates "free, offline"; uploads user media |
| D | Pure-Python DSP on PCM | free | weak, slow | re-implementing R128 is a correctness risk |

**Decision: A.** It is the only option that is simultaneously free, offline, already
installed, standardised (EBU R128) and compatible with the pack boundary rules. B stays
the right *next* engine for beats/onsets, behind the same evidence contract (a new
`analyzer` literal + a reviewed adapter), not mixed into this one.

## 3. Architecture

```
file ──► creative/perception/analyzer.py ──► sealed evidence (JSON) ──► TypedCommand.input
          (the ONLY process runner)             content-bound               │
                                                                            ▼
                                       creative/packs/perception (PURE) ── CommandBus
                                       analysis.mark_silences      (B, reversible)
                                       analysis.mark_scene_cuts    (B, reversible)
                                       analysis.assess_loudness    (A, read-only)
```

* The **pack is pure** (stdlib + pydantic): it validates evidence and edits state; it never
  opens a file. This is the same split as the slideshow pack (`AssetEvidence`).
* The **adapter owns every subprocess**: argv only (no shell), `-protocol_whitelist file`
  (a container cannot make FFmpeg open `http:`), an absolute resolved path (a filename can
  never become an option), stderr streamed with only relevant lines kept (bounded memory),
  a watchdog that kills the whole process group at `timeout_s`, and media longer than the
  limit refused rather than half-analysed.
* **Evidence is content-bound**: `asset_sha256` must equal the clip's/asset's
  `content_sha256`, so evidence about other bytes (stale analysis, a re-render, another file)
  is refused. The file is hashed before analysis and its `(size, mtime_ns)` re-checked after.
* **Idempotent and reversible**: marker ids are derived from the evidence hash, so replaying
  the same evidence adds nothing; one command = one transaction = one `system.undo`.
* **Time mapping** is explicit: asset time → timeline time through the clip window
  (`timeline.start + floor((t − source.start) · timeline.len / source.len)`); instants outside
  the half-open window are *counted* (`skipped_outside_clip`), never clamped.

## 4. What the integrity check does **not** claim

`evidence_hash` detects corruption and truncation in transport; it is **not** a signature.
A client able to submit commands can compute it. That is acceptable by construction: the
operations that consume evidence only add reversible markers or return advice, which
`timeline.mark` already allows — no privilege is gained. If evidence ever gates something
irreversible, it must be signed (the pack trust root, ADR 0006, is the model).

## 4b. Indirection formats are refused (HLS / concat / SDP / DASH)

FFmpeg has text formats that *name other files or URLs* (HLS `.m3u8`, `ffconcat`, SDP, DASH `.mpd`);
local-file-read attacks through them are a known class (CVE-2016-1897/1898). Defence in depth:

1. `-protocol_whitelist file` before `-i` (no network, no `data:`/`crypto:`/`subfile:` chains);
2. the analyzer sniffs the first KiB and **refuses any non-binary input** with `AnalysisInputError`
   *before any process is started* (real containers carry NUL/high bytes; every indirection format is text);
3. error messages echo the path only, never file content.

Verified live on FFmpeg 6.1.1 (system) and 7.0.2 (bundled). Not independently confirmed: the exact CVE
ids and versions in an external "BLOCKED" report that cited none checkable from this repository.

## 5. Known limits (stated, not hidden)

* Scene detection is FFmpeg's scene-score heuristic: hard cuts are reliable, slow dissolves
  are not.
* FFmpeg 6 prints `-inf` and FFmpeg 7 prints `-70.0` for a signal below the BS.1770 gate;
  both are normalised to "not measurable" (`None`) — pinned by a test on both builds.
* Times are the container's normalised timeline (what a player shows); AAC priming makes a
  silence start read a few tens of milliseconds late versus the PCM source.
* The pack lifecycle is `AVAILABLE`: the evidence path is real end to end. Beats/onsets are
  *not* part of this lane (`audio.detect_beats` is still the skeleton).

## 6. Use

```bash
nexus analyze silences talk.wav --noise-floor-db -40 --min-silence-s 0.5 > silence.json
nexus analyze scenes   clip.mp4 --threshold 0.3                          > scenes.json
nexus analyze loudness mix.wav                                           > loudness.json
```

The JSON is the exact `input.evidence` of the three operations. `analysis.assess_loudness`
returns `normalize_arguments` that validate against `audio.normalize_loudness` (Level C —
the caller still has to confirm).

## 7. Confirmation (how drift is caught)

| Rule | Where |
|---|---|
| pack is pure, adapter is the only runner, no shell, no network client | `tests/architecture/test_perception_boundary.py` |
| manifest data-only, offline, composed, lifecycle record present | same + `test_pack_activation_completeness.py`, `test_capability_lifecycle.py` |
| parsers on captured FFmpeg 6.1/7.0 output + hostile text | `tests/unit/test_perception_parsers.py` |
| real FFmpeg (system + bundled) incl. option-injection, playlist→http, timeout, flood, mutation | `tests/unit/test_perception_adapter.py` |
| operations, mapping, idempotency, undo, foreign evidence | `tests/unit/test_perception_pack.py` |
| cross-pack contract with `audio.normalize_loudness` | `tests/unit/test_perception_composition_contract.py` |
| pack coverage ≥ 95 % (canonical evidence) | `scripts/pack_coverage.py` |
