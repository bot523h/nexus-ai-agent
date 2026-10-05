# Job lifecycle contract — reference

Source: `docs/architecture/JOB_LIFECYCLE.md` (living view). Enforced by
`tests/unit/test_job_lifecycle.py`, `tests/unit/test_failure_semantics.py`,
`tests/integration/test_job_lifecycle_queue.py`, `tests/integration/test_gate5_closure.py`,
`tests/integration/test_verification_gap_closure.py`, and
`tests/architecture/test_verification_registry_ratchet.py`.

## Ownership map

| Step | Owner | Module |
|---|---|---|
| Command validation, staging, idempotency key | surface | `bot/creative_surface.py` |
| Durable job row, state machine, verification phase | Job layer | `adapters/in_process_job_queue.py`, `application/ports/job_queue.py`, `jobs/` |
| Execution semantics, artifact publication, probe/sha evidence | Runtime | `creative/rendering/*`, `creative/packs/*`, `creative/studio/*`, `creative/slideshow/ffmpeg.py` |
| Worker adapter (chain assembly, typed failures) | worker | `creative/render_jobs.py` |
| Result rendering to the user | notifier | `bot/app.py` (grandfathered) |

The Job layer never re-implements runtime semantics: verification evidence functions *are* the
runtime's (`sha256_file`, the allow-listed `probe_video`).

## Verifier registry

| Job type | Verifier | Artifact dialect |
|---|---|---|
| `creative_render` | `jobs.creative_verification.creative_render_verifier` | `output.mp4` / `captions.srt` / `timeline.otio`; media probe |
| `slideshow_render` | `jobs.creative_verification.slideshow_render_verifier` | rendered master at `output_path`; real media probe |
| `story` | `jobs.feature_verification.story_verifier` | Pillow-rendered PNG; structural decode |
| `pdf_extract` | `jobs.feature_verification.pdf_extract_verifier` | text layer staged then published; UTF-8 decode |

## Three artifact identities

| Identity | Content | Verified how |
|---|---|---|
| logical | `project_id = shot-<idempotency_key>` + output asset id | re-derived from the key; persisted in the result |
| spec | canonical operation id + compiled lane IR hash (`sha256(canonical payload)`) | recorded by the worker (`spec_ir_hash`) |
| physical | output path + size + `sha256:<hex>` + probe evidence | re-measured now: exists → size>0 → expected path → containment → sha recompute → probe |

## Publication order (outside the workspace)

`execute → stage → verify → publish atomically → re-probe published bytes → persist success`

For lanes whose destination lives outside the job workspace (today `pdf_extract`), the `.prev` copy is
kept until the completion CAS; a refusal retracts the staged temp or restores `.prev`.

## Failure classification

`jobs/failure_semantics` classifies each failure as `FAILED_RETRYABLE` or `FAILED_TERMINAL`. Failure
paths always end in one of the two failure states; `processing/verifying → pending` is recovery
(cancel/resume), never success. No retry scheduler exists — the reserved scheduler edge is
out-of-matrix by design.

## Negative-test matrix (M1–M10)

`JOB_LIFECYCLE.md` §8 lists the negative tests (fencing, takeover, publication retraction, verifier
crash, etc.). A change to the lifecycle must keep them green; the §17 regression is the worked
counter-example.

## Notification and trace contracts

- §9 notification: success is announced only after the completion CAS commits; refusal notifications
  are typed.
- §10 trace: `trace_id`/`job_id` are recorded; a `job_id=null` trace was a reproduced defect fixed in
  the Gate-5 closure (`docs/audits/GATE5_CLOSURE_2026-09-24.md`).

## Durable creative queue evidence (task-232)

For queue-only `timeline.trim`, the queue checkpoints a verified passport + attempt before the
completion CAS; recovery can re-read and re-verify the checkpoint and reconcile completion without
re-invoking the renderer. A crash before that checkpoint still allows at-least-once handler execution.
