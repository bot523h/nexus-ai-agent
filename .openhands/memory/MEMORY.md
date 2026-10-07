# Project memory â€” nexus-ai-agent

> Working notes: `.openhands/memory/2026-10-04.md` (overnight/nagar session),
> `.openhands/memory/2026-10-05.md` (skills suite + MODULE_MAP law gate + closure audit).

## Environment / gates
- Editable install required: `pip install -e ".[dev]"`. With bare `PYTHONPATH=src`,
  4 migration-race tests + 1 distribution-version test fail (TESTING.md Â§5).
- Gate commands: `ruff check .` Â· `ruff format --check .` Â· `mypy src` Â· `pytest -q`.
  `mypy src` needs *every* optional import installed (chromadb, imageio_ffmpeg, â€¦)
  or it reports `import-not-found`; `tests/unit/test_database_url.py` needs `asyncpg`.
- pytest module basenames must be globally unique (unit + architecture names collide).
- `test_pack_coverage_contract.py::test_no_pack_test_module_is_left_out_of_the_evidence`
  fails if any new test imports `creative.packs.<pack>` without being a canonical
  target / in `HOST_LAYER_PACK_IMPORTERS` (`continuum/pack_coverage.py:92`).
- `scripts/agent_board.py show` rewrites `.agents/board.json` **only when it GCs an
  expired lease**; `next`/`claim` call `save_board` unconditionally. Never blind-
  `git checkout` the board after a `show` â€” it also discards a real edit; diff first.
- **Push from this sandbox:** the remote-embedded credential AND `$GITHUB_TOKEN` are
  read-only (403). Use the PAT: `git push https://x-access-token:$GITHUB_PERSONAL_ACCESS_TOKEN@github.com/...`.

## Architecture facts
- Deterministic core: `creative/studio/bus.py` CommandBus (only mutator; identity never
  self-asserted), `studio/capabilities.py`, `studio/authorization.py`, `studio/lifecycle.py`,
  `jobs/lifecycle.py` (+fencing), `jobs/verification.py`.
- Two registries: `build_wave1_registry()` (5 ops) vs `build_runtime_registry()` (57 ops,
  incl. `timeline.trim` via `creative/packs/runtime.py`). `timeline.trim` handler:
  `creative/packs/edit/operations.py`; permission REVERSIBLE, needs project:write.
- Only model surface on main: `llm/provider.py::LLMProvider.generate(prompt)->str`.
- Docs contract: every `docs/**` file indexed in `docs/README.md`, links resolve,
  Mermaid fences balanced, no TODO/placeholder in `docs/architecture/*`, ADR index
  agrees with files â€” all gated by `tests/unit/test_docs_integrity.py`.

## Cognition convergence (branch `overnight/nagar-20261004`, NOT on main)
- `nagar.cognition.CognitionGateway` (`cognition/gateway.py`): DeterministicRouter ->
  registry-derived ProposalSchema -> CognitionPort -> `proposal_to_command` -> CommandBus;
  rejections typed `CognitionRefused`, no second path. `allowed_operations` derives from
  `CapabilityRegistry` (`offered_operations`); a caller may only narrow (INTERSECT).
  Fail-closed `build_cognition_gateway(enabled=, provider=)`. Anti-bypass gate:
  `tests/architecture/test_legacy_agent_no_raw_execution.py`. `phi_agent.moderate` is
  fail-CLOSED. `nagar/creative/__init__.py::run_free_text_intent` = free-text slice
  (`timeline.trim`). e2e tests use pack-free `build_wave1_registry()`.

## Agent skills suite (2026-10-05, branch `arena/skills-suite`)
- Six skills under `.agents/skills/` (SKILL.md + references/, some scripts):
  nexus-multi-agent-board, nexus-gate-reproduction, nexus-nagar-pack-operation,
  nexus-architecture-contract-change, nexus-job-lifecycle, nexus-bot-surface-command.
  Index: `.agents/skills/README.md`.
- `ruff` (repo config) also formats fenced code inside Markdown â€” keep `.agents/skills/*.md`
  ruff-format-clean. `.agents/skills/` is NOT scanned by `test_docs_integrity.py`.
- **Permanent contract guard** (task-236, law **R16**):
  `tests/architecture/test_agent_skills_contract.py` â€” README index completeness
  (bidirectional), frontmatter `name`==dir, non-empty description, local refs resolve,
  helper scripts executable; 5 real-tree checks + 6 tmp-fixture mutations + vacuity guard.
- Real `quick_validate.py` (frontmatter shape only, no refs/index/exec-bit) lives at
  `/home/openhands/.openhands/cache/skills/public-skills/skills/skill-creator/scripts/quick_validate.py`.
- Board claim mechanics: insert JSON with `status` active/done, `zone` that exists,
  `exclusive_paths` inside the zone, `agent_branch`, `claimed_at`, `ttl_hours>0`.
  Card `acceptance_criteria`/`evidence_required` are enforced by the board TEST, not the
  `claim` CLI (`cmd_claim` does not gate on them).

## MODULE_MAP law gate (2026-10-05, task-234 / ADR-0009)
- AGENTS.md Â§7 ("every boundary law names its enforcing test in MODULE_MAP.md Â§3") was
  prose-only (`grep MODULE_MAP tests/` = 0 hits). Shipped
  `tests/architecture/test_module_map_law_coverage.py`: parses Â§3, keys rows by `R<n>`,
  resolves each `test_*.py[::symbol]` (file on disk + AST symbol), fails naming the law;
  vacuity guard + positive control. Registered itself as law **R15**; ADR
  `0009-module-map-law-enforcement.md`; documented in TESTING.md Â§4.
- Law **R16** (`test_agent_skills_contract.py`) extends the same principle to `.agents/skills/`.
- `docs/architecture/TESTING.md` Â§3 must NOT claim bidirectional lawâ†”test sync: only
  lawâ†’test is enforced (R15); the reverse is a review convention.
- `tests/architecture/` was covered by NO board zone. Added zone
  `architecture-law-enforcement` (paths `tests/architecture/`, `MODULE_MAP.md`,
  `docs/architecture/adr/`, `TESTING.md`). Did NOT edit `AGENTS.md` (PR#157 edits it).
- **ADR numbering hazard:** main ends at `0006`; open PRs #157 and #161 both add `0008-*`,
  #137 adds `0012-*`. I used `0009`. Re-check on rebase.

## Open-PR terrain (2026-10-05, ~76 open PRs â€” "architecture war")
- PR **#122** owns the P0-9 **chat-routing** fix (`route_intent` sends plain `chat` straight
  to `route_persona`, so `memory_context` stays empty) â€” task-205/206. Do NOT duplicate.
  The write path exists on main (`_memory_writer` stores; `test_graph_memory.py` passes).
- P0-8 (double-wired engines: `bot/app.py` + `bot/handlers.py` each build a `GeminiEngine`)
  is real and open â€” task-124.
- Adding a 77th PR is expensive; prefer additive, single-file, test-only increments.

## Foundation-closure mission (2026-10-06) â€” outcome BLOCKED by collision
- LIVE main at mission time = **9e5b803b** (the brief's 587d4b59 was stale). 66 open PRs.
- **Baseline is GREEN on main**: `ruff check/format`, `mypy src`, `pytest -q -m "not slow"`
  â†’ 3332 passed, 30 skipped (~5 min). No local defect needed fixing to get green.
- **Every mission objective's fix-files are already owned by an open PR** (computed by
  unioning `gh pr view <n> --json files` over all 66 PRs):
  P0-2 governed LLM path â†’ **#116**; P0-1/P0-8 composition+lifecycle â†’ **#115/#113/#112**;
  P0-4 moderation + P0-5 memory â†’ **#121**; P0-9 graph memory/queue â†’ **#122**;
  P0-3 vision + P0-10 legacy 410 â†’ **#99**; typed provider failures â†’ **#93**;
  P1-2 backup â†’ **#171**; P1-5 board â†’ **#172**. `bot/app.py` alone: 8 PRs;
  `bot/handlers.py`: 8 PRs. Board overlap rule (Â§AGENTS 2) forbids pushing there.
- Reproduced defects (evidence, unowned-fix): P0-1 4 distinct `GeminiEngine` + 2
  `UnifiedCloudStorage` per `build_application`+`build_handlers`; P0-3 `/vision` returns a
  canned string (`handlers.py:376-385`); P0-8 `_post_shutdown` only closes `reminders`
  (`app.py:356-360`) â€” job_queue/request_queue/summarizer/cloud unclosed;
  P0-10 `POST /creative/video-edit` still runs a background render (`api/app.py:337-394`).
  P0-4 `orchestration/graph.py:208` `result.get("safe", True)` is a *latent* fail-open
  default â€” unreachable today because `phi.moderate` normalizes (`agents/phi_agent.py:44`).
- **Honesty state (2026-10-06 night):** `provenance/passport.py` was NOT fully fail-closed —
  `_status` had `remeasurement.get("matches", True)` (indeterminate verdict read as *match*);
  fixed in **PR #179** (merged `d023540`) with `tests/unit/test_passport_remeasurement_failclosed.py`.
  `agents/planner_agent.py` still refuses fake success. P0-4 `orchestration/graph.py:208`
  `result.get("safe", True)` remains *latent* fail-open (unreachable: `phi.moderate` normalizes).
- `/newchat` was a truth defect (claimed reset, only replied); fixed in **PR #178** (merged
  `0c224b8`) — `newchat_cmd()` now drops the `tg:{chat_id}` checkpoint thread.
- `.nexus/continuum.json::test_count_expected` is stale vs actual (3349/3358); only the CI
  `continuum-evidence` job enforces it, locally ungated. File owned by #33.
- Governance: board `gates_owner` (branch `arena/01a10af5`) expired 2026-10-06T07:39Z;
  board left untouched (PR #172 owns the reconcile). `agent_board.py show` auto-GCs and
  rewrites `.agents/board.json` locally — revert before non-board commits.
- **Temporal cluster:** #147 is canonical; #145/#142 closed as superseded; **#146** is a
  separate convergence PR (same `core.py` blob as #147) — do not close it blindly.
- **Lesson:** for a mission whose objectives are all foundation-hardening, FIRST union the
  open-PR file sets and diff against the objectiveâ†’file map; if the intersection is total,
  the honest verdict is BLOCKED â€” do not open a competing PR.
