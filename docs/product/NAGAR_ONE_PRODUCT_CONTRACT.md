# NAGAR One — Product Contract

Status: **living view** (updated in place). Authority: the repository owner's
mission brief `NAGAR-ONE` (Stage 0). This document is the single product frame
the engineering stages build against; when code and this page disagree, the code
wins and this page is corrected in the same PR.

## 1. What the product is

| Thing | What it is | What it is not |
|---|---|---|
| **NAGAR Studio** | The primary product: the creative workspace where a person states intent, previews, approves and inspects results. | Not a Telegram feature. |
| **NEXUS** | The engine underneath Studio: intelligence, policy, execution, verification and infrastructure. | Not a UI. |
| **Telegram** | One **channel adapter** to NEXUS (alongside the CLI and HTTP surfaces). | Not the product identity. |
| **PWA** | The first user-interface path. | Native mobile is out of scope this mission. |

Canonical identity is a channel-independent `Principal`
(`AppPrincipal` + `AppChannelBinding`); Telegram/CLI/HTTP are bindings to it.
Evidence: `storage/models.py` (`AppPrincipal`, `AppChannelBinding`,
`AppProject`, `AppProjectMembership`), `docs/architecture/PRODUCT_FRAME.md`.

## 2. The entities a user must be able to name

Project · Asset · Version · Intent · Preview · Approval · Job · Artifact ·
Evidence. Each maps to a concrete, inspectable record (see the truth matrix).

## 3. The two non-negotiable laws

1. **Intent ≠ Authority.** AI proposes (a typed plan); policy/authority permits;
   the CommandBus executes; an independent verifier judges. No agent, model or
   service may grant itself execution authority by producing a plan.
   Evidence: `creative/studio/bus.py` (the single write path),
   `nagar/ibpc/authority.py`, `nagar/ibpc/approval.py`.
2. **One authority per responsibility.** One queue, one execution authority, one
   artifact-passport chain, one provenance ledger, one project-revision store.
   A new store/queue/authority requires documented, evidence-backed necessity —
   the simpler existing mechanism wins.

## 4. The target architecture chain

```
Intent → Strategy → Typed Creative IR → Compiler → Policy/Authority
      → Command DAG → CommandBus → Sandboxed Execution → Independent Verifier
      → Evidence/Receipt → Artifact Passport → Lineage → Project Graph
```

`SWARM = THINK · GRAPH = DECIDE · BUS = EXECUTE · VERIFIER = JUDGE`.
Each stage's current implementation and gaps are recorded in
[`../engineering/NAGAR_ONE_TRUTH_MATRIX.md`](../engineering/NAGAR_ONE_TRUTH_MATRIX.md).

## 5. Traceability requirement

Every unit of work must be traceable: **Requirement → Evidence → Design → Code
→ Test → CI → Documentation**. A capability is only "done" when that chain is
unbroken; "implemented" without a test and evidence is not done.

## 6. Scope of the NAGAR-ONE mission (Stages 0–3)

- **Stage 0** — this contract + the truth matrix + architecture freeze.
- **Stage 1** — durable Project ↔ Revision ↔ AssetVersion relationship.
- **Stage 2** — Creative Graph & Passport closure (lineage from valid records).
- **Stage 3** — production-grade Live Editing (durable session/approval/privacy).

Explicitly out of scope: a full `/v1` API, a new Principal system, a PWA, native
mobile, and any new provider — these are later roadmap items.

## 7. Honesty rule

Nothing in this repository may be presented to a user as a real execution when it
is simulated, mock, description-only or in-memory-only. Capabilities that are not
production-ready must be labelled as such (see the truth matrix vocabulary:
`VERIFIED` / `PARTIAL` / `MISSING` / `BLOCKED` / `DEFERRED`).
