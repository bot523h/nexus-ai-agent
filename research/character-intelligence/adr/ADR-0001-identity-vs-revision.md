# ADR-0001 — Identity is immutable, appearance is a revision

- Status: **Accepted** (implemented and tested)
- Date: 2026-11-09
- Context: Character Capsule mission (ARENA-B)

## Context

A character in a series changes constantly: haircut in episode 2, coat in episode 3,
scar after episode 3, voice maturing across a season. A single "character version"
number cannot express this, and a single mutable record cannot answer the only
question that matters later: *did the person change, or did the production drift?*

## Decision

Split the record into two axes:

1. **`identity_version`** — the invariants of who the character is (face geometry,
   eye colour, body proportions). Only `new_identity_version()` may change them.
2. **`revision_no`** — everything else (hair, skin tone, wardrobe, voice pacing,
   posture, canon facts), changing through `apply_changes()`.

Every field of every module declares its scope (`FIELD_SCOPES`). A change to an
`IDENTITY` field raises `ScopeViolation` before any module is rebuilt, so a rejected
batch leaves the capsule untouched.

## Consequences

* A shot can pin `(character_id, identity_version, revision_no, capsule_digest)` and
  remain meaningful forever; a later revision cannot retroactively change what the
  shot was verified against.
* "The character changed" becomes an auditable event (a new identity version with a
  note), not an edit.
* Cost: two numbers plus a digest instead of one, and callers must choose the right
  door. `RevisionConflict` (optimistic concurrency) is required to prevent silent
  overwrites.

## Alternatives rejected

| alternative | why rejected |
|---|---|
| one `version` integer for everything | cannot distinguish "new haircut" from "new person"; makes drift detection meaningless |
| free-form JSON patch with no scopes | the most common edit ("just fix the jaw") becomes irreversible identity loss |
| separate capsule per appearance | combinatorial explosion; wardrobe × lighting × episode |

## Evidence

`experiments/character_capsule/model.py`, `tests/character_capsule/test_capsule_model.py`
(16 tests, including the two `ScopeViolation` refusals and `RevisionConflict`).
