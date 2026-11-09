# Visual Continuity Evaluation — Design

Status: **VERIFIED** — implemented in `continuity.py`, exercised by 127 tests and a
12-scenario benchmark.

## 1. The unit of decision is a *dimension*, not a score

A single "similarity 0.87" cannot tell a reviewer whether the face drifted or the
coat changed. Each dimension is measured, banded and stated separately:

| dimension | measured from | applicable when |
|---|---|---|
| `face` | eye-anchored crop: high-pass chroma, per-cell gradients, geometry, skin/eye tone | a face is present and above the quality gate |
| `body` | silhouette ratios + 16-point width profile | framing is full-body and body quality > gate |
| `hair` | hair-region shape + tone relative to skin | portrait with a localised face (**advisory**: no threshold) |
| `wardrobe` | garment chroma, 3 vertical bands, pattern energy | portrait with a localised face |
| `scene` | border background colour vs the scene's declared environment | always |
| `voice` | signature vector (F0, timbre, rate, breathiness) | audio supplied |
| `motion` | gait vector over a sequence | sequence supplied |
| `expression` | mouth-curvature proxy (sign only) | declared expression has a sign expectation |
| `canon` | typed conflicts from the canon engine | always |

## 2. Three bands, fitted from data

```
        accept                 reject
  ────────┼──────────────────────┼────────▶ distance
   MATCH         REVIEW              DRIFT
```

* `accept` = p95 of genuine distances (per dimension), floored at `MIN_ACCEPT = 0.10`
* `reject` = p05 of impostor distances, widened to at least `MIN_BAND = 0.10`
* overlap (`accept > reject`) is **reported**, never hidden by forcing a single
  threshold; the boundary becomes the midpoint and both errors are stated.

Why fitted: a threshold is a property of a corpus and a descriptor. Copying one
from another system is how a verifier ends up confidently wrong.

## 3. What counts as genuine and impostor (per dimension)

| dimension | genuine | impostor |
|---|---|---|
| face | same character, condition changed (angle, expression, outfit2) | other characters **and** the six deliberate drift variants |
| body | same character, different full-body take | other characters' full-body frames |
| wardrobe | **within one character**: same outfit | **within one character**: changed outfit (identity calibration is wrong here — two people may share a costume) |
| scene | frames sharing a declared background | frames from a different declared background |
| voice/motion | same character, session variants | cross-character |

The wardrobe row is the subtle one: calibrating costume change on *different
people* would make "wearing the wrong coat" almost invisible, because most
different people wear different clothes anyway.

## 4. Quality gates (ISO/IEC 29794-5, pruning not scoring)

`QUALITY_GATES = {face: 0.35, body: 0.50, hair: 0.30, wardrobe: 0.25}`. A frame
below its gate is reported `NOT_MEASURED`; if it is a required dimension for the
framing, the verdict floors at `INSUFFICIENT_EVIDENCE`. A clipped, tiny or
unusable frame must never produce `IDENTITY_DRIFT`.

Face quality itself is measured, not declared: it combines the face area fraction
with a clipping penalty (fraction of pixels at 0/255), so a blown highlight lowers
quality instead of silently shifting the features.

## 5. Eight states and their precedence

```
CANON_CONFLICT > IDENTITY_DRIFT > WARDROBE_DRIFT > BODY_DRIFT
> REVIEW_REQUIRED > INSUFFICIENT_EVIDENCE > VERIFIED_WITH_LIMITATIONS > VERIFIED
```

`INSUFFICIENT_EVIDENCE` is deliberately ranked *above* the verified states: absence
of evidence is not evidence of continuity.

## 6. Policies that stop the engine from lying

1. **Silence is not contradiction**: a shot that omits a persistent canon effect
   yields `STORY_EFFECT_NOT_ASSERTED` → `REVIEW_REQUIRED`, not `CANON_CONFLICT`.
2. **Canon-declared appearance changes** (EVT-001's scar) may downgrade a face
   drift to `REVIEW_REQUIRED`, never to `VERIFIED`.
3. **Advisory dimensions** (hair today) are reported with their distance but
   cannot move the verdict, because no threshold could be calibrated for them.
4. **Full-body frames** cannot measure hair/wardrobe in this prototype (no garment
   segmenter) and say so; they are not silently scored as "no change".
5. **Expression** is a curvature proxy: it can confirm a declared *sign* for
   smile/sad, and it abstains (NOT_MEASURED) for neutral/angry/surprised.

## 7. Metrics reported

* per dimension: `genuine_n`, `impostor_n`, genuine p95, impostor p05, EER on the
  calibration split;
* on the **held-out character**: FAR/FRR/TAR at the accept boundary and EER;
* scenario pass rate against declared acceptable outcome sets;
* environment (wall time, peak RSS, GPU `NOT_MEASURED`);
* everything unmeasurable written as `NOT_MEASURED` with a reason string.

## 8. Known weaknesses (measured, not suspected)

* face held-out FAR **0.600**, FRR **0.250** (4 genuine / 10 impostor) — weak.
* hair: no calibrated threshold (no hair-change samples).
* wardrobe and body separate cleanly (wardrobe EER 0.0 on holdout, body EER 0.0),
  but both have very few samples (n=3–6 per class).
* voice/motion: n=1 impostor sample each — EER 0.0 is **not** meaningful there.
