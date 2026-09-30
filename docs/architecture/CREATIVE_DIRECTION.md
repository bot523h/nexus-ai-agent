# Nagar Creative Direction — the Intent-First Backbone and the First Vertical Slice

**Status:** Living document (product direction; revised when the chosen slice changes)
**Scope:** the product-level architecture *above* the proven execution chain — what the backbone is,
which vertical slice is built first, and what is deliberately deferred
**Verified against:** `main` @ `e5b326b` (v3.13.0) for the merged seams, plus the creative-execution
spine landed with this page (`src/nexus_ai_agent/creative/spine/`)
**Owner zone:** `docs-architecture` (see `.agents/board.json`)

> This page records a **decision about direction** and reports the state of the first slice honestly.
> The execution chain it builds on is described in [`CREATIVE_STUDIO.md`](CREATIVE_STUDIO.md) and
> [`COMMAND_CAPABILITY_CONTRACT.md`](COMMAND_CAPABILITY_CONTRACT.md). Any row in §2 or §4 that
> disagrees with the tree is a defect in this page.

---

## 1. The frame: intent, not tools

Nagar is not an "AI editor". An editor still hands the user a tool palette — saturation, contrast,
a Gaussian blur radius — and leaves the sequencing to the human. The product frame is the opposite:
the user states a **goal and its constraints**, and the system decides what has to change, compiles
that decision into typed operations, executes them under authority, and *proves* the result.

| The old frame | The Nagar frame |
|---|---|
| user picks a tool and its parameters | user states an intent and its constraints |
| the tool does what it is told | the system derives the operations the intent requires |
| "it rendered" is the result | a measured artifact plus its lineage is the result |
| memory is the project file | memory is the project graph |

The rest of this page is the consequence of that frame: one backbone, one first slice, and an
explicit list of the capabilities that must wait for it.

## 2. The backbone

Every feature — present or future — is a projection of one loop. If a proposed capability cannot be
expressed as an entry point or an exit point of this loop, it is not ready to be built.

```mermaid
flowchart TB
    intent["Intent<br/>goal + constraints"]
    graph["Creative Graph<br/>intents · capabilities · artifacts · evidence"]
    compiler["Capability Compiler<br/>intent → typed plan"]
    policy["Policy and Authority<br/>grants · permissions · trust root"]
    bus["CommandBus<br/>the only dispatch path"]
    lane["Render lane<br/>one FFmpeg process"]
    verify["Verification<br/>probe + sha256"]
    artifact["Artifact and Lineage<br/>bound to intent and revision"]
    intent --> graph
    graph --> compiler
    compiler --> policy
    policy --> bus
    bus --> lane
    lane --> verify
    verify --> artifact
    artifact --> graph
```

Read against the tree, this loop is now **built in memory and guarded end to end**; what remains is
**durability**:

| Stage | State in the tree | Where |
|---|---|---|
| Intent | **present** — typed `Intent` + a resolver port (rules today, LLM later) | `creative/spine/models.py` |
| Creative Graph | **present, in-memory** — an append-only node graph (intent · capability · artifact · evidence) with parent edges; **not yet persisted** | `creative/spine/models.py` |
| Capability Compiler | **present** — compiles an intent to registry-checked operations; refuses an operation the registry does not expose | `creative/spine/compiler.py` |
| Policy and Authority | **present** — grants, the A/B/C/D ladder, the pack trust root | `creative/studio/authorization.py`, `creative/packs/trust.py` (ADR 0006) |
| CommandBus | **present** — one envelope, one registry, one atomic dispatch; the spine dispatches through it and never around it | `creative/studio/bus.py`, `creative/spine/execution.py` |
| Render lane | **present** — the only process-spawn site, no shell | `creative/rendering/executor.py` |
| Verification | **present** — probe + sha256, six-state job lifecycle, fenced CAS | `creative/rendering/executor.py`, `jobs/verification.py` |
| Artifact and Lineage | **present, in-memory** — each executed command yields an artifact node bound to its transaction and evidence; **not yet a durable row** | `creative/spine/execution.py` |

The remaining gap is therefore precise: **the graph and the lineage are not yet durable**, and the
reference→recipe capability is a heuristic port, not a media analyzer (see §6). Everything else the
loop needs exists and is guarded.

## 3. Why the tempting capabilities are not the first slice

The strongest-looking candidate — reverse engineering a trending reference into an editing recipe —
is an *input* to the compiler, not the backbone. Choosing it first would build an extractor with
nowhere to attach what it extracts: no intent to satisfy, no project revision to bind a decision to,
no lineage to attribute a result to. It would be a one-shot trick, not an engine. The spine now
gives it somewhere to attach, so it is the *next* capability (§6), not the first.

The same test applies to the rest:

| Candidate | Why it is not first | It is really a |
|---|---|---|
| Reference → Creative Recipe (reverse engineering) | needs a graph to attach a recipe to, and a confidence-labeled model so it extracts a *concept*, never a copy | compiler input |
| Multiverse (parallel variants) | branches without a persisted graph and lineage have nothing to branch from or invalidate | projection of the graph |
| Closed-loop social feedback | "learn" needs artifact lineage plus audience evidence before any pattern can be attributed | exit point of the loop |
| Physics / atmosphere packs | a capability, not a seam — it must land through the compiler and registry like every other pack | capability |
| Pack marketplace | needs an external publisher authority the trust root does not yet grant | ecosystem layer |
| Creative intelligence / style DNA | needs months of lineage, and must be reported as an *observed pattern with confidence and evidence*, never asserted as fact | graph analytics |

Each of these has an evidence-based trigger, not a date. They are listed in §6.

## 4. The first vertical slice

**Slice name:** *One asset, one intent, one verified artifact — the explainable single-project loop.*

The slice is deliberately the smallest loop that exercises every stage of §2 exactly once:

```mermaid
flowchart LR
    u["One user<br/>one asset"]
    i["One typed intent<br/>goal + constraints"]
    g["Graph nodes<br/>intent + capabilities"]
    p["Typed plan<br/>registry-checked commands"]
    b["CommandBus dispatch<br/>policy + authority"]
    a["Artifact node<br/>+ evidence"]
    r["SpineRun<br/>measured facts, no assertions"]
    u --> i --> g --> p --> b --> a --> r
```

**Shipped in this slice (`creative/spine/`):**

* a typed, immutable `Intent` (goal + constraints + project scope) and a `RulesIntentResolver`
  behind an `IntentResolver` port (an LLM resolver is a drop-in);
* a `RulesCapabilityCompiler` that compiles an intent to registry-checked operations and refuses an
  operation the registry does not expose — the registry stays the single allow-list;
* a `GraphIntentPlanner` that records a capability node per planned operation, parented on the
  intent;
* `CreativeExecutionSpine`, which dispatches every step through `CommandBus` (schema-2 commands with
  actor + provenance when an actor is configured) and records an artifact node + an `EvidenceRecord`
  per run;
* the first capability of §6, `reference → CreativeRecipe → Intent`, as a heuristic analyzer behind
  a `RecipeAnalyzer` port that abstracts a *strategy*, never a copy.

**Proven by tests:** `tests/unit/test_creative_spine.py` (the loop, lineage, policy refusal, the
no-copy constraint), `tests/architecture/test_spine_boundary.py` (the spine is not a second write
path; stdlib + pydantic + studio only), `tests/unit/test_spine_mutations.py` (5/5 mutants killed).

**Explicitly out of scope for the slice (the remaining gap):** a *durable* graph/lineage store
(this slice is in-memory, keyed by the bus's `state_revision`/`state_hash`), real perceptual
reference analysis, new media operations, new packs, any learning, any branching, any social
connector. A slice that adds a capability and a seam at once cannot tell you which one broke.

## 5. Acceptance criteria for the slice

A slice is complete only when each of these is proven by a named test. Status is reported honestly:

1. **Typed intent.** A `CreativeIntent` (goal + constraints + project scope) round-trips; an empty or
   malformed request is refused typed, before any handler runs. **Proven** —
   `test_resolver_rejects_an_empty_request`, `test_intent_for_a_different_project_is_rejected`.
   *Durability is not yet claimed (in-memory only).*
2. **Compiler resolves against the registry.** The intent compiles to a plan and every operation
   resolves through the registry; an operation outside the registry is refused *before* dispatch.
   **Proven** — `test_compiler_refuses_an_operation_absent_from_the_registry`,
   `test_compiler_refuses_to_guess_a_target_operation`.
3. **One dispatch path.** Execution enters through `CommandBus` only. **Proven** —
   `tests/architecture/test_spine_boundary.py::test_only_the_bus_invokes_an_operation_handler`
   (the spine is not a second write path).
4. **Lineage is a fact, not a claim.** Each artifact node binds the transaction, the resulting
   `state_hash`, and an `EvidenceRecord`. A command the bus refuses produces no artifact. **Proven** —
   `test_artifact_lineage_is_traceable_to_its_intent`, `test_bus_refusal_produces_no_artifact`.
5. **Stale-safe.** The spine refuses an intent for a foreign project and reuses the bus's existing
   `state_revision`/`state_hash` preconditions, so a stale plan is refused untouched. **Proven for
   the foreign-project case**; revision-precondition wiring is the bus's existing behaviour, exercised
   by `tests/unit/test_command_capability_contract.py`.
6. **No unlabeled assertion.** A reference analysis is heuristic and carries a low, explicit
   confidence; the recipe carries the "do not copy the reference" constraint. **Proven** —
   `test_reference_becomes_a_strategy_not_a_copy`, `test_recipe_to_intent_carries_the_no_copy_constraint`.

## 6. Deferred capabilities and their triggers

| Capability | Trigger (all evidence-based) |
|---|---|
| Durable graph + lineage store | this in-memory slice is green **and** a storage adapter persists graph nodes and round-trips them by `(intent_id, project_id, state_revision)` |
| Real reference → Creative Recipe | a media-capable `RecipeAnalyzer` (behind the existing port) extracts composition/timing/colour/pacing with measured confidence, and a test proves reference → *recipe*, never reference → copy |
| Multiverse | durable graph + artifact lineage exist **and** an incremental re-render test proves only the branches whose decisions changed are invalidated |
| Closed-loop social feedback | artifact lineage exists **and** a social-connector port plus an evidence table records audience response with confidence |
| Physics / atmosphere packs | the compiler and registry carry them as packs through the existing lifecycle gate (pack maturity `STABLE`/`EXPERIMENTAL`) — no new seam |
| Pack marketplace | an external publisher authority is added to the trust root (ADR 0006 ships with none today) and `PackRegistry.activate()` accepts a genuinely signed external pack |
| Creative intelligence / style DNA | enough lineage exists to report a pattern with confidence and evidence, and the model is structurally forbidden from asserting an unlabeled fact |

## 7. What this changes about the existing documents

- [`CREATIVE_STUDIO.md`](CREATIVE_STUDIO.md) describes the **execution** chain (bus, packs, lane);
  this page describes the **product** chain above it. They must not disagree about which stages
  exist — the §2 table is the reconciliation.
- Any new capability must still respect the pack rules in
  [`MODULE_MAP.md`](MODULE_MAP.md) §5 and the trust root in
  [`adr/0006`](adr/0006-capability-pack-trust-root.md).
- The decision itself is recorded in [`../DECISION_LOG.md`](../DECISION_LOG.md) (D-0024). When a
  summary here disagrees with the log, the log wins.

## 8. How to verify this document

```bash
# the slice's own guards (loop, boundary, mutation)
pytest -q tests/unit/test_creative_spine.py tests/unit/test_spine_mutations.py \
          tests/architecture/test_spine_boundary.py
# the seams named in §2 exist and are guarded
pytest -q tests/architecture/test_command_capability_boundary.py \
          tests/architecture/test_nagar_studio_isolation.py \
          tests/architecture/test_rendering_lane_boundary.py
# documentation integrity (index, links, diagrams, no placeholders)
pytest -q tests/unit/test_docs_integrity.py
```

If a row in §2 or §4 disagrees with the tree, the document is wrong — fix it in the same PR that
changed the code (the rule in [`../README.md`](../README.md)).
