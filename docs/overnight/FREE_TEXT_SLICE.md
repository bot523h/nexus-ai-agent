# Free-text → verified artifact (Gate-C vertical slice)

A single, additive production surface that turns **user free text** into a
**verified artifact**, using only substrate that already exists.  It adds no
capability, no policy, no actor source and no second execution path.

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
    G --> H[proposal_to_command: actor/project from root]
    H --> I[CommandBus: policy + authorizer + apply]
    I --> J[timeline.trim artifact + history]
    J --> K[verify_trim_artifact: independent judgment]
```

The seam is `nagar.creative.run_free_text_intent`.  It is the **only** function
that accepts free text, and it never calls `CommandBus.dispatch` itself — it
delegates to `CognitionGateway`, which is the one place model output can become
a candidate command.

The host caller is `nexus intent <text>` in `cli.py`: it composes the provider
(`nagar.composition`), builds the bus through the single canonical factory
(`creative.render_jobs.build_job_bus`), runs the slice, prints an independent
verification verdict and can write a real JSON receipt.  With no model
configured it prints `clarification_required` and executes nothing.

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
| the real CLI caller applies and writes a verified receipt | `test_nagar_intent_cli.py::test_cli_intent_applies_and_writes_verified_receipt` |
| the CLI Model Kill Test clarifies and executes nothing | `test_nagar_intent_cli.py::test_cli_intent_model_kill_reports_clarification_and_writes_receipt` |

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
  model has been exercised in this environment, so the claim is ARTIFACT-PROVEN,
  not PRODUCTION-PROVEN.
* Recipe crystallization is **not** activated — no independently validated
  recipe exists.
* With no model configured the slice *clarifies*; it does not invent.  This is
  the Model Kill Test at the slice level, and it proves infrastructure/model
  separation only, not open-ended creativity.

See [`../overnight/COGNITION_CONVERGENCE.md`](COGNITION_CONVERGENCE.md) for the
gateway itself, and `nagar_overnight/REPORT.md` for the live mission record.
