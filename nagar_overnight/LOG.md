# Nagar Overnight — LOG (append-only)

Session: `overnight/nagar-20261004`. All timestamps are UTC. Every entry is a
fact from a command that was run, not a claim.

## Phase 0 — live reconnaissance (read-only)

- `git rev-parse HEAD` → `e5b326b2eaf691a638d030ad57acf1ce60016ef0` on `main`.
  The mission's historical SHA is **still live** — no drift.
- `git ls-remote origin "arena/*"` → no arena branches on the remote.
- Open PRs observed: #150 ("verified CreativeWork trim path"), #151, #152, #153.
- Board `.agents/board.json` schema 2, updated `2026-09-27T16:54:20Z`.
- **Decision:** historical claims about Arena A/B were treated as context only;
  nothing was assumed. The live tree is the source of truth.

## Phase 1 — baseline

- `.venv` created; `pip install -e ".[dev]"` → EXIT=0 (editable install matters).
- `ruff check .` → PASS.
- `mypy src` → PASS (247 source files at baseline).
- `pytest -q` → **1 failed, 2914 passed, 30 skipped** in 248s.
  - The one failure, `test_knowledge_hardening.py::test_r_f28_concurrent_identical_learns_collapse_into_one`,
    is a **pre-existing timing flake**: it passes 3/3 in isolation. Recorded as
    a baseline failure; the mission says do not increase the failure set.

## Phase 2 — deep recon

- Wrote `docs/overnight/RECON.md` with file:line evidence.
- Key finding: the deterministic substrate is strong; the **cognition boundary
  is absent**. The only model surface is `LLMProvider.generate(prompt) -> str`
  (`llm/provider.py:6`).
- **Mode decision: MODE B** — the required substrate for a full intent→artifact
  vertical slice exists, but there is no safe seam to build one overnight without
  duplicating the studio/queue/verifier stack. Per the mission's rule ("if
  evidence is ambiguous, choose MODE B"), a small additive cognition package is
  built instead of a speculative re-creation.

## Phase 3 — implementation (additive, authority-free)

Created `src/nexus_ai_agent/nagar/cognition/`:

| File | Purpose |
|---|---|
| `context.py` | `CognitionContext`, `ProposalSchema`, `CognitionBudget`, `ProducerIdentity` |
| `proposal.py` | `TypedProposal`, `Refusal`, `RefusalReason`, `parse_proposal` (fail-closed) |
| `port.py` | `CognitionPort` protocol |
| `null.py` | `NullCognition` (Model Kill Test) |
| `bridge.py` | `proposal_to_command` (pure; builds a candidate schema-2 command) |
| `router.py` | `DeterministicRouter` (pure, reason-coded) |

Tests added: `tests/unit/test_cognition_boundary.py`,
`tests/unit/test_cognition_router.py`,
`tests/unit/test_cognition_bus_integration.py`,
`tests/architecture/test_cognition_isolation.py`.

### Adversarial self-review (found real defects, fixed them)

1. `schema_version=True` was coerced to `1` by Pydantic (`bool` ⊂ `int`) → the
   version gate could be bypassed. Fixed with a strict bool check + `strict=True`
   on the int/float fields.
2. `confidence=True` was coerced to `1.0`. Fixed by `strict=True`.
3. `input` was unbounded (a 300 KB payload was accepted). Fixed with a 512 KiB
   finite-JSON validator matching the bus's own input bound.
4. NaN in a *Python mapping* (not JSON text) reported `malformed`; made the
   non-finite reason explicit.

Each defect got a regression test.

### Coverage-contract interaction

`tests/unit/test_pack_coverage_contract.py::test_no_pack_test_module_is_left_out_of_the_evidence`
failed after adding the integration test (it imports the `edit` pack). The
contract's own rule is that such a test must be a canonical target **or** be
registered as host-layer evidence. The integration test is genuinely host-layer
evidence (the cognition↔bus boundary), so it was added to
`HOST_LAYER_PACK_IMPORTERS` with an honest description — **not** by weakening
the contract.

## Gate results (final)

- `ruff check .` → PASS
- `ruff format --check src tests` → PASS
- `mypy src` → PASS (255 source files)
- new cognition tests → 60 passed
- `pytest -q` → see REPORT.md "Tests" (recorded there once the run completes)

## Commit

- `3789abc` feat(nagar): additive model-optional cognition boundary
- Final full suite after the commit: `2978 passed, 0 failed, 30 skipped`.

## Phase 4 — model-backed adapter (LocalCognition)

Chosen mode: MODE B retained. Live recon found **no path on main where raw model
text reaches privilege** — model output is schema-validated at every adapter
(render JSON mode, image_gen decode); the legacy video_director model output
drives *edits* but has no authority surface. So the security task is a
documented trace, not a patch, and the genuinely-missing capability is the
model-backed producer the boundary was designed for.

Implemented (additive, zone nagar-cognition):

- adapter.py: LocalCognition — a real CognitionPort over the existing provider
  contract (async generate(prompt, system) -> str), reused structurally via
  TextGenerator, so the cognition package never imports the heavy llm package.
  Provider text goes straight into parse_proposal (single fail-closed gate).
  Bounded: asyncio.wait_for wall-clock + budget.max_attempts. Fail-closed:
  provider exception/timeout/oversized/empty/malformed -> Refusal with a stable
  code. No dispatch, no authority, no I/O, no shell.
- CognitionObserver — dependency-free counts of stages only; never records
  prompt/response content. Token accounting intentionally omitted (provider
  contract exposes no usage field — not fabricated).
- RefusalReason.PRODUCER_FAILED added.
- parse_proposal(..., provenance=...) hardening: a trusted provenance now
  overrides any provenance in the model payload (a producer cannot forge its
  origin / impersonate the null producer).
- Architecture isolation gate updated honestly: asyncio now allowed (the port
  is async); subprocess/os/eval/exec/socket/etc. stay forbidden.

Evidence:
- tests/unit/test_cognition_adapter.py — 27 tests.
- tests/unit/test_cognition_e2e_pipeline.py — 11 tests (fake provider ->
  LocalCognition -> bridge -> real ProjectAuthorizer -> real CommandBus with the
  pack-free Wave-1 registry -> handler).
- tests/unit/test_cognition_boundary.py — +2 tests for provenance override.
- Full suite: 3016 passed, 30 skipped, 0 failed (baseline 2978 + 38 new).
- ruff check / ruff format --check clean; mypy src 256 files clean.

