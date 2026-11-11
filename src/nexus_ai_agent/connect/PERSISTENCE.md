# CONNECT persistence and key management

This slice stores only encrypted credential material. The database contains connection metadata, an encrypted DEK envelope, an AES-GCM nonce, ciphertext, AAD, and the KEK id. It never contains a KEK or plaintext credential.

## Cryptographic design

Each credential write generates a fresh 256-bit DEK and a fresh 96-bit AES-GCM data nonce. The DEK is wrapped with the active 256-bit KEK using AES-GCM and a separate wrap nonce. `connection_id`, `credential_id`, and the envelope schema version are authenticated as AAD, so a ciphertext cannot be moved to another connection or silently decoded under another schema.

The environment key ring is a bootstrap adapter, not the final production KMS. Set `NEXUS_CONNECT_KEK_RING` to a JSON object mapping key ids to base64-encoded 32-byte keys and set `NEXUS_CONNECT_ACTIVE_KEK_ID` to the key used for new writes. Keep old KEK ids available until all rows have been rewrapped and backup retention permits retirement.

## Rotation runbook

1. Provision a new KEK in the approved KMS/HSM or local bootstrap secret manager.
2. Add the new key id to the ring and switch `NEXUS_CONNECT_ACTIVE_KEK_ID`.
3. Run `SqlConnectionStore.rewrap_all()`. This rewrites only wrapped DEKs; ciphertext and data nonces do not change.
4. Verify that the returned count matches the expected rows and that reads succeed with the new ring.
5. Retire the old key only after backups and rollback windows are covered. A missing old key fails closed and makes old ciphertext unavailable; it never falls back to plaintext.

A suspected compromise requires disabling the compromised KEK in the provider, rotating to a new KEK, and re-encrypting affected records with new DEKs if the old DEK may have been exposed. Rotation is not a substitute for revocation: `revoke()` is optimistic-versioned and sets a terminal connection state.

## Operational boundaries

The store uses the existing `get_session()` authority and declares no engine
or ORM metadata of its own. The canonical row is registered in
`storage.models.py`; a follow-up integration slice should provide the KMS/HSM
adapter and production key bootstrap.

The canonical SQLModel row is registered in `storage.models`, so SQLite
bootstrap and Alembic use the same metadata authority. Production deployments
should still run the migration before first use. This slice has no API or MCP
wiring; callers must resolve the canonical principal and `ProjectAccess` before
exposing a connection operation.

The shared `pyproject.toml` is currently owned by another active Claim, so the
direct `cryptography` dependency declaration is intentionally deferred rather
than changed out-of-band. The next packaging owner must add
`cryptography>=43,<48` before release and run the dependency audit; the code
fails explicitly at import time if the approved AEAD library is absent.

## Research basis

The design follows NIST SP 800-57 Part 1's key lifecycle guidance, OWASP's
recommendation to centralize secret lifecycle and audit controls, Google Cloud's
envelope-encryption separation of DEKs and KEKs, and the `cryptography`
AES-GCM contract requiring unique nonces and matching AAD on decrypt:

- NIST SP 800-57 Part 1 Rev. 5: <https://csrc.nist.gov/pubs/sp/800/57/pt1/r5/final>
- OWASP Secrets Management Cheat Sheet: <https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html>
- OWASP Cryptographic Storage Cheat Sheet: <https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html>
- Google Cloud envelope encryption: <https://docs.cloud.google.com/kms/docs/envelope-encryption>
- Python `cryptography` AEAD API: <https://cryptography.io/en/latest/hazmat/primitives/aead/>
