# NAGAR CONNECT API Contract

**Version:** `v1`  
**Mode:** read-only foundation  
**Status:** contract only; no router or MCP server is wired  
**Authentication:** future connection adapter; raw credentials never appear in requests, responses, logs, traces or evidence

## Common rules

Every request is bound to a connection id, canonical principal and one project scope. The adapter must authenticate first, resolve the connection, check expiry/revocation, and ask the existing project authorizer for project access. A missing dependency fails closed.

Responses are scope-bound and contain stable ids, state, hashes and evidence references only. They do not include raw credentials, filesystem paths, provider tokens, shell commands or unverified artifact claims.

## REST endpoints

| Method | Path | Required scope | Request | Response | Errors | Idempotency / audit |
|---|---|---|---|---|---|---|
| GET | `/v1/connect/projects` | `project.list` | none | `{projects:[{project_id,name,state_revision,state_hash}]}` | 401/403/503 | read event with connection id; no mutation |
| GET | `/v1/connect/projects/{project_id}` | `project.read` | path id | project identity and current revision summary | 401/403/404/503 | read event; project must be in grant |
| GET | `/v1/connect/projects/{project_id}/assets` | `asset.read` | bounded pagination | stable asset ids, media kind, measured hash | 401/403/404/503 | read event; no raw paths |
| GET | `/v1/connect/projects/{project_id}/jobs/{job_id}` | `job.read` | path ids | canonical job state and stable evidence ids | 401/403/404/409/503 | read event; no retry mutation |
| GET | `/v1/connect/projects/{project_id}/artifacts/{artifact_id}` | `artifact.read` | path ids | measured artifact identity and verification/passport summary | 401/403/404/409/503 | read event; only canonical verifier output |
| GET | `/v1/connect/projects/{project_id}/evidence/{evidence_id}` | `evidence.read` | path ids | immutable evidence receipt | 401/403/404/409/503 | read event; canonical source only |

No `POST`, `PUT`, `PATCH` or `DELETE` endpoint is part of this slice. A future write endpoint must define an explicit idempotency key, map to a canonical `TypedCommand`, and prove the complete execution path before implementation.

## MCP contract

The initial MCP server, when separately implemented, may expose only these read tools:

- `list_projects(scope: project.list)`
- `get_project(project_id, scope: project.read)`
- `list_assets(project_id, scope: asset.read)`
- `get_job(job_id, project_id, scope: job.read)`
- `get_artifact(artifact_id, project_id, scope: artifact.read)`
- `get_evidence(evidence_id, project_id, scope: evidence.read)`

The MCP registry must reject tools that mutate state, invoke shell/FFmpeg/providers directly, bypass the API/authorizer, or return data from another project. The MCP layer has no authority of its own.

## Error contract

Use stable categories, without secret or provider details:

- `401 connection_unauthenticated`
- `403 connection_scope_denied`
- `404 resource_not_found`
- `409 resource_not_verified_or_conflict`
- `410 connection_expired_or_revoked`
- `503 authority_unavailable`

## E2E acceptance

A production claim requires an integration test that starts with an external test client and ends with an existing verified artifact/evidence receipt. Unit tests of `ScopeGrant` or a mocked authorizer do not satisfy this acceptance criterion.
