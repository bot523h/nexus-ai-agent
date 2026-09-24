---
status: accepted
date: 2026-09-24
deciders: session arena/01a0d43c (task-179, Gate 2 reconciliation)
consulted: main @ 035a896, open PR#68, branch arena/01a0d3a8 (no PR), Stripe/IETF idempotency practice
informed: Agent 1 (runtime owner), Agent 2 (contract branch owner), gates queue
---

# 0005. Canonical command and capability contract: versioning, location, and reconciliation

## Context and Problem Statement

Two Gate 2 agent reports claimed incompatible canonical contracts. Agent 1
(open PR#68, branch `arena/01a0d37d`) evolves the merged `TypedCommand` with
`schema_version = 2` while keeping the external identifier `nagar.command.v1`,
wired into `CommandBus`. Agent 2 (branch `arena/01a0d3a8`, no PR) declares a
parallel `CommandEnvelope` with canonical `nagar.command.v2`, a new
`creative/contracts/` package, `docs/contracts/`, and ADR 0005–0008. Both
cannot be true: the protocol id, the envelope schema version, the code
location, and the documentation location each need exactly one canonical
answer. The behaviour contract itself is decided in
[`D-0013`](../../DECISION_LOG.md); this record decides versioning
governance, canonical location, and which proposal survives, with scored
evidence.

## Decision Drivers

- Repository fit: the winner must extend merged modules and the existing test
  suite, not duplicate them (weight 40%).
- Security: authorization and replay semantics must be enforced at the bus,
  not asserted in prose (weight 20%).
- Backward compatibility: the six shipped manifests and the in-process
  runtime call sites keep working (weight 15%).
- Simplicity: one envelope, one registry, one bus; no parallel universe
  (weight 15%).
- Testability: every claim needs an executable negative proof (weight 10%).

## Considered Options

1. **(A) Evolve in place, compat-preserving** — protocol `nagar.command.v1`
   + envelope schema `1|2`, optional claims, fail-closed authorization seam,
   scoped payload-bound idempotency; legacy shape-1 dispatch preserved.
2. **(B) Agent 2's parallel v2 envelope** — `creative/contracts/` package,
   `CommandEnvelope` with `envelope_version` default `nagar.command.v2`,
   `docs/contracts/`, ADR 0005–0008.
3. **(C) Minimal additive change** — new fields with no enforcement.
4. **(D) Full v2 cutover** — rename the protocol id to `nagar.command.v2`
   everywhere (code, manifests, docs, logs).
5. **(A′) PR#68 verbatim** — option A but with required actor/project/
   provenance claims, breaking the runtime-owned call sites.

## Decision Outcome

Chosen option: **(A)**, because it is the only option that is simultaneously
enforced at the bus, backward compatible, and confined to the contract
boundary. Scoring (evidence-weighted, 100 points):

| Option | Repo fit (/40) | Security (/20) | Compat (/15) | Simplicity (/15) | Testability (/10) | Total |
|---|---|---|---|---|---|---|
| A (chosen) | 40 | 17 | 14 | 14 | 10 | **95** |
| B (Agent 2) | 8 | 10 | 6 | 5 | 6 | 35 |
| C (minimal) | 38 | 8 | 14 | 13 | 7 | 80 |
| D (cutover) | 15 | 15 | 0 | 8 | 8 | 46 |
| A′ (PR#68 verbatim) | 35 | 18 | 5 | 10 | 9 | 77, infeasible |

- B loses on evidence, not taste: zero bus integration (the envelope is
  never dispatched), duplicated models and error hierarchies, a hardcoded
  operation snapshot that rots, an actor `default_factory` that cannot
  construct, system-behaviour records filed in the docs-layer ADR directory
  against this directory's rule 3, and a hardcoded weakening of
  `test_docs_integrity.py` instead of indexing. No PR, no CI.
- C keeps parsing but leaves authorization unenforced, failing the gate's
  acceptance criteria.
- D rewrites a protocol id pinned by six manifests, the state code, the TDD,
  and the decision log, for no wire change.
- A′ is the substantive winner on security but breaks
  `creative/slideshow/*` call sites the contract lane must not own; the
  reconciliation keeps its pipeline and hardens compat instead. This gate
  supersedes PR#68's contract scope; PR#68's queue hardening and runtime
  call-site migrations stay valid follow-ups for their lanes.

Canonical locations: the contract lives in `creative/studio/` (code) and
`docs/architecture/COMMAND_CAPABILITY_CONTRACT.md` (documentation). A
`docs/contracts/` tree is rejected: ADR 0001 fixes the living-docs layout,
and every page there must be indexed and linked.

### Consequences

- (+) One canonical contract with executable negative proofs and a scored,
  reviewable decision trail.
- (+) Zero changes outside `creative/studio/`, contract tests/guards, and
  docs; the full suite stays green without touching runtime files.
- (+) The v1-vs-v2 dispute is closed structurally: a `v2` protocol id is
  banned from `src/` by an architecture guard.
- (−) The deprecated implicit-local-trust path survives until the runtime
  owner lands explicit grants (board task-181).
- (−) Expected textual conflicts with PR#68 (same studio/docs files) and
  PR#67 (`bus.py`, `CREATIVE_STUDIO.md`); merge order resolves them.
- (~) Salvaged from Agent 2: advisory capability snapshots, fail-closed
  locality, the reserved `preview` mode, and the operation-matrix question
  answered from live builders.

## Confirmation

- `pytest -q tests/unit/test_command_capability_contract.py
  tests/architecture/test_command_capability_boundary.py` — versioning,
  authorization, capability, policy, reference, idempotency, revision, and
  pipeline-order proofs, including `nagar.command.v2` rejection.
- `tests/architecture/test_command_capability_boundary.py::test_single_canonical_envelope_and_protocol`
  — the structural pin (one envelope, one protocol, no v2 id in `src/`).
- `pytest -q tests/unit/test_docs_integrity.py` — this record indexed in
  `adr/README.md` and `docs/README.md`.
- `ruff check . && ruff format --check .`, `mypy src`,
  `pytest -q -m "not slow"` — gates green with zero regression.

## Pros and Cons of the Options

### Option A — evolve in place, compat-preserving (chosen)

- (+) extends merged modules; enforced at the bus; runtime untouched
- (+) additive versions with a documented sunset rule
- (−) carries one deprecated legacy path until task-181 lands

### Option B — Agent 2's parallel v2 envelope

- (+) raises real questions (matrix, snapshots, locality, dry-run)
- (−) parallel package with no bus integration; hardcoded snapshots; gate
  weakening; no PR or CI; violates the ADR scope rule

### Option C — minimal additive change

- (+) smallest diff
- (−) authorization unenforced; fails the gate criteria

### Option D — full v2 cutover

- (+) a single new identifier everywhere
- (−) rewrites pinned contracts for no wire change; maximal blast radius

### Option A′ — PR#68 verbatim

- (+) strongest fail-closed posture of the candidates
- (−) breaks runtime-owned call sites outside the contract lane

## More Information

- [`D-0013`](../../DECISION_LOG.md) — the behaviour decision (pipeline,
  authorization, idempotency, revision semantics)
- [`../COMMAND_CAPABILITY_CONTRACT.md`](../COMMAND_CAPABILITY_CONTRACT.md) — the canonical contract page
- [PR#68](https://github.com/bot523h/nexus-ai-agent/pull/68) — Agent 1's proposal (superseded contract scope)
- Branch `arena/01a0d3a8-nexus-ai-agent` — Agent 2's proposal (rejected; no PR)
- [`../REFERENCES.md`](../REFERENCES.md) §3 — docs-as-code practice behind the location rule
