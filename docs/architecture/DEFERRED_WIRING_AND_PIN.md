# Deferred wiring & the schema-2 revision pin — design notes (2026-11-08)

Two items were assessed during the F1 campaign and deliberately **not**
implemented. Each is recorded here with the evidence and the owner decision it
needs, so a future slice starts from truth instead of re-deriving it.

## 1. AssetVersion producer wiring (task-282 Slice 2, `as an AssetVersion` leg)

**Goal.** When a `creative_render` job publishes its artifact, also register the
bytes in the Asset Library and populate `projectartifactbinding.artifact_version_id`,
so the advertised `artifact → revision → asset-version` chain resolves end-to-end.

**Why it is not implemented now.** The Asset Library
(`src/nexus_ai_agent/creative/assets/`, task-266) is owned by **another zone**.
The wiring requires importing `AssetLibraryService` (plus a blob store) into
`creative/render_jobs.py` — a cross-zone dependency into a capability with a
single owner and its own authorization model. Landing it from the durable-state
branch would be a boundary violation, and the durable-state boundary law
(`tests/architecture/test_durable_project_state_boundary.py`) exists precisely to
keep this package from reaching into other authorities.

**Design if/when the zone is handed over.**

- The composition root (not `render_jobs`) constructs the `AssetLibraryService`
  and injects an `ingest` callable into the publication, mirroring how
  `artifact_publications` is already injected into the queue. No new authority,
  no import from `project_state` into `assets`.
- The publication's `publish` step already measures the digest; the same bytes
  are handed to `ingest` (idempotent on the content address), and the returned
  `AssetVersion.version_id` is passed to `SqlArtifactBindingStore.bind(...)`.
- The bind path already accepts and validates an optional `artifact_version_id`;
  only the producer is missing.
- **Fail-closed:** an ingest failure must refuse the job exactly as a bind
  refusal does today (no artifact_version_id ⇒ no COMPLETED for a pinned job).

**Owner decision:** hand the zone over, or assign the wiring to the asset-library
owner with the durable bind contract as the interface.

## 2. Schema-2 canonical revision pin (D-0038 residual)

**The residual.** `CreativeRenderPayload._canonical_schema_is_complete` requires
`principal_id`, `project_id`, `channel_binding_id` and `intent` for
`schema_version == 2`, but does **not** require `revision_id` / `revision_hash`.

**What the truth is.** The **canonical handoff lane is already pinned end-to-end.**
`build_creative_handoff` refuses with `revision_unavailable` when the revision
store is absent or the admitted revision is missing (there is no "latest"
fallback), and `build_creative_payload` emits the pin whenever it produces a
schema-2 payload. So there is **no live hole** in the canonical lane; the residual
is only that the *model* does not forbid an unpinned schema-2 payload.

**Why the obvious fix is wrong.** Requiring the pin in the shared model
(`revision_id is not None` whenever `schema_version == 2`) was implemented and
measured: it turns **14 tests RED** because the **IBPC execution lane**
(`nagar/ibpc/service.py`, ~L860) is a *second*, legitimate schema-2 producer with
no project-revision concept at all. A lane-agnostic model requirement would break
a working lane. The change was reverted.

**Options (owner decision).**

1. **Accept** the admission-boundary refusal as sufficient — the canonical lane
   cannot produce an unpinned schema-2 job, so the invariant holds where it
   matters. Lowest cost; no code change.
2. **Lane-scoped enforcement** — add a worker-side requirement that a
   *canonical-origin* job (one carrying `intent`) must also carry the pin. Keeps
   IBPC unaffected; adds a real defense-in-depth check at the worker, which is
   where untrusted payloads are actually admitted.
3. A schema split (a distinct `canonical` payload type) — larger refactor, not
   warranted for this residual.

**Recommendation:** option 2, owned by the canonical-execution zone (the file is
under a queued `pr-repair-246` claim; the durable-state zone does not own it).

_This note was produced by an AI agent (OpenHands) on behalf of the repository
owner._
