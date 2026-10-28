# ADR: Nagar P0 Intent, NEL, Semantic Time, and Causal Evidence

**Status:** Accepted for the first vertical slice
**Date:** 2026-10-29

## Context

Nagar already has a canonical Creative Studio model, capability registry, CommandBus, durable queue, renderer, verifier, execution trace, and append-only causal journal. A parallel DSL, queue, renderer, or graph store would split authority and make verification claims unreliable.

## Decisions

1. **NEL v1 is a small declarative language.** It supports one typed trim operation over a semantic segment, preservation flags, evidence requirements, and preview. It does not execute shell/Python/FFmpeg, accept arbitrary paths, or carry dynamic operations.
2. **Intent and AST/IR are evidence and plans, not authorization.** Actor, tenant, project access, policy, revision, and commit authority remain backend and CommandBus responsibilities.
3. **Canonical time is integer PTS plus an explicit timebase.** Semantic labels are revision-scoped; stale or low-confidence candidates require clarification and never silently mutate state.
4. **Causal memory is a typed projection.** The P0 graph validates scope, stable IDs, and edge endpoints, while durable storage remains the existing provenance journal and passport plane.
5. **Undo is compensation.** History is append-only; a cause query selects prior operations and a later canonical undo/compensation command records the new causal event.
6. **Verified success is evidence-derived.** A receipt cannot claim `VERIFIED_SUCCESS` without an artifact, verifier reference, and evidence references.

## Alternatives rejected

- A second execution bus or renderer: rejected because it would violate the single canonical execution path.
- Free-form model-generated commands: rejected because model output is not authority and allows operation/tenant smuggling.
- Floating-point seconds as stable IDs: rejected because PTS/timebase and revision are the runtime authority.
- Graph-only persistence: rejected because the repository already owns a hash-chained causal journal and artifact passport.

## Consequences

The first slice is deterministic and testable without a model or external service. UI/API integration can consume the same typed contracts later. A migration is not required for the pure contracts; persistence wiring must use existing journal conventions and receive a separate compatibility review.

## Risks and rollback

The primary risk is contract drift with an in-flight perception or plan-transaction PR. Before merging, compare the touched files and schema names against PRs #247, #205, #150, #140, #135, and #134. Rollback is a normal branch revert; no main branch or production data is modified by this change.
