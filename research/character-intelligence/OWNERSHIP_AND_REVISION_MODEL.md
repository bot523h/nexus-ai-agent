# Ownership and Revision Model

Status: **VERIFIED** — the rules below are enforced in code and tested
(`tests/character_capsule/test_capsule_model.py`, 16 tests).

## Two axes, never one

| axis | question | door | effect |
|---|---|---|---|
| `identity_version` | *who is this?* | `new_identity_version(face_changes=…, body_changes=…)` | a new person-like identity; old shots stay pinned to the old version |
| `revision_no` | *what changed about them?* | `apply_changes([…])`, `with_module(…)` | hair, wardrobe, voice pacing, posture, canon facts |

A capsule is `(character_id, identity_version)`; `revision_no` is a pointer to the
current record of that identity. Shots pin **both** plus the capsule digest and
every module digest (see `CAPSULE_SCHEMA.md`).

## The refusal that makes it real

```text
capsule.apply_changes([ChangeIntent(field_path="face.jaw_width_ratio", value=0.31)])
→ ScopeViolation: face.jaw_width_ratio is IDENTITY-scoped; mint a new identity version instead
```

The prototype does not merely document this rule; `ScopeViolation` is raised before
any module is rebuilt, so a rejected batch leaves the capsule untouched (all or
nothing). `RevisionConflict` is raised when the caller's `expected_revision` no
longer matches — the optimistic-concurrency check that keeps two editors from
silently overwriting each other.

## Roles and permissions

Implemented in `security.py` as a role × permission matrix with an audit trail that
records **refusals too**:

| role | may |
|---|---|
| `VIEWER` | read metadata, read reports |
| `EDITOR` | revise mutable fields; cannot touch identity, cannot export |
| `DIRECTOR` | approve a new identity version, author wardrobe timeline |
| `SECURITY` | rotate keys, revoke consent, read the audit trail |
| `EXPORTER` | export *only* with a valid consent record and no revocation |

## Consent as a gate, not a checkbox

`ConsentLedger.require(character_id, purpose)` returns a record or raises
`ConsentRequired` / `ConsentRevoked`. Revocation is terminal for protected
operations (export, render) and is written to the audit trail. For synthetic
characters the ledger stores an explicit synthetic declaration instead, so the
absence of consent is never silently interpreted as permission.

## Revision history

* The database schema in `storage.py` keeps `identity_versions` (immutable rows,
  keyed by `(character_id, version)`) and `capsule_revisions` (append-only rows).
* A revision row records: `revision_no`, `created_at`, `author`, `note`,
  `diff` (field path → old/new), and the resulting capsule digest.
* **Deleting** an identity version is refused by the API; superseding it is a new
  version number. This is what makes "the character changed in episode 4" auditable.

## Ownership

Ownership is by *mission zone*, not by character:

| object | owner | may delegate |
|---|---|---|
| capsule + revision history | the character's owning zone | editors (mutable fields only) |
| calibration artifact | the verification owner | nobody (a threshold is a shared contract) |
| evidence bundles | the producing run | nobody (append-only) |

A character cannot be "transferred" by copying a JSON file: the copy fails
`capsule.verify()` against the calibration it was signed with unless the digests
match, and a copied capsule without its consent records cannot be exported.

## What is deferred

* Multi-tenant key custody, HSM-backed signing and per-character key rotation:
  **DEFERRED** (needs a real KMS, which this environment does not have).
* Approval workflows (two-person rule for identity changes): **DEFERRED**, the role
  matrix is the placeholder.
* Merge of divergent revisions: **DEFERRED** — revisions are append-only, so a
  merge would be a new revision derived from both, and that needs product input.
