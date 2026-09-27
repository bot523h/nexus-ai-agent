# Trust and Truth Control Plane

Five domains decide whether a claim this system makes is backed by something.
This page is the **boundary map** between them: who produces a fact, who
consumes it, which component is allowed to be the authority, what happens when
the answer is unknown, and which test keeps that true.

It is deliberately honest about status. A domain is `PROVEN` only when the
implementation is on `main`, the guard exists, and an adversarial mutation of
that guard turns the suite red.

| Domain | Question it answers | Status (2026-09-27) |
|---|---|---|
| A — command truth | did the action the user was told about actually happen? | NOT PROVEN (open PR #99) |
| B — pack trust | may this capability pack execute? | PROVEN — on `main` at merge commit `e6b06e04`, CI run 36314653561 green 13/13 incl. `trust-mutations` |
| C — execution truth | were pixels really produced, or only planned? | NOT PROVEN (open PR #97) |
| D — evidence truth | is the coverage/CI evidence measured, or asserted? | NOT PROVEN (open PRs #95, #98) |
| E — replay truth | can the same command commit twice? | PARTIALLY PROVEN — process-local reservations, honestly documented in `studio/bus.py`; restart and multi-worker semantics unproven |

## Domain B interfaces (the part that is proven)

| # | Interface | Producer | Consumer | Authority | Failure mode | Test | Mutation |
|---|---|---|---|---|---|---|---|
| B1 | manifest bytes → validated manifest | `load_manifest()` | `verify_manifest()` | Pydantic models (`extra="forbid"`, `Literal[False]` escapes) | invalid manifest raises `PackManifestError`; nothing is registered | `test_pack_manifest_verify.py` | `placeholder_reported_as_signed` |
| B2 | validated manifest → canonical signed bytes | `canonical_signing_bytes()` | signer (external) and verifier (here) | the function itself — exactly one exists repo-wide | none: pure function, ASCII-only, signature field excluded, scheme-tagged | `test_pack_trust_root.py::test_canonical_bytes_*`, 10-case tamper matrix | `signature_no_longer_covers_capabilities`, `canonical_bytes_lose_their_scheme_tag` |
| B3 | trust-root document → `TrustRoot` | `TrustRoot.load()` | `verify_manifest()` | `creative/packs/trust_root.json` (public keys only, empty today) | unreadable/unknown-schema root ⇒ `TrustRootError` ⇒ **error** on the report, never a silent downgrade | `test_unusable_trust_root_is_an_error_on_the_report` | `broken_trust_root_silently_ignored`, `missing_trust_root_passes` |
| B4 | (manifest, trust root) → `TrustDecision` | `evaluate_trust()` | `VerificationReport.signature_state` | the trust root — never the manifest's `trusted_publisher` | 8 non-trusted states, each distinguishable; `trusted` is false for all of them | `test_pack_trust_root.py` (state machine, revocation, forged key) | `unknown_publisher_becomes_trusted`, `revoked_key_still_verifies`, `malformed_signature_accepted` |
| B5 | signature check | `verify_ed25519()` | `evaluate_trust()` | RFC 8032 with pinned edge cases | canonical `S`, canonical points, small-order `A` rejected, equation enforced | RFC 8032 vectors, malleability, identity-key forgery | `malleable_signature_accepted`, `small_order_key_accepted`, `verification_result_ignored` |
| B6 | verified pack → executable pack | `PackRegistry.activate()` | runtime capability registry | `TRUSTED_SIGNATURE_STATES` (exactly `{"verified"}`) | external pack not `verified` ⇒ `PackRegistryError`; nothing becomes callable | `test_external_pack_cannot_be_activated_without_verified_signature` | `activation_gate_removed`, `unknown_publisher_becomes_trusted` |

**Non-bypass**: `RegisteredPack` is constructed in exactly one module
(`registry.py`), the trust path never imports the delivery signing seam, and
verification reports trust without granting it (`report.ok != report.trusted`).
All four are architecture ratchets in
`tests/architecture/test_pack_trust_boundary.py`.

## Cross-domain contracts B must not violate

- **B → A**: a command response may say a pack is *registered* or *verified*;
  it may only say *executable* when `PackRegistry.active_packs()` says so.
  Domain A owns the wording; domain B owns the fact.
- **B → C**: activation grants the right to *run* an operation. It never
  implies pixels were produced; that claim belongs to domain C's execution
  evidence.
- **B → D**: the pack-coverage harness measures the pack substrate, including
  `trust.py` and `ed25519.py`. Coverage of a trust module is not evidence that
  the trust boundary holds — the mutation campaign is.
- **B → E**: activation is not idempotency-protected state; it is an operator
  action on a process-local registry. Restart semantics belong to domain E.

## Duplicate authority: disposition

| Mechanism | Verdict | Disposition |
|---|---|---|
| `creative/packs/trust.py` + `trust_root.json` | **canonical** for pack activation authority | ADR 0006 |
| `creative/packs/delivery/signing.py` | **not** an activation authority | Transport seam for exported OTIO documents. Its PyNaCl-absent fallback is symmetric HMAC-SHA256 under the same secret used to verify, so a verifier can forge; it must not be described as Ed25519-equivalent. Owned by the `delivery-interop` zone / PR #99 — handoff recorded on that PR. Not reachable from any production call site today (only its own tests import it). |
| `TRUSTED_SIGNATURE_STATES = frozenset()` (PRs #86/#88) | superseded | Same gate, unreachable state. Those branches also have **no merge base with `main`**; see the audit note on each PR. |

## Threat model (ADR 0006, restated here for the whole plane)

**In scope, and tested:** manifest tampering, forged publisher name, forged
signature, revoked key, malformed signature, canonicalisation ambiguity,
activation bypass, missing or corrupt trust root.

**Out of scope, held as assumptions:** compromise of the shipped trust-root
distribution itself (the repository/release channel), compromise of a
publisher's private key, and compromise of the host filesystem. These are
assumptions, not defects: a local-first runtime that trusts its own installed
files cannot defend against an attacker who already controls them.
