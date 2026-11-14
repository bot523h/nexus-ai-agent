# Waves 1–6 verification ledger (PR #343)

Every number below was measured on this branch, at the revision named in each
wave. Nothing here is projected from a plan, and the two items that could **not**
be measured in this environment are stated as such rather than filled in.

Branch: `arena/fd837e50-nexus-ai-agent` · base: `main` at `deab2b3e`

## Per-wave record

| Wave | Revision | Commits | What changed | Local evidence |
|------|----------|---------|--------------|----------------|
| 1–3 | `299e931` | checkpoint 3A + 3B | subprocess timeouts (morpheus mux, continuum gate/provenance), PROMETHEUS fail-closed digests, reconciler verified registration + CLI + maintenance workflow, SIGMA landings without a `jobs.creative_passport` import in `project_state/store.py` | reconciler 148 passed; reconciler CLI/verified 7 passed; maintenance workflow contract 17 passed / 1 skipped; `mypy src` clean |
| 4 | `0e6fabc` | B1–B4 audio kernels | non-destructive port of `ambisonics_binaural_fdn`, `av_subframe_foley_sync`, `psychoacoustic_bark_mastering`, `spectral_voice_restoration` + mutation harness (149 probes) + `ffmpeg_ops` JSON/banner channel parity; `cinema_kernels.py` untouched | 668 passed (14-file audio/omega set); banner file 57 passed |
| 5 | `fa4d69e` | engine capabilities | six engine ports (`omega`…`hyperion`) on `OperationContext`, `ENGINE_PORT_ATTRIBUTES`, one read-only `verify_media` capability per engine with a typed `EngineNotComposedError`, composition-root adapters that re-measure digests, `requires_opt_in` registry policy, `OPERATION_TRUTH.json` regenerated | `tests/unit/test_engine_capabilities.py` 15 + `tests/architecture/test_engine_capability_boundary.py` 6; `tests/architecture` 406 passed; truth doctor clean; `mypy src` 552 files clean |
| 6 | `6c62064`, `3b3b2f6`, `fb6eae7` | Studio submission + publication re-probe fix | signed `POST /api/studio/projects/{id}/renders` (principal gate + HMAC envelope + project scope) → the single trust translation → durable queue; submission pinned to the committed head; **fixed** the post-publish re-probe that could never complete a `creative_render` job whose publication was registered | `tests/integration/test_studio_unified_convergence_e2e.py` 9 passed (real FFmpeg, real verifier, real publication, `register_verified_asset_version`); creative/queue/verification integration set 117 passed |

## Gate results at `fb6eae7`

| Gate | Command | Result |
|------|---------|--------|
| lint | `ruff check .` | All checks passed |
| format | `ruff format --check .` | 1390 files already formatted |
| types | `mypy src` | Success: no issues found in 552 source files |
| deps | `deptry src` | 2 findings, both the pre-existing `llama-cpp-python` / `llama_cpp` pair |
| coverage | `COVERAGE_FILE=/tmp/.coverage_master python scripts/pack_coverage.py` | **ACCEPTED** — 8/8 packs ≥ 95.00% (audio 99.58, caption 96.18, core 95.97, delivery 98.58, edit 96.46, motion 98.55, portrait 99.43, scene 99.53, slideshow 97.89; total 97.65%) |
| truth | `python -m nexus_ai_agent.nagar --docs` + `python -m nexus_ai_agent.diagnostics.truth --root .` | written; `OK — no findings` |
| docs index | `python -m pytest tests/unit/test_docs_integrity.py` | 115 passed |

## Honest gaps

* **`docs/evidence/p0/evidence.json` was not regenerated.** `scripts/p0_evidence.py`
  needs a live PostgreSQL (`NEXUS_DATABASE_URL`, default `127.0.0.1:5433`); that
  service does not exist in this environment. Re-running the generator there would
  have meant inventing command output, so the bundle is untouched and the Wave-6
  measurements are recorded separately in `docs/evidence/p0/wave6_addendum.json`
  (same schema discipline: command, exit code, real output tail, revision).
* **PR #343's head branch** is `arena/750b78f5-nexus-ai-agent`. This session can
  only push to `arena/fd837e50-nexus-ai-agent`, so the waves above are on that
  branch and PR #343's own CI has not yet run against them. Fast-forwarding the
  PR requires a push to `arena/750b78f5-nexus-ai-agent`, which is out of this
  session's scope.
* Two pre-existing flakes were seen under combined runs and pass in isolation:
  `tests/unit/test_creative_studio.py::test_background_task_flow` (10 s budget for
  a real FFmpeg job) and `tests/unit/test_request_queue.py::test_queue_module_stays_within_python_310_asyncio_api`
  (`ast.parse` recursion-depth state left by an earlier test).
