# ADR-0009 — One door for identity, immutable outfit versions, verified shot pins

- Status: **Accepted** (implemented and tested at `757dafc`)
- Date: 2026-11-09
- Closes: F20, F21, F22, F28, F29, F30

## Context

The capsule advertised a distinction between *identity* (who the character is) and
*revision* (what happened to them this episode) — ADR-0001. The implementation leaked:

* `with_module` accepted a change to `eye_spacing_ratio`, so an identity change was
  expressible as an ordinary revision (F28).
* the documented identity door raised `TypeError` for a voice recast (F29).
* `with_outfit` rewrote a released outfit version in place, so a shot pinned to
  `OUT-0001` v1 could resolve to different colours later (F30).
* a shot's pins were never checked by the evaluator, so forged digests still scored
  (F20).
* a missing timeline silently became "scene 1" (F21), and scene wardrobe rules were
  stored but never consulted (F22).

## Decision

1. **Field scopes are data**, declared per module (`Scope.IDENTITY`/`REVISION`/
   `APPROVAL`). Two independent guards enforce them: `module.updated()` refuses an
   identity field outright, and `with_module` compares every identity field of the
   proposed module against the current one and raises `ScopeViolation`.
2. **`new_identity_version(face_changes=, body_changes=, voice_changes=)` is the only
   door.** It validates each field's scope, bumps the touched module's revision and
   `identity_version` and the capsule revision together, then re-seals.
3. **Outfit versions are immutable.** A same-version change raises; a new version must
   be higher and records `supersedes = ("<version>:<content_digest>", ...)`.
4. **The evaluator verifies pins before measuring.** `validate_capsule_pins` runs
   first and raises `PinMismatch`; the report records both the pinned and the verified
   revision, so the agreement is checkable after the fact.
5. **Unknown fields in a shot manifest are refused**, because a pin the loader silently
   drops is a pin that does not apply.
6. **No timeline means `NOT_MEASURED`** for the story-dependent dimensions, with the
   reason in `limitations`; nothing assumes scene 1.
7. **Scene rules take precedence** over sequence baselines and change events in
   `expected_outfit(..., scene_id=...)`, because a scene rule is a canonical statement
   about that scene.

## Consequences

* An identity change requires an explicit, auditable act; a revision cannot smuggle one.
* Old shots stay resolvable: their outfit version and content digest are still there.
* A shot authored against an older capsule is refused instead of verified against the
  wrong one, and the verifier's answer is about the shot that was authored.
* When story order is unknown, wardrobe and canon are reported unmeasured rather than
  guessed; the visual dimensions still produce a verdict.

## Alternatives considered

* **Only guard inside `with_module`.** Insufficient: `updated()` would still hand out a
  module with a changed identity field, and any other caller could use it.
* **Version-free outfits ("latest wins").** Simpler, and it silently rewrites history —
  the defect F30 describes.
* **Trust the shot's pins.** Then a pin is decoration and the verifier answers a
  question nobody asked.
