# Personal Cloud Vault: concurrency contract

The vault is multi-writer by construction: a push (writer) and a garbage collection
(collector) run in different processes, on different machines, against the same
bucket. Nothing here is a lock. Everything here is a *conditional write plus a
proof*, because a lock that can be acquired by a writer whose clock or network is
slow is not a lock — it is a scheduling suggestion.

This page states the contract the S3 driver implements in
`src/nexus_ai_agent/storage/personal_cloud_vault/byos_drivers.py` and names the tests
and mutants that pin each clause. It is the reference for the code docstrings, not a
substitute for them.

## The four objects

| Object | Written by | Contains |
| --- | --- | --- |
| `chunks/<chunk_id>.chk` | writer, collector (restore) | one encrypted chunk; the id is the content hash |
| `leases/<lease_id>.json` | writer | `lease_token`, `expires_at_epoch`, `ttl_seconds`, `chunk_ids` |
| `gc-fences/<chunk_id>.fence` | collector | `fence_token`, an absolute claim on one chunk |
| `manifests/…` | writer | the snapshot record a reader trusts |

## Identity, not equality

Every write has an identity: the object's `ETag` **plus** what the write itself
declared. For a lease that is `lease_token` (a fresh `uuid4().hex` per write, minted
by `acquire_push_lease` and by every `update_push_lease`). For a chunk it is the
triple `(x-amz-version-id, x-amz-meta-nexus-chunk-gen, ETag)`, where the generation
stamp is a metadata header the upload mints.

The distinction is not academic. Chunk ids are content-addressed, so an `ETag` is a
function of the bytes and nothing else: delete a chunk, re-upload identical bytes,
and the `ETag` comes back **identical** while the object on the bucket is a different
object written by a different writer. This is the ABA replacement a collector leaves
behind, and an identity built on the `ETag` alone cannot see it. `M21` removes the
version and generation components and the campaign proves the resulting blindness;
`test_identical_bytes_reupload_with_same_etag_is_detected_as_aba_replacement`
asserts the `ETag` is unchanged *before* asserting the conflict, so the test cannot
pass by accident.

An object the bucket identifies with neither a version id nor a generation stamp
returns `UNKNOWN_GENERATION`, which is fail-closed: `_lost_materialised_chunks`
treats it as "lost" whether it appears before or after the write (`M22`,
`test_missing_version_and_generation_metadata_fails_closed`).

## No unconditional delete, anywhere

The rule that closes the whole family of races is one sentence: **a lease or a fence
is only ever removed through a precondition on the identity that was read.**

* `release_push_lease(lease_id, expected_etag=…)` sends `If-Match`. An empty
  `expected_etag` is refused with `VaultTransportError` — "unknown identity" is not a
  licence to delete (`M20`).
* Reaping an expired lease re-reads it and requires *both* `current_etag ==
  expected_etag` and `current_lease_token == expected_lease_token` (`hmac.
  compare_digest`), then confirms the deadline is still in the past by the clock-skew
  margin, and only then deletes with `If-Match` (`M17`). Anything else returns
  `False`, and the caller keeps that lease's chunks in its active set: a chunk kept
  alive costs one GC pass, a chunk deleted on an ambiguous state is gone
  (`test_stale_lease_cleanup_cannot_delete_a_concurrently_refreshed_lease`).
* A `404` from the backend is *known absence*, so it is safe;
  `409`/`412` is *state moved*, so it aborts.

`test_absent_lease_etag_never_causes_unconditional_lease_deletion` covers the
degraded backend shape: no `ETag` reported at all means the lease is not reaped and
its chunk stays protected.

## Rollback belongs to the writer that failed

`acquire_push_lease` and `update_push_lease` write the lease, then prove (fences,
chunk existence, fences again) and roll their own object back on refusal. The
rollback is ownership-bound:

1. no `our_write_etag` (the backend never said which object our `PUT` made) ⇒ do
   nothing at all;
2. read the object back, and abort unless the current `ETag` is ours **and** the body
   still carries our `lease_token` — a refresher or a third writer that replaced the
   object in between wins by definition;
3. the restoring `PUT` (or the `DELETE` when there was no prior body) carries
   `If-Match: our_write_etag`, so the observation and the write are one atomic
   decision at the store. `409`/`412` stops the rollback; it is never retried and
   never forced.

`M18` replaces that body with an unconditional `PUT`/`DELETE` and
`test_failed_rollback_never_deletes_a_third_writers_newer_lease` kills it by
asserting the third writer's lease is byte-identical afterwards.

## Time belongs to the bucket

`expires_at_epoch` is written by the writer, on the writer's clock. A writer whose
clock lags by two hours writes a lease that is "already expired" to a collector
running on a correct clock — and reaping it deletes the chunks it protects. The
deadline is therefore re-derived as `max(expires_at_epoch, Last-Modified +
ttl_seconds)` and every "this is dead" decision is widened by
`CLOCK_SKEW_SAFETY_MARGIN_SECONDS` (300 s). Both skew directions are tested
(`test_a_lagging_writer_clock_never_makes_its_fresh_lease_look_expired`,
`test_a_leading_writer_clock_never_shortens_the_lease_it_wrote`, `M13`).

## Audio inherits the same rule

The two audio cores follow the same fail-closed shape at their own boundary: a
non-finite scalar parameter (`floor`, `strength`, `smoothing`, `overlap`,
`noise_percentile`, `percentile`, `bias_correction_db`, `frame_length`,
`sample_rate`, `oversample`, `hop_seconds`) is a `ValueError` before any transform
runs, and `clean_reference` must be finite **and** the same length as `samples`.
Silently returning `snr_gain_db=None` for a broken reference is the audio equivalent
of deleting a lease without a precondition: the caller cannot tell "not applicable"
from "not measured" (`M23`, `M24`,
`test_wiener_rejects_nan_and_inf_scalar_parameters`,
`test_wiener_rejects_non_finite_clean_reference`).

## Evidence

* `tests/unit/test_personal_cloud_vault_lease_fence.py` — 22 tests, including the
  five lease state-machine proofs above.
* `tests/creative/audio_engine/test_finite_input_guards.py` — 33 tests, including
  the scalar and `clean_reference` contracts.
* `tests/architecture/test_cp4_fence_protocol_mutations.py` — 24 source mutants,
  each scored only when the *named* test is the one that fails; M17–M24 are the
  clause-by-clause proof of this page.
* Coverage of the four modules under `--cov-branch` on this suite:
  `byos_drivers.py` 92 %, `editorial_cut_solver.py` 100 %, `wiener_denoise.py` 81 %,
  `loudness_bs1770.py` 77 %.

## What this page does not claim

It does not claim the protocol is safe against a bucket that ignores
conditional-write headers — it is not, and no client-side scheme can make it so.
Every clause above is a precondition *sent to* the store; the store has to honour it.
It also does not claim liveness: a writer that loses the race is told to retry, and a
lease whose owner died is cleaned up only after its TTL plus the skew margin.
