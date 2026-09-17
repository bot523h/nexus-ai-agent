#!/usr/bin/env bash
# Thin wrapper over scripts/bootstrap_env.py — the real logic (venv location,
# pinned packages, verification) lives in that module so it is unit-testable
# and never drifts between the CLI and the tests.
#
# Runs under the *system* interpreter: bootstrap_env.py itself creates the venv
# from sys.executable, so this works on a cold checkout where .venv/ does not
# exist yet.
#
# Usage:
#     bash scripts/bootstrap_dev.sh          # create (or reuse) the venv + install
#     bash scripts/bootstrap_dev.sh --verify # only assert the pin contract holds
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "${REPO_ROOT}/scripts/bootstrap_env.py" "$@"
