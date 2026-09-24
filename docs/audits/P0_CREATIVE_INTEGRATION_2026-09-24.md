# P0 creative integration — audit record (2026-09-24)

**Session:** `arena/01a0d2d6-nexus-ai-agent` (owner directive: *Production Integration + Security +
Queue/Worker + Backup Closure*).
**Verified from zero at** `main` = `2cf2213` (*Merge pull request #62*), with PR#64
(`arena/01a0d2a2`, *Creative Runtime (task-176 s2)*) **open and `mergeable=CONFLICTING`** — reviewed,
never merged or cherry-picked. `.agents/board.json` carried **0 active claims** and no
`gates_owner` for an active claim.
**Rerun status:** every number below was produced by running the command in this record on this
branch; nothing is quoted from an earlier report.

## 1. Factual state before the work (the three P0s, reproduced)

| Defect | How it was verified | Verdict before |
|---|---|---|
| NAG-001 — surface not connected to production | `bot/app.py` contained no registration of `/edit` `/caption` `/grade`; `worker.default_job_handlers()` had no `creative_render` key, so any such queue row would die as an unknown job type | confirmed |
| NAG-002 — canonical render lane had no production caller | only tests and the slideshow lane referenced `creative/rendering.render_lane` | confirmed |
| NAG-003 — success claimable without an artifact | legacy HTTP lane backgrounded work into a row nobody verified; no artifact check existed anywhere in the path | confirmed |
| NAG-004 — SSRF on `POST /creative/video-edit` | with a real loopback listener, `video_url=http://127.0.0.1:<port>/…` **reached the listener**: `tests/unit/test_legacy_creative_security.py` on the pre-fix route = **20 failed, 3 passed in 182.38 s** | confirmed |
| NAG-005 — IDOR on `GET /creative/jobs/{job_id}` | unauthenticated GET returned 200 including `input_data` (staged local paths, source URLs) | confirmed |

## 2. Owned changes (exclusive zone; no touch on `creative/rendering/**`, `creative/otio/**`,
`creative/packs/**`, `creative/execution.py`, `creative/rendering/plan.py`,
`creative/rendering/compiler.py`, `storage/`, `llm/`, no migration, no new dependency)

| File | Change |
|---|---|
| `src/nexus_ai_agent/adapters/creative_render_job.py` | **new**: `CreativeRenderPayload` (extra=forbid) + trust-boundary containment, `EXECUTABLE_OPERATIONS`, `NUMERIC_SLOTS`/`numeric_args` (parse + bounds *before* the encoder is resolved), `build_lane_ir` (the single Agent-B seam), `verify_lane_artifact` (sha256 + re-probe ±50 000 µs), typed `CreativeRenderError`, `creative_render_job` handler |
| `src/nexus_ai_agent/bot/creative_surface.py` | rewired surface: typed request → validate → normalize → stage media in `creative_<id>` workspace → enqueue with a message-anchored idempotency key → localized reply; **no framework import** |
| `src/nexus_ai_agent/bot/creative_notify.py` | **new**, framework-free: `notify_creative_completion(completion, sender)` — verified document or localized failure, contained-workspace cleanup in `finally`, never raises |
| `src/nexus_ai_agent/bot/app.py` | composition root: `TelegramCreativeSender` (the only Telegram send path for creative results), `notify_creative_completion` routing, `CommandHandler` registration of the three commands, typed surface at the registration site |
| `src/nexus_ai_agent/worker.py` | `default_job_handlers()["creative_render"] = creative_render_job` (registration only; no renderer in the worker) |
| `src/nexus_ai_agent/api/app.py` | legacy lane hardened + deprecated: request-path `validate_url`, 500 MiB upload cap, bounded 200 MiB guarded download (60 s, ≤5 redirects, media-type allow-list), `_public_job_view` minimization, HMAC gate on both routes, `deprecated=True` |
| `src/nexus_ai_agent/maintenance/backup.py` | verified backup chain (pre-upload integrity + inventory → upload → download → sha256 round-trip → restore verification), `sqlite3.Error → BackupVerificationError` |
| `src/nexus_ai_agent/i18n/locales/*.json` | 18 `creative.*` keys in all 15 locales |
| tests | `tests/unit/test_creative_surface.py`, `test_creative_render_job.py`, `test_creative_notify.py`, `test_creative_i18n.py`, `test_legacy_creative_security.py`, `test_maintenance_backup.py`, `tests/architecture/test_creative_single_path.py`, `tests/integration/test_creative_chain_e2e.py`; `tests/unit/test_creative_studio.py` updated to the authenticated/minimized read |
| docs | this record, [`../ops/CREATIVE_PRODUCTION_PATH.md`](../ops/CREATIVE_PRODUCTION_PATH.md), `DECISION_LOG.md` (D-0010 + r8), `README.md` index rows, `SECURITY.md` T15/T16, `OBSERVABILITY.md` read-route row |

## 3. User journey (proven, not asserted)

`tests/integration/test_creative_chain_e2e.py` (5 tests, real `InProcessJobQueue`, real
`default_job_handlers()` map, real FFmpeg encode — only Telegram and R2 are stubbed):

1. `/edit trim 0 2` replies `⏳ Queued edit/trim — job <id>` in the caller's language;
2. the durable row exists with `job_type=creative_render`, and the worker resolves the handler from
   `default_job_handlers()`;
3. a spy proves **Agent B's `render_lane` ran** (lane op `trim`, output `<workspace>/master.mp4`);
4. the job reaches COMPLETED only after `verify_lane_artifact` passed; the delivered document's
   sha256, hashed from the bytes **at send time**, equals `result["output_sha256"]`;
5. the caption is the localized `creative.completed` string and the workspace is gone afterwards.

Failure half, same path: a non-video payload produces a **durable FAILED** row and exactly one
localized message (`creative.failed.media_unusable`, Persian locale) with no key, no path, no
exception text. Redelivering the same Telegram update produces **one** queue row. An operation
without an executable lane primitive (`/caption burnin`) creates **zero** queue rows and answers
`creative.not_available`.

## 4. Security

* **SSRF (T15).** RED first: the pre-fix route contacted a real loopback listener (20 failed /
  3 passed, 182.38 s). GREEN after: canonical `core/ssrf_guard.py::validate_url` in the request
  path (400 *before* a job row exists), `SafeAsyncTransport` re-validating every connect and
  redirect hop, bounded read, timeout, media-type allow-list. Tests cover `127.0.0.1`, `localhost`,
  `::1`, private IPv4/IPv6, redirect→private, oversized response, unsupported scheme.
* **IDOR (T16).** RED first: unauthenticated 200 with `input_data`. GREEN after: fail-closed HMAC
  authn on both legacy routes (503 without `NEXUS_API_HMAC_KEY`, 401 unsigned/stale, ±300 s,
  constant-time), minimized `_public_job_view` (no `input_data`, no paths, no raw error), identical
  404 for unknown ids.
* **One path, enforced.** `tests/architecture/test_creative_single_path.py` (12 tests) fails if a
  second `creative_render_job` definition, a second `render_lane(` caller, an unguarded
  `httpx.AsyncClient`, a re-implemented SSRF range, a missing `deprecated`/HMAC control, or a
  framework import in the surface/notifier appears.

## 5. Worker / queue

* Registration: `worker.default_job_handlers()["creative_render"] is
  adapters.creative_render_job.creative_render_job` (pinned by test); no renderer code in
  `worker.py`.
* Contract: COMPLETED ⇔ artifact exists, non-empty, re-hashes to the lane's digest and re-probes to
  the lane's duration; every other outcome raises a typed error ⇒ durable FAILED with
  `"[<code>] <redacted>"`. The notifier cannot flip a status (queue writes the terminal state before
  the hook; the hook never re-raises — regression test with a deliberately broken notifier).

## 6. Backup (P0-C)

Chain: dump → pre-upload integrity/inventory → upload → download → sha256 round-trip → restore
(SQLite reopened read-only and compared; PostgreSQL footer + round-trip, server-side restore
labelled as needing a live instance). Failure classes fail closed: corrupt source, empty source,
schemaless source, truncated/wrong artifact, remote corruption, upload failure, restore failure,
missing R2 configuration. `tests/unit/test_maintenance_backup.py` = 13 tests.

**Incident `nightly-backup-never-succeeded` — root cause.** GitHub Actions API: the `maintenance`
workflow ran `backup-db` four times (`35975483186`, `35838057275`, `35705502209`, `35580701817`) and
every run failed at the step `Run nexus maintenance backup`; the single green run (`35586584833`,
2026-09-21) was the *housekeeping* cron with `backup-db` **skipped**. Step timings show 1–2 s
failures, i.e. the fail-closed `ProviderUnavailable` gate (`_build_provider` runs before any dump).
Reproduced locally: exit 1 in 619 ms with `R2_*` unset (803 ms with them empty, which is what
Actions does with an unset secret), message
`❌ backup failed: R2 is not configured (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET) — nothing was uploaded`.
The step log itself was **not retrievable** here (Actions log blobs return EOF; repository-secret
listing is 403 for the available token), so the attribution rests on step timing plus a local
reproduction of the same exit path. Fix is operator-side: set the four `R2_*` secrets (and
`NEXUS_DATABASE_URL`), then a `workflow_dispatch` run of `maintenance/backup-db`.

## 7. Test evidence (this branch, `pytest -q -m "not slow"` unless stated)

| Suite | Result |
|---|---|
| `tests/unit/test_creative_surface.py` | 16 passed |
| `tests/unit/test_creative_render_job.py` | 20 passed |
| `tests/unit/test_creative_notify.py` | 7 passed |
| `tests/unit/test_creative_i18n.py` | 19 passed |
| `tests/unit/test_legacy_creative_security.py` | 23 passed |
| `tests/unit/test_maintenance_backup.py` | 13 passed |
| `tests/architecture/test_creative_single_path.py` | 12 passed |
| `tests/integration/test_creative_chain_e2e.py` | 5 passed |
| `tests/unit/test_i18n_parity.py` / `tests/unit/test_agent_board.py` | 34 / 18 passed |
| **full gate suite** | **2 failed, 1945 passed, 21 skipped in 89.29 s** |

Both remaining failures are outside this change and reproduce on pristine `main`:
`tests/unit/test_database_url.py::TestPgEngineSelection::test_real_engine_builds_without_network`
(`asyncpg` is not installed) and
`tests/unit/test_litellm_provider.py::test_factory_builds_routing_chain_wrapped_in_fallback`
(`litellm` is not installed) — both are declared *base* dependencies (`pyproject.toml:37,44`) and
were simply absent from the sandbox venv. `ruff check .` = **All checks passed**;
`ruff format --check .` = **459 files already formatted**; `mypy src` = 2 errors, both
`import-not-found` for `chromadb.utils` / `litellm` in files this change does not touch;
`pytest -q tests/unit/test_docs_integrity.py tests/unit/test_agent_board.py` = **65 passed**.

## 8. Anti-vacuity (each mutation must turn its guardian red)

| # | Mutation | Guardian that went red |
|---|---|---|
| M1 | remove `creative_render` registration | `test_creative_single_path.py::test_worker_registers_the_creative_handler` (+1) |
| M2 | remove the legacy read auth gate | `test_legacy_creative_security.py::test_job_read_is_fail_closed_and_minimized` |
| M3 | return the raw job row instead of the projection | same as M2 |
| M4 | remove request-path `validate_url` | `test_legacy_creative_security.py::test_video_url_never_reaches_loopback` |
| M5 | drop `SafeAsyncTransport` | `test_creative_single_path.py::test_every_http_client_in_the_api_is_ssrf_guarded` |
| M6 | remove the `verify_lane_artifact(...)` call | `test_creative_render_job.py::test_artifact_that_does_not_exist_fails_the_job` |
| M7 | make the worker swallow the failure | `test_creative_chain_e2e.py::test_runtime_failure_is_durable_failed_with_a_localized_message` |
| M8 | delete one `creative.*` key from a locale | `test_creative_i18n.py` + `test_i18n_parity.py` |
| M9 | remove the remote sha256 round-trip check | `test_maintenance_backup.py::test_remote_corruption_is_caught_by_the_round_trip` |
| M10 | neuter restore-inventory verification | `test_maintenance_backup.py::test_restore_verification_detects_row_drift` |
| M11 | let a non-executable operation reach the queue | `test_creative_render_job.py::test_operation_without_a_lane_primitive_is_refused` |
| M12 | re-add a framework import to the notifier | `test_creative_single_path.py::test_surface_and_notifier_stay_framework_free` + the frozen import-baseline test |

All twelve turn their guardian red on the final tree; the mutated file is restored byte-identical
after each run (asserted in the harness).

## 9. CI / remote

Local gates above are green except the environment gaps named in §7. CI has not been run for this
branch from here; the repository's maintenance workflow is red for the operator-side reason in §6.

## 10. Remaining blockers / handoff to Agent B

1. **R2 secrets** (operator action) — without them the nightly backup stays red by design; the chain
   itself is verified by the 13-test suite.
2. **Render-plan bridge** — `build_lane_ir` in `adapters/creative_render_job.py` is deliberately the
   only seam that maps a validated request to lane ops. When `creative/rendering/plan.py`
   (`compile_execution_plan`, PR#64) lands, replace that one function's body; the surface, queue
   envelope, handler contract, notifier and end-to-end test shape stay unchanged.
3. **Refused operations** stay refused until the missing primitives exist: `caption.transcribe`,
   `caption.burn_in` (libass/speech stage), `color.apply_lut` (lut3d asset stage),
   `delivery.make_proxy_480p` (produces a proxy record, not a 480p file), OTIO export. They are
   advertised and answered honestly instead of being queued.
