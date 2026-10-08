---
status: accepted
date: 2026-09-21
deciders: session arena/01a0c4c1 (task-131)
consulted: AGENTS.md multi-agent protocol, scripts/agent_board.py
informed: all agents working on this repository
---

# 0004. Coordination board schema 2: declared prerequisites and acceptance criteria

## Context and Problem Statement

The coordination board (`.agents/board.json`) grew by accretion: a task list, three "10 forward tasks" waves, a claim list, zones, and a takeover/deferred log — with field names inconsistent between entries, completed history interleaved with live work, and tasks whose *prerequisites* were only stated in prose inside the scope string. The tool (`scripts/agent_board.py`) reads only `status`, `agent_branch`, `claimed_at`, `ttl_hours`, `exclusive_paths`, and `deferred_log`, so nothing structural enforced the rest. Two incidents followed on 2026-09-21: three sessions claimed the same letter identity, and a task was implemented twice on two branches because its prerequisite (an open PR) was buried in text.

## Decision Drivers

- A claim must be meaningful without reading prose: prerequisites, acceptance criteria, and evidence belong in fields.
- The board must remain readable by the existing zero-dependency CLI (backward compatibility is a hard constraint).
- Completed work is history; it belongs in a condensed history section, not interleaved with live claims (which is what made collisions hard to see).
- One gates owner, one file-zone owner, lease TTL — the existing rules are good and should survive unchanged.

## Considered Options

1. Keep schema 1 and add prose discipline ("everyone writes prerequisites in `scope`").
2. Replace the board with GitHub Issues/Projects as the single source of truth.
3. **Schema 2**: keep the CLI's frozen field names, add structured fields, split live work from frozen history, and give the tool a test suite.

## Decision Outcome

Chosen option: **3**.

| Aspect | Schema 1 | Schema 2 |
|---|---|---|
| Task fields | `task`, `zone`, `status`, `scope`, `exclusive_paths` | + `title`, `priority`, `prerequisites`, `acceptance_criteria`, `evidence_required`, `owner_hint`, `deliverables` |
| Live vs history | one `claims[]` mixing everything | `claims[]` for claimable/live work; `history` block for completed waves/PRs (ids only, no claim semantics) |
| Waves | three parallel "ten forward tasks" arrays with drifting fields | one `next_work` array (wave 4) + `waves_closed` history; each entry carries acceptance criteria |
| Protocol text | `manifesto_fa`, `merge_order_note`, `gates_rule`, `identity_map_note` | + `protocol_version: 2`, `identity_rule`, `lease_ttl_hours`, `verification_rule` |
| Enforcement | none | `tests/unit/test_agent_board.py`: schema shape, unique task ids, active claims must have branch+timestamp+TTL, every exclusive path must belong to a declared zone, no active claim may overlap another's paths, and the CLI's `claim`/`check`/`release`/`gc` behaviour is tested |
| Compatibility | — | field names the CLI reads are unchanged, so `show`/`next`/`claim`/`release`/`defer`/`check` keep working |

### Consequences

- (+) A newcomer answers "may I start?" without reading prose: look at `prerequisites` + `status` + the overlap check.
- (+) Collisions of the kind that produced duplicate implementations become a failing test rather than a discovery days later.
- (+) History is preserved (waves, PRs, incidents) but no longer competes with live claims.
- (−) The board is larger; mitigated by the history block being id-only and by the CLI printing only live state.
- (−) A one-time merge cost: other open PRs (#32, #33) edit `board.json` and will conflict textually; the rule for resolution stays "the newest state wins, and it must then satisfy the schema-2 test".

## Confirmation

`pytest -q tests/unit/test_agent_board.py` plus `python scripts/agent_board.py show|next|check` on the rewritten board. The schema test fails on: duplicate task ids, an active claim without an owner branch or a lease timestamp, a zone that does not exist, overlapping exclusive paths between two active claims, or a missing prerequisite reference.

## Pros and Cons of the Options

### Option 1 — prose discipline

- (+) zero migration
- (−) prose is exactly what failed; nothing can be tested

### Option 2 — GitHub Issues/Projects

- (+) rich UI, notifications, native linking to PRs
- (−) the board would leave the repository, and the repository is the *only* medium shared by parallel sandboxes; a network-dependent board breaks the offline-first rule and the CLI's zero-dependency guarantee

### Option 3 — schema 2 (chosen)

- (+) structured, testable, still file-based and offline
- (−) migration effort and one conflict cycle with open PRs

## More Information

- [`../../../AGENTS.md`](../../../AGENTS.md) — protocol contract (identity by branch, one gates owner, leases)
- [`../../../.agents/board.json`](../../../.agents/board.json) — the board itself
- [`../../../scripts/agent_board.py`](../../../scripts/agent_board.py) — the CLI (unchanged command surface)
- [`../REFERENCES.md`](../REFERENCES.md) §6
