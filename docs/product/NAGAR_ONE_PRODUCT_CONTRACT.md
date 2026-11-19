# NAGAR One — Product Contract

Status: **living view** (updated in place; last re-anchored 2026-11-19 to `main` @
`d6876ab378b88b2ccca1187b1f3c51be5bf52657`, v3.13.0). Authority: the repository
owner's mission brief `NAGAR-ONE` (Stage 0). This document is the single product frame
the engineering stages build against; when code and this page disagree, the code
wins and this page is corrected in the same PR.

## 1. What the product is

| Thing | What it is | What it is not |
|---|---|---|
| **NAGAR Studio** | The primary product: the creative workspace where a person states intent, previews, approves and inspects results. | Not a Telegram feature. |
| **NEXUS** | The engine underneath Studio: intelligence, policy, execution, verification and infrastructure. | Not a UI. |
| **Telegram** | One **channel adapter** to NEXUS (alongside the CLI and HTTP surfaces). | Not the product identity. |
| **PWA** | The Studio shell (`GET /studio/`, static, dependency-free) — a *client* of the verified read API, not a delivery channel of its own. | Not a second authority, not an offline write path, and not the delivery contract in Stages 0–3 (see §1.1). |

Canonical identity is a channel-independent `Principal`
(`AppPrincipal` + `AppChannelBinding`); Telegram/CLI/HTTP are bindings to it.
Evidence: `storage/models.py` (`AppPrincipal`, `AppChannelBinding`,
`AppProject`, `AppProjectMembership`), `docs/architecture/PRODUCT_FRAME.md`.

### 1.1 Delivery path vs. interface: which one is authoritative

Earlier revisions of this page said both "the PWA is the first user-interface
path" (§1) and "a PWA is explicitly out of scope" (§6). Both statements were
about different things, and leaving them side by side made the contract read as
self-contradictory — so the split is now explicit:

* **Authoritative delivery in Stages 0–3 = verified HTTP read API + CLI.** A
  result is delivered when the durable records exist and a reader can fetch and
  inspect them through the gated read surface (`StudioReadService` behind
  `/api/studio/projects/{p}/jobs/{j}/state`, plus the CLI entry points). That is
  what "done" means for this mission; the records, the passport, the evidence
  tree and the explicit gaps are the product output.
* **The PWA is the next interface layer on top of that same API.** The shell at
  `GET /studio/` is real and tested (session cookie, CSP, no secrets in the
  client) but it is read-only, and it is a *consumer*: it adds no authority, no
  durability and no delivery guarantee of its own. A full PWA — write paths,
  per-user identity, approvals, artifact download — is the next step, not the
  current contract, and every one of those items is gated on a Stage 3
  capability (durable session/approval) that does not exist yet.
* **Named gap, stated rather than hidden:** an authorized artifact-download
  route is currently DEFERRED (`src/nexus_ai_agent/web/README.md`, "What is
  deliberately not built"). Until it exists, "delivery" means the read API and
  the CLI — not a download link.

Consequence for reviewers: a change that adds PWA screens is welcome, but it
cannot be cited as evidence that a Stage 0–3 capability shipped. The evidence for
that is a record plus a passing test, exactly as §5 requires.

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

Explicitly out of scope for Stages 0–3: a full `/v1` API, a new Principal
system, a **complete** PWA (write paths, per-user identity, approvals,
authorized artifact download — §1.1), native mobile, and any new provider. These
are later roadmap items; the existing read-only Studio shell (§1) is not one of
them and is not a delivery path either.

## 7. Honesty rule

Nothing in this repository may be presented to a user as a real execution when it
is simulated, mock, description-only or in-memory-only. Capabilities that are not
production-ready must be labelled as such (see the truth matrix vocabulary:
`VERIFIED` / `PARTIAL` / `MISSING` / `BLOCKED` / `DEFERRED`).
