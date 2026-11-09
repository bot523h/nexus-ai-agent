# Capsule Schema — `character-capsule/1`

Status: **VERIFIED** (produced and re-parsed by the prototype; sealed digests
verified in `tests/character_capsule/test_capsule_model.py`).

## Scopes and prefixes

All digests are `sha256:<hex>` over UTF-8 canonical JSON
(`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`). Domain
separation uses a scope prefix, so a module digest can never be replayed as a
capsule digest:

```
nagar.character-capsule.v1/capsule\n
nagar.character-capsule.v1/face\n
nagar.character-capsule.v1/body\n
nagar.character-capsule.v1/hair\n
nagar.character-capsule.v1/voice\n
nagar.character-capsule.v1/motion\n
nagar.character-capsule.v1/wardrobe\n
nagar.character-capsule.v1/canon\n
nagar.character-capsule.v1/calibration\n
nagar.character-capsule.v1/continuity-report\n
```

## Top level

```json
{
  "schema": "character-capsule/1",
  "character_id": "NAGAR-CHAR-000001",
  "identity_version": 1,
  "revision_no": 3,
  "digest": "sha256:…",
  "note": "human-readable revision note",
  "modules": { "face": {…}, "body": {…}, "hair": {…}, "voice": {…}, "motion": {…},
               "wardrobe": {…}, "canon": {…} },
  "module_digests": { "face": "sha256:…", "…": "sha256:…" },
  "assets": [ { "asset_id": "AST-0001", "kind": "image/png",
                "digest": "sha256:…", "bytes": 12345,
                "consent_ref": "CNS-0001", "synthetic": true } ]
}
```

`digest` covers `{character_id, identity_version, revision_no, note, module digests,
assets}` — never the module payloads themselves, so a single module edit moves
exactly one module digest plus the capsule digest.

## Modules

| module | identity-scoped (immutable) | revision-scoped (mutable) |
|---|---|---|
| `face` | `eye_colour`, `eye_spacing_ratio`, `eye_size_ratio`, `jaw_width_ratio`, `nose_length_ratio`, `nose_width_ratio`, `mouth_width_ratio` | `brow_thickness_ratio`, `skin_colour`, `skin_texture`, `freckles` |
| `body` | `shoulder_width_ratio`, `hip_width_ratio`, `limb_thickness_ratio` | `height_cm`, `posture`, `build` |
| `hair` | — | `style`, `colour`, `length`, `fringe` |
| `voice` | `f0_hz`, `timbre` | `speaking_rate`, `breathiness`, `accent` |
| `motion` | `gait` (cadence, stride, sway) | `posture_habit` |
| `wardrobe` | outfit *versions* are immutable once minted | baselines, change events, scene rules |
| `canon` | `IMMUTABLE` facts cannot be edited or removed | `MUTABLE` facts change through a revision |

## Wardrobe payload

```json
{
  "revision": 1,
  "outfits": {
    "OUT-0001": {"outfit_id": "OUT-0001", "version": 1, "label": "harbour coat",
                  "top_colour": "#3d5a73", "bottom_colour": "#2f3a46",
                  "accent_colour": "#c9d1d9", "material": "wool",
                  "accessories": ["scarf"], "era_from_scene": 1, "era_to_scene": null}
  },
  "sequence_baselines": {"SEQ-EP1-A": "OUT-0001"},
  "change_events": [{"event_id": "WCE-0001", "sequence_id": "SEQ-EP1-A",
                     "from_shot_index": 3, "outfit_id": "OUT-0002",
                     "reason": "story beat"}],
  "scene_rules": {"SC-003": "OUT-0002"}
}
```

## Canon payload

```json
{
  "revision": 1,
  "facts": {"FACT-001": {"fact_id": "FACT-001", "predicate": "eye_colour",
                          "value": "#3c5f8a", "kind": "IMMUTABLE"},
            "FACT-004": {"fact_id": "FACT-004", "predicate": "scar",
                          "value": "left_cheek", "kind": "MUTABLE",
                          "added_at_scene": 3}},
  "story_events": [{"event_id": "EVT-001", "kind": "injury", "scene_order": 3,
                    "description": "cliff-road fall leaves a scar on the left cheek",
                    "effects": {"scar": "left_cheek"}}],
  "biography": "…", "personality": ["watchful", "dry humour"],
  "relationships": {"NAGAR-CHAR-000003": "estranged colleague"}
}
```

## Continuity report — `character-capsule-continuity-report/1`

```json
{
  "schema": "character-capsule-continuity-report/1",
  "shot_id": "SH-109",
  "character_id": "NAGAR-CHAR-000001",
  "capsule_revision": 1,
  "capsule_digest": "sha256:…",
  "calibration_digest": "sha256:…",
  "verdict": "IDENTITY_DRIFT",
  "dimensions": [{"dimension": "face", "band": "DRIFT", "distance": 0.449,
                   "score": 0.690, "state": "IDENTITY_DRIFT", "measured": true,
                   "detail": "…"}],
  "conflicts": [],
  "limitations": ["…"],
  "processing_ms": 0.0,
  "evidence_digest": "sha256:…"
}
```

## Versioning rules

1. Adding a **new optional field** is a minor change: readers ignore unknown keys.
2. Changing a scope of an existing field is a **breaking** change: it must bump
   `identity_version` for every affected character and ship a schema `…/2`.
3. `capabilities`/`schema` strings are part of the digest input: a schema change
   therefore invalidates old digests by construction, which is intended.
4. Calibration artifacts are versioned separately (`character-capsule-calibration/1`)
   because a threshold is a property of a *dataset*, not of a character.
