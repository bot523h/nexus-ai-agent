#!/usr/bin/env bash
# Bootstrap a persistent development environment for this repo.
#
# Rationale (owner decision): Arena snapshots exclude directories matching
# common toolchain names (`.venv`, `.cache`, `build`, `dist`, …), so a venv
# created inside the repo (e.g. `.venv/`) does NOT survive between turns.
# This script recreates that environment deterministically in a location
# outside the repo tree, in a few minutes.
#
# Usage:
#     bash scripts/bootstrap_dev.sh          # create (or reuse) the venv + install
#     bash scripts/bootstrap_dev.sh --verify # only assert the toolchain works
#
# The project package is installed editable (`pip install -e ".[dev]"`) so
# `make lint`, `make types`, and `make test` run against the checkout.

set -euo pipefail

VENV_DIR="${NEXUS_DEV_VENV:-/home/user/.nexus-tools/venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

verify() {
    local py="$1"
    "$py" - <<'PY'
import importlib.metadata as md
for pkg, expected in [("alembic", "1.20.0")]:
    got = md.version(pkg)
    if got != expected:
        raise SystemExit(f"{pkg} version mismatch: got {got}, expected {expected}")
print("toolchain OK:", ", ".join(f"{p}={md.version(p)}" for p in ("alembic", "sqlalchemy", "sqlmodel")))
PY
}

if [[ "${1:-}" == "--verify" ]]; then
    verify "${VENV_DIR}/bin/python"
    exit 0
fi

if [[ -x "${VENV_DIR}/bin/python" ]]; then
    echo "[bootstrap] venv already exists at ${VENV_DIR} — verifying…"
    verify "${VENV_DIR}/bin/python" && exit 0
    echo "[bootstrap] verification failed; recreating the venv."
fi

echo "[bootstrap] creating venv at ${VENV_DIR}"
"${PYTHON_BIN}" -m venv "${VENV_DIR}"

echo "[bootstrap] upgrading pip"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip >/dev/null

# numpy must land before llama-cpp-python/sentence-transformers resolve it;
# pinning avoids torch/numpy ABI churn breaking the heavy extras.
echo "[bootstrap] installing pinned numpy first"
"${VENV_DIR}/bin/python" -m pip install "numpy==1.26.4" >/dev/null

echo "[bootstrap] installing project + dev extras (this is the slow step)"
"${VENV_DIR}/bin/python" -m pip install -e ".[dev]"

verify "${VENV_DIR}/bin/python"
echo "[bootstrap] done. Activate with: source ${VENV_DIR}/bin/activate"
