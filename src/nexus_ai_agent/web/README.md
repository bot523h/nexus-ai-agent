# NAGAR One — Local-First Creative Studio (web app)

The NAGAR Studio web app is the first user-interface path of NAGAR One. The product frame lives in [product contract](../../../docs/product/NAGAR_ONE_PRODUCT_CONTRACT.md).

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

## Capability truth (mirrors `api/studio_web.py`)

Statuses are copied from the capability matrix served at `GET /api/studio/overview`.
If this table and the code disagree, the code wins and this table is corrected in the same change.

| Capability | Status | Reason (from the matrix) |
| --- | --- | --- |
| Session | VERIFIED | Cookie session derived from the deployment token. |
| Job state | VERIFIED | Read-only state of one job by id. |
| Project scope | VERIFIED_WITH_LIMITATIONS | Projects are a deployment policy (`NEXUS_STUDIO_PROJECT_IDS`), not per-user membership. |
| Per-user identity | BLOCKED | No HTTP login binds to `AppPrincipal`; every browser session is the single operator credential. |
| Job listing | DEFERRED | `JobRegistry` only exposes `get_job(job_id)`. |
| Intent submit | VERIFIED_WITH_LIMITATIONS | `POST /api/studio/projects/{id}/renders` admits one HMAC-signed intent into the queue and answers 202. No preview or approval session; IBPC approval stays in-process. |
| Approvals | DEFERRED | Approval persistence and cross-process single use are not durable yet. |
| Asset library | DEFERRED | No HTTP surface. |
| Resource packs | VERIFIED_WITH_LIMITATIONS | `GET /api/studio/resource-packs` re-verifies installed packs read-only; it cannot install, repair or activate one. |
| Artifact download | VERIFIED | `GET /api/studio/projects/{project_id}/jobs/{job_id}/artifact` streams the MP4 (64 KiB chunks) only after `authorize_artifact_download` passes: passport seal, PASS verdict and on-disk digest. Cross-project jobs return 404; traversal 403; malformed path 422; passport or byte mismatch 409; unconfigured studio 503. Evidence: `tests/integration/test_studio_artifact_download.py`. |

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
