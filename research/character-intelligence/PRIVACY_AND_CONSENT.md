# Privacy and Consent Model

Status: **VERIFIED** for the synthetic-only case; the real-person path is
implemented as *refusal-by-default* and marked `DEFERRED` where it would need a
legal process this environment cannot provide.

## Two subject classes, one rule each

| subject | what is stored | what is required | what is forbidden |
|---|---|---|---|
| **SYNTHETIC** (this mission) | generated frames, voice and gait parameters, declarations | an explicit `synthetic=true` declaration on the capsule and on every asset ref | presenting the character as a real person; implying consent that was never given |
| **REAL PERSON** | reference assets of the person | written, specific, informed, revocable consent that names AI cloning (image **and** voice) | any use outside the named purposes; any use after revocation; any "implied" consent |

The prototype does not process real people at all. `CHARACTERS` in
`experiments/character_capsule/dataset.py` are parametric and the corpus is
rendered from them, so the entire benchmark is synthetic by construction — and the
evidence bundle asserts `"real_person": false`.

## Consent record shape

```python
ConsentRecord(
    consent_id="CNS-0001",
    character_id="NAGAR-CHAR-000001",
    subject_class=SubjectClass.SYNTHETIC,     # or REAL_PERSON
    purposes=(ConsentPurpose.RENDER, ConsentPurpose.EXPORT),
    granted_at="2026-11-01T00:00:00Z",
    expires_at=None,
    document_ref="<opaque reference to the signed release>",
    revoked_at=None,
)
```

* `covers(purpose, now=…)` answers the question the exporting code actually asks.
* `ConsentLedger.require(...)` raises `ConsentRequired` when no record covers the
  purpose and `ConsentRevoked` when it was revoked — both are typed refusals, not
  booleans a caller can misread.
* Revocation is **terminal** for protected operations and is written to the audit
  trail with the reason.

## Why voice is called out separately

ELVIS (Tennessee, in force 2024-07-01) reaches *simulated* voice and tool
providers; the FCC's 2024 ruling brings AI voices under TCPA; the NO FAKES Act is
**not law** as of this mission. A consent that names "likeness" but not "voice" is
therefore insufficient for a real person. `ConsentPurpose.VOICE_CLONE` exists as a
distinct purpose so that a release must name it explicitly.

## Data minimisation and retention

| data | retention decision | status |
|---|---|---|
| capsule modules | indefinite while the character exists | implemented (rows are never deleted, only superseded) |
| reference frames | only the frames a shot needs; gallery references are explicit | implemented |
| evidence bundles | append-only, one per run | implemented |
| audit trail | append-only | implemented |
| embeddings / derived biometrics | not produced in this prototype (classical features are not stored) | by construction |
| consents | kept after revocation (a revocation is itself evidence) | implemented, durable |
| imported consents | quarantined; authorize nothing until an owner adopts them | implemented |

## Revocation semantics (enforced, not advisory)

1. `revoke_consent(consent_id, reason)` marks the record, writes a revocation row and
   appends to lineage.
2. **Revocation is terminal**: the same `consent_id` can never be re-issued, and the
   ledger refuses re-insertion with `ConsentRevoked`. A *new* consent needs a new id.
3. Revocations are persisted and rehydrated on open, so a restart cannot resurrect a
   revoked record.
4. `AccessPolicy.require(EXPORT)` runs first, then the consent decision; a refusal
   writes `allowed=false` plus the reason to the audit table.
3. **Already-rendered outputs are not recalled** — the system cannot reach them.
   This is stated in the consent document and is the honest limitation of any local
   revocation: recall is an organisational process, not a code path.

## Obligations the capsule carries (not enforces)

* **EU AI Act Art. 50(2)/(4)** from 2026-08-02: machine-readable marking of
  synthetic media and deepfake disclosure. The capsule and every evidence bundle
  carry `synthetic_marking`; applying it to exported files is the exporter's job.
* **Art. 50(3)** deployer notice when biometric categorisation is used: relevant
  here because *verification* is biometric-adjacent; the prototype tells users that
  a face comparison is being performed rather than doing it silently.

## Deferred (with reason)

| item | why deferred |
|---|---|
| verifiable consent documents (signature verification against a release) | needs a document-signing service and a legal definition of the release |
| consent expiry automation (re-consent reminders) | needs a scheduler and an owner |
| DSAR / erasure workflow | needs a deletion model that does not break append-only evidence; that is a product decision, not a prototype one |
| cross-border transfer rules | legal, not technical |
