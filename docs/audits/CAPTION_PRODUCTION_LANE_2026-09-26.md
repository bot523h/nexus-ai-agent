# Caption production lane — live audit and engineering decisions

Audit updated: 2026-09-27 (UTC). Baseline: `main` and this branch both resolved to
`6624a133329a22f1b66dce463e0b207385ee8416` before edits. Version: `3.13.0`.
This note distinguishes pack registration from operations that actually execute.

## Forensic findings

- `creative/packs/caption` was already a pure typed substrate with all ten IDs
  in its registry/manifest. `caption.transcribe` had a live adapter in the
  creative job worker, but the pure operation itself only accepted pinned
  transcript JSON. `WhisperLocalCaptionEngine` used `faster-whisper`, loaded
  lazily and called through `asyncio.to_thread`; named model profiles could
  still trigger the upstream implicit Hub download unless the process happened
  to be in offline mode. The adapter now hashes source bytes with bounded 1 MiB
  reads, stores typed `source_sha256`, derives transcript IDs from source bytes
  plus engine/version/profile/language parameters, and rejects size/mtime changes
  during inference; the model weight digest remains `null` rather than fabricated.
- Word projection from text used deterministic largest-remainder integer
  allocation. The existing `caption.align_words` handler did not use it for
  supplied word timings: it independently clamped endpoints, leaving overlaps,
  gaps, non-monotone order, and failure to cover the segment.
- SRT and ASS were deterministic integer formatters. ASS text accepted raw
  backslashes/override blocks, and karaoke floored every word independently
  while forcing at least one centisecond, which could exceed the event duration.
- `caption.diarize` is a transparent energy-VAD anchor + deterministic gap
  grouping heuristic. There is no neural diarization model; speaker labels are
  heuristic turns, not identities.
- The Argos adapter runs locally and asynchronously via `to_thread`, but the
  command operation only performs glossary substitutions; it does not dispatch
  through Argos. The distinction is now included in its result/provenance.
- `caption.style_vazirmatn` previously wrote a new asset record with the same
  content hash as its parent. It now requires the typed transcript, verifies a
  canonical transcript SHA-256 and source-media lineage against the parent
  caption, emits actual deterministic ASS text, hashes the output, and records
  the packaged font's measured SHA-256. This proves document generation, not
  durable byte storage or pixel rendering. `caption.burn_in` still fails closed:
  a hash of parent hashes is never represented as rendered video bytes.
- `caption.transcribe` now requires engine identity plus source SHA-256 and
  validates that digest against the registered audio/video AssetRecord before
  CommandBus state changes. SRT/ASS generation also validates source asset type
  and available digest evidence. Engine-backed transcribe callers missing that
  evidence fail closed.
- The lane's real media burn-in is explicitly deferred by board task-174, whose
  scope owns `rendering/ir.py`, `rendering/compiler.py`, `render_jobs.py`, and
  `bot/creative_surface.py`; `render_jobs.py` is additionally fenced by active
  task-181. No files in those fences were changed. Chosen file paths passed
  `python scripts/agent_board.py check` on this branch before edits.
- `assets/fonts/Vazirmatn.ttf` existed in the checkout. The caption manifest
  pointed to a nonexistent in-pack path and contained placeholder model/font
  digests. The real font is now bundled inside the pack with OFL-1.1 text and a
  manifest SHA-256 recomputed from its bytes.

## Gap matrix

| Operation | Contract | Pure logic | Real adapter | Runtime | Render | Tests | Status |
|---|---|---|---|---|---|---|---|
| `caption.transcribe` | typed + strict | source digest bound to media AssetRecord | faster-whisper port | creative worker | SRT document output | adapter + bus | PARTIAL: model weights not shipped; local directory required; no real ASR fixture in this environment |
| `caption.align_words` | typed | exact-cover monotone projection | not needed | CommandBus | n/a | adversarial overlap/range/zero-span | FIXED |
| `caption.diarize` | typed | merge/group/assign | energy VAD anchors only | CommandBus | n/a | deterministic anchors/turns | HONEST HEURISTIC; no neural identity |
| `caption.translate_local` | typed | glossary substitutions + coverage | Argos adapter exists separately | bus and adapter not joined | n/a | adapter + glossary tests | PARTIAL: not neural translation through this bus operation |
| `caption.generate_srt` | typed | integer-microsecond SRT/VTT | n/a | CommandBus | UTF-8 document bytes in worker | golden + hash | PASS for pure document output |
| `caption.generate_ass_rtl` | typed | strict ASS/RTL/karaoke | n/a | CommandBus | no measured FFmpeg render here | golden + mixed-script/injection | PASS as ASS document generation; render is not proven |
| `caption.style_vazirmatn` | typed transcript + bounded style schema | canonical transcript/source lineage check; deterministic ASS | packaged Vazirmatn font | CommandBus | returns actual ASS document + derived content hash | success + tamper rejection | PASS for ASS document generation; durable storage and rendered pixels not proven |
| `caption.highlight_words` | typed | ASS karaoke projection | n/a | CommandBus | no visual render in this lane | timing + golden | PASS for subtitle timing tags, not visual pixel proof |
| `caption.search_transcript` | typed | deterministic segment/word search | n/a | CommandBus | n/a | positive/negative queries | PASS |
| `caption.burn_in` | typed + confirmation | no fake result | trusted renderer not connected | CommandBus refusal | blocked by task-174/task-181 | failure/state-hash test | UNAVAILABLE; no derived artifact claimed |

## Twenty golden engineering invariants

These are executable review constraints for this lane, not claims that every
backend or media integration exists:

1. **Output truth:** only return an artifact after generating its actual bytes;
   hashes of inputs or parent hashes are not rendered artifacts.
2. **Fail closed:** missing engines, weights, fonts, caption content, or trusted
   renderers produce typed refusal, never a plausible placeholder.
3. **Offline by default:** no model, glossary, translation package, or cloud
   service is fetched implicitly; downloads require explicit opt-in.
4. **Typed boundaries:** Pack input/output crosses `CaptionEnginePort` and local
   adapters as validated models, not loosely-shaped backend dictionaries.
5. **Authorization continuity:** permissions, capability checks, and user
   confirmation remain enforced by CommandBus; handlers do not bypass them.
6. **Media binding:** transcript source IDs and SHA-256 values must match a
   registered audio/video asset wherever that evidence is supplied or required.
7. **Transformation lineage:** every derived document records its parent,
   source transcript digest, source media digest, and transformation identity.
8. **Content addressing:** output hashes are computed from the exact UTF-8 bytes
   returned, not reused from parents or calculated from metadata.
9. **Integer timing:** canonical time is integer microseconds; no float timing
   arithmetic is used in projection or serialization.
10. **Exact projection:** inferred word timings are monotone, bounded, and cover
    the intended segment span without cumulative drift.
11. **Explicit quantization:** millisecond/centisecond conversion has named,
    deterministic rounding and rejects positive cues that collapse to zero.
12. **Injection-safe serialization:** untrusted ASS control syntax and bidi
    overrides are neutralized before document output.
13. **Script fidelity:** Persian/Arabic text is preserved in logical order;
    the pack does not pretend to do shaping or visual reordering itself.
14. **Measured font provenance:** the packaged Vazirmatn binary is hashed from
    bytes and licensed alongside the package; no invented digest is accepted.
15. **Bounded work:** audio duration, model cache cardinality, CPU concurrency,
    cue counts, and renderer resources are bounded or rejected.
16. **Event-loop safety:** blocking decode/translation work is dispatched to
    worker threads; async handlers do not run inference inline.
17. **Reproducibility:** identical validated transcript and style inputs yield
    byte-identical SRT/VTT/ASS documents and the same output digest.
18. **Failure atomicity:** validation or handler failure leaves project state,
    history, and outputs unchanged; duplicate output IDs cannot overwrite assets.
19. **Atomic materialization:** filesystem artifacts are written via the trusted
    worker's atomic-write path; the pure pack does not claim that persistence
    occurred merely because it returned a document.
20. **Ownership respect:** renderer IR/compiler/worker/surface changes stay
    fenced by board owners; integration proceeds through coordination, never by
    editing around exclusive-path claims.

## Research decisions (primary sources)

1. **Inference engine and model policy.** Options: OpenAI Whisper/PyTorch,
   faster-whisper/CTranslate2, whisper.cpp, or a cloud ASR. Chosen: faster-whisper
   behind `CaptionEnginePort`, CPU `int8` defaults, lazy import, word timestamps,
   and explicit local model directory unless `allow_download=True` or the
   explicit `NEXUS_SPEECH_ALLOW_DOWNLOAD=1` opt-in. SYSTRAN documents the
   CTranslate2 implementation, word timestamps, CPU int8, and that size-name
   model loading downloads from Hugging Face automatically:
   <https://github.com/SYSTRAN/faster-whisper>. This rejects silent Hub access;
   cost is users must provision weights explicitly. The upstream README's
   small/int8 CPU benchmark is hardware-specific (8-thread i7-12700K, ~1.48 GB
   RAM); manifest reserves 2,048 MB as a conservative budget, not a cross-device
   guarantee. The manifest's 2-minute in-memory audio limit is enforced by a
   lazy PyAV duration-metadata probe before model loading/inference; unknown
   duration is a typed refusal, not unbounded work. PyAV documents container,
   stream, and stream-time-base duration semantics:
   <https://pyav.org/docs/develop/api/time.html>.
2. **Word timing and quantization.** Chosen: canonical integer microseconds;
   adapter float seconds are converted via decimal string + `ROUND_HALF_UP`;
   SRT and ASS emit by integer division (truncate sub-millisecond/centisecond
   remainder), while karaoke centiseconds use largest remainder and exactly
   cover the serialized event duration. This avoids cumulative float drift and
   keeps output deterministic. ASS karaoke duration is centiseconds per
   Aegisub's ASS tag guide: <https://aegisub.org/docs/latest/ass_tags/>. The
   W3C WebVTT spec requires cue start offsets to be nondecreasing and each cue
   end to be strictly after its start; overlapping cues are permitted:
   <https://www.w3.org/TR/webvtt1/>. Our VTT writer validates those ordering
   rules and refuses cues whose positive duration collapses at millisecond
   serialization. SRT is a de-facto convention rather than a formal ISO/W3C
   standard; the writer retains sequential cue numbers, comma milliseconds,
   blank-line separation, and rejects serialized zero-duration cues. Whisper
   word timings are model alignment estimates, not frame-accurate ground truth;
   the adapter preserves engine word stamps where available and the pack uses
   explicit proportional exact-cover projection only where words are absent.
   Upstream reference: <https://github.com/SYSTRAN/faster-whisper>.
3. **RTL/shaping boundary.** Chosen: NFC and untrusted bidi-override removal at
   ASS cue serialization, RLM punctuation anchoring, no manual visual reorder
   or Arabic reshaping. Unicode UAX #9 defines bidi resolution and directional
   isolates: <https://www.unicode.org/reports/tr9/>. FriBidi implements UAX #9
   (<https://github.com/fribidi/fribidi>); HarfBuzz provides OpenType shaping
   (<https://harfbuzz.github.io/>). FFmpeg's official filter docs say
   `subtitles` uses libass, needs `--enable-libass`, and complex script shaping
   needs HarfBuzz; those capabilities vary by build:
   <https://ffmpeg.org/ffmpeg-filters.html#subtitles-1>. The script generation
   can be tested locally; platform-specific pixel correctness still needs a
   real libass/HarfBuzz build smoke.
4. **Font asset.** Chosen: the repository's actual Vazirmatn binary is copied
   into the package's `fonts/` resource so wheel installs can resolve it;
   manifest path/hash are verified against bytes and OFL is bundled. Upstream
   font project and exact upstream OFL text:
   <https://github.com/rastikerdar/vazirmatn> and
   <https://raw.githubusercontent.com/rastikerdar/vazirmatn/master/OFL.txt>.
   Trade-off: 122,752 bytes duplicated versus relying on a checkout-only path.
5. **Translation.** Chosen adapter: Argos Translate local API; installed model
   packages are required, and this code path does not update package indexes or
   download models. Upstream documents local packages and `translate(text,
   from_code, to_code)`: <https://github.com/argosopentech/argos-translate>.
   The command's glossary-only handler is not misrepresented as the Argos neural
   adapter; composition is a known follow-up.
6. **Async and Python.** Repository minimum is Python 3.10, where
   `asyncio.to_thread` exists. Model loading, transcription, and Argos calls stay
   off the event loop. Per-key bounded model cache (two entries) avoids repeat
   weight loads while limiting retained models; per-model lock serializes decode
   to avoid concurrent decode on a shared model, plus a process-wide CPU
   inference lock avoids overlapping CPU decodes across model instances. Model
   output generators are consumed under the lock and mapped directly to the
   typed transcript (no duplicate full list of raw backend segment objects).
   Python reference:
   <https://docs.python.org/3.10/library/asyncio-task.html#asyncio.to_thread>.
7. **FFmpeg boundary.** Direct agent FFmpeg strings remain prohibited. The
   trusted lane's typed IR/compiler/executor is the required path. FFmpeg docs
   define subtitle filters and fontsdir/shaping options at
   <https://ffmpeg.org/ffmpeg-filters.html>; wiring is intentionally not
   duplicated in the pack.

## Local verification state

The three caption unit files were run in isolation during early diagnostics
(`52 passed`), then the complete non-slow repository suite was run after
installing `.[dev]`: **2,335 passed, 30 skipped, 0 failed** on Python 3.11.2.
The skips are explicitly reported by pytest: PostgreSQL-only integration cases,
core/extras-specific matrix contracts, and optional test-leg checks. Targeted
caption/architecture/worker/docs tests also passed: `130 passed`. Full-repository
`ruff check .`, `ruff format --check .` (503 files), `mypy src` (242 source
files), version lockstep, compile checks, and `git diff --check` passed. The
working branch fixes three failures observed on the prior PR head: the worker
fake engine now provides measured source provenance; the pack import allowlist
includes the pure-stdlib `unicodedata`; and this audit is indexed in
`docs/README.md`.

The manifest verifier still reports exactly the expected `unsigned_manifest`
warning; the Ed25519 signature remains a placeholder and is deliberately not
trusted/downloadable. No model weights or Persian audio fixture were
shipped/downloaded, no real faster-whisper inference was performed, and no
real Argos package translation was run. PyAV/ffprobe probing and FFmpeg/libass/
HarfBuzz render validation were not performed here; burn-in remains blocked by
the task-174/task-181 renderer ownership fences. The full local suite is strong
but does not replace the pending exact-commit GitHub CI run on the new commit,
Python 3.10/3.12 parity, PostgreSQL integrations, or measured media rendering.

Mutation probes were performed as actual source edits in turn, each source file
restored in `finally`, with a separate pytest process per mutant. `11/11 killed`:
word-cover projection, ASS RTL marker, microsecond half-up rounding, unknown
asset guard, unavailable adapter refusal, offline missing-model refusal,
transcribe without engine evidence, ASS bidi-injection sanitizer, strict model
extra rejection, burn-in false-success refusal, and the 2-minute audio resource
cap. Exact runner: `python /tmp/run_caption_mutations.py`; all 11 reported
`KILLED` and the script reported `11/11 mutants killed; source restored after
each probe`. Two additional real-source mutations were applied to the current
lineage guards: disabling style transcript-digest equality and disabling media
SHA-256 equality. Each made its targeted regression test fail; both sources were
restored in `finally` and reported `KILLED` (`2/2`).

The manifest verifier explicitly reports the signature placeholder; no
signature is fabricated. No model weights or Persian audio fixture are
shipped/downloaded; no real faster-whisper inference or Argos package
translation was run. PyAV/ffprobe probing and FFmpeg/libass/HarfBuzz rendering
were not performed. Local tests/CI evidence do not convert these unexecuted
integrations into completion claims. Exact-head GitHub CI for the new commit is
still required after push; PR #100's prior SHA is not treated as evidence for
the updated tree.
