# NAGAR CONNECT Threat Model

**Task:** `nagar-connect-foundation-01`
**Base SHA:** `c3aa7c85e4b64400cb8d51b0880ef5e6cafd4e1b`

This is a contract-level threat model. A control marked **gap** is not silently treated as implemented.

| Threat | Attack path | Existing control | Gap | Mitigation / test |
|---|---|---|---|---|
| Credential theft | Stolen external key is replayed | No raw secret enters the contract package | No persistent rotation/revocation authority | Use an approved secret manager; bind every lookup to connection id; test revocation and rotation |
| Token replay | Same request/key is resent | CommandBus has scoped idempotency for canonical commands | Transport replay nonce is not implemented | Add timestamp/nonce contract at the adapter; reject stale/reused nonce |
| Expired credential | Old key requests project data | `CredentialDescriptor.is_usable()` and connection expiry fail closed | No durable clock/revocation source | Persist expiry and test boundary timestamps |
| Revoked credential | Revoked key continues after revocation | Contract states reject revoked connection/credential | No cross-process immediate revocation path | Central revocation store/cache with bounded staleness; integration test |
| Scope escalation | Client asks for `project.write` with read scope | `authorize_read()` checks exact project and scope | Canonical authorization adapter not wired | Delegate to `ProjectAuthorizer`; never trust request scope; adversarial tests |
| Cross-project access | Connection for A asks for B | Exact project membership check in `ScopeGrant` | No tenant/principal store in CONNECT | Authorizer must bind actor and project; cross-project integration test |
| Actor impersonation | Client submits another actor id | `ActorIdentity` is a claim and CommandBus requires trusted authorizer | External authentication-to-actor mapping absent | Provider adapter resolves identity; reject self-asserted actor; test forged actor |
| Tenant isolation failure | Principal from tenant A targets tenant B | No tenant field is invented in this slice | Canonical tenant authority not located | Do not ship multi-tenant runtime until canonical tenant binding is proven |
| Confused deputy | CONNECT uses its own service authority | Package has no execution or grant authority | Future adapters could over-privilege a service actor | Inject real authorizer and preserve caller identity; boundary review |
| Secret leakage | Secret appears in logs/errors/evidence | Contract has no raw-secret field and rejects extra fields | Adapter/logging behavior unimplemented | Redaction tests over transport, trace and error paths |
| Unauthorized intent | External caller submits a mutation | This slice exposes no mutation function | Write adapter not implemented | Keep MCP read-only; route future writes only through `TypedCommand` + CommandBus |
| Duplicate request | Retry causes duplicate mutation | Existing CommandBus idempotency key and conflict semantics | External request-to-command key mapping absent | Derive stable key from authenticated request identity; integration test |
| Audit tampering | Caller edits evidence or audit fields | Existing provenance/passport authorities own evidence | CONNECT audit adapter absent | Return immutable identifiers and read canonical evidence only |
| Verifier bypass | Caller receives unverified artifact | Existing passport builder reconciles queue facts and evidence | CONNECT read surface absent | Only expose passport/evidence after canonical verification; negative test |
| Insecure MCP exposure | Tool exposes shell/provider/hidden mutation | No MCP implementation in this slice | Tool allow-list and auth adapter absent | Read-only tool registry with exact scopes; architecture boundary test |

## Failure rule

Missing principal, credential, scope, project access, authorizer, persistence, verifier or evidence is a refusal or an explicit unavailable result. It is never an implicit allow and never a successful artifact claim.

## Required integration evidence before production status

```text
external test client
  -> authenticated connection
  -> canonical principal resolution
  -> scope + project authorization
  -> (read-only data OR canonical TypedCommand)
  -> existing execution lifecycle
  -> independent verification
  -> ArtifactRecord + EvidenceRecord / ArtifactPassport
```

Until this chain is executed against real repository authorities, the status remains `VERIFIED_WITH_LIMITATIONS` or `BLOCKED`, never `VERIFIED`.
