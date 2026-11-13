# OMEGA session handoff — bootstrap source for the next session

**This file is the sole bootstrap source for the next OMEGA session.** Every
number in it was measured on the tree it names. Where something is unverified it
says so and gives the tier; nothing here is a prose-only claim.

Written at session closure. The audio-engine hardening work is complete enough
to hand over: the engine measures what it claims, refuses what it cannot verify,
and every guard is held by a mutation probe.

---

## 1. Where this stands

| item | value |
|---|---|
| branch | `arena/288ef95e-nexus-ai-agent` |
| base | `c3aa7c8` (`main` merge point) |
| head | see §10 — the closure commit, which cannot name itself |
| PR | [#337](https://github.com/bot523h/nexus-ai-agent/pull/337) — **open, unmerged, deliberately** |
| status string | `HARDENED_BUT_NOT_COMPLETE` |
| commits | 25 |
| CI | 24/24 jobs `success` on the pre-closure head; the closure commit re-polls (§10) |

The PR is held for Phase-2 integration of four parallel agent branches. It is
**not** waiting on a defect.

---

## 2. Commit list, in order

| # | commit | what it did |
|---|---|---|
| 1 | `6915d32` | OMEGA-02: real audio execution plane + evidence-based lifecycle |
| 2 | `16929f8` | claim the OMEGA STAGE-A hardening zone on the board |
| 3 | `17a08a0` | close four P0 audio defects found by forensic audit |
| 4 | `1fb5d0b` | the workspace becomes a thin adapter on the filesystem authority |
| 5 | `07ac3fd` | measure the resource budget instead of describing one |
| 6 | `6b43175` | separate measurement evidence from the provenance passport |
| 7 | `b0f90ae` | split `STRICT_LINEAR` from `DYNAMIC_ALLOWED`, measure crest reduction |
| 8 | `620855b` | measure the engine's refusals to 95% coverage |
| 9 | `e77703e` | replace the stale file map with the measured STAGE-A ledger |
| 10 | `162a1e6` | repair the four CI regressions PR #337 introduced |
| 11 | `ac02b8b` | §4.7: octave-stable tempo + GUM uncertainty (**closed ledger tasks 1–2**) |
| 12 | `d063f39` | Part 2 self-audit — the `STRICT_LINEAR` crest check was **circular** |
| 13 | `947b09d` | Part 3A — independent BS.1770-5 meter, cross-verified against FFmpeg |
| 14 | `f99326a` | Part 3B — decision-directed Wiener denoise, measured |
| 15 | `960ea26` | Parts 3C+3D — M/S matrix, HPSS, forensic QC, de-esser |
| 16 | `5910c4f` | Part 4 — mutation campaign 39 → 74 probes |
| 17 | `ba2643b` | Part 5 — close coverage gaps; **fix the Wiener NaN** |
| 18 | `ced3680` | the hardening report |
| 19 | `d539d5c` | record green CI; stop understating the 3.10 evidence |
| 20 | `14f2fa9` | the ledger was stale *against* the work — §4.7 was already done |
| 21 | `8a88383` | cite the final green run |
| 22 | `fba5e0e` | Wave-2.1: every module ≥95%, **the duplicated failure**, the 1:1 stereo case |
| 23 | `4b17b52` | record Wave-2.1 and the two defects it found |
| 24 | `75bd152` | cite run `43393070087` |
| 25 | *(closure)* | **this commit**: FFmpeg banner portability, 3.10 guard, this handoff |

---

## 3. Measured state

### 3.1 Coverage — per module, not aggregate

`coverage run --source=src/nexus_ai_agent/creative/audio_engine` over the engine
suites (532 passed, 1 skipped):

```
TOTAL                2584    80   97%
verify.py             100%      stereo_hpss.py     100%
recipe.py             100%      workspace.py        99%
wiener_denoise.py      98%      ed25519_sign.py     98%
execution.py           97%      models.py           97%
process.py             97%      numpy_dsp.py        96%
continuity.py          95%      ffmpeg_ops.py       95%
forensic_qc.py         95%      loudness_bs1770.py  95%
```

**The bar is per-module.** An aggregate hides the one file nobody exercised —
that is exactly how `verify.py` sat at 92% while the package read 95%.

`workspace.py`'s two remaining lines are genuinely unreachable: the specific
`ContentLimitExceeded` branch is shadowed by the generic `FilesystemBoundaryError`
handler it subclasses. Behaviour is correct; see `test_omega_wave2_module_refusals.py`.

### 3.2 Mutation campaign

```
scripts/mutate_omega_audio.py   77 probes   77/77 KILLED
```

`ANCHOR_MISSING`, `AMBIGUOUS_ANCHOR` and `SURVIVED` all fail the gate. Re-run
after **every** source edit — `ruff format` has already invalidated one anchor,
and a cast that fixes a mypy error invalidated another.

### 3.3 Full suite

`pytest tests/unit tests/architecture` — see §10 for the closure numbers.

### 3.4 The parser this session fixed

`parse_audio_stream_channels` in `ffmpeg_ops.py`, before and after:

| FFmpeg | banner fragment | before | after |
|---|---|---|---|
| 9.0.2 | `48000 Hz, mono, s16` | `1` | `1` |
| 9.0.2 | `48000 Hz, stereo, fltp` | `2` | `2` |
| 6.1.1 | `48000 Hz, 1 channels, s16` | **`None`** | `1` |
| 6.1.1 | `48000 Hz, 2 channels, flt` | **`None`** | `2` |

`None` made `probe()` fail closed, which surfaced as nine live failures reading
`pulse_96.wav carries no decodable audio stream`. The bug was invisible in
sandboxes that ship 9.x — this one provisions `ffmpeg-linux-x86_64-v9.0.2` from
the `imageio-ffmpeg` wheel.

---

## 4. Forward tasks — exact status

| # | task | status | where |
|---|---|---|---|
| 1 | Full PCM/FFT/STFT/denoise measurement protocol | **CLOSED** — `fba5e0e` | `docs/architecture/AUDIO_DSP_MEASUREMENT_PROTOCOL.md` |
| 2 | STAGE-B K1: `RationalTime` + delta shot-DAG + verified `-c copy` | `OUT_OF_ZONE` | not started; STAGE-B is gated behind STAGE-A |
| 3 | STAGE-B K2: `K[R\|t]` + Brown-Conrady + thin-lens CoC + GUM Jacobian | `OUT_OF_ZONE` | depends on K1 |
| 4 | STAGE-B K3: scene-linear, Bradford, albedo/shading, Monge–Kantorovich | `OUT_OF_ZONE` | needs K1/K2 |
| 5 | STAGE-B K4: Allen–Berkley ISM RIR, cepstral timbre, viseme solver | `OUT_OF_ZONE` | last kernel |
| 6 | `DYNAMIC_ALLOWED` on a stereo source (the 1:1 case) | **CLOSED** — `fba5e0e` | `test_omega_stage_a_hardening.py::test_dynamic_allowed_on_a_stereo_source_measures_the_1_to_1_case` |
| 7 | `AssetVersion` persistence adapter (task-282) | `OUT_OF_ZONE` | **contract written**: doctrine §11.1 |
| 8 | `publish_audio_artifact(...)` on `studio/bus.py` | `OUT_OF_ZONE` | **contract written**: doctrine §11.2 |

Tasks 7 and 8 are the two items that cannot be closed from the audio zone. Each
now has an exact signature and its invariants in
`docs/architecture/NAGAR_MATHEMATICAL_ENGINEERING_DOCTRINE.md` §11, so Phase-2
integration is a call site and not a redesign.

---

## 5. Known open items, with verification tier

| item | tier | detail |
|---|---|---|
| **Python 3.10** | `UNVERIFIED_LOCALLY` | No 3.10 interpreter obtainable in the sandbox. Attempts recorded in §7. Relies on CI's blocking `python-parity (3.10)` and `continuum-evidence (3.10)` legs, which are green. **Do not report 3.10 as locally verified.** |
| **DECISION_LOG r5 vs `probe_json()`** | by inspection | r5 forbids a second *required* binary. `probe_json()` exists but has **no production call site** — only the `source="ffprobe_json"` label and its own definition. The production path is `probe()` + the FFmpeg banner. The 14 `probe()` call sites are listed in §6.2. Not a violation, but a reader who sees `probe_json` may think otherwise. |
| **`_SUMMARY_*` regexes** | partially hardened | `loudnorm` JSON already uses `rfind("{")`/`rfind("}")` + `json.loads`, so it is not line-offset fragile. The `ebur128` summary regexes (`_SUMMARY_I_RE`, `_SUMMARY_LRA_RE`, `_SUMMARY_PEAK_RE`, `_SUMMARY_THRESHOLD_RE`) still match label text; they are **not** hardened against a future FFmpeg renaming a summary label. `_DURATION_RE` likewise. Tier: `BANNER_FAMILY_VERIFIED, SUMMARY_LABELS_UNCHANGED` — no FFmpeg we could obtain renamed them. |
| **`OPERATION_TRUTH.json`, `.agents/board.json`, `docs/README.md`** | merge-time attention | Sigma and Beta also touch these. No code conflict, but any merge order that resolves them mechanically will drop rows. **Re-generate** with `python -m nexus_ai_agent.nagar --docs` after each merge rather than hand-merging. |
| **`creative/slideshow` audio modules** | 0–67% | No audio suite targets them. Out of the audio zone. |

---

## 6. Reference: the surfaces this session touched

### 6.1 The parser

```python
from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import (
    parse_audio_stream_channels,  # "mono"|"1 channels"|"5.1(side)"|"7.1.4" -> int|None
    probe_banner_channels,  # one banner line -> (channels, rate, codec, layout)
)
```

`probe_banner_channels` exists so the banner shape can be tested against verbatim
lines from several releases without invoking the binary. The parser is what
changes between versions; the subprocess call around it does not.

### 6.2 The 14 `probe()` call sites in production

```
execution.py:500, 537, 662, 676, 704, 1126, 1276, 1288   (8)
ffmpeg_ops.py:548, 798                                    (2)
verify.py:257, 421, 576, 819                              (4)
```

All of them consume `channels`. Every one of them was returning `None` on a 6.1.1
host.

---

## 7. Environment reproduction

### 7.1 The snapshot can reset between turns

Symptom: `HEAD` rewinds to `c3aa7c8`, `.venv` disappears, but **untracked files
survive**. Do not re-clone and do not redo work. Restore:

```bash
git fetch origin arena/288ef95e-nexus-ai-agent
git update-ref refs/heads/arena/288ef95e-nexus-ai-agent \
  "$(git rev-parse origin/arena/288ef95e-nexus-ai-agent)"
git reset --mixed HEAD
```

### 7.2 Rebuild the venv

```bash
python3 -m venv .venv                       # 3.11.2 in this sandbox
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pip install coverage "ruff==0.16.8"
```

**Pin `ruff==0.16.8`.** `.[dev]` allows `ruff>=0.4` and pip installs 0.17.5,
which reformats files 0.16.8 leaves alone. CI pins `ruff==0.16.8` at
`.github/workflows/ci.yml:810`. A gate that disagrees with CI is not a gate.

### 7.3 How FFmpeg is resolved

`imageio_ffmpeg.get_ffmpeg_exe()`, via `resolve_ffmpeg_bin()`. This sandbox gets
`.../imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v9.0.2`. **Check the version
before trusting a green run** — 9.x hides the banner defect that 6.1.1 exposes:

```bash
.venv/bin/python -c "
import subprocess, imageio_ffmpeg as i
print(subprocess.run([i.get_ffmpeg_exe(),'-version'],capture_output=True,text=True).stdout.splitlines()[0])"
```

### 7.4 `pack_coverage.py` needs a clean tree

`python scripts/pack_coverage.py` refuses a dirty tree: any tracked, untracked or
ignored-importable change under the evidence paths makes the run
`UNVERIFIED` / `NOT ACCEPTED`. **Commit first, then measure.** The verdict is only
meaningful as `verdict: ACCEPTED` on a clean checkout.

### 7.5 Python 3.10: what was attempted, and what failed

| attempt | outcome |
|---|---|
| `command -v python3.10` | not found |
| `apt-get install python3.10` | no root (`/var/lib/dpkg/lock-frontend` permission denied) |
| `pip install uv` → `uv python install 3.10` | `uv` installed; the interpreter download **failed** — `invalid peer certificate: UnknownIssuer` |
| manual `curl` of the python-build-standalone tarball | `302` to `release-assets.githubusercontent.com`, **not in the egress allowlist**; final byte count 0 |

Classification: **`UNVERIFIED_LOCALLY — relies on CI Python 3.10 job`.**

What *was* done instead: `tests/unit/test_omega_wave2_py310_compat.py` greps the
audio zone for `StrEnum`, `datetime.UTC`, `ExceptionGroup`, bare `tomllib` and
`typing.Self` without `typing_extensions`, ignoring docstrings and comments so a
`datetime.UTC` *explanation* is not a `datetime.UTC` *usage*. The zone was already
clean — `verify.py::_utc_now` uses `datetime.timezone.utc` deliberately and says
so.

---

## 8. DO NOT — for the next session

1. **Do not merge PR #337.** Held for Phase-2 convergence of four branches.
2. **Do not make `ffprobe` a hard dependency.** Decision r5. `probe_json()` may
   stay optional; it must not become mandatory in any call site.
3. **Do not edit Sigma's or Beta's zones.** `studio/bus.py`, the task-282 DB
   schema, and everything outside `creative/audio_engine/**`,
   `creative/packs/audio/**`, `creative/studio/audio_engine.py`,
   `creative/studio/evidence.py`, `creative/slideshow/audio.py`,
   `scripts/mutate_omega_audio.py` and the audio tests.
4. **Do not trust a green mutation campaign from before a source edit.** Audit
   the anchors; `ruff format` and a mypy cast have each broken one.
5. **Do not regenerate `OPERATION_TRUTH.json` by hand.** Run
   `python -m nexus_ai_agent.nagar --docs`.
6. **Do not claim 3.10 was verified locally.** It was not (§7.5).
7. **Do not add a test file named like an audio suite without registering it** in
   `PACK_TEST_TARGETS` (`continuum/pack_coverage.py`). An unregistered
   pack-importing module makes pytest exit non-zero, which flips
   `continuum-evidence` to `accepted=False` — and that gate reports the failure as
   a coverage problem when it is a provenance problem.
8. **Do not run `pack_coverage.py` on a dirty tree** and believe the result.

---

## 9. The four defects this session found by measurement

Recorded because each one is the kind that only a falsifying test finds.

1. **The `STRICT_LINEAR` crest check was circular** (`d063f39`) — it compared the
   artifact to the previous manifest, so a crushed file passed. Now it requires
   the *source* and asserts invariance at 0.25 dB.
2. **`estimate_noise_psd` floored at `float64.tiny`** (`ba2643b`) — 2.2e-308 is
   non-zero, so it reads as a floor, but any real signal divided by it overflows
   and the WOLA returns NaN. Now `1e-20`, and the guards are bit-identical on
   real input (`10.365383619626568 dB` before and after).
3. **`verify_passport` appended the target failure twice** (`fba5e0e`) — one
   failure produced two identical codes. Every existing test asked "is this code
   present", which a duplicate cannot fail; the fix came with a test that asserts
   the *count*.
4. **The channel-layout parser only understood `mono`/`stereo`** (closure) — so
   FFmpeg 6.1.1 made every artifact fail closed as "no decodable audio stream".
   Invisible on 9.x, and the sandbox ships 9.x.

---

## 10. Closure verification

Filled in by the closure commit; the SHA is in the commit subject and in the PR
body, because a document cannot contain its own hash.

```
ruff check .                 clean
ruff format --check .        clean
mypy src                     Success: no issues found
mypy --strict audio_engine   Success: no issues found (15 files)
campaign                     77/77 KILLED
full suite                   see the closure commit message
pack_coverage.py             verdict: ACCEPTED (clean tree, post-commit)
CI                           poll to 24/24 on the final SHA
```
