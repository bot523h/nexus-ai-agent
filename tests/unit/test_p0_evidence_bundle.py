"""The P0 evidence bundle must stay reproducible and hash-bound.

Guards the properties that make the bundle trustworthy rather than decorative:
every check in it must have exited 0 at generation time, the recorded source
revision must be a real 40-hex commit, and every "fixed site" must carry a
sha256 that still matches the file on disk — a bundle that no longer describes
the tree is worse than no bundle.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BUNDLE = REPO / "docs" / "evidence" / "p0" / "evidence.json"


@pytest.fixture(scope="module")
def bundle() -> dict:
    if not BUNDLE.is_file():
        pytest.skip("P0 evidence bundle not generated in this checkout")
    return json.loads(BUNDLE.read_text())


def test_every_recorded_check_passed(bundle: dict) -> None:
    assert bundle["all_checks_passed"] is True
    for name, check in bundle["checks"].items():
        assert check["exit_code"] == 0, f"{name} was not green at generation time"


def test_source_revision_is_a_real_commit(bundle: dict) -> None:
    assert re.fullmatch(r"[0-9a-f]{40}", bundle["source_revision"])


def test_fixed_sites_are_hash_bound_to_the_tree(bundle: dict) -> None:
    assert bundle["fixed_sites"], "no fixed sites recorded"
    for rel, recorded in bundle["fixed_sites"].items():
        actual = hashlib.sha256((REPO / rel).read_bytes()).hexdigest()
        assert actual == recorded, (
            f"{rel} changed since the evidence bundle was generated "
            f"(recorded {recorded[:12]}, actual {actual[:12]}) — regenerate it"
        )
