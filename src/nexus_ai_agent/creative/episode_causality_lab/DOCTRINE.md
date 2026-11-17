# ATLAS — Episodic Causality & Edit-Planning Lab (design note)

**Status:** EXPERIMENTAL. This package is an *advisory analysis layer*. It is not a
registered production capability and does not own project state.

**Zone:** `src/nexus_ai_agent/creative/episode_causality_lab/**` and
`tests/creative/episode_causality_lab/**`. No files outside this zone are edited by
the lab. It deliberately does **not** create another `CommandBus`, executor, asset
database, passport, audio DSP engine, or Chronos physics kernel.

## 1. Purpose and scope

ATLAS consumes an *explicitly declared* structured story and produces:

* typed continuity findings with source references;
* exact temporal-consistency results with a real negative-cycle witness;
* a bounded, deterministic edit-plan proposal (or an explicit refusal);
* incremental-invalidation results that provably equal a full recomputation;
* an advisory handoff for the authorized execution layer.

**It does not** infer biometric identity, invent characters, prove that two real
actors look identical, or certify media quality. Anything not explicitly supplied
is reported as `UNKNOWN` / `NEEDS_REVIEW`, never silently upgraded to `PASS`.

## 2. Status semantics

`PASS`, `FAIL`, `UNKNOWN`, `NEEDS_REVIEW`, `INFEASIBLE`, `FEASIBLE_NOT_PROVEN_OPTIMAL`,
`PROVEN_OPTIMAL` (only for the exact finite problem solved), `RESOURCE_LIMIT`,
`UNSUPPORTED`. Absence of data is never evidence of continuity.

## 3. Milestone A — three-valued causal state

Facts are `TRUE` / `FALSE` / `UNKNOWN`. An unstated fact stays `UNKNOWN` unless the
input declares a closed-world policy. State transitions are pure functions of
`(WorldState, Event)`. Findings are emitted when a declared fact contradicts an
event (e.g. transferring a prop the actor is explicitly known not to hold). When
possession is `UNKNOWN`, the result is a `NEEDS_REVIEW` finding, not a failure and
not a pass. Flashback edit-order is not story-world order.

## 4. Milestone B — exact temporal consistency

A Simple Temporal Network over exact integer ticks. A constraint
`lower <= t_B - t_A <= upper` becomes edges `A -> B : upper` and `B -> A : -lower`.
Infeasibility is detected with Bellman–Ford (super-source), and the reported cycle
is independently re-weighed so `total_weight < 0` is verified, not asserted. Node,
edge and relaxation limits are enforced; hitting a limit yields `RESOURCE_LIMIT`,
never `PASS`.

## 5. Milestone C — bounded edit-plan optimizer

A dynamic program over `(beat_index, previous_candidate)` with at-most-once
candidate use and contiguous beat coverage. Stable tie-breaking makes identical
inputs produce identical proposals. Unknown compatibility evidence is never treated
as verified. `PROVEN_OPTIMAL` is returned only when the complete finite search
finished; a budget cutoff returns `FEASIBLE_NOT_PROVEN_OPTIMAL` when a complete plan
exists, else `RESOURCE_LIMIT`. The score is an editorial preference, not a claim of
objective quality.

## 6. Milestone D — incremental analysis

`IncrementalEngine.update(prev, prev_report, new)` shares the exact `replay`
transition code with the full engine, so `incremental == full_recompute` holds for
findings and statuses. When dependency information is incomplete the engine falls
back to full recomputation rather than risk a false `PASS`.

## 7. Milestone E — real artifact handoff

`export_report` writes deterministic bytes through `WorkspaceFilesystem` (the
repository's authorized, symlink-resistant boundary), then independently re-opens
the file and re-hashes it; `artifact.verified` is `True` only if the on-disk bytes
match. `build_handoff` emits an `advisory` manifest — it never mints a passport or
dispatches real commands.

## 8. Milestone F — adversarial and falsification tests

The suite contains 20+ adversarial input/refusal cases: duplicate ids, unsupported
schema, missing references, conflicting possession, knowledge-before-reveal,
unknown-as-pass, ambiguous ordering, contradictory bounds and negative cycles, an
invalid witness, negative frame budgets, forged `verified=True`, missing evidence,
resource limits, wrong tie-breaking, excessive nesting, path traversal, and
tampered persisted output.

## 9. Known limitations

* Natural-language screenplay understanding is **out of scope**; only the declared
  structured format is parsed.
* The temporal module reasons about *declared planning constraints*; it does not
  assemble encoded media timestamps (that is Chronos) and does not implement an
  FFmpeg concat engine.
* The optimizer is optimal only for the finite candidate set under its documented
  assumptions.
* No real preview render is executed here; the exact remaining wiring for a later
  authorized integration PR is recorded in the handoff note.
