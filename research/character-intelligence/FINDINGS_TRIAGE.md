# Findings triage — PR #326 review, 31 reported issues

**Scope**: every finding raised by the Sourcery review on PR #326 (30 findings:
3 Critical / 13 High / 14 Medium by the reviewer's own severity), plus the one
finding that the reviewer's static analysis produced from a misreading (F01).

**Rule applied**: a finding is called *confirmed* only after its failure was
reproduced on the code that was actually at `5fa6470` (the pre-fix HEAD of this
branch), and *closed* only after a regression test exists that fails on that code
and passes on the fixed code. Nothing in this table is inferred from reading a
diff.

| field | value |
|---|---|
| pre-fix code | `5fa6470` (branch `arena/337c3bc1-nexus-ai-agent`) |
| fix commit | `757dafc` (this branch; the docs commit that follows updates this table only) |
| board task | `task-nagar-character-intelligence-02`, generation 2, claimed by this branch |
| regression suite | `tests/character_capsule/` — 127 tests |
| verdicts | **30 confirmed, 1 not reproducible** |

Legend for *state after fix*: `CLOSED` = the attack now fails and a test proves
it; `CLOSED-BY-DESIGN` = the behaviour was a design decision that is now stated
and enforced rather than accidental; `NOT-REPRODUCIBLE` = the claim did not hold.

---

## Consent, authorization and cryptography

| id | claim | severity | how it failed before | what changed | regression test | state after fix |
|---|---|---|---|---|---|---|
| F01 | SQL injection through `character_id` | High (reported) | Not reproducible: every statement is parameterised; a hostile id matches 0 rows and the schema survives `DROP TABLE`-shaped input | no code change; the property is pinned so a future f-string regression fails | `test_f01_a_hostile_id_reads_nothing_and_leaves_the_schema_intact` | **NOT-REPRODUCIBLE** |
| F02 | export ignores authorization | Critical | a `VIEWER` (zero permissions) exported every asset of a consenting subject | `export_bundle` calls `AccessPolicy.require(EXPORT)` **before** reading a byte, audits allow *and* deny, and raises `AccessDenied` | `test_f02_a_viewer_cannot_export_assets`, `test_f02b_only_the_owner_role_carries_every_permission` | CLOSED |
| F04 | `cipher=None` stored reference images in clear | Critical | the PNG bytes were on disk verbatim and nothing recorded the fact | a repository without a cipher now refuses to start (`CryptoUnavailable`); plaintext needs `allow_plaintext="<reason>"`, which is persisted in `meta` and reported by `storage_policy()` | `test_f04_a_cipher_actually_encrypts_the_stored_bytes`, `test_f04b_plaintext_storage_requires_a_named_reason` | CLOSED |
| F05 | `encrypt()` then `decrypt()` raised `KeyUnavailable` | Critical | the blob could not be read back at all; the "encryption" was write-only | NCC2 envelope: `"NCC2" \| version \| header \| kek_nonce \| wrapped_key(48) \| data_nonce \| body`, the data key sealed with XSalsa20-Poly1305 under the KEK, the header bound as associated data | `test_f05_an_encrypted_asset_reads_back_byte_identical`, `test_f05b_a_missing_kek_file_is_a_typed_refusal` | CLOSED |
| F09 | re-adding a revoked consent cleared `revoked_at` | High | the record came back to life and authorized a later export | revocation is terminal in the ledger *and* in SQLite; re-inserting the same id raises `ConsentRevoked` | `test_f09_a_revoked_consent_can_never_be_reissued_under_the_same_id` | CLOSED |
| F10 | consent history vanished after a restart | High | a reopened store had 0 records and exported a `REAL_PERSON` with no consent | records and revocations are persisted and rehydrated in `CapsuleRepository.__init__` | `test_f10_consent_survives_a_restart`, `test_f10b_a_revocation_survives_a_restart` | CLOSED |
| F11 | expiry compared as text, not time | High | `…T00:00:00+05:00` sorted after a later `…Z` instant, so expired consent still authorized | every timestamp is parsed to an aware UTC `datetime` (`parse_utc`) before any comparison | `test_f11_expiry_is_compared_as_time_not_as_text` | CLOSED |
| F12 | `rights` and `territory` were never consulted | High | a consent limited to `rights=("identity",)` and `territory="TR"` authorized a worldwide export | `ConsentRecord.decision` checks purpose, granted_at, expiry, right and territory and returns the reason that is written to the audit log | `test_f12_rights_and_territory_gate_an_export`, `test_f12b_a_synthetic_subject_does_not_need_consent`, `test_f12c_an_export_is_audited_with_the_consent_decision` | CLOSED |
| F31 | duplicate bytes raised `IntegrityError` | Medium | registering the same image twice crashed with a unique-constraint error | `BlobStore.put` is idempotent on content; the asset row is reused for the same `(character, sha256, role)` | `test_f31_registering_the_same_bytes_twice_is_idempotent_not_an_error`, `test_f31b_asset_bytes_are_verified_against_the_recorded_digest` | CLOSED |

## Import / export boundary

| id | claim | severity | how it failed before | what changed | regression test | state after fix |
|---|---|---|---|---|---|---|
| F03 | import followed `../outside-secret.bin` | Critical | a manifest-supplied relative path read a file outside the bundle | `_read_bundle_file` resolves the path, requires it to stay under the bundle root, refuses symlinks and re-hashes the bytes against `bundle_sha256` | `test_f03_import_refuses_a_manifest_that_points_outside_the_bundle` | CLOSED |
| F13 | import did not restore the capsule | High | the imported bundle produced no stored capsule | import writes the capsule (re-sealed and verified) through the same compare-and-swap path | `test_f13_and_f17_import_restores_capsule_and_quarantines_consent` | CLOSED |
| F17 | imported consent was lost (or trusted) | High | consent records were dropped | they are restored into a quarantine table and stay inert until `adopt_imported_consent(..., principal)` with `MANAGE_CONSENT` and a document digest | `test_f13_and_f17_import_restores_capsule_and_quarantines_consent` | CLOSED-BY-DESIGN |
| F18 | a failed export wiped the destination | High | `shutil.rmtree(destination)` ran before any validation | export writes into `.<name>.staging-<pid>-<rand>` and swaps it in with `os.replace`, restoring the previous directory if the swap fails | `test_f18_a_failed_export_leaves_the_previous_destination_intact` | CLOSED |
| F19 | a bundle could import a foreign `AST-` id | High | `AST-000002` (owned by another character) was accepted from the manifest | the export refuses to reference a foreign asset and the import rejects an asset id that belongs to another character | `test_f19_import_refuses_an_asset_owned_by_another_character` | CLOSED |

## Media decoding

| id | claim | severity | how it failed before | what changed | regression test | state after fix |
|---|---|---|---|---|---|---|
| F08 | unbounded PNG decode | Critical | 186,696 bytes of header expanded to 192 MB (8000×8000 RGB) before any check | `DecodeLimits` (max_side, max_pixels, input/IDAT/decompressed bytes, audio frames) is checked from the IHDR/RIFF header **before** allocation; bombs raise `MediaLimitExceeded` | `test_f08_a_pixel_bomb_is_refused_before_allocating`, `test_f08b`…`test_f08g` | CLOSED |

## Evidence integrity vs authenticity

| id | claim | severity | how it failed before | what changed | regression test | state after fix |
|---|---|---|---|---|---|---|
| F06 | a self-signed bundle verified as `VALID` | Critical | the verifier read the public key out of the bundle it was checking | authenticity requires a `TrustAnchor` supplied by the consumer; the embedded key only proves possession, so a self-signed bundle is `UNTRUSTED_SIGNER` | `test_a_signature_alone_is_not_authenticity`, `test_the_consumer_trust_anchor_makes_a_signature_valid` | CLOSED |
| F07 | missing/partial artifacts still `VALID` | High | verification passed with none of the recorded files present | every recorded artifact must be supplied; a partial check is `INCOMPLETE` and `checks["all_artifacts_supplied"]` is false | `test_missing_artifacts_are_incomplete_never_valid`, `test_artifacts_were_never_offered_cannot_be_complete` | CLOSED |
| F14 | the bundle never described the frames or audio | High | only the report was bound, so the media were unconstrained | the bundle binds the corpus manifest (per-frame sha256), the audio digests and the motion digests, with an explicit `binding_scope` | `test_f14_and_f15_a_bundle_binds_every_frame_and_audio_track` | CLOSED |
| F15 | rewritten PNG kept the same corpus digest | High | the corpus digest covered metadata only | `FrameRecord.sha256/size_bytes` come from the encoded bytes; the corpus digest is a digest over those digests | `test_f15b_a_rewritten_frame_changes_its_record_not_just_the_corpus_digest` | CLOSED |
| F16 | a malformed key silently meant "unsigned" | Medium | an unparsable key produced an unsigned bundle with no error | key loading is typed (`KeyUnavailable`), signing reports `NOT_CONFIGURED` vs `FAILED` vs `SIGNED`, and an unusable trusted key yields `NOT_VERIFIABLE` | `test_a_reamed_key_is_reported_not_verified`, `test_no_key_configured_is_a_typed_refusal` | CLOSED |

## Identity, canon and shot binding

| id | claim | severity | how it failed before | what changed | regression test | state after fix |
|---|---|---|---|---|---|---|
| F20 | forged digests still produced `VERIFIED_WITH_LIMITATIONS` | Critical | a shot carrying `sha256:000…` for capsule and modules was scored | `ContinuityEvaluator.evaluate` calls `validate_capsule_pins` first and raises `PinMismatch`; the report carries both the pinned and the verified revision | `test_f20_a_shot_pinned_to_a_forged_capsule_digest_is_refused`, `test_f20b_a_shot_manifest_with_unknown_fields_is_refused` | CLOSED |
| F21 | `timeline=None` scored scene 3 as order 1 | High | a missing timeline silently became "scene 1", so change events fired wrongly | without a timeline the story-dependent dimensions are `NOT_MEASURED` with a stated limitation (`scene_context`) | `test_f21_without_a_timeline_scene_dimensions_are_not_measured` | CLOSED |
| F22 | `scene_rules` was dead data | High | a rule saying "SC-003 wears OUT-0002" was ignored and the shot was flagged `WARDROBE_DRIFT` | `expected_outfit(..., scene_id=)` gives scene rules precedence over sequence baselines and change events | `test_f22_a_scene_rule_beats_the_sequence_baseline` | CLOSED |
| F28 | `with_module` could change `eye_spacing_ratio` | Critical | 0.235 → 0.300 inside a revision, i.e. a silent identity change | `with_module` compares every `Scope.IDENTITY` field and raises `ScopeViolation`; `updated()` refuses the field even earlier | `test_f28_a_face_field_cannot_change_inside_a_revision`, `test_f28b_a_revision_scoped_field_still_works`, `test_f29c_an_identity_change_is_not_expressible_as_a_module_edit` | CLOSED |
| F29 | `new_identity_version(voice_changes=…)` raised `TypeError` | High | a voice recast was impossible through the documented door | the door accepts `face_changes` / `body_changes` / `voice_changes`, validates every field's scope, bumps the module revision and `identity_version` together | `test_f29_a_voice_change_mints_an_identity_version`, `test_f29b_a_non_identity_field_cannot_ride_along` | CLOSED |
| F30 | `with_outfit` rewrote v1 in place | High | `#803020 → #008040` under the same `(outfit_id, version)`, silently changing what an old shot resolved to | outfit versions are immutable: same-version changes raise, a new version must be higher and records `supersedes` with the previous content digest | `test_f30_an_existing_outfit_version_cannot_be_rewritten`, `test_f30b_a_superseding_version_must_be_higher_and_records_its_parent` | CLOSED |

## Benchmark validity

| id | claim | severity | how it failed before | what changed | regression test | state after fix |
|---|---|---|---|---|---|---|
| F23 | holdout character was inside the fitted statistics | Critical | the gallery z-score used all three characters, then the third was reported as "held out" | statistics, thresholds and galleries are fitted on the calibration ids only; the split is recorded inside `calibration.json` and re-checked | `test_f23_the_holdout_character_is_not_in_the_fitted_split`, `test_f23b`, `test_f23c`, `test_f23d`, `test_f23e`, `test_f23f` | CLOSED |
| F24 | identical voice arrays gave distance 0.0 | High | the scenario compared a take with itself | `voice_signature`/`voice_samples`/`motion_sequence` take a `session`; the scenario compares session `b` against reference session `a` | `test_f24_voice_sessions_differ_for_the_same_speaker`, `test_f24b_the_voice_scenario_compares_two_recordings` | CLOSED |
| F25 | published `evidence_digest` did not recompute | High | `processing_ms` was inside the digest, so every published report failed its own check | `EVIDENCE_DIGEST_EXCLUSIONS = ("processing_ms", "evidence_digest")`, declared in the published document | `test_f25_every_published_report_digest_recomputes`, `test_f25b_editing_a_published_report_breaks_its_digest` | CLOSED |
| F26 | module-scope `resource` broke Windows imports | Medium | `import resource` at module scope raised on Windows | `peak_rss_mb()` imports it lazily and reports `NOT_MEASURED` when unavailable | `test_f26_no_module_scope_resource_is_imported_for_measurement` | CLOSED |
| F27 | fixed `bundle_id`, double write, non-atomic publish | High | two runs overwrote each other's evidence; the results file was rewritten after hashing | per-run id, `.<run_id>.staging` published by `os.replace`, `FileExistsError` on a reused id, results written exactly once | `test_f27_each_run_gets_its_own_bundle_id`, `test_f27b`, `test_a_run_publishes_atomically_under_its_own_id`, `test_results_document_is_self_describing` | CLOSED |

---

## Findings that changed the *expected* result instead of the code

Two items in this review were not code bugs but dishonest expectations. Both were
changed openly rather than quietly:

1. **F24's scenario expectation.** `extra_voice_identity` used to pass because the
   candidate and the reference were the same array. With a real second session the
   verdict is `VERIFIED_WITH_LIMITATIONS` and the voice dimension is
   `NOT_MEASURED`, because this corpus's look-alike pair differs by 3 Hz
   (186 Hz vs 189 Hz) while a session shift moves f0 by ~2 Hz: the calibration
   split cannot separate genuine from impostor pairs at all
   (EER = 1.0). The scenario now declares `INSUFFICIENT_EVIDENCE` as an acceptable
   verdict **only** in the `NOT_MEASURED` case; if a future corpus separates the
   pair, a threshold is fitted and the scenario must reach
   `VERIFIED_WITH_LIMITATIONS` (see `ADR-0010`).
2. **The pre-fix 12/12.** The old benchmark reported 12/12 with a voice threshold
   fitted from one take per speaker (std ≈ 0 on several dimensions). That number
   was produced by a band that classified whatever it was given. The revalidated
   run is still 12/12, but every number behind it is now either measured and
   published (FAR/FRR/EER), or explicitly `NOT_MEASURED`. See
   `BENCHMARK_REVALIDATION.md` for the side-by-side.

## What is *not* fixed here

* Face FAR on the held-out character is **0.6** at the accept boundary. The
  descriptor is classical (no learned embedding; no licensed model available
  offline), so this is a property of the method, not a bug to be tuned away. It is
  reported in `benchmark_results.json`, in `BENCHMARK.md` and in
  `BENCHMARK_REVALIDATION.md`.
* `hair` has no threshold: it is advisory (`QUALITY_GATES["hair"]` and a colour
  comparison), and the benchmark says so instead of inventing a band.
* Signing is `NOT_CONFIGURED` in CI by design: no key material is stored in the
  repository. The bundle therefore verifies as `UNSIGNED`, never `VALID`.
* The pre-fix reproduction harness lived in a scratch directory outside the
  repository (`/tmp/repro/probe.py`) and is not committed; the reproducible form of
  the same attacks is the regression suite, which fails on `5fa6470` by construction.
