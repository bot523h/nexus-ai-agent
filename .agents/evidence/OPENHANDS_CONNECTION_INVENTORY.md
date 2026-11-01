# OPENHANDS_CONNECTION_INVENTORY.md (M13, 2026-10-29T21:38Z)

| Connection | Access | Evidence (this session) |
|---|---|---|
| GITHUB_PERSONAL_ACCESS_TOKEN (REST + GraphQL) | Read + Write | `GET /repos/bot523h/nexus-ai-agent` → 200 |
| gh CLI (GITHUB_TOKEN, account bot523h) | Read + Write | `gh auth status` → Logged in |
| git (origin remote) | Read + Push | `git fetch`/`git push` succeeded (M12) |
| OPENHANDS_API_KEY (automation service) | Read + Write | `GET /api/automation/v1` → 200; automation created |
| LANGSMITH_API_KEY | Read | `GET api.smith.langchain.com/info` → 200 |
| Local agent-server API (localhost:8001) | Unavailable | connection refused (000) |
| LLM keys (Groq/Gemini/jve) | Present | not exercised (no external spend) |
| Repository tooling | Read + Exec | pytest, ruff, mypy, agent_board.py, pr_convergence.py |
