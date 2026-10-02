# Multi-Agent Working Protocol (NEXUS AI Agent)

Version 1.0 — 2026-09-21
Companion files: `AGENTS.md` (root contract), `.agents/board.json` (state), `scripts/agent_board.py` (CLI).

## 1. Why this exists

Work on this repository is performed by multiple AI agents running in **separate,
isolated sandboxes** (each Arena session gets its own clone and its own
`arena/*` branch). Two such agents can code at the same time without corrupting
each other's work **if and only if** they partition work explicitly. There is no
cross-sandbox filesystem, no shared process, and no daemon — the **git repository
itself is the only shared medium**. So the coordination state must live inside the
repo and travel through normal `pull` / `commit` / `push`.

This is the same pattern used by multi-agent engineering workflows (e.g. running
several Claude Code sessions over git worktrees of one repo):

* **Isolation layer** — one branch (or worktree) per agent; nothing is shared at
  the file level until a PR merges. Arena already enforces the branch-per-session
  half of this.
* **Coordination layer** — a shared task board with **leases** (claims that carry
  an owner, a scope of exclusive paths, and a TTL) plus a **deferral log** so a
  stopped agent leaves a durable note instead of a silent conflict.
* **Referee layer** — a deterministic overlap check that any agent (or CI) can run
  before pushing, and a single **gates owner** rule so two agents never race the
  same lint/test suite.

## 2. Task lifecycle

```
queued ──claim──▶ active (lease: owner + TTL + exclusive_paths)
                     │
        ┌────────────┼─────────────┐
     release      defer         TTL expiry
        ▼            ▼             ▼
      done      deferred ──▶ queued again (auto via gc)
                + note in deferred_log
```

* **claim** — atomic *by convention*: the agent pulls the board, flips one task to
  `active` with its branch name, commits and pushes **immediately**. If two agents
  race the same push, git itself surfaces the conflict; the loser re-runs `show`
  and loses the claim (first-pushed lease wins, later push must not overwrite a
  fresher `claimed_at`).
* **TTL / stale takeover** — every lease has `ttl_hours` (default 24). `gc` (run
  automatically inside `show`/`next`) flips expired leases to `expired`, making
  the task claimable again. An agent that is still working re-runs `claim` to
  renew its own lease (heartbeat).
* **defer** — the "stop politely" primitive. Writes a permanent entry to
  `deferred_log` with both a Persian and English note (the Persian template is the
  canonical one from `AGENTS.md`) plus a `resume_when` condition. A deferred task
  that is blocked by another *active* claim is excluded from `next`.
* **release** — task finished → `done`, zone freed. The first agent to run
  `next` afterwards becomes the new owner; this is how work "automatically"
  continues after the previous agent finishes.

## 3. The two-agent schedule currently in force

1. **Agent A** (`arena/01a0c316-nexus-ai-agent`) holds
   `P0-security-batch` (zone `core-security`, `gates_owner: true`): global auth
   middleware, dashboard PII removal, `/cloud` + `/download` path-traversal fixes,
   the protocol files themselves. It owns `bot/handlers.py` for the duration.
2. **Agent B** (any other session) must take `feature-wiring-batch`
   (zone `feature-wiring`) **only after** Agent A's PR merges to `main`. Until
   then the task stays `queued` and is recorded in `deferred_log` — this is the
   documented "I stopped because the other agent was working" state. The `defer`
   note guarantees the wiring work is not forgotten when the block clears.
3. `bot/handlers.py` appears in **both** zones' `exclusive_paths`: it is the
   highest-conflict file in the repo, so the active lease holder owns it and the
   other agent must not touch it, not even "just a small edit".

## 4. Command reference

```bash
python scripts/agent_board.py show                    # board + auto-gc of stale leases
python scripts/agent_board.py next --branch BR        # what should I work on?
python scripts/agent_board.py claim TASK --branch BR [--ttl 24] [--gates]
python scripts/agent_board.py release TASK --branch BR
python scripts/agent_board.py defer TASK --branch BR --fa "…" --resume-when "…"
python scripts/agent_board.py check --files "src/a.py,src/b.py" --branch BR   # referee
```

`check` exits `1` when any listed file overlaps another branch's **active**
`exclusive_paths` — wire it into a pre-push hook or a CI job; either way it is the
mechanical answer to "may I push this?"

## 5. Failure modes and how the protocol absorbs them

| Failure | Absorption |
|---|---|
| Two agents claim simultaneously | Both commit the board; git push conflict surfaces it; the push carrying the earlier `claimed_at` wins, the loser re-runs `show` and defers. |
| Agent dies mid-task without releasing | Lease expires after TTL → `gc` frees the zone; the `deferred_log` and `scope` fields preserve intent. |
| Agent works from a stale clone | `check` against a fresh `pull` of `main`/board before push; CI overlap check is the backstop. |
| Someone ignores the board | The PR referee (`check`) and code review reject the overlap; the board is also the first thing `AGENTS.md` orders every agent to read. |

## 6. Extending

New task → add a `claims` entry with `status: "queued"`, a `zone`, a crisp
`scope`, and explicit `exclusive_paths` (directories with trailing `/`, files
verbatim). New zone → add to `zones` with the path prefix list. Schema is
versioned (`"schema": 1`); bump deliberately and update this document.
