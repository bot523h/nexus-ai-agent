# Runbook — the color / exposure lane (`nexus.apply.lane` → `exposure`)

**Scope:** how the `exposure` lane op becomes real pixels, what is guaranteed,
and how to check it without touching a media file.
**Owner zone:** `nagar-render-lane` (`src/nexus_ai_agent/creative/rendering/`).
**Companion decisions:** D-0005 … D-0008 in [`../DECISION_LOG.md`](../DECISION_LOG.md).

The `exposure` op is the **executable twin** of the pack operation
`color.adjust_exposure` (`creative/packs/delivery/`). The pack op is the
declarative, reversible journal entry; the lane op is what an encode actually
runs. They are deliberately two layers: a grade can be recorded in a `Project`
long before any FFmpeg process exists.

---

## 1. The mapping table

Every number below was read out of FFmpeg's own filter sources, not prose docs.

| Lane field | Range / default | Compiled to | FFmpeg declared bound | Source |
|---|---|---|---|---|
| `exposure_ev` | −4.0 … +4.0, default `0.0` | `eq=gamma=clamp(2**EV, 0.1, 10.0)` | gamma `av_clipf(..., 0.1, 10.0)` | `libavfilter/vf_eq.c` `set_gamma` |
| `contrast` | 0.2 … 3.0, default `1.0` | `eq=contrast=<value>` (identity) | `av_clipf(..., -1000.0, 1000.0)` | `libavfilter/vf_eq.c` `set_contrast` |
| `temperature_k` | 1000 … 40000, default `6500` | `colortemperature=temperature=<K>:pl=<0\|1>` | `{.dbl=6500}, 1000, 40000` | `libavfilter/vf_colortemperature.c` options table |
| `tint` | −50.0 … +50.0, default `0.0` | `colorbalance=gm=<tint/50>:pl=<0\|1>` | `{.dbl=0}, -1, 1` | `libavfilter/vf_colorbalance.c` options table |
| `preserve_lightness` | default `True` | `pl=1` on both RGB stages | `pl` default `0` in both filters | both option tables |

Two mappings deserve the detail:

**EV → gamma.** `vf_eq.c` `create_lut` computes `v = v ** (1 / gamma)`, so
`gamma = 2 ** EV` doubles exposure per stop and **positive EV brightens** — the
photographic convention. `2 ** 4 = 16` exceeds the filter's `10.0` ceiling and
`2 ** -4 = 0.0625` sits below its `0.1` floor, so the usable *unclamped* band is
**±log2(10) ≈ ±3.32 EV**. Outside that band the lane clamps in Python, which is
the whole point: FFmpeg would clip silently via `av_clipf` and the argv would
claim a grade the pixels never received.

**tint → gm.** `colorbalance`'s `gm` is *"set green midtones"* and is *added* to
the green channel, so **positive tint pushes green, negative pushes magenta**.
`±50` lands exactly on the filter's `±1`. There is no hidden scaling.

## 2. Hard contracts

These are enforced in code and pinned by golden tests. Breaking one is a bug.

1. **Clamping happens in the IR, never in FFmpeg.** What the argv says is what
   the pixels get. `ExposureOp.gamma` is the single source of truth; the
   compiler formats it, it does not recompute it.
2. **Neutral stages are elided; `eq` is always emitted.** `eq` is a genuine
   no-op at neutral (`vf_eq.c` `check_values` sets `adjust = NULL` when
   contrast `1.0`, brightness `0.0`, gamma `1.0`), so emitting it costs nothing.
   The RGB filters never short-circuit and `kelvin2rgb(6500)` is
   ≈ `(1.000, 0.997, 0.981)` — **not** an exact identity — so at 6500 K with
   tint 0 both are dropped, which also removes the pixel-format round trip.
3. **At most one yuv→rgb→yuv round trip per op, shared by both RGB filters.**
   `eq` accepts only planar YUV/gray; `colortemperature` and `colorbalance`
   accept only RGB. Their format sets are **disjoint**, so a conversion is
   unavoidable — but it happens once each way and the stream returns to
   `yuv420p` so every downstream op sees the format it would have seen anyway.
4. **`pl` is always explicit.** Both filters default `pl` to `0`; the lane emits
   `pl=1` (or `pl=0`) so lightness behaviour never depends on a filter default
   changing upstream.
5. **Exposure never moves the clock.** It is absent from the duration algebra by
   design; `tests/unit/test_lane_duration_algebra.py` restates that algebra
   independently over seeded random lanes and pins the agreement, and refuses
   any op kind it has not classified.
6. **An exposure op on an audio-only lane is a `LaneError`, not a silent drop.**
   The video chain is never built for an audio main, so a grade would vanish
   while the journal still claimed it.
7. **No new pixel formats, no new processes.** The op adds filtergraph text
   only. `executor.py` remains the single `subprocess` site
   (`tests/architecture/test_rendering_lane_boundary.py`).

## 3. Validation without FFmpeg

The compiler is pure, so the whole contract is checkable with no binary, no
media file and no network. This is the first thing to run when a grade looks
wrong — it tells you what *would* be executed.

```bash
python - <<'PY'
from nexus_ai_agent.creative.rendering import (
    ExposureOp, LaneIR, LaneSource, compile_lane,
)

main = LaneSource("main", "/media/main.mp4", "video", 10_000_000)
grades = {
    "neutral":   ExposureOp(),
    "+1 stop":   ExposureOp(exposure_ev=1.0),
    "ceiling":   ExposureOp(exposure_ev=4.0),
    "tungsten":  ExposureOp(temperature_k=3200, tint=12.5),
    "cool+tint": ExposureOp(exposure_ev=-0.5, contrast=1.15,
                            temperature_k=7500, tint=-25.0,
                            preserve_lightness=False),
}
for name, op in grades.items():
    graph = compile_lane(LaneIR(main=main, ops=(op,))).filtergraph
    stage = [x for x in graph.split(";") if "eq=gamma=" in x][0]
    print(f"{name:10} ev={op.exposure_ev:+.1f} -> {stage}")
PY
```

Expected (abridged):

```text
neutral    ev=+0.0 -> [vbase]eq=gamma=1.000000:contrast=1.000000[v1]
+1 stop    ev=+1.0 -> [vbase]eq=gamma=2.000000:contrast=1.000000[v1]
ceiling    ev=+4.0 -> [vbase]eq=gamma=10.000000:contrast=1.000000[v1]
tungsten   ev=+0.0 -> ...format=rgb24,colortemperature=temperature=3200:pl=1,colorbalance=gm=0.250000:pl=1,format=yuv420p[v1]
```

The gates, with the exact CI invocations:

```bash
ruff check . && ruff format --check .
mypy src
pytest -q tests/unit/test_rendering_lane_exposure.py tests/unit/test_lane_duration_algebra.py
pytest -q -m "not slow"          # whole suite; CI runs: pytest -v -m "not slow" --tb=short
```

### Real-encode evidence (measured, not asserted)

Golden pins prove the argv; they cannot prove FFmpeg *accepts* it. Verified
locally on **FFmpeg 7.0.2** (the static `imageio-ffmpeg` wheel), encoding a real
2 s `testsrc2` + sine source through five grades and reading back frame 10:

| Grade | Mean luma | Duration |
|---|---|---|
| EV −1 | 67.97 | 2 000 000 µs |
| EV 0 (neutral) | 92.84 | 2 000 000 µs |
| EV +1 | 133.37 | 2 000 000 µs |
| EV 0, 3200 K, tint +25 | 92.06 | 2 000 000 µs |
| EV +4 (clamped ceiling) | 207.84 | 2 000 000 µs |

Mean luma is monotonic in EV with a 65.4-level two-stop spread, white balance
measurably moves pixels, duration is unchanged in every case, and all five
graphs were accepted — which is the only proof that the option spellings
(`colortemperature=temperature=`, `colorbalance=gm=`, `pl=1`) and the
`yuv420p → rgb24 → yuv420p` round trip are valid on a real build. This was a
one-off local check, **not** a committed test; making it a permanent gate is
staged on the board as `color-lane-real-encode-evidence`.

## 4. Troubleshooting

| Symptom | Most likely cause | Check / fix |
|---|---|---|
| Grade has no visible effect | EV and contrast are neutral, so `eq` is a real no-op and the RGB stages were elided | Run the §3 script; a neutral line with no `format=rgb24` is correct behaviour, not a bug |
| Grade looks weaker than the EV asked for | EV beyond ±3.32 is clamped by design (`2**4=16` → `10.0`) | Read `ExposureOp.gamma_is_clamped`; split the grade across two ops or accept the ceiling |
| `LaneError: exposure op needs a video main asset` | Lane main is audio | Correct and intentional (contract 6). Grade the video lane, not the audio one |
| Skin tones shift with white balance | `preserve_lightness=False` | Keep the default `True` so `pl=1` is emitted on both RGB stages |
| Colours look green/magenta after a tint | Sign convention: **positive tint = green** | Negate the tint value; see §1 |
| Unexpected extra `format=` conversions | More than one exposure op in the lane, each paying its own round trip | Merge the grades into a single `ExposureOp` |
| `No such filter: 'colortemperature'` at encode time | FFmpeg build predates the filter (added 2021) or lacks it | `ffmpeg -filters \| grep -E 'colortemperature\|colorbalance'`; upgrade the binary (`NEXUS_FFMPEG_BIN` overrides the lookup) |
| Duration of the master changed after grading | Regression — exposure must be duration-neutral | Run `pytest -q tests/unit/test_lane_duration_algebra.py`; it fails on any algebra slip |

## 5. Changing a bound

The golden tests pin FFmpeg's declared ranges as **contracts**. If FFmpeg
narrows or widens them, `tests/unit/test_rendering_lane_exposure.py` goes red on
purpose — a silent upstream range change must not reach a master file
unreviewed. To move a bound deliberately: update the constant in
`creative/rendering/ir.py`, the matching golden assertion, the table in §1, and
the D-0007 source note in the same commit.
