# P0 runtime boundary

`OBSERVED_IN_CODE`: the existing product owns the durable queue, worker, CommandBus, renderer, verifier, and causal journal.

`PROPOSED/IMPLEMENTED`: `nagar.p0.runtime.preview_nel` is a pure planning adapter. It performs:

```text
NEL text -> typed IR (microsecond units) -> revision/tenant/project Semantic Time
resolution -> explicit timebase conversion to PTS ticks -> preview
```

It has no queue dependency and returns `job_created=False` unconditionally. Ambiguous, stale, low-confidence, cross-tenant, malformed, or project-mismatched requests stop before job creation.

`PROPOSED/IMPLEMENTED`: `canonical_command_input` maps only a `PREVIEW_READY` plan to the existing `timeline.trim` input vocabulary. It does not authorize or dispatch. The eventual commit adapter must re-check actor membership, tenant, policy, current revision, idempotency, and permission between preview and commit, then call the canonical CommandBus/job path. A model or NEL AST is never sufficient authority.
