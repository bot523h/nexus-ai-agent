"""Shared helpers for the Character Capsule prototype tests.

The prototype lives in ``experiments/character_capsule`` (research code, kept out
of the installed package on purpose). CI runs the bare ``pytest`` entry point, so
the repository root is *not* on ``sys.path``; every test module calls
:func:`ensure_path` before importing the experiment package. Doing it here, in one
place, keeps that environment detail out of the assertions.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def ensure_path() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


ensure_path()
