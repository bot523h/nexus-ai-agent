"""Pack-owned font asset access; metadata is computed from shipped bytes."""

from __future__ import annotations

import hashlib
from pathlib import Path


def vazirmatn_asset() -> tuple[Path, str]:
    """Return the packaged Vazirmatn font path and its actual SHA-256 digest."""
    path = Path(__file__).resolve().parent / "fonts" / "Vazirmatn.ttf"
    if not path.is_file():
        raise FileNotFoundError(f"packaged Vazirmatn font is missing: {path}")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()
