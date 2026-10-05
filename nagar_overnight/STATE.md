# Nagar Overnight — STATE

| Field | Value |
|---|---|
| Current phase | Phase 5 — **durability**: free-text intent on the durable queue + existing worker |
| Current task | Persist the typed intent as one logical durable job and prove recovery/tamper |
| Chosen mode | **MODE B** (substrate present; durable handoff additive, no second execution path) |
| Base SHA (historical main) | `e5b326b2eaf691a638d030ad57acf1ce60016ef0` |
| Converged onto | `origin/main` `5a228ea` (merge of #156); PR #155 lineage `6e41123` |
| Branch | `arena/nagar-durable-handoff` (successor of `overnight/nagar-20261004`) |
| Working SHA | branch tip of `arena/nagar-durable-handoff` (exact SHA bound in the PR/report) |
| Last known green SHA | branch tip — full suite **3220 passed / 30 skipped / 0 failed** (docs delta is docs-only) |
| Next action | Open the successor PR (base `main`) recording ancestry to #155; owner review of #155 remains independent. |
| Current blockers | none |

## Phase 3 result (convergence)

- `ruff check .` -> PASS; `ruff format --check` -> PASS; `mypy src` -> PASS (268 files)
- `pytest -q` -> **3176 passed, 30 skipped, 0 failed** (converged baseline was 3160; +16)
- New/changed this phase:
  - `nagar/cognition/adapter.py` — canonical `LLMPort` (`complete`) preferred over legacy `generate`;
    idempotency key propagated to exactly that seam.
  - `nagar/cognition/{port,null,gateway}.py` — idempotency key through the seam; gateway `actor`/
    `project_id`/`producer` accessors; `completion` is the canonical builder arg.
  - `nagar/composition.py` — the only place a provider is built; a `FakeLLM` fallback is
    reported `not_configured` (never fed into a proposal as a real model).
  - `nagar/observation.py` — cognition decision -> existing causal journal (observation, not
    authority); failure degrades evidence only. Outside the pure cognition package so the
    isolation gate stays green.
  - `creative/render_jobs.py` — `build_job_bus` = the single canonical bus factory (registry +
    server-policy opt-in); CLI and slice reach the bus through it (ONE path preserved).
  - `cli.py` — `nexus intent`: real host caller (free-text -> typed proposal -> registry -> bus ->
    verified artifact; independent verdict; optional JSON receipt; Model Kill Test observable).
  - tests: `test_nagar_free_text_slice.py` (45), `test_nagar_intent_cli.py` (4), architecture gates
    updated to the hardened shape.

## Artifact proof (this phase)

- Applied path driven end-to-end with a **declared scripted model** (external seam only):
  `status: applied`, `verified: True`, real receipt written
  (`/tmp/nagar_artifact_receipt.json`, 946 bytes, `sha256:72a4d498...`).
- Model Kill Test (no model configured): `status: clarification_required`, `state_revision: 0`,
  real receipt with `verified: false` — nothing executed.

## Phase 5 result (durability)

- `ruff check src tests` -> PASS; `ruff format --check` -> PASS; `mypy src` -> PASS (270 files)
- `pytest -q` -> **3220 passed, 30 skipped, 0 failed**
- New/changed this phase:
  - `nagar/creative/handoff.py` — `run_free_text_intent_durable`: propose-only gateway →
    typed canonical `CreativeRenderPayload` (`extra="forbid"`) → `JobQueuePort.enqueue`
    (closed job type `creative_render`, stable intent idempotency key). Refusals enqueue nothing.
  - `creative/render_jobs.py` — `build_job_bus` = the single canonical bus factory (worker unchanged).
  - `cli.py` — `nexus intent` now enqueues + drains + reports the durable outcome; fail-closed
    when no model.
  - tests: `test_nagar_cognition_queue_handoff.py` (19), `test_nagar_durable_handoff_e2e.py` (3),
    `test_nagar_durable_handoff_boundary.py` (7), `test_nagar_intent_cli.py` (2),
    `test_phi_moderation_fail_closed.py` (4).

## Durable / artifact proof (this phase)

- Typed intent → ONE durable job (same intent collapses to the same `job_id`); the row is
  persisted by the real `InProcessJobQueue`.
- The job survives the death of the enqueuing process: a child process enqueues then `os._exit`s;
  a fresh queue `resume_pending_jobs()` recovers it and the existing worker renders the artifact.
- Real artifact: `output.mp4` exists, `sha256` matches the bytes on disk, the probe measures the
  requested duration, the registered verifier reports `status == "verified"`.
- Tamper-refusing: mutating one byte after completion makes the independent verifier fail.
