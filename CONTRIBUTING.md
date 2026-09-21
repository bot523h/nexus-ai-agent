
Dependencies that touch database or migration paths use exact == pins; all other dependencies use >=.

## Multi-agent coordination (required reading for AI agents)

If you are an AI agent (Arena session, Claude Code, …) or a human working in
parallel with one: **read `AGENTS.md` at the repo root first.** Claims, leases
and deferrals are managed through `.agents/board.json` and
`scripts/agent_board.py` (see `docs/MULTI_AGENT_PROTOCOL.md` /
`docs/MULTI_AGENT_PROTOCOL.fa.md`). Claim your task, push the board change
immediately, never touch a file under another agent's active exclusive paths,
and record a deferral note when you must stop because another agent holds the
zone. Only the current `gates_owner` claim runs the shared quality gates
(`make lint`, `make types`, `make test`) against main-bound work.
