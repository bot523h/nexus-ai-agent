# NEXUS agent skills

Repository-specific skills for agents working in `nexus-ai-agent`. Each skill is a `SKILL.md` with
YAML frontmatter plus optional `references/` (detailed contracts) and `scripts/` (deterministic
helpers). They encode the repository's constitution so an agent does not have to rediscover it.

| Skill | Use it when |
|---|---|
| [`nexus-multi-agent-board`](nexus-multi-agent-board/SKILL.md) | coordinating with parallel agents: claim a task, check for overlap, release/defer. |
| [`nexus-gate-reproduction`](nexus-gate-reproduction/SKILL.md) | setting up the environment or reproducing the lint/type/test gates locally. |
| [`nexus-nagar-pack-operation`](nexus-nagar-pack-operation/SKILL.md) | adding or changing a Nagar capability-pack operation. |
| [`nexus-architecture-contract-change`](nexus-architecture-contract-change/SKILL.md) | changing a boundary law, ADR, DECISION_LOG, i18n keys, or the release version. |
| [`nexus-job-lifecycle`](nexus-job-lifecycle/SKILL.md) | adding a durable job type, worker handler, or artifact verifier. |
| [`nexus-bot-surface-command`](nexus-bot-surface-command/SKILL.md) | adding a Telegram command, callback, or user-facing handler. |

## Scope note

These directories live under `.agents/`, which is fenced by the `docs-architecture` and `coordination`
zones on the claim board. Adding or editing a skill is a board-layer change: read
`nexus-multi-agent-board`, then claim/coordinate before pushing.

They are **not** under `docs/`, so `tests/unit/test_docs_integrity.py` does not index them. The
repository's own lint gate (`ruff check .` / `ruff format --check .`) does cover the `scripts/` here,
so keep them formatted.

## Verifying a skill

```bash
# structural validation (skill-creator's validator)
python3 <skill-creator>/scripts/quick_validate.py .agents/skills/<skill>

# lint gate (the repo's own config)
ruff check .agents/skills && ruff format --check .agents/skills
```
