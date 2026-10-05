---
name: nexus-gate-reproduction
description: This skill should be used when setting up the nexus-ai-agent development environment, when asked to "run the gates", "reproduce CI locally", "why does pytest fail locally but pass in CI", "install the package", "run ruff/mypy/pytest", or before claiming a task is done. It encodes the editable-install gotcha, the four gate commands, the environmental-only failures, and the exact evidence a reviewer accepts.
---

# NEXUS Gate Reproduction

## Purpose

The repository's merge gates are mechanical and cheap to run, but three environmental traps make a
naive local run report failures that are not real code defects. This skill reproduces the gates
faithfully and distinguishes a genuine regression from an environment artifact.

The gates (from `docs/architecture/TESTING.md` §1 and the `Makefile`):

| Gate | Command |
|---|---|
| Lint | `ruff check .` |
| Format | `ruff format --check .` (note: also `src tests` in some CI steps) |
| Types | `mypy src` |
| Tests | `pytest -q -m "not slow"` |

`make lint && make types && make test` is the local equivalent.

## The three environment traps

### 1. The editable-install gotcha (the important one)

A bare `PYTHONPATH=src` fails **five** tests for purely environmental reasons: four
migration-race tests spawn `python -m nexus_ai_agent.cli` subprocesses that cannot import a
non-installed package, and `test_version_command::test_running_version_matches_installed_distribution`
needs distribution metadata. `pip install -e . --no-deps` fixes all five. CI installs the package,
so it never sees this. Documented in `docs/architecture/TESTING.md` §5.

### 2. Console-script `pytest` vs `python -m pytest`

CI runs the console-script `pytest` entrypoint, where the repository root is **not** on `sys.path`.
Two guards exist because of this and both must stay green:

- `tests/architecture/test_test_suite_hygiene.py` — a test module must never `from tests.unit.x import ...`
  (that only resolves under `python -m pytest`).
- `tests/architecture/test_scripts_import_boundary.py` — a test must never `import scripts` (the
  `scripts/` namespace is not installed). Reusable logic lives in the installed `nexus_ai_agent`
  package; files under `scripts/` are thin CLIs over it.

### 3. Globally unique test basenames

pytest's default discovery requires unique module basenames. Two files named
`test_cognition_boundary.py` (one in `tests/unit`, one in `tests/architecture`) collide with a
collection error. Before creating a test file, confirm the basename is unused:

```bash
find tests -name "<your-basename>.py"
```

## Procedure

```bash
# 1. Create the environment exactly as CI does
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # editable install is what makes the 5 tests pass

# 2. Run the gates
make lint && make types && make test

# 3. Diagnostic-only runs (allowed even without the gates owner role)
ruff check .
python -m pytest -q tests/unit/<target>.py
python -m pytest -q tests/architecture/       # the fitness functions: fast, pure AST/JSON
python -m pytest -q tests/unit/test_docs_integrity.py   # documentation gates
```

If `uv` is available it is faster and cross-platform: `uv venv .venv && uv pip install -e ".[dev]"`.

## Reading a failure honestly

- **Real regression** — the failure names a production path and reproduces with
  `python -m pytest` after `pip install -e .`.
- **Environment artifact** — one of the five editable-install tests, or a `ModuleNotFoundError` for
  an optional heavy dep (`chromadb`, `sentence_transformers`, `llama_cpp`). Confirm by installing
  the editable package / the extra, then re-running.
- **Collection error** — a duplicate test basename or a `tests.`/`scripts.` import (traps 2–3).

Never weaken a test, and never mark a gate green on the strength of a partial run. Evidence is the
exact command and its observed result.

## Additional Resources

- **`references/gate-map.md`** — every gate and CI job, the known environmental-only failures, the
  optional-extras legs, and the `pack_coverage` contract that a new test can trip.
- **`scripts/reproduce_gates.sh`** — creates the venv, installs editable, and runs the four gates
  with a clear pass/fail summary.

## One gates owner

Exactly one agent (the holder of `gates_owner: true` in `.agents/board.json`) runs the full gates on
main-bound work. Other agents run diagnostics and write "deferred to gates owner" in the PR body.
This skill is how the gates owner reproduces them; see the `nexus-multi-agent-board` skill for the
claim mechanics.
