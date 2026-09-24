# Creative production path — operator runbook

**Scope:** the single production path for creative work (Telegram `/edit`, `/caption`, `/grade`
→ durable queue → `creative_render` → render lane → verified artifact → localized delivery), the
deprecated HTTP lane next to it, and the verified backup chain.
**Owner:** Production Integration / Security / Reliability (owner directive 2026-09-24).
**Enforcement:** [`tests/architecture/test_creative_single_path.py`](../../tests/architecture/test_creative_single_path.py)
(this file is a description of what runs; when it disagrees with the tests, the tests win).

## 1. The chain, and where each link lives

```
Telegram update
  └─ /edit | /caption | /grade                      bot/creative_surface.py   (PTB glue)
       ├─ validate + normalize, in the user's language
       ├─ stage the replied media into a workspace  <creative_temp_dir>/creative_<id>/
       └─ JobQueuePort.enqueue("creative_render")   idempotency_key = creative:<user>:<chat>:<msg>:<cmd>:<op>
            └─ durable row (nexus_job_queue)        adapters/in_process_job_queue.py
                 └─ worker handler                  adapters/creative_render_job.py
                      ├─ re-validate (trust boundary: a queue row is data, never a permission)
                      ├─ probe the staged source    creative/slideshow/ffmpeg.py::probe_video
                      ├─ build the lane IR          adapters/creative_render_job.py::build_lane_ir
                      ├─ ONE encoder                creative/rendering (render_lane)
                      ├─ verify the artifact        verify_lane_artifact (sha256 + re-probe)
                      └─ COMPLETED + measured evidence
                           └─ completion hook      bot/app.py → bot/creative_notify.py
                                ├─ COMPLETED → master.mp4 delivered as a document
                                ├─ FAILED    → creative.failed.<code>, localized
                                └─ workspace removed after delivery
```

Registration is the composition root's job and happens exactly once: `worker.default_job_handlers()`
maps `"creative_render"` to `adapters.creative_render_job.creative_render_job`, and `bot/app.py`
registers the three command handlers from `build_creative_handlers(job_queue)`. No renderer lives in
`worker.py`, and `bot/handlers.py` is not touched.

## 2. What each command can actually do today

The promise is the `EXECUTABLE_OPERATIONS` table in `adapters/creative_render_job.py` — every entry
has a real lane primitive. Anything else is refused twice (surface copy, then the worker's trust
boundary) with `creative.not_available` / `unsupported_operation`; it is never queued and never
reported as a render.

| Command | Operation | Canonical op | Lane primitive | Status |
|---|---|---|---|---|
| `/edit` | `trim <in> [out]` | `timeline.trim` | `TrimOp` | executable |
| `/edit` | `speed <factor>` | `timeline.speed_ramp` | `SpeedOp` | executable |
| `/edit` | `reverse` | `timeline.reverse_segment` | `ReverseOp` | executable |
| `/grade` | `exposure <ev>` | `color.adjust_exposure` | `ExposureOp` | executable |
| `/caption` | `transcribe`, `burnin` | — | needs a speech/libass stage | refused (`not_available`) |
| `/grade` | `lut`, `proxy`, `otio` | — | needs a lut3d/proxy/export stage | refused (`not_available`) |

Limits: media up to **30 s** (`CreativeSurfaceMapper.MAX_DURATION_S`), trim `out` clamped to the
probed duration, one output file per job (`master.mp4`), render timeout
`settings.slideshow_render_timeout_seconds` (the slideshow budget — one engine, one budget).

## 3. Failure semantics (the part that used to lie)

* **COMPLETED means "verified bytes exist".** The handler returns success only after
  `verify_lane_artifact` re-hashed the produced file, compared it with the lane's own evidence and
  re-probed its duration. Anything else raises `CreativeRenderError` and the queue persists a
  durable `failed` row.
* **Typed vocabulary** (`ERROR_CODES`, mirrored by the notifier's `KNOWN_FAILURE_CODES`):
  `invalid_request`, `unsupported_operation`, `media_missing`, `media_unusable`, `render_failed`,
  `ffmpeg_unavailable`, `artifact_verification_failed`, `timeout`, plus `internal` for an untyped
  crash. The row stores `"[<code>] <redacted detail>"` — absolute paths are reduced to basenames.
* **The notifier never changes the verdict.** `notify_creative_completion` runs after the terminal
  state is durable, never re-raises, and deletes only a workspace it can prove is under
  `creative_temp_dir`.
* **No raw keys reach a user.** Every user-visible string comes from the catalog
  (`creative.queued`, `creative.completed`, `creative.failed.*`, the seven `CreativeErrorCode`
  messages); an unknown code degrades to `creative.failed.internal`.

## 4. Operating it

| Task | Command |
|---|---|
| Run the tests that pin this path | `pytest -q tests/unit/test_creative_surface.py tests/unit/test_creative_render_job.py tests/unit/test_creative_notify.py tests/unit/test_creative_i18n.py tests/architecture/test_creative_single_path.py` |
| Run the real end-to-end proof (real queue + real FFmpeg) | `pytest -q tests/integration/test_creative_chain_e2e.py` |
| See what the queue is doing | job status endpoint / the queue sidecar (`nexus_jobs` table) |
| Clean up leaked workspaces | `nexus maintenance housekeeping --dry-run` then without `--dry-run` (removes stale `creative_*` workspaces too) |

Knobs: `CREATIVE_TEMP_DIR` (workspaces), `NEXUS_FFMPEG_BIN` (explicit binary; otherwise the
canonical resolver), `SLIDESHOW_RENDER_TIMEOUT_SECONDS` (shared render budget).

## 5. The single seam for the render-plan bridge (handoff)

`adapters/creative_render_job.py::build_lane_ir` is the **only** function that turns a validated
request into lane ops, and it is the only place that names lane primitives. When
`creative/rendering/plan.py` (`compile_execution_plan`, PR#64) and the multi-segment assembly land,
that one function is replaced; nothing else in the chain changes — not the surface, not the queue
envelope, not the notifier, not the tests' end-to-end shape.

## 6. The deprecated HTTP lane is closed, not deleted

`POST /creative/video-edit` and `GET /creative/jobs/{job_id}` are **not** wired to the Telegram
bot and are deprecated at the route level (`deprecated=True`). They remain for external callers
that already hold the API key, and they are fail-closed:

| Control | Behaviour |
|---|---|
| Authn (both routes) | HMAC-SHA256 over `"{X-NEXUS-Timestamp}:{raw body}"`, ±300 s freshness, constant-time compare |
| Fail-closed default | no `NEXUS_API_HMAC_KEY` ⇒ **503**, never an open route |
| Authz / IDOR | the HMAC principal **is** the lane's principal; responses are a minimized projection (`_public_job_view`) that excludes `input_data`, filesystem paths, source URLs, raw error text |
| SSRF | `core/ssrf_guard.py::validate_url` in the request path (400 before a job row exists), `SafeAsyncTransport` re-validating every connect **and every redirect hop** |
| Resource caps | 500 MiB uploads, 200 MiB URL fetches with a bounded read, 60 s download timeout, ≤5 redirects, media-ish content types only |
| Failure propagation | a remote fetch failure is a durable `failed` row (not a fake 200), and the temp file is removed in `finally` |

New integrations must use the canonical path in §1. Evidence for each row:
`tests/unit/test_legacy_creative_security.py` (23 cases, including a real loopback listener) and
`tests/architecture/test_creative_single_path.py`.

## 7. Backup chain, and the nightly incident

`nexus maintenance backup` (and `--dry-run`, which touches nothing) now runs a full chain and fails
loudly at every step:

```
source DB ──dump──> artifact ──pre-upload integrity + inventory──> upload (R2)
   ^                                                                   │
   └── restore verification (sqlite readonly reopen + inventory) <── download <── sha256 round-trip
```

Failure classes that must turn the run red (all covered by `tests/unit/test_maintenance_backup.py`):
corrupt source, empty source, schemaless source, artifact that is wrong/truncated/unreadable,
remote corruption detected by the round-trip, upload failure, restore failure, and missing R2
configuration (`ProviderUnavailable` before anything is dumped).

**Incident `nightly-backup-never-succeeded` (recorded 2026-09-23, re-verified 2026-09-24).**
Hard evidence from the GitHub Actions API: the `maintenance` workflow has run `backup-db` four
times (`35975483186`, `35838057275`, `35705502209`, `35580701817`) and **every** run failed at the
step `Run nexus maintenance backup`; the only green run of that workflow (`35586584833`,
2026-09-21) was the *housekeeping* cron — its `backup-db` job was **skipped**. In the three runs
where step timing is available the failing step lived **1–2 s**, which is the fail-closed
`ProviderUnavailable` gate, not a database or upload path (`_build_provider` runs *before* the
dump). Reproduced locally on this branch, exit code 1:

```
$ env -u R2_ACCOUNT_ID -u R2_ACCESS_KEY_ID -u R2_SECRET_ACCESS_KEY -u R2_BUCKET nexus maintenance backup
❌ backup failed: R2 is not configured (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET) — nothing was uploaded
exit=1  (619 ms; 803 ms when the four variables are present but empty, which is what
         GitHub Actions does with an unset secret)
```

The step's stdout could **not** be downloaded from this environment (the Actions log blob endpoint
returns EOF; listing repository secrets returns 403 for the integration token), so the conclusion
is "the fail-closed provider gate fired in ≤2 s", established from step timing plus a local
reproduction of the same exit path — not from the log text. The fix is operator-side: set the four
`R2_*` repository secrets (and `NEXUS_DATABASE_URL` for a real database) and re-run the workflow
with `workflow_dispatch`; the chain above then proves the artifact round-trip before the job goes
green.
