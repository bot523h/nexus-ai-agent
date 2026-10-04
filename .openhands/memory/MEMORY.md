# Project memory — nexus-ai-agent

## Environment / gates
- Editable install is required for the full suite: `pip install -e ".[dev]"`.
  With a bare `PYTHONPATH=src`, 4 migration-race tests + 1 distribution-version
  test fail for environmental reasons (documented in docs/architecture/TESTING.md §5).
- Gate commands: `ruff check .` · `ruff format --check src tests` · `mypy src` · `pytest -q`.
- pytest module basenames must be globally unique: two files named
  `test_cognition_boundary.py` (unit + architecture) collide — rename one.
- `tests/unit/test_pack_coverage_contract.py::test_no_pack_test_module_is_left_out_of_the_evidence`
  fails if ANY new test file imports `nexus_ai_agent.creative.packs.<pack>` without
  being a canonical target or listed in `HOST_LAYER_PACK_IMPORTERS`
  (src/nexus_ai_agent/continuum/pack_coverage.py:92). Classify honestly, do not weaken.
- `scripts/agent_board.py show` has a side effect: it auto-GCs expired leases and
  rewrites `.agents/board.json`. Read it, then `git checkout -- .agents/board.json`
  if you did not intend to claim anything.

## Architecture facts (verified on main @ e5b326b)
- Deterministic core: `creative/studio/bus.py` CommandBus (only path that mutates
  state; identity never self-asserted), `studio/capabilities.py` registry,
  `studio/authorization.py` ProjectAuthorizer/ProjectAccess, `studio/lifecycle.py`
  pack gate, `jobs/lifecycle.py` durable jobs + fencing, `jobs/verification.py`
  independent verification.
- Two registries: `build_wave1_registry()` (5 ops) vs `build_runtime_registry()`
  (57 ops, includes `timeline.trim` via `creative/packs/runtime.py:204`).
- `timeline.trim` handler: `creative/packs/edit/operations.py:60`; input model
  `creative/packs/edit/models.py:29`; permission REVERSIBLE, requires project:write.
- Only model surface on main: `llm/provider.py` `LLMProvider.generate(prompt)->str`.
  Legacy chat/agent paths consume free text directly (hidden-authority surface).

## Session learnings (overnight/nagar-20261004)
- New additive package: `src/nexus_ai_agent/nagar/cognition/` (typed, authority-free
  proposal boundary + DeterministicRouter + NullCognition). One-way dependency:
  cognition -> studio, enforced by tests/architecture/test_cognition_isolation.py.
- GitHub push from this sandbox is BLOCKED: the App installation token is read-only
  (`Resource not accessible by integration` on POST git/refs) even though the user
  shows admin:true. Do not retry endlessly; the owner must grant `contents: write`.
- Pre-existing flake: `tests/unit/test_knowledge_hardening.py::test_r_f28_...`
  (timing-sensitive cache-stampede; passes in isolation).
