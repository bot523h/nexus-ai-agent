#!/usr/bin/env bash
# Reproducible CI install with a CPU-only PyTorch and a PyPI-only remainder.
#
# Why not `pip install -e ".[dev]"` alone: sentence-transformers (core dep)
# pulls `torch`, and the default PyPI wheel ships the CUDA stack (~2.3 GB),
# which exhausts a standard runner (Errno 28). Why not a global
# PIP_EXTRA_INDEX_URL either: an *extra* index is consulted for EVERY
# distribution, which is a dependency-confusion surface — a package present on
# both indexes can be served from the extra one. Here the CPU index is used
# ONLY for torch, and every other package resolves from PyPI.
#
# Usage: scripts/ci_install.sh "<editable-spec>" [extra pip args...]
#   scripts/ci_install.sh ".[dev]"
#   scripts/ci_install.sh "." pytest pytest-asyncio
#   scripts/ci_install.sh ".[pdf]" pytest pytest-asyncio
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONSTRAINTS="${REPO_ROOT}/constraints/ci-cpu-torch.txt"
EDITABLE="${1:-.[dev]}"
shift || true

# 1. CPU torch, from the PyTorch CPU index EXCLUSIVELY (no PyPI in this step),
#    bound to the pinned version so the exact build is deterministic.
python -m pip install \
  --index-url https://download.pytorch.org/whl/cpu \
  --only-binary=:all: \
  -c "${CONSTRAINTS}" \
  torch

# 2. Everything else, from PyPI ONLY, under the same constraint so pip cannot
#    replace the CPU torch with a CUDA-enabled build.
python -m pip install \
  --index-url https://pypi.org/simple \
  -c "${CONSTRAINTS}" \
  -e "${EDITABLE}" "$@"

# 3. Fail closed: prove no CUDA stack and the expected CPU build survived.
python - <<'PY'
import importlib.metadata as m
import sys

cuda = sorted(
    d.metadata["Name"]
    for d in m.distributions()
    if (d.metadata["Name"] or "").lower().startswith("nvidia-")
    or (d.metadata["Name"] or "").lower() == "triton"
)
torch_version = m.version("torch")
problems = []
if cuda:
    problems.append(f"CUDA/NVIDIA packages present: {cuda}")
if not torch_version.endswith("+cpu"):
    problems.append(f"torch is not the CPU build: torch=={torch_version}")
if problems:
    print("ci_install: " + "; ".join(problems), file=sys.stderr)
    sys.exit(1)
print(f"ci_install: torch=={torch_version}, no CUDA packages")
PY
