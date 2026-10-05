#!/usr/bin/env bash
# Reproduce the nexus-ai-agent merge gates locally, exactly as CI does.
#
# Why a script: the editable install is what makes five otherwise-failing tests
# pass (docs/architecture/TESTING.md §5), and the four gates must run in order.
# Running this without the gates-owner role is fine for diagnostics; see
# .agents/skills/nexus-multi-agent-board/SKILL.md rule 4.
#
# Usage:
#   bash .agents/skills/nexus-gate-reproduction/scripts/reproduce_gates.sh
#
# Set SKIP_INSTALL=1 to reuse an existing environment.

set -u
cd "$(dirname "$0")/../../../.."   # repository root

pass=0
fail=0
run() {
  local name="$1"; shift
  echo "== $name =="
  if "$@"; then
    echo "   PASS: $name"
    pass=$((pass + 1))
  else
    echo "   FAIL: $name"
    fail=$((fail + 1))
  fi
  echo
}

if [ "${SKIP_INSTALL:-0}" != "1" ]; then
  if [ -x ".venv/bin/python" ]; then
    . .venv/bin/activate
  else
    python -m venv .venv && . .venv/bin/activate
  fi
  echo "== install (editable, required) =="
  pip install -e ".[dev]" || { echo "install failed"; exit 2; }
  echo
fi

run "ruff check"          ruff check .
run "ruff format --check" ruff format --check .
run "mypy src"            mypy src
run "pytest (not slow)"   pytest -q -m "not slow"

echo "summary: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
