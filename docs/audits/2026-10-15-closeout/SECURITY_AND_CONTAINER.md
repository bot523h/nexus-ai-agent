# Security review and container verification — #218 head

Each row names the control, the code or test that proves it, and the honest status.

## 1. Application controls

| Area | Control | Proof | Status |
|---|---|---|---|
| Owner/admin surfaces | dashboard API fails closed: token required, DB failure → error, never an empty 200 (A-03/A-06) | `tests/unit/test_dashboard_api.py`, `tests/integration/test_api_gate_inventory.py` (OpenAPI-driven inventory of every route: each is gated or explicitly public) | VERIFIED |
| Moderation | moderation surface is actually wired and fails closed when the LLM is unavailable (A-02b, `phi` agent) | `tests/unit/test_surface_moderation.py`, `tests/unit/test_feature_wiring.py`, `tests/unit/test_phi_moderation_fail_closed.py` | VERIFIED |
| Message path | a database failure must never read as a handled message (A-01 negative control) | `tests/integration/test_a01_message_path.py`, `tests/integration/test_real_session_factory.py` | VERIFIED |
| Filesystem tool boundary | workspace root opened `O_NOFOLLOW`; symlinks and traversal refused at every primitive; non-directory ancestors raise the typed `FilesystemBoundaryError` (re-scoped #209) | `tests/unit/test_filesystem_policy_primitives.py` (28), `tests/unit/test_filesystem_boundary.py`, `tests/unit/test_execution_staging.py` | VERIFIED |
| Self-update | updater is bounded (allow-listed git/pip steps, timeouts, capability probe → `unsupported` without `.git`/`git`) (A-07/A-19) | `tests/unit/test_updater.py`, CI `container` job updater step | VERIFIED (contract) / VERIFIED_WITH_LIMITATIONS (in-image, see §3) |
| Hashing | no MD5/SHA-1 for security purposes (`S324` at zero), SHA-256 content fingerprints (A-09) | ruff `S324`, `tests/unit/test_core_fingerprint.py` | VERIFIED |
| Time | naive datetimes forbidden (`DTZ` at zero); one UTC clock contract for storage (`storage/clock.py`, closeout commit `c894d2d`) | ruff `DTZ`, `tests/unit/test_migrations.py`, `tests/unit/test_time_contract.py` | VERIFIED |
| Schema authority | Alembic-only schema, migration metadata recorded, Telegram ids BIGINT on PostgreSQL (A-17) | `tests/unit/test_telegram_id_width.py`, `tests/integration/test_migrate_*.py`, CI `migrate-postgres` | VERIFIED |
| Secrets in CI | workflows are read-only (`permissions: contents: read`), pinned actions, timeouts on every job; no Telegram/cloud secret is used by any job | `tests/unit/test_ci_lint_parity.py`, `tests/unit/test_ci_continuum_evidence.py`, `tests/unit/test_maintenance_workflow_contract.py` | VERIFIED |
| Event-loop availability | no blocking file I/O inside `async def` in `src/` (A-12) — a slow disk cannot stall every Telegram update | ruff `ASYNC` (production rule), `tests/unit/test_async_offload.py` | VERIFIED |
| Governance | board referee 14/14, Gate-5 fencing 17/17, execution-core 17/17 mutants killed, all three campaigns in CI (`governance-mutations`) | CI job + `scripts/*_mutations.py` | VERIFIED |

## 2. Dependency audit (`pip-audit`, 2026-10-15, venv of the fast suite)

7 findings in 2 packages, **0 with a fixed release**: `chromadb 1.5.9` (4 CVEs — server/RBAC
surfaces; this project embeds `PersistentClient` only and never starts the Chroma server) and
`diskcache 5.6.3` (pickle cache files; transitive via `llama-cpp-python`, not imported by
`src/`). Details and the exposure argument: `TECHNICAL_DEBT_LEDGER.md` §6. Not a CI gate yet
(would be permanently red until upstream fixes exist).

## 3. Container verification (A-16)

### What the repository proves without Docker (unit contract, every CI run)

`tests/unit/test_container_contract.py`:

* `USER nexus` is set after the install steps and before `CMD`;
* the image declares a **mode-aware** `HEALTHCHECK` (`docker/healthcheck.sh`): polling mode
  checks for a live `… cli run-bot …` process in the PID namespace (there is no HTTP server to
  probe), webhook mode probes `/healthz` on the bound port (`PORT > DASHBOARD_PORT > 8000`);
* the probe script is executed for real against a local HTTP server (webhook) and a live
  `run-bot` process (polling), positive and negative;
* the CI `container` job keeps the shape described below (positive **and** negative probe
  cases; `docker run -i` so the updater heredoc cannot pass vacuously).

### What CI proves with Docker (`container` job, added in commit `a0c2027`)

First real run: workflow run 38793879776 on head `a0c2027`, job 116625684891 — **all 8 steps
success**, 7 min 07 s wall clock including the image build (started 2026-10-15T19:27:28Z,
completed 19:34:35Z).

Against the freshly built production image: runtime user is `nexus` (uid ≠ 0); `/app/data` and
`/app/data/chroma` writable by that user; the CLI imports as that user; `HEALTHCHECK` points at
`/app/docker/healthcheck.sh` and the script is executable; polling probe passes with a
run-bot-shaped process and fails without one; webhook probe passes when `/healthz` answers and
fails when it does not; `probe_update_capability()` inside the image reports *unsupported*
(`.git` and `git` absent). Image size/user/cmd are printed for the record.

### Limitation (why A-16 stays `VERIFIED_WITH_LIMITATIONS`)

CI has no Telegram token, so the polling/webhook probes run against stand-in processes with the
same observable shape (`python -c … run-bot nexus_ai_agent`; `http.server` serving `/healthz`).
This proves the probe plumbing *inside the image*; it does not prove a live bot session. The
owner's first real deployment should record `docker inspect --format '{{.State.Health.Status}}'`
after the 120 s start period — that single line upgrades A-16 to `VERIFIED`.
