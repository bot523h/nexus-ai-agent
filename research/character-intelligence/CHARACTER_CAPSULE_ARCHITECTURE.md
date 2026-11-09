# Character Capsule — Architecture

## One sentence

A **Character Capsule** is a sealed, versioned record of who a character is
(immutable identity) plus what may legitimately change (revisions), paired with an
**independent** verifier that turns a shot's pixels and its declarations into one
of eight explicit verdicts backed by hashes.

## Layers

```
 shot metadata ──┐                            ┌── verdict (8 states)
 frame bytes ────┼──▶ observe() ──▶ features ─┼── dimension distances
 capsule ────────┤        (vision)            ├── conflicts (canon, wardrobe plan)
 calibration ────┘                            └── evidence bundle (hashes)
```

### 1. Capsule model (`model.py`, `wardrobe.py`, `canon.py`)

* `CharacterCapsule` = `character_id` + `identity_version` + `revision_no` +
  modules (`face`, `body`, `hair`, `voice`, `motion`, and optionally `wardrobe`,
  `canon`) + `asset_refs`, sealed with a scoped digest.
* Every module declares `FIELD_SCOPES`: `IDENTITY` fields cannot be edited through
  `apply_changes` (that raises `ScopeViolation`); the only door is
  `new_identity_version(...)`, which bumps `identity_version`.
* `WardrobeModule` holds outfits (each with a version and an era), sequence
  baselines, ordered change events and scene rules.
* `CanonModule` holds immutable facts, mutable facts and an append-only
  `story_events` ledger whose effects persist for all later scenes.

### 2. Measurement (`vision.py`, `audio_motion.py`)

Classical, deterministic, dependency-light (numpy + our own PNG/WAV codecs):

* white-balance (grey-world, bounded gains) → skin mask → **eye-anchored square
  crop** (landmark alignment, not bounding-box alignment);
* face groups: high-pass chroma structure (illumination removed), per-cell
  normalised gradient histogram, geometry (interocular, jaw, nose, mouth, eye
  energy), skin/eye chroma;
* body groups: silhouette ratios + 16-point width profile from a per-row
  background estimate;
* wardrobe groups: garment chroma, three vertical bands, pattern energy;
* scene: background colour of the border;
* voice/motion: signature and gait feature vectors.

Distances are **per-group z-scored RMS, then averaged with fixed weights** so a
128-dimension gradient group cannot outvote a 7-dimension geometry group.

### 3. Verification (`continuity.py`)

* Thresholds are **fitted**, not chosen: `accept` = genuine p95, `reject` =
  impostor p05, with a declared minimum band of 0.10 and a declared acceptance
  floor of 0.10. Overlap is reported, never hidden.
* Quality gates (ISO/IEC 29794-5 style): a frame below the gate is
  `NOT_MEASURED`, and the verdict floors at `INSUFFICIENT_EVIDENCE` instead of
  claiming drift.
* `REQUIRED_BY_FRAMING`: a portrait must measure face/hair/wardrobe; a full-body
  frame must measure body. A missing required dimension cannot be "verified".
* Canon-declared appearance markers (the scar EVT-001 gives) downgrade a face
  drift to `REVIEW_REQUIRED`, never to `VERIFIED`.

### 4. Evidence (`evidence.py`)

`EvidenceBundle` = subject + assertions + content-addressed records + **one** hard
binding (digest over the ordered record set, with explicit exclusions) + lineage
(capsule digest, corpus digest) + optional detached Ed25519 signature. The shape
maps onto C2PA 2.x (`c2pa.hash.data` + claim signature); it is a mapping, not a
C2PA implementation.

## Data flow for one shot

1. Load capsule at the revision the shot pins; `capsule.verify()` checks the seal.
2. `observe()` the frame and every gallery reference.
3. Face: min distance over gallery, gated by quality → band → state.
4. Body/hair/wardrobe: same, restricted to frames where the dimension is
   measurable (a full-body frame has no measurable wardrobe here — stated).
5. Scene: measured background vs the scene's declared environment.
6. Canon + wardrobe plan: typed conflicts.
7. Aggregate by `STATE_PRECEDENCE`, apply the missing-required rule, emit report
   with `calibration_digest`, `capsule_digest`, `evidence_digest`.

## Why the verifier is independent

It shares **no code** with the renderer that produced the frames: it re-measures
pixels. It also shares no code with Arena 1. A verifier that imports the generator
would verify its own assumptions.

## Deliberate non-goals

No render engine, no composition compiler, no new queue/CommandBus/registry, no
paid API, no character marketplace, no full season. See
[ADR-0005](adr/ADR-0005-no-arena1-coupling.md).
