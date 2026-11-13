# NAGAR CONNECT Foundation

**Task:** `nagar-connect-foundation-01`
**Branch:** `nagar/connect-foundation-20261111`
**Base SHA:** `c3aa7c85e4b64400cb8d51b0880ef5e6cafd4e1b`
**Status:** `VERIFIED_WITH_LIMITATIONS`

## Decision

NAGAR CONNECT is a controlled external boundary, not a new authority. Its first slice is contract-only and read-only. It binds an external connection to the canonical `ActorIdentity`, describes project and scope limits, and rejects inactive or expired credentials before an adapter asks the existing project authorizer for a real grant.

The package in `src/nexus_ai_agent/connect/` contains only immutable validation contracts and a fail-closed read pre-check. It does not authenticate raw secrets, persist credentials, create project grants, dispatch commands, create jobs, verify artifacts, or mint passports.

The authority chain remains:

```text
External agent
  -> transport authentication adapter (future, outside this slice)
  -> ConnectionContract / scope pre-check
  -> existing ProjectAuthorizer
  -> canonical TypedCommand + CommandBus (writes only; future adapter)
  -> existing job lifecycle / verifier / ArtifactPassport
  -> evidence-bound response
```

## Reused canonical authorities

| CONNECT concept | Existing authority | Decision |
|---|---|---|
| Actor identity | `creative.studio.models.ActorIdentity` | Reuse; no identity registry |
| Project access | `creative.studio.authorization.ProjectAuthorizer` and `ProjectAccess` | Reuse; scope metadata never becomes a grant |
| Intent / command | `TypedCommand` and `CommandBus` | Reuse for any future write slice |
| Artifact and evidence | `creative.spine.models.ArtifactRecord`, `EvidenceRecord` | Reuse |
| Passport | `provenance.passport.ArtifactPassport` | Reuse; no second verifier/passport |
| Durable project/revision state | `creative.project_state` and the single storage schema | Reuse; no CONNECT database |

## What CONNECT owns and does not own

CONNECT owns transport-facing contract vocabulary, connection lifecycle metadata, credential metadata without raw secrets, and scope-bound read semantics. It does not own authentication providers, secret storage, revocation persistence, authorization policy, project state, command execution, queues, provider calls, verification, or evidence.

`NEXUS_CONNECT_KEY` and `NEXUS_LLM_KEY` remain separate architectural concerns. No model gateway or BYOK path is introduced here.

## Live discovery

The live base was inspected before the branch was created. The existing CommandBus already enforces actor/project authorization, capability checks, idempotency and revision preconditions. `ActorIdentity` is canonical and explicitly documented as a caller claim that must be checked against an injected trusted grant. The API currently has a global dashboard bearer token and HMAC protection for state-changing legacy endpoints, but no connection registry or project-scoped external principal adapter was found. Durable project state, artifact binding, creative evidence and passports already exist, but are not exposed through a CONNECT read contract.

The current repository does not prove a production E2E path for external connection authentication through authorization, canonical execution, independent verification and artifact evidence. This slice therefore does **not** claim production readiness.

## Deferred work

1. Persistent connection/credential records and encrypted secret handling, pending an approved secret-management authority and migration plan.
2. API router wiring and MCP server wiring, pending a stable authentication adapter and project-scoped read services.
3. Write intents, because the external-to-`TypedCommand` mapping and durable job admission path are not yet proven as a single E2E flow.
4. Revocation propagation across processes, because the current contract has no durable revocation source.
5. `docs/README.md` indexing, because that path is owned by an active closeout claim/PR and was intentionally not modified.

## Acceptance statement

The contract package and its boundary tests are complete for this slice. The feature is **not** production-ready until the deferred dependencies are implemented through existing authorities and the integration matrix in `NAGAR_CONNECT_API_CONTRACT_20261111.md` passes against real persistence and verification.
