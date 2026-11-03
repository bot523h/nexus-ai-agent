#!/usr/bin/env bash
# Reproducible CI install: CPU-only PyTorch with a strict source-provenance
# boundary, then the full application closure from PyPI.
#
# Why not `pip install -e ".[dev]"` alone: sentence-transformers (core dep)
# pulls `torch`, and the default PyPI wheel ships the CUDA stack (~2.3 GB),
# which exhausts a standard runner (Errno 28). Why not a global
# PIP_EXTRA_INDEX_URL either: an *extra* index is consulted for EVERY
# distribution, which is a dependency-confusion surface.
#
# Source provenance is exact, not approximate:
#   * step 1 installs ONLY the `torch` distribution, from the PyTorch CPU index
#     EXCLUSIVELY, and with `--no-deps` so the CPU index is never consulted for
#     any other distribution (not even torch's own transitive requirements);
#   * step 2 installs the editable project and EVERY other distribution from
#     PyPI ONLY, under the same `-c constraints/ci-cpu-torch.txt` pin, so the
#     exact `+cpu` build survives and nothing CUDA can enter;
#   * step 3 fails closed unless `pip check` is clean, no nvidia-*/triton
#     package is present, and `torch` is still the `+cpu` build.
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

# 1. CPU torch alone, from the PyTorch CPU index EXCLUSIVELY (no PyPI in this
#    step). --no-deps keeps the CPU index from resolving anything but the
#    torch distribution itself; torch's transitive requirements are resolved
#    from PyPI in step 2, where the environment is otherwise complete.
python -m pip install \
  --index-url https://download.pytorch.org/whl/cpu \
  --only-binary=:all: \
  --no-deps \
  -c "${CONSTRAINTS}" \
  torch

# 2. Everything else, from PyPI ONLY, under the same constraint so pip cannot
#    replace the CPU torch with a CUDA-enabled build.
python -m pip install \
  --index-url https://pypi.org/simple \
  -c "${CONSTRAINTS}" \
  -e "${EDITABLE}" "$@"

# 3. Fail closed: dependency closure complete, no CUDA stack, CPU build intact.
python - <<'PY'
import importlib.metadata as m
import subprocess
import sys

problems: list[str] = []

# The full closure must be satisfiable with the pinned CPU torch in place.
check = subprocess.run(
    [sys.executable, "-m", "pip", "check"],
    capture_output=True,
    text=True,
)
if check.returncode != 0:
    problems.append(f"pip check failed: {check.stdout.strip()}{check.stderr.strip()}")

cuda = sorted(
    d.metadata["Name"]
    for d in m.distributions()
    if (d.metadata["Name"] or "").lower().startswith("nvidia-")
    or (d.metadata["Name"] or "").lower() == "triton"
)
if cuda:
    problems.append(f"CUDA/NVIDIA packages present: {cuda}")

torch_version = m.version("torch")
if not torch_version.endswith("+cpu"):
    problems.append(f"torch is not the CPU build: torch=={torch_version}")

if problems:
    print("ci_install: " + "; ".join(problems), file=sys.stderr)
    sys.exit(1)
print(f"ci_install: torch=={torch_version}, pip check clean, no CUDA packages")
PY
