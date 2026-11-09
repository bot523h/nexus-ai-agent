# Security hardening report — Character Capsule (PR #326 remediation)

| field | value |
|---|---|
| code under review | `5fa6470` |
| fix commit | `757dafc` |
| board task | `task-nagar-character-intelligence-02` (generation 2) |
| findings | 31 reported, 30 confirmed and closed, 1 not reproducible (`FINDINGS_TRIAGE.md`) |
| regression tests | `tests/character_capsule/` — 127 passed |
| evidence run | `experiments/character_capsule/.work/results/final-757dafc` (regenerate with the command in `TEST_EVIDENCE.md`) |

This document states the **contracts** that the fixes establish. The triage table
holds the per-finding evidence; this file holds the model a reviewer needs in
order to judge whether the contracts are enforced at the right boundary.

---

## 1. The four boundaries

Everything in this island is about who may read, change, publish and vouch for a
character. The hardened design has exactly four boundaries, and each one has a
single enforcement point:

| boundary | question | enforced by | failure mode |
|---|---|---|---|
| **authorization** | may this principal do this at all? | `AccessPolicy.require(principal, permission)` — called before any I/O | `AccessDenied`, audited with `allowed=false` |
| **consent** | does the subject's consent cover *this* use? | `ConsentLedger.decide(character, purpose, right, territory)` at `now` | `ExportBlocked` with the reason string that lands in the audit log |
| **containment** | can this path or id leave its owner? | `_read_bundle_file` (bundle-relative, no symlinks, re-hashed) + asset ownership checks | `ValidationError` / `IntegrityError`, nothing written |
| **authenticity** | does a trusted party vouch for these bytes? | `TrustAnchor` supplied by the consumer, never read from the bundle | `UNTRUSTED_SIGNER`, `INCOMPLETE`, `NOT_VERIFIABLE` or `INVALID` — never `VALID` |

The ordering is deliberate and tested: **permission → consent → read → write**, and
**structure → bytes → signature** on the verification side. A refusal at any step
leaves the destination untouched (`F18`).

## 2. Cryptography: the NCC2 envelope

```
"NCC2" | version=2 | header_len(2) | header(canonical JSON)
       | kek_nonce(24) | wrapped_key(48) | data_nonce(24) | body
```

* A fresh 32-byte data key per blob, sealed with XSalsa20-Poly1305 under the KEK.
* The header (`{purpose, digest, media_type, plaintext_bytes, algorithm, …}`) is
  **associated data**: changing it invalidates the tag instead of being ignored.
* `key_id = sha256(kek)[:23]` is a non-secret identifier, recorded next to each
  blob and reported by `storage_policy()`. Rotating the KEK is therefore a
  detectable event, not a silent decryption failure.
* The KEK comes from `CHARACTER_CAPSULE_KEK_FILE`. **No key material is stored in
  the repository.** A missing or malformed key file is `KeyUnavailable`.
* Encryption is the default. A store without a cipher refuses to start
  (`CryptoUnavailable`); plaintext requires `allow_plaintext="<reason>"`, which is
  written to `meta` and echoed by `storage_policy()["plaintext_reason"]`.

Residual risk, stated: XSalsa20-Poly1305 binds the ciphertext, not the *file name*
or the row that points at it. A blob swapped for another blob of the same content
type is detected by the recorded `sha256` (checked on every read) but not by the
AEAD alone. That is acceptable because the digest is the identity of the blob.

## 3. Consent model

* A record is `(consent_id, character_id, subject_reference, purposes[], rights[],
  territory, granted_at, expires_at?, document_digest, revoked_at?)`.
* **Revocation is terminal.** The same `consent_id` can never be re-issued — not by
  `add`, not by import, not by a reopened database (`F09`, `F10b`).
* All timestamps are parsed to aware UTC `datetime` before comparison (`F11`).
* `territory` accepts `*` / `WORLDWIDE` / `GLOBAL`, otherwise a comma-separated
  list, compared case-insensitively. `rights` is a closed vocabulary
  (`CONSENT_RIGHTS`), so a typo cannot grant anything.
* **Import never mints authorization.** An imported consent record lands in
  `consent_imports` (inert). `adopt_imported_consent(consent_id, principal)` needs
  `MANAGE_CONSENT` *and* a `document_digest`, is recorded in lineage, and refuses a
  second adoption (`F13`, `F17`).
* Synthetic subjects are exempt by **declared subject class**, not by absence of a
  record — and the exemption is written into the export manifest with its reason.

## 4. Import / export containment

* Export: `EXPORT` permission → consent decision → read assets → write to
  `.<name>.staging-<pid>-<rand>` → `os.replace`. A failure at any point removes the
  staging directory and leaves the previous destination byte-identical (`F18`).
* The manifest records, for each asset: `asset_id`, `sha256`, `file`, and
  `bundle_sha256` (digest of the decrypted bytes actually written).
* Import validates the **whole** bundle before writing anything: schema, character
  id, asset ownership, per-file containment, per-file digest, capsule re-seal and
  `verify()`.
* Refused by construction: absolute paths, `..` segments, symlinks that resolve
  outside the bundle, a file whose bytes do not match `bundle_sha256`, and an
  `asset_id` already owned by another character (`F03`, `F19`).

## 5. Evidence: integrity is not authenticity

Six states, in severity order, with the exact meaning a consumer may rely on:

| state | meaning | exit code |
|---|---|---|
| `INVALID` | the recorded bytes changed, or the bundle contradicts itself | 1 |
| `UNTRUSTED_SIGNER` | signed, but not by the key the consumer trusts | 4 |
| `NOT_VERIFIABLE` | a signature exists but cannot be checked here | 5 |
| `INCOMPLETE` | the bundle describes artifacts that were not supplied | 3 |
| `UNSIGNED` | integrity is checkable, authenticity was never established | 2 |
| `VALID` | structure + every artifact byte + a *trusted* signature | 0 |

Notes that matter in practice:

* A bundle that carries its own public key is **never** `VALID` (`F06`). The
  embedded key is checked (so an edited claim is still detected as `INVALID`), but
  trust must come from outside.
* `--expect-key-id` pins the signing key id, so a rotated key does not silently
  inherit the old key's trust.
* `binding_scope` is published inside the bundle, including what is **excluded**
  (`benchmark_results.json`, because it contains the bundle digest — binding it
  would be circular).
* `digest_scope` is published for the calibration and for the results document, so
  a reader can recompute the digest and see exactly which fields were excluded
  (`created_at`, absolute paths, `processing_ms`).

## 6. Identity and shot binding

* Field scopes are declared per module (`Scope.IDENTITY` / `REVISION` / `APPROVAL`).
  `with_module` refuses a change to any `IDENTITY` field (`ScopeViolation`), and
  `module.updated()` refuses it earlier — two independent guards, both tested.
* `new_identity_version(face_changes=, body_changes=, voice_changes=)` is the only
  code path that advances `identity_version`; it bumps the touched module's revision
  and the capsule revision together, so the pair cannot drift.
* Outfit versions are immutable. A new version must be higher and records
  `supersedes` with the previous `version:content_digest`.
* A shot carries its own pins (capsule revision, capsule digest, per-module
  digests). `ContinuityEvaluator.evaluate` verifies them **before measuring**
  (`PinMismatch`), and the report records both the pinned and the verified revision.
* Without a timeline the story-dependent dimensions are `NOT_MEASURED`; the
  report says so instead of assuming scene order 1 (`F21`).

## 7. Bounded decoding

`DecodeLimits` is checked from the header before allocation:
`max_width/max_height` (8192), `max_pixels` (16 MP), `max_input_bytes` (32 MB),
`max_idat_bytes` (48 MB), `max_decompressed_bytes` (96 MB), `max_audio_frames`
(10 min @ 48 kHz). A 8000×8000 header in 186 KB is refused in microseconds (`F08`).

## 8. What an attacker still can do

Stated explicitly, because a report that claims none is not credible:

1. **Sign with their own key.** They will get `UNTRUSTED_SIGNER`, not `VALID`. If a
   consumer is configured to trust that key, that is the consumer's decision.
2. **Replace the whole bundle and its files consistently.** Every internal check
   passes; only the trust anchor distinguishes it. Provenance beyond the anchor
   (e.g. public transparency logs) is out of scope.
3. **Read a plaintext store.** If an operator passes
   `allow_plaintext="<reason>"`, the bytes are readable — but the reason is stored
   and reported, so the state is visible rather than assumed.
4. **Replay an old, valid, correctly signed bundle.** No freshness/expiry claim is
   made; `created_at` is recorded but is not a revocation mechanism.
5. **Exploit the classical face descriptor.** Face FAR on the held-out character is
   0.6 at the accept boundary. This is a measurement limitation, disclosed in the
   benchmark, not a control that is claimed to work.

## 9. Non-goals (unchanged, per mission constraints)

No edits to `src/nexus_ai_agent/**`, `pyproject.toml`, CI configuration or
`docs/architecture/**`; no new queue/ledger/CommandBus/parallel authority; no
feature work outside hardening; Arena-1 scope untouched. Where a finding would have
required a protected path, it was left as a documented dependency instead.
