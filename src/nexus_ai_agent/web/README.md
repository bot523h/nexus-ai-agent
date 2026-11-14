# NAGAR Studio web app

Status: first vertical slice (`task-nagar-studio-web-01`). The architecture decision is
recorded in the last section of this document (ADR-style). It lives next to the code, not under
`docs/`, because `docs/README.md` and the ADR index were exclusively claimed by
`task-284-nagar-creative-composition` when this slice was delivered. Once those paths are
free, promote it to `docs/product/` and `docs/architecture/adr/` and index it.

## What exists

| Surface | Path | Backing |
| --- | --- | --- |
| App shell (PWA, fa/RTL, light/dark) | `GET /studio/` | static files in `src/nexus_ai_agent/web/static/` |
| Session | `GET/POST/DELETE /studio/session` | `api/studio_session.py` over the dashboard gate |
| Overview + capability truth | `GET /api/studio/overview` | `api/studio_web.py` |
| Job state, plan, artifact passport, evidence, lineage, gaps | `GET /api/studio/projects/{p}/jobs/{j}/state` | existing `StudioReadService` (unchanged contract) |

Screens: sign-in, workspace (configured projects, open a job by id, recent ids),
job view (status stepper with polling until terminal, intent, plan, artifact
passport, evidence tree, lineage with explicit gaps, contract gaps), capabilities,
settings (theme, sign out, build id).

## What is deliberately not built (and why)

| Capability | Status | Missing contract |
| --- | --- | --- |
| Per-user identity | BLOCKED | no HTTP login bound to `AppPrincipal` |
| Job listing | DEFERRED | `JobRegistry` only has `get_job(job_id)` |
| Intent submit / preview | DEFERRED | IBPC sessions are in-process (NAGAR-ONE Stage 3) |
| Approvals | DEFERRED | not durable / cross-process |
| Asset library | DEFERRED | no HTTP surface |
| Artifact download | DEFERRED | no authorized download route |

## Security properties (each has a test)

- No secret in JS, `localStorage`, `sessionStorage`, IndexedDB or the service worker.
- Cookie: `__Host-`, `HttpOnly`, `Secure`, `SameSite=Strict`, 4 h, tamper-evident,
  key derived from the deployment token (rotation revokes all).
- Login/logout need a custom header; failed logins are throttled.
- Strict CSP (`default-src 'none'`, no inline script/style, no third-party origin).
- Every `/api/studio/*` response is `Cache-Control: no-store`.
- The service worker never intercepts `/api/*` or non-GET requests.
- All server text is inserted as text nodes (no HTML parsing in client code).
- Unauthorized and nonexistent projects/jobs are indistinguishable (404).

## Run locally

Set `NEXUS_DASHBOARD_TOKEN` and `NEXUS_STUDIO_PROJECT_IDS=project-a`, start the API,
open `https://<host>/studio/` (browsers also accept `http://localhost`). Sign in with
the dashboard token.

## Tests

`tests/unit/test_studio_session.py`, `tests/unit/test_studio_web.py`,
`tests/architecture/test_studio_web_boundary.py`.

## Decision record (ADR-style): dependency-free PWA over the existing Studio read surface

- Status: accepted (first vertical slice), 2026-11-10

## Context

`main` had no user interface. The only Studio HTTP surface is the read-only
`GET /api/studio/projects/{project_id}/jobs/{job_id}/state`, guarded by the shared
dashboard bearer token, with project scope as a deployment policy
(`NEXUS_STUDIO_PROJECT_IDS`). The product contract names a PWA as the first UI path
but puts it outside NAGAR-ONE Stage 0-3. The repository is Python-only: CI has no
Node toolchain.

Hard constraints from the mission: no second execution path, queue, authority or
token system; no secret in browser storage or client code; no mock capability.

## Decision

1. **Installable PWA, no build step.** Static ES modules, one stylesheet, one
   service worker, served same-origin by the existing FastAPI app under `/studio/`.
2. **Session wrapper, not a new auth system.** `POST /studio/session` exchanges
   the deployment token once for an `HttpOnly; Secure; SameSite=Strict`
   `__Host-` cookie holding `v1.<exp>.<nonce>.<mac>`. The MAC key is derived from the
   dashboard token (domain-separated HMAC), so there is no new secret, and rotating
   the token revokes every session. The cookie proves exactly what the bearer proves
   and no more. An `Authorization` header, when present, stays authoritative.
3. **Honest capability table in code.** `GET /api/studio/overview` returns projects
   and a capability list (`VERIFIED` ... `DEFERRED`). The UI renders it; nothing the
   backend cannot do is simulated.
4. **Shell-only offline.** The service worker caches the application shell and never
   `/api/*`; job state, plans and evidence are never stored on the device.

## Options considered

| Option | For | Against | Verdict |
| --- | --- | --- | --- |
| A. React/Vite SPA | ecosystem, component model | adds Node + npm supply chain + CI job to a Python-only repo, for a read-only first slice | rejected now; revisit when editing/canvas views land |
| B. Server-rendered HTML (Jinja) | no JS toolchain | polling/offline/install need JS anyway; mixes presentation into API layer | rejected |
| C. Telegram Mini App only | reuses the bot channel | no standalone product, depends on Telegram client | deferred (can load the same `/studio/`) |
| D. **Dependency-free PWA (chosen)** | zero supply chain, strict CSP trivially, installable, offline shell | hand-rolled DOM helper, no component library | accepted |
| Auth: token in `localStorage`/header from JS | trivial | script-readable secret; forbidden | rejected |
| Auth: server session table | per-session revocation | second store/authority | rejected |
| Auth: **derived stateless cookie (chosen)** | no new secret/store, HttpOnly | single stolen cookie lives until `exp` (4 h) | accepted, documented |

## PWA decision

Worth it, in a narrow form. Value: installable full-screen on phones (the existing
audience uses Telegram on mobile), an offline shell with an honest offline state,
and a clean update path (build-hash cache key). Not used: push notifications,
background sync, caching of data. They would store or relay private job state and
have no backend contract.

## Consequences

- One shared operator credential remains the only identity. Per-user login is
  **BLOCKED**: `AppPrincipal`/`AppChannelBinding` exist in storage with no HTTP
  binding. Until then the app is an operator console, not a multi-user product.
- Throttle on failed logins is process-local and global (proxy-safe, DoS-able only
  for *new* logins). A multi-process deployment needs a shared limiter.
- Job listing, intent submission, approvals, asset browsing and artifact download
  have no backend contract and are **DEFERRED**, never faked.
- The Vazirmatn font is served from the caption pack path (one binary in the repo).
- `artifact_path` is part of the existing read contract but host-internal; the UI
  never renders it. Consider removing it from the public view in a follow-up.
