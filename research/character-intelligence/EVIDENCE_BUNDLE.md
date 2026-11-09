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
PYTHONPATH=. python scripts/character_capsule/check_evidence.py \
    --bundle experiments/character_capsule/.work/results/evidence_bundle.json \
    --asset-dir experiments/character_capsule/.work/results
# exit 2 → UNSIGNED (integrity checkable, authenticity not established)
```

Exit codes: `0` VALID, `2` UNSIGNED, `1` INVALID.

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
| verify with a different public key | `INVALID` | `test_a_foreign_key_does_not_verify` |
| rely on an unsigned bundle | `UNSIGNED` + explicit reason | `test_bundle_binds_every_artifact_it_names` |

## What it does not prove

* **Nothing about the pixels being unmodified beyond the recorded digests**: the
  bundle binds what the run wrote, not what a camera captured.
* **Nothing about C2PA trust lists** — there is no X.509 chain, no JUMBF container,
  no conformance claim. `c2pa_manifest_shape()` returns a *mapping* and labels
  itself as shape only.
* **Nothing about time** — `created_at` is asserted by the producer, not
  countersigned by a timestamp authority (`DEFERRED`).
* **Nothing about consent** — the disclosure block states the synthetic situation;
  a real-person capsule would need a verifiable consent reference, which is
  `DEFERRED` pending the consent ledger's production storage.
