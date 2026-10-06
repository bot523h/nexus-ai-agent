# Free-text → verified artifact (Gate-C vertical slice)

A single, additive production surface that turns **user free text** into a
**verified artifact**, using only substrate that already exists.  It adds no
capability, no policy, no actor source and no second execution path.

The slice is now **durable**: the typed intent is persisted as one logical job
on the existing queue and executed by the existing worker, so it survives a
process crash and cannot be double-executed.

## The one path

```mermaid
flowchart TD
    A[user free text] --> B[normalize + bound 500 chars]
    B --> C[CognitionGateway.run]
    C --> D[DeterministicRouter: level]
    D -->|L2 model selected| E[LocalCognition over injected LLMPort]
    D -->|L4 human / blocked| Z[clarification_required]
    E --> F[parse_proposal -> TypedProposal | Refusal]
    F --> G[offered set = registry ∩ timeline.trim]
    G --> H[typed canonical creative_render payload]
    H --> I[JobQueuePort.enqueue - one durable job, stable intent key]
    I --> J[worker: existing creative_render_job]
    J --> K[existing CommandBus: policy + authorizer + apply]
    K --> L[timeline.trim artifact + history]
    L --> M[verify: independent judgment]
```

The seam is `nagar.creative.run_free_text_intent` (gateway-only, no dispatch)
and its durable successor `nagar.creative.run_free_text_intent_durable`, which
turns the typed proposal into the canonical `CreativeRenderPayload`
(`extra="forbid"`) and enqueues it.  Neither calls `CommandBus.dispatch` — the
existing worker does, through `CognitionGateway`/`build_job_bus`, the one place
model output can become a candidate command.

The host caller is `nexus intent <text>` in `cli.py`: it composes the provider
(`nagar.composition`), builds the bus through the single canonical factory
(`creative.render_jobs.build_job_bus`), enqueues the durable job, drains it,
prints the durable outcome plus an independent verification verdict.  With no
model configured it prints `clarification_required` and enqueues nothing.

## Why `timeline.trim`

`timeline.trim` is registered, `REVERSIBLE` and `AVAILABLE`
(`creative/studio/capabilities.py`, `creative/packs/edit/`) and already has a
canonical handler, an input model (`TrimInput`) and a real bus pipeline.  It is
the smallest operation that produces a real, independently checkable artifact
(a derived asset with hash + lineage), so the slice proves the *whole* chain
without inventing a new operation.

## Invariants (each is a test)

| Invariant | Test |
|---|---|
| free text → typed proposal → registry → bus → artifact | `test_nagar_free_text_slice.py::test_free_text_produces_a_verified_artifact` |
| the actor comes from the composition root, never the model | `…::test_free_text_is_typed_before_dispatch_and_actor_comes_from_root` |
| no provider ⇒ explicit clarification, never a command | `…::test_no_provider_yields_clarification_never_a_command` |
| 15 hostile model outputs never dispatch | `…::test_hostile_model_output_never_dispatches` |
| retrieved memory text cannot create authority | `…::test_retrieved_memory_text_cannot_create_authority` |
| the slice never owns a bus / authorizer | `test_nagar_creative_slice_boundary.py` |
| typed intent → one durable job, same key collapses | `test_nagar_cognition_queue_handoff.py` |
| real artifact, tamper-refusing, survives process death | `test_nagar_durable_handoff_e2e.py` |
| the slice never imports a worker/queue/verifier | `test_nagar_durable_handoff_boundary.py` |
| the CLI Model Kill Test clarifies and enqueues nothing | `test_nagar_intent_cli.py::test_cli_intent_with_no_model_reports_clarification_and_enqueues_nothing` |

## Durable handoff (the queue contract)

The typed proposal never enters the queue as free text or a command name.  It is
translated by `nagar.creative.handoff.build_creative_payload` into the canonical
`CreativeRenderPayload`:

* `command` / `operation` are **constants** (closed mapping), never model values;
* `workspace_dir`, `input_path`, `media_duration_us`, `user_id`, `chat_id` and
  the `idempotency_key` are **composition-root facts**, never model output;
* the only model-supplied values are the trim in/out points, strictly validated
  (finite, ordered, within the authoritative duration);
* `job_type` is the closed constant `creative_render`, so no model text can name
  a handler.

The intent idempotency key is derived deterministically from
`actor + project + normalized intent` (`cognition:<sha256>`), so the same intent
collapses to one logical durable job while a genuinely different intent gets a
distinct identity.  `job_id`, `command_id` and the intent key are three distinct
concepts.

## Independence

`verify_trim_artifact` re-reads the bus's **committed** project and checks the
derived asset's hash, lineage and duration.  It does not trust the handler's
returned dictionary, so the producer is not the sole judge of its artifact.

## Honest limits

* One operation only.  Broader intent coverage needs more registered operations
  and a richer projection of deterministic facts — not a new path.
* The provider is real and injected (`nagar.composition.build_cognition_provider`)
  and a real host caller exists (`nexus intent`).  The applied path has only been
  driven with a *declared* scripted model at the external seam — no paid/cloud
  model has been exercised in this environment, so the claim is ARTIFACT-PROVEN
  and DURABLE-PROVEN, not PRODUCTION-PROVEN.
* Durable proof: the typed job is persisted by the real `InProcessJobQueue`,
  collapses duplicate intents onto one logical job, survives the death of the
  enqueuing process (`resume_pending_jobs`), and renders a real `.mp4` whose
  sha256 + probe are independently verified; mutating the bytes fails
  verification (`test_nagar_durable_handoff_e2e.py`).
* Recipe crystallization is **not** activated — no independently validated
  recipe exists.
* With no model configured the slice *clarifies*; it does not invent.  This is
  the Model Kill Test at the slice level, and it proves infrastructure/model
  separation only, not open-ended creativity.

See [`../overnight/COGNITION_CONVERGENCE.md`](COGNITION_CONVERGENCE.md) for the
gateway itself, and `nagar_overnight/REPORT.md` for the live mission record.
