# Contributing

Dependencies that touch database or migration paths use exact `==` pins; all other
dependencies use `>=`.

## Local quality rails (one command each)

```bash
make hooks          # install the pre-commit hooks
make version-check  # VERSION == pyproject.toml == newest released CHANGELOG heading
make lint           # ruff check . && ruff format --check .
make types          # mypy src
make test           # pytest -q -m "not slow"
```

* **Ruff has one pinned version, declared twice on purpose**: `rev:` in
  `.pre-commit-config.yaml` and `ruff==…` in the `lint-fast` job of
  `.github/workflows/ci.yml`. They must stay equal, and `pyproject.toml` must still
  admit that version — `tests/unit/test_ci_lint_parity.py` fails otherwise. Bump both
  files in one commit: ruff's format output changes between minors, so a split pin
  means "green locally, red in CI".
* **Lint never blocks the tests.** `lint-fast` installs no package and reports lint and
  version drift in seconds; the `test` job deliberately declares no `needs:` gate, so a
  broken-lint push shows the lint failure while the test signal stays independent.
* **Version drift fails before the install.** `scripts/check_version_lockstep.py` is
  standard-library only and runs as the first step of the fast rail (and as
  `make version-check` locally), so a drifted `VERSION` / `pyproject.toml` /
  `CHANGELOG.md` is reported in about a second instead of after a full dependency
  install.

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
