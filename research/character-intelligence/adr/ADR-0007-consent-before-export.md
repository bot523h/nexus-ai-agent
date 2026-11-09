# ADR-0007 — Permission, then consent, then bytes; import never mints authorization

- Status: **Accepted** (implemented and tested at `757dafc`)
- Date: 2026-11-09
- Closes: F02, F09, F10, F11, F12, F13, F17

## Context

Export was the weakest point of the prototype. Any caller could export any character;
consent records were stored but only their existence was consulted (purpose, rights and
territory were decoration — F12); expiry was compared as text, so a `+05:00` timestamp
could outlive the instant it named (F11); revocation was not terminal (F09) and did not
survive a restart (F10); and importing someone else's bundle either dropped their
consent or would have trusted it wholesale (F13, F17).

Two different questions were being answered with one flag: *may this user export?* and
*may this subject be exported?* They have different owners, different audit needs and
different failure modes.

## Decision

1. **Two gates, in this order, before any byte is read:**
   `AccessPolicy.require(principal, Permission.EXPORT)` then
   `ConsentLedger.decide(character, purpose, right, territory)`.
2. **Every scope dimension is checked**: purpose, `granted_at`, `expires_at`, `rights`
   (closed vocabulary), `territory` (`*`/`WORLDWIDE`/`GLOBAL` or a list).
3. **All timestamps are parsed to aware UTC `datetime`** before comparison. A
   malformed timestamp is a validation error, never a string comparison.
4. **Revocation is terminal and durable.** The same `consent_id` can never be
   re-issued; revocations are written to their own table and to lineage, and the
   ledger is rehydrated from disc on open.
5. **Synthetic subjects are exempt by declared subject class**, and the exemption and
   its reason are written into the export manifest — an exemption that leaves no trace
   is indistinguishable from a bypass.
6. **Import quarantines consent.** An imported record lands in `consent_imports` and
   authorizes nothing. `adopt_imported_consent(id, principal)` requires
   `MANAGE_CONSENT`, a `document_digest`, and records the adoption in lineage; a second
   adoption is refused.
7. **Both refusals are audited** with `allowed=false` and the exact reason string, so a
   reviewer can reconstruct *why* an export did not happen.

## Consequences

* A `VIEWER` gets `AccessDenied`, not a bundle (F02).
* An expired consent cannot be resurrected by a timezone trick (F11).
* An imported bundle cannot bootstrap itself into legality (F17) — somebody with
  authority has to adopt each record, and the adoption is in the lineage.
* The audit table grows with every attempt, refused or not. That is the point.

## Alternatives considered

* **A single `can_export` boolean.** Cannot express rights, territory or time, which is
  what the finding was about.
* **Trust imported consent.** Convenient, and it makes the import path a privilege
  escalation: whoever writes a bundle decides what the subject consented to.
* **Check consent lazily (at write time).** The destination would already contain
  decrypted assets when the check runs; a refusal after the fact is not a control.
