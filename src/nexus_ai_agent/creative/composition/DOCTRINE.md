# AETHEL composition — doctrine (Wave 2)

**Status:** EXPERIMENTAL analysis/composition layer. Renders nothing; owns no
project state; dispatches no command.

## 1. Exact rational time

`RationalTime(value: int, rate: int)` and `TimeRange(start, duration)` are exact
(`fractions.Fraction` internally). Canonical rates: `24000/1001` (23.976),
`30000/1001` (29.97), `60000/1001` (59.94), `24`, `25`, `30`, `48000`, `96000`.
`rescale` refuses a conversion that does not land on an exact tick
(`RateMismatchError`) — there is never a silent fractional-frame rounding, so a
multi-hour NTSC timeline accumulates **zero** drift.

## 2. TimelineDAG

Typed nodes (`track`, `clip`, `transition`, `effect`, `audio_bus`) and typed
edges. `topological_order` is a deterministic Kahn sort that raises
`CyclicGraphError` naming the actual remaining nodes. `total_frames` is the
critical-path length.

## 3. Time warp

A monotone uniform cubic Bezier with control points `(t1, tau1)`, `(t2, tau2)`
maps destination `t in [0,1]` to source `tau in [0,1]`. The inverse uses
Newton-Raphson with the analytic derivative, with a bisection fallback to
`1e-9`. A decreasing `tau` is refused unless `reverse=True`.

## 4. J/L cuts

`JLCut(video_start_frame, audio_start_frame, overlap_frames)` models an
independent audio pre-lap; `crossfade_gains(alpha) = (cos(alpha*pi/2),
sin(alpha*pi/2))` is the constant-power crossfade.

## 5. Porter-Duff

`composite(op, src, dst)` mixes premultiplied-alpha linear-radiance float32
buffers. The 12 classic operators use the exact `(Fa, Fb)` table below; `PLUS`,
`MULTIPLY` and `SCREEN` use their linear-light kernels. `check_premultiplied`
enforces `0 <= C <= alpha <= 1` and rejects NaN/Inf.

| Operator | Fa | Fb |
|---|---|---|
| Clear | 0 | 0 |
| Src | 1 | 0 |
| Dst | 0 | 1 |
| SrcOver | 1 | 1-a_s |
| DstOver | 1-a_d | 1 |
| SrcIn | a_d | 0 |
| DstIn | 0 | a_s |
| SrcOut | 1-a_d | 0 |
| DstOut | 0 | 1-a_s |
| SrcAtop | a_d | 1-a_s |
| DstAtop | 1-a_d | a_s |
| Xor | 1-a_d | 1-a_s |

`srgb_to_linear`/`linear_to_srgb` are the exact IEC 61966-2-1 EOTF pair.

## 6. ATLAS adapter

`compile_atlas_plan_to_dag(plan)` turns an ATLAS `PlanResult` into a linear
TimelineDAG of clip nodes chained with `finish_to_start` edges.
