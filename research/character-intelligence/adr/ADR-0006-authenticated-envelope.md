# ADR-0006 — An authenticated, versioned envelope (NCC2) instead of a bare cipher

- Status: **Accepted** (implemented and tested at `757dafc`)
- Date: 2026-11-09
- Closes: F04, F05, F16

## Context

The prototype "encrypted" blobs by calling a stream cipher with a key derived on the
fly. Two things were wrong. First, `encrypt()` and `decrypt()` disagreed about the
layout, so an encrypted blob could not be read back at all (F05) — the confidentiality
was write-only. Second, the absence of a cipher silently meant plaintext on disk with
no record of the fact (F04), and a malformed key silently meant "unsigned" (F16).

A default that degrades quietly is worse than a default that fails: the operator
believes the assets are encrypted because the configuration says `cipher:` exists.

## Decision

1. One envelope format, versioned from the first byte:

   ```
   "NCC2" | version(1) | header_len(2) | header(canonical JSON)
          | kek_nonce(24) | wrapped_key(48) | data_nonce(24) | body
   ```

2. A fresh 32-byte data key per blob, wrapped with XSalsa20-Poly1305 under the KEK.
3. The header is **associated data**. Changing `purpose`, `digest`, `media_type` or
   the plaintext length invalidates the tag instead of being ignored.
4. `key_id = sha256(kek)[:23]` is recorded next to the blob. It is not secret; it
   makes "wrong key" a typed, visible condition instead of a decryption failure.
5. Encryption is the default. A repository constructed without a cipher raises
   `CryptoUnavailable`. Plaintext requires `allow_plaintext="<reason>"`, which is
   persisted in `meta` and reported by `storage_policy()["plaintext_reason"]`.
6. The KEK comes from `CHARACTER_CAPSULE_KEK_FILE`; no key material is ever stored in
   the repository or in a fixture.

## Consequences

* Rotating the KEK is detectable: blobs keep the `key_id` they were written with.
* Every read re-checks the recorded `sha256`, so a blob swapped for another blob of
  the same type is caught by the digest rather than by the AEAD alone.
* A test or a lab run must pass `allow_plaintext=...`; two words of friction buy a
  recorded, auditable state instead of an assumption.
* The format is not C2PA, JUMBF or COSE. It is a local storage envelope whose job is
  to keep reference assets confidential at rest; publishing an asset is a separate,
  audited operation (see ADR-0007).

## Alternatives considered

* **AES-GCM with a single key.** Fine cryptographically, but binds the prototype to a
  backend it does not need, and would not answer the "wrong key" question any better
  than a key id does.
* **Age / libsodium sealed boxes.** More moving parts than the prototype needs, and
  its metadata story is worse than an explicit header.
* **Keep `cipher=None` as "plaintext is fine".** Rejected: that is exactly the silent
  degradation F04 identified.
