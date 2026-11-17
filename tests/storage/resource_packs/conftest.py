from __future__ import annotations

from pathlib import Path

import pytest
from resource_pack_testkit import PackMirror


@pytest.fixture()
def mirror(tmp_path: Path) -> PackMirror:
    root = tmp_path / "mirror"
    root.mkdir()
    return PackMirror(root=root)
