# P0 runtime boundary

`OBSERVED_IN_CODE`: the existing product owns the durable queue, worker, CommandBus, renderer, verifier, and causal journal.

`IMPLEMENTED`: `nagar.p0.runtime.preview_nel` is a pure planning adapter. It performs:

```text
NEL text -> typed IR (integer microseconds) -> revision/tenant/project Semantic Time
resolution -> rational PTS/timebase conversion -> bounds/evidence checks -> preview
```

PTS conversion uses exact rational arithmetic and rounds once to integer microseconds. A signed NEL delta is applied to the rational endpoint before rounding; binary floating point is not used. Positive extensions require authoritative source bounds. Ambiguous, stale, low-confidence, cross-tenant, malformed, missing-evidence, or project-mismatched requests stop before job creation. Evidence is accepted only from verified, digest-bound, project/tenant/revision-scoped `EvidenceRef` records; `source_model` is descriptive metadata, never proof.

`IMPLEMENTED`: `canonical_command_input` maps only a `PREVIEW_READY` plan to the existing `TrimInput` vocabulary (`clip_asset_id`, `in_point_us`, `out_point_us`). Plan metadata such as semantic segment ID, revision, evidence requirements, and canonical NEL hash is carried in the typed `P0PlanEnvelope`, never smuggled into the operation input. `TrimInput` validation is repeated at the mapping boundary and optional fields are omitted.

`IMPLEMENTED`: `nagar.p0.commit.commit_preview` is the first queue-facing adapter. It rechecks project/tenant/revision, durable directory authorization, channel binding, write permission, source-asset bounds, and canonical payload validation before calling the existing `CREATIVE_RENDER_JOB_TYPE` queue. It requires the existing provenance observer and emits a post-enqueue `JOB_ENQUEUED` fact linked to the canonical NEL hash, operation/idempotency key, semantic segment, revision, and evidence requirements; it does not create a second ledger. It returns an actual queue job ID and status `enqueued`; it never reports renderer success. The existing worker remains responsible for the second authorization check, CommandBus mutation, artifact verification, and result/passport evidence.

`IMPLEMENTED/FAIL-CLOSED`: zero-delta trims are rejected as `noop_trim`; `PRESERVE speech_continuity=true` is not silently claimed as enforced and returns typed clarification; negative trims whose intent names a pause are rejected as `unsupported_intent` rather than treating a selected pause as the whole output. The current canonical render payload has no field for a durable project revision or semantic plan envelope, and the worker reconstructs its minimal project at revision zero. Therefore `commit_preview` refuses nonzero revisions rather than pretending that a preview for a later revision is protected by the queue row. Supporting multi-revision Nagar commits requires a reviewed extension of the canonical intent/worker precondition schema; this P0 does not bypass that boundary. Durable causal linkage uses the existing job/intent/artifact chain and observer; semantic plan metadata is not fabricated into a passport.

`BLOCKED/EXPLICIT`: this slice does not implement intent-to-NEL generation, durable timeline mutation/revision fencing beyond the existing revision-zero seam, or a new causal persistence store. Those are follow-on contracts requiring the canonical application/worker owners and are not represented as completed behavior here.
