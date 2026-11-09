# Benchmark Dataset Specification — `character-capsule-corpus/1`

Status: **VERIFIED** — generated deterministically by
`experiments/character_capsule/dataset.py`; corpus digest
`sha256:b62debe7…2bc1` (63 frames) reproduced across runs.

## Why procedural

Photographic faces would need consent and a licence that this environment cannot
establish, and a research-only weight pack is not admissible (see
`LICENSE_AND_MODEL_WEIGHT_MATRIX.md`). The corpus is therefore rendered from
parametric character specs: cheap, deterministic, and **fully disclosable** — a
reviewer can read exactly what each frame is.

Consequence, stated in every result document: thresholds are **not** transferable
to photographs (`NOT_MEASURED`).

## Characters

| id | role | why it exists |
|---|---|---|
| `NAGAR-CHAR-000001` | protagonist (Mahtab Ravi) | positive class, capsule subject |
| `NAGAR-CHAR-000002` | look-alike (Sahar Ravi) | hard impostor: same hair, same wardrobe family, close geometry |
| `NAGAR-CHAR-000003` | distinct (Baran Dehqan) | **held-out**: never used to fit a threshold |

Split: characters 1–2 calibrate, character 3 is held out (`character_split`).

## Frames per character

| key pattern | purpose | condition |
|---|---|---|
| `-ref-front` | reference | same camera, 5600 K |
| `-ref-three-quarter` | reference_alt | yaw 32° |
| `-ref-profile` | reference_alt | yaw 72° (hard case) |
| `-ref-warm` / `-ref-cool` | reference_alt | 3200 K @1.15 / 7800 K @0.85 |
| `-ref-smile` | reference_alt | expression changed |
| `-ref-outfit2` | reference_alt | second registered outfit |
| `-ref-fullbody` | reference_alt | full-body, mid-tone backdrop |
| `-ref-fullbody-warm` | reference_alt | full-body, warm key |
| `-drift-{eye_spacing,jaw_width,skin_tone,eye_colour,nose,mouth}` | drift | deliberate identity change, **labelled impostor** |
| `-shot-angle` | eval | yaw 32°, different take (intensity 1.06) |
| `-shot-light` | eval | 3400 K @1.18 (clipping case) |
| `-shot-expression` | eval | smile |
| `-shot-outfit2` | eval | second outfit |
| `-shot-environment` | eval | different set (6200 K, `#6b7280`), scar present, sad |
| `-shot-fullbody` | eval | full-body, slouched |

63 frames = 3 characters × 21 frames. Frames are 384×768 PNG.

## Labelling rules

1. `purpose` is *provenance*, not truth: `reference` = canonical gallery,
   `reference_alt` = additional gallery, `drift` = deliberate identity change,
   `eval` = probe.
2. A probe is **genuine** only against its own character's gallery; `drift` frames
   are **impostor against their own character** — that is the whole point of them.
3. The **canon-marker** frame (`-shot-environment`) is excluded from identity
   calibration: the scar is declared by the story, so the difference is expected.
4. **Quality-pruned** frames (face below the gate) are excluded from calibration and
   listed in `calibration.pruned_probes`.
5. Gallery comparison is **min over references**, matching what the evaluator does
   at verification time. A single-reference calibration would fit a different,
   harder problem than the one deployed.

## Leakage controls

* Every `eval` frame is rendered with a distinct exposure (intensity 1.06) so no
  probe is bit-identical to a gallery frame.
* `ref-three-quarter` and `shot-angle` share a view but not an exposure; this is
  stated because it makes the "angle" scenario easier than a real angle change.
* The look-alike shares hair and wardrobe *by design* — its separation must come
  from face geometry and tone, not from clothing.

## Determinism

The corpus is a pure function of its spec table; the digest covers frame bytes plus
the labelling metadata. Two runs into different directories produce the same digest
(test: `test_corpus_digest_is_reproducible`).

## What the corpus does **not** contain

| missing | consequence |
|---|---|
| photographs | no claim about real-world accuracy |
| occlusion (coat over bag, mask) | occlusion robustness untested |
| hair changes | hair has no calibrated threshold |
| more than one look-alike | FAR estimate rests on a single hard impostor |
| video (true motion) | gait features come from a synthetic sequence, `EXPERIMENTAL` |
| crowd scenes / multiple people | single-subject assumption |

Extending the corpus is the first item in `ROADMAP_NOW_NEXT_LATER.md`.
