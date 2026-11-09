# ADR-0008 — Separate integrity from authenticity: six evidence states

- Status: **Accepted** (implemented and tested at `757dafc`)
- Date: 2026-11-09
- Closes: F06, F07, F14, F15, F16

## Context

A single `VALID`/`INVALID` verdict conflates four independent questions: is the bundle
well formed, are the recorded bytes the bytes on disk, were all the artifacts supplied,
and does a party I trust vouch for it. The old verifier answered all four with one bit
and got three of them wrong: a self-signed bundle was `VALID` (F06), a bundle whose
files were absent was `VALID` (F07), and the media were not bound at all (F14, F15).

## Decision

Exactly one state, chosen by severity, with a stated meaning:

| state | meaning | exit code |
|---|---|---|
| `INVALID` | recorded bytes changed, or the bundle contradicts itself | 1 |
| `UNTRUSTED_SIGNER` | signed, but not by the key the consumer trusts | 4 |
| `NOT_VERIFIABLE` | a signature exists but cannot be checked in this environment | 5 |
| `INCOMPLETE` | artifacts described by the bundle were not supplied | 3 |
| `UNSIGNED` | integrity is checkable, authenticity was never established | 2 |
| `VALID` | structure + every byte + a trusted signature | 0 |

Supporting rules:

1. **The trust anchor comes from the consumer.** A key inside the bundle is checked (so
   an edited claim is `INVALID`) but never trusted, so a self-signed bundle is
   `UNTRUSTED_SIGNER`. `--expect-key-id` pins the signing key id for rotation.
2. **Every recorded artifact must be supplied.** A partial check is `INCOMPLETE`.
3. **The media are bound.** The bundle records per-frame `sha256` and `size_bytes`, the
   audio and motion digests, and a `binding_scope` that names what is bound **and what
   is excluded** (`benchmark_results.json`, because it contains the bundle digest —
   binding it would be circular).
4. **Digest scopes are published.** `calibration.digest_scope` and
   `results_digest_scope` list their exclusions, so a reader can recompute and see
   exactly what was left out.

## Consequences

* "The bundle verified" now has six distinct meanings, and CI can act on the exit code.
* In CI the bundle is always `UNSIGNED` (no key in the repository, by design), so the
  evidence path is exercised without pretending authenticity was established.
* An evidence bundle can never be fully self-describing *and* self-verifying: the
  results document that embeds the bundle digest is deliberately outside the binding.

## Alternatives considered

* **Boolean `ok`.** What existed; it is what allowed three false `VALID`s.
* **Treat "no key configured" as `VALID` with a warning.** A warning nobody's CI reads
  is the same as a pass.
* **Bind `benchmark_results.json` too.** Circular: the document contains the bundle's
  digest, so any change to the bundle changes the document that vouches for it.
