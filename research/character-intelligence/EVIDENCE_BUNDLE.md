# Evidence Bundle — content, verification, and what it does not prove

Status: **VERIFIED** — written by every benchmark run; verification round-trip
tested (11 tests in `tests/character_capsule/test_evidence_bundle.py`). In this
environment the bundle is **UNSIGNED** because no signing key is configured, and it
says so.

## What is in a bundle

```json
{
  "bundle_id": "EVD-NAGAR-CHAR-000001-RUN",
  "subject": {"character_id": "NAGAR-CHAR-000001",
               "capsule_digest": "sha256:…",
               "calibration_digest": "sha256:…",
               "media_type": "application/json"},
  "assertions": {
     "summary": {"scenarios": 12, "passed": 12, "…": "…"},
     "scenarios": [{"scenario": "…", "actual": "…", "passed": true}],
     "synthetic_marking": {"ai_generated": true,
                            "regulation": "EU AI Act Art. 50(2) applies from 2026-08-02"},
     "not_measured": {"human_review": "NOT_MEASURED", "…": "…"},
     "binding_scope": {"bound": ["calibration.json", "continuity_reports.json",
                                  "corpus_manifest.json"],
                        "excluded": {"benchmark_results.json": "circular"}}
  },
  "records": [{"name": "calibration.json", "role": "calibration",
                "media_type": "application/json",
                "sha256": "sha256:…", "size_bytes": 12345}],
  "hard_binding": {"algorithm": "sha256", "digest": "sha256:…", "exclusions": []},
  "lineage": ["sha256:…(capsule)", "sha256:…(corpus)"],
  "created_at": "2026-11-09T11:46:05Z",
  "disclosure": {"ai_generated": true, "real_person": false,
                  "consent": "not applicable: every subject is synthetic"},
  "signature": null,
  "claim_digest": "sha256:…"
}
```

## Verification

```bash
RUN=experiments/character_capsule/.work/results/final-757dafc

# integrity only: every artifact must be supplied, no trust anchor
PYTHONPATH=. python scripts/character_capsule/check_evidence.py \
    --bundle $RUN/evidence_bundle.json --asset-dir $RUN
# exit 2 → UNSIGNED (integrity checkable, authenticity not established)

# authenticity: the consumer supplies the key it trusts
PYTHONPATH=. python scripts/character_capsule/check_evidence.py \
    --bundle $RUN/evidence_bundle.json --asset-dir $RUN \
    --trust-anchor trusted-signer.pub [--expect-key-id <key id>]
# exit 0 → VALID
```

Six states, one per outcome (ADR-0008):

| state | meaning | exit |
|---|---|---|
| `VALID` | structure + every artifact byte + a *trusted* signature | 0 |
| `INVALID` | recorded bytes changed, or the bundle contradicts itself | 1 |
| `UNSIGNED` | integrity checkable, authenticity never established | 2 |
| `INCOMPLETE` | artifacts described by the bundle were not supplied | 3 |
| `UNTRUSTED_SIGNER` | signed, but not by the trusted key (or key id) | 4 |
| `NOT_VERIFIABLE` | a signature exists but cannot be checked here | 5 |

## Design decisions and their reasons

1. **One hard binding over the ordered record set.** C2PA allows exactly one hard
   binding per manifest; a bundle with several independent bindings invites an
   attacker to keep the ones that still pass. `exclusions` exists so a caller must
   state, in the document, which bytes were not hashed.
2. **The results document is deliberately excluded.** `benchmark_results.json`
   embeds the bundle digest; binding a document that contains the binding is
   circular. This is the same problem C2PA solves by excluding the manifest bytes —
   here the exclusion is one whole file, named in `binding_scope`.
3. **`UNSIGNED` is a first-class state, not a failure to be papered over.** C2PA's
   own guidance is that absence of a manifest proves nothing. A prototype that
   signed with a checked-in key would look better and prove less.
4. **Keys live outside the repository.** `CHARACTER_CAPSULE_SIGNING_KEY_FILE` must
   point at a file holding 32 raw bytes or 64 hex characters; if it is unset,
   `load_signing_key` raises `KeyUnavailable` rather than inventing one.
5. **Disclosure travels with the evidence.** EU AI Act Art. 50(2) marking and
   deepfake disclosure must survive leaving the database, so they are assertions in
   the signed claim, not a column in a table.

## Threat coverage (measured by tests)

| attack | outcome | test |
|---|---|---|
| swap one byte of a described artifact | `files_match = false`, `ok = false` | `test_tampering_with_an_artifact_invalidates_the_bundle` |
| edit a record's metadata | `hard_binding_matches_records = false` | `test_tampering_with_a_record_breaks_the_hard_binding` |
| edit the claim after signing | `INVALID`, `signature_valid = false` | `test_signature_does_not_survive_an_edit` |
| verify with a different public key | `UNTRUSTED_SIGNER` (not `INVALID`: well formed, not trusted) | `test_a_foreign_key_does_not_verify` |
| sign a bundle with a key the bundle itself carries | `UNTRUSTED_SIGNER`, never `VALID` | `test_a_signature_alone_is_not_authenticity` |
| pin a key id and sign with the same bytes under another id | `UNTRUSTED_SIGNER` | `test_an_expected_key_id_mismatch_is_refused` |
| withhold one recorded artifact | `INCOMPLETE`, `missing_records` names it | `test_missing_artifacts_are_incomplete_never_valid` |
| rely on an unsigned bundle | `UNSIGNED` + explicit reason | `test_bundle_binds_every_artifact_it_names` |
| rewrite a frame and re-run the corpus digest | per-frame `sha256` changes, binding fails | `test_f15b_a_rewritten_frame_changes_its_record_not_just_the_corpus_digest` |

## What it does not prove

* **Nothing about the pixels being unmodified beyond the recorded digests**: the
  bundle binds what the run wrote, not what a camera captured.
* **Nothing about C2PA trust lists** — there is no X.509 chain, no JUMBF container,
  no conformance claim. `c2pa_manifest_shape()` returns a *mapping* and labels
  itself as shape only.
* **Nothing about time** — `created_at` is asserted by the producer, not
  countersigned by a timestamp authority (`DEFERRED`).
* **Nothing about consent by itself** — the disclosure block states the synthetic
  situation; a real-person export additionally requires the consent gate described in
  `SECURITY_HARDENING_REPORT.md` §3, and an *imported* consent record stays inert
  until an owner adopts it.
* **Nothing about freshness** — a correctly signed old bundle replays. `created_at`
  is recorded, not countersigned.
