# Threat Model — Character Capsule

Status: **VERIFIED** for the implemented controls (tested); items marked
`DEFERRED` have no implementation and are listed as such rather than implied.

## Assets

1. The capsule's **identity truth** (who the character is) and its revision history.
2. **Reference assets** (frames, audio) — personal data if the subject is real.
3. **Consent records** — who allowed what, until when.
4. **Evidence bundles** — what a verdict rests on.
5. **Keys** — data-encryption keys (KEK/DEK) and the signing key.

## Adversaries

| # | adversary | goal |
|---|---|---|
| A1 | careless internal editor | "just fix" the face ratio, silently rebase a shot |
| A2 | malicious internal user | alter a verdict or an asset and pass it as original |
| A3 | external attacker with storage access | read or swap reference assets |
| A4 | impersonator with a synthetic face | pass a look-alike as the character |
| A5 | rights-holder dispute | prove or disprove that a given output used a character |
| A6 | tool provider without consent discipline | reuse a real person's likeness |

## Threats, controls, residual risk

| id | threat | control (implemented?) | residual risk |
|---|---|---|---|
| T1 | identity silently rewritten | `ScopeViolation` on IDENTITY fields; `RevisionConflict` on stale `expected_revision` (**yes**) | an editor with DB access can insert a new identity version; audit trail is the only trace (`PARTIAL`) |
| T2 | shot re-pointed at a newer capsule | shots pin `capsule_revision` + `capsule_digest` + all module digests (**yes**) | a client that ignores the pinned digest is out of scope |
| T3 | asset substituted in storage | per-object DEK wrapped by a KEK; canonical header bound inside the ciphertext (**yes**, PyNaCl `SecretBox`) | `SecretBox` has no AAD; binding is done by sealing the header inside the ciphertext (documented, not hidden) |
| T4 | key material leaks into the repo | no key in source; `CHARACTER_CAPSULE_KEK_FILE` / `CHARACTER_CAPSULE_SIGNING_KEY_FILE` must point outside; missing key = typed refusal (**yes**) | an operator can still commit a key; a pre-commit scan is `DEFERRED` |
| T5 | look-alike accepted | calibrated face band + look-alike as the designated hard impostor (**yes**) | measured FAR 0.600 on held-out face pairs — the weakest control in the system |
| T6 | verdict forged after the fact | content-addressed evidence bundle + optional Ed25519 signature; tamper detection tested (**yes**) | unsigned run (`UNSIGNED`) provides integrity, not authenticity; signing key custody `DEFERRED` |
| T7 | real person's likeness used without consent | `ConsentLedger` + `AccessPolicy` gate export/render; revocation terminal (**yes**) | consent *verification* (is the signature on the release real?) `DEFERRED` |
| T8 | model licence violation (research-only weights) | licence matrix; excluded packs; no weights committed (**yes**) | a future integrator can still add them; review is manual |
| T9 | synthetic media presented as real | capsule carries `synthetic_marking`; bundle repeats it as an assertion (**yes**) | enforcement at export/distribution is outside this repo |
| T10 | denial of service via huge assets | size limits in `BlobStore` + PNG decoder bounds (**partial**) | decompression-bomb tests are limited to hand-built cases |
| T11 | audit trail tampering | append-only audit rows with actor + action + reason (**yes**) | no hash-chaining or external anchoring (`DEFERRED`) |
| T12 | replay of an old verdict for a new shot | report binds `shot_id` + `capsule_digest` + `calibration_digest` + `evidence_digest` (**yes**) | a consumer that ignores those fields is out of scope |

## Trust boundaries

```
[authoring UI] --(signed capsule revisions)--> [capsule store] --(sealed digests)--> [verifier]
                                                     |                                    |
                                              [asset store + KEK]              [evidence bundle]
                                                                                        |
                                                                      [reviewer / downstream]
```

* The **verifier trusts nothing but its own measurements** and the calibration
  digest it was handed; it re-derives every distance from pixels.
* The **reviewer trusts the bundle**, not the process that wrote it.
* The **KEK is outside the boundary of this repository** by construction.

## Explicit non-threats (out of scope)

* A compromised OS or hypervisor.
* A malicious calibration artifact (an attacker who can rewrite thresholds can
  make any verdict; protecting that requires signed calibration, `DEFERRED`).
* Photographic deepfake detection — this system verifies *continuity with a
  capsule*, not whether a human is real.

## Known gaps to state out loud

1. Face verification is the weakest control (FAR 0.600 on 4/10 held-out scores).
2. Evidence signing is not exercised here (no key); the code path is tested with
   ephemeral keys only.
3. Calibration artifacts are not signed; a signed-thresholds design is `DEFERRED`.
4. No rate limiting on the verification API (there is no API here — `DEFERRED`).
