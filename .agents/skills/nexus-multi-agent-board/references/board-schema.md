# Board schema 2 — reference

Source of truth: `.agents/board.json`; enforcement: `tests/unit/test_agent_board.py` (18 tests);
layout decision: `docs/architecture/adr/0004-board-schema-2.md`.

## Top-level shape

Schema 2 requires these blocks (`test_top_level_shape_is_schema_2`):

| Block | Meaning |
|---|---|
| `schema` | must be `2` |
| `protocol` | `protocol_version: 2`, plus the Persian manifesto, identity rule, TTL, gates rule |
| `zones` | named file-zone declarations: `{id, paths[], description?}` |
| `claims` | the task cards (see below) |
| `deferred_log` | bilingual notes for blocked tasks |
| `next_work` | exactly **ten** forward tasks (the protocol requires ten) |
| `history` | closed waves, merged PRs, incidents — records, never claims |

## A claim card

```json
{
  "task": "task-126",
  "status": "available_sequenced_post_33",
  "zone": "cli-packs-registry",
  "agent_branch": "",
  "claimed_at": null,
  "ttl_hours": 24,
  "gates_owner": false,
  "exclusive_paths": ["src/nexus_ai_agent/cli.py", "tests/unit/test_packs_cli.py"],
  "prerequisites": ["task-123"],
  "acceptance_criteria": "...",
  "evidence_required": "..."
}
```

## What each schema test refuses

| Test | Fails when |
|---|---|
| `test_task_ids_are_unique` | two cards share a `task` id |
| `test_statuses_are_legal` | a `status` outside `LEGAL_STATUSES` |
| `test_active_claims_carry_owner_timestamp_and_zone` | an `active` card lacks `agent_branch`, `claimed_at` (format `%Y-%m-%dT%H:%M:%SZ`), `ttl_hours > 0`, or `exclusive_paths`; or its `zone` is undeclared |
| `test_exclusive_paths_belong_to_a_declared_zone` | a claim path is not equal-to / a prefix of / prefixed-by any path of its zone |
| `test_no_two_active_claims_own_the_same_path` | two `active` cards have overlapping paths |
| `test_prerequisites_reference_existing_tasks` | a `prerequisites` entry names a task that does not exist (`PR#33` is the one allowed non-task token) |
| `test_open_work_declares_acceptance_criteria` | a claimable card (`queued`, `available*`, `expired`, `deferred`) has no `acceptance_criteria` |
| `test_deferred_log_references_real_tasks` | `deferred_log` names an unknown task |
| `test_next_work_network_is_structured` | `next_work` is not exactly 10 entries, or an entry lacks `priority`/`zone`/`acceptance_criteria`/`title` |
| `test_history_records_closed_work_without_claim_semantics` | `history.waves_closed`/`pull_requests` empty, or a PR entry lacks `pr` or a `state` in `{MERGED, CLOSED, OPEN}` |

## Legal statuses

`queued`, `active`, `done`, `expired`, `deferred`, `available`,
`completed_released`, `completed_merged`, `completed_delivered_via_PR34`,
`active_in_review`, `superseded_by_PR33`, `assigned_to_B`, `assigned_to_B_next`,
`assigned_to_E_pr33`, `available_sequenced_post_33`, `available_sequenced_post_32`,
`available_sequenced_post_32_33`.

A new status must be added to `LEGAL_STATUSES` in `tests/unit/test_agent_board.py` **and** justified —
the enum is deliberately closed so the board cannot drift into free-form prose.

## `exclusive_paths` matching rule

`scripts/agent_board.py::_path_matches`:

- a path ending in `/` fences a **directory**: matches the directory itself and anything under it;
- otherwise it is an **exact file**.

So `src/nexus_ai_agent/cli.py` does *not* fence `src/nexus_ai_agent/cli_helpers.py`, but
`src/nexus_ai_agent/features/` fences everything under `features/`.

## The lease clock

A lease is live while `now <= claimed_at + ttl_hours` (default 24 h). `show`, `next`, and `claim`
call `gc_expired`, which flips stale `active` cards back and (in `show`) rewrites the file. This is
why a `show` can dirty the tree — and why the board tests pin `agent_board._now` instead of trusting
the calendar (the 2026-09-22 clock-bomb incident).

## Zone list (current)

`audit-remainder`, `ci-quality`, `ci-quality-2`, `cli-packs-registry`,
`continuum-evidence-measurement`, `coordination`, `coordination-audit`,
`coordination-nagar-split`, `coordination-referee`, `coordination-schema2`, `core-database`,
`core-packaging`, `core-security`, `creative-surface`, `delivery-interop`,
`delivery-interop-markers`, `docs-architecture`, `e2e-deploy`, `external-intelligence`,
`feature-wiring`, `feature-wiring-continuation`, `feature-wiring-dead-engines`, `features-rag`,
`features-rag-i18n`, `hardening-wave4-batch`, `i18n-parity`, `job-lifecycle`,
`job-lifecycle-gate5-closure`, `pack-trust-root`, `job-verification-gap-closure`, `llm-local`,
`nagar-caption-engine`, `nagar-contract-gate`, `nagar-creative-edit`, `nagar-opgap`,
`nagar-portrait`, `nagar-render-lane`, `nagar-runtime-activation`, `nagar-runtime-call-sites`,
`nagar-scene`, `p0-stabilization`, `packaging+interop`, `pr51-ci-repair`, `quality-packs`,
`release-docker-ffmpeg`, `release-metadata`, `repo-hygiene`, `security-boundary`,
`storage-observability`, `storage-r2-integration`, `storage-r2-smoke`, `storage-resilience`,
`storage-resilience-boundaries`, `storage-runbook`, `llm-request-queue-lifecycle`,
`provenance-ledger`, `durable-creative-queue-evidence`.

Re-read the live list with:

```bash
python3 -c "import json;print('\n'.join(z['id'] for z in json.load(open('.agents/board.json'))['zones']))"
```

## Known collisions to respect

- `docs-architecture` owns `.agents/` and `AGENTS.md` — a board edit collides with it.
- `coordination` also owns `.agents/` and `AGENTS.md`.
- `continuum-evidence-measurement` owns `scripts/pack_coverage.py` and the coverage tests.
- `nagar-contract-gate` owns `docs/architecture/MODULE_MAP.md`, `TESTING.md`, `docs/README.md`.
- `repo-hygiene` owns `scripts/` and the top-level status docs.

Because skill directories live under `.agents/skills/`, adding a skill is a board-layer change:
coordinate with the `docs-architecture`/`coordination` owners before pushing.
