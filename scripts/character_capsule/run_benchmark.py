#!/usr/bin/env python3
"""Run the Character Capsule visual-continuity benchmark.

This is a thin CLI: all logic lives in ``experiments/character_capsule/benchmark.py``
so the behaviour is importable and testable without the ``scripts`` namespace.
The experiment package is *not* installed (it is research code, deliberately
outside ``src/nexus_ai_agent``), so the repository root is added to ``sys.path``
here — the one thing this wrapper is allowed to do.

Usage::

    python scripts/character_capsule/run_benchmark.py \
        --work-dir experiments/character_capsule/.work
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.character_capsule.benchmark import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
