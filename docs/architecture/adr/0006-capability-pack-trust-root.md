---
status: accepted
date: 2026-09-27
deciders: arena/01a0e200-nexus-ai-agent (trust + truth control plane)
consulted: RFC 8032, TUF specification, Sigstore threat model, "Taming the many EdDSAs"
informed: pack substrate owners, security-boundary lane
---

# 0006. Capability packs get an explicit trust root, not a self-asserted one

## Context and Problem Statement

A capability-pack manifest carried `security.trusted_publisher` and
`security.signature`, and the verifier turned a *structurally parseable*
signature into the state `format_only_unverified` with severity `warning`.
Nothing outside the manifest was consulted, so the artifact under judgement
supplied its own verdict: an attacker-authored pack that named any publisher
and put 40 plausible characters in `signature` produced the same report as a
genuinely signed pack, and `PackRegistry.activate()` accepted it. Four of the
six shipped manifests made this worse: their placeholders start with
`base64:placeholder-…`, while the placeholder detector only knew
`base64:replace`, so they were reported as *signature accepted, just not
checked* rather than *unsigned*.

## Decision Drivers

- Authority must come from outside the artifact being judged.
- Local-first: activation must work fully offline, with no CA, log or network.
- Python 3.10 floor, and the pack substrate's import allow-list forbids heavy
  or impure dependencies.
- Every non-verified outcome must be distinguishable and fail closed.
- Key compromise must be recoverable without shipping a new release.

## Considered Options

1. **Keep warning-only verification** (status quo).
2. **Refuse every external pack forever** (empty "trusted states" set).
3. **Sigstore / cosign keyless signing** (Fulcio certificates + Rekor log).
4. **Full TUF client** (root/targets/snapshot/timestamp roles, thresholds).
5. **Reduced TUF-shaped trust root**: a pinned, versioned document of
   publishers → Ed25519 public keys with per-key revocation, threshold 1,
   verified offline against canonical manifest bytes.

## Decision Outcome

Chosen option: **5**, because it is the smallest construct that still puts the
authority outside the artifact.

- Option 1 is the defect.
- Option 2 is honest but terminal: it makes "trusted" unreachable, so the
  distinction between *registered*, *verified* and *trusted* can never be
  tested end-to-end, and the first real publisher forces a redesign under
  pressure. It is also indistinguishable, from the caller's side, from a
  broken verifier.
- Option 3 (Sigstore) buys short-lived keys and public transparency, but
  requires an online CA (Fulcio), an online log (Rekor) and a TUF root to
  bootstrap them. It contradicts offline activation and adds a large
  dependency surface for a runtime that installs packs from disk.
- Option 4 (full TUF) defends against rollback, freeze and mix-and-match
  attacks on a *live metadata repository*. This project has no such
  repository: packs are not fetched through a metadata service, so those
  roles would be ceremony without a threat to answer. TUF's ideas that *do*
  apply — a pinned key list, explicit revocation, an explicit threshold, and
  canonical bytes — are adopted.
- Option 5 therefore keeps TUF's substance and drops its transport.

Concretely (`src/nexus_ai_agent/creative/packs/trust.py`):

- `trust_root.json` ships with the runtime, contains **public keys only**, and
  today contains **zero** publishers — so every external pack resolves to
  `unknown_publisher` and is refused activation. Onboarding a publisher is a
  data change; revoking one is `"status": "revoked"` on the key entry.
- The trust root is **not** overridable by an environment variable. An env var
  that redirects the root of trust is a downgrade switch; an explicit argument
  (`TrustRoot.load(path)`, `PackRegistry(trust_root=…)`) keeps substitution
  visible in code.
- `canonical_signing_bytes()` is the single serialisation both signer and
  verifier use: the whole validated manifest minus `security.signature`,
  sorted keys, tight separators, pure ASCII, prefixed with a scheme tag so
  bytes cannot be replayed under another scheme.
- `TrustState` has nine outcomes (placeholder, unsupported algorithm,
  malformed signature, no trust root, unknown publisher, no trusted keys,
  revoked key, invalid signature, verified). Exactly one grants trust.
- Verification reports trust; it does not grant it. `PackRegistry.activate()`
  is the single enforcement point for external packs.

### Ed25519 implementation

One implementation, always active: a dependency-free RFC 8032 verifier
(`packs/ed25519.py`) with pinned edge-case semantics — canonical `S` (`S < L`,
rejecting the malleable `S + L` variant), canonical point encodings,
small-order public keys rejected, cofactorless equation. An *optional*
`cryptography` backend was implemented first and then removed: two backends
can disagree on exactly these edge cases, and a "library missing" branch is
where fail-open bugs grow. Verification handles only public data, runs at
activation time, and costs single-digit milliseconds.

### Consequences

- (+) An untrusted manifest can no longer promote itself; an unknown
  publisher, a forged signature, a revoked key and a tampered field are four
  distinct, fail-closed states.
- (+) No new runtime dependency; the pack substrate's import allow-list holds.
- (+) Key rotation and revocation are data changes with no release.
- (−) The repository cannot verify that a *real* publisher key works until one
  is onboarded; tests use ephemeral keys generated at run time.
- (−) Pure-Python verification is slower than libsodium (milliseconds, at
  activation only) and is not constant-time — acceptable because it processes
  only public data.
- (~) `security.signature_algorithm` stays `Literal["ed25519"]`; a second
  algorithm would need a new `TrustState` and a new `TRUSTED_STATES` member.

## Confirmation

- `tests/unit/test_pack_trust_root.py` — 35 tests: the state machine, per-field
  tamper matrix, revocation, canonical-byte stability, RFC 8032 vectors,
  malleability, small-order forgery, registry enforcement.
- `tests/architecture/test_pack_trust_boundary.py` — ratchets: no signing
  primitive or key material in the shipped trust path, exactly one
  canonicalisation function, activation gated on `TRUSTED_SIGNATURE_STATES`,
  `ok` and `trusted` kept separate.
- `scripts/pack_trust_mutations.py` — 12 adversarial mutations of the trust
  boundary; every mutant must turn the suite red (12/12 killed).
