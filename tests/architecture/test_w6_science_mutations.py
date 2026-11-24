"""W6 science-module mutation campaign — VALID_KILL gate + SHA-256 restore.

Runs ``scripts/w6_mutations.py`` inside pytest (single shot) and asserts:

* the campaign exits 0 with ``20/20 mutants killed (VALID_KILL)``, and
* every mutation target file is byte-identical (SHA-256) to its digest taken
  before the campaign started — the fail-closed restore guarantee.

This is the test that proves the science modules are not merely "covered" but
*falsifiable*: a broken guard or formula must actually fail the tests.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

TARGET_FILES = (
    "src/nexus_ai_agent/creative/science/photochemical_emulsion.py",
    "src/nexus_ai_agent/creative/science/physical_optics_radiometry.py",
    "src/nexus_ai_agent/creative/science/optimal_transport_spd.py",
    "src/nexus_ai_agent/creative/science/symplectic_hamiltonian_pipeline.py",
    "src/nexus_ai_agent/creative/character/model.py",
    "src/nexus_ai_agent/creative/character/continuity.py",
)


def _digests() -> dict[str, str]:
    return {rel: hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() for rel in TARGET_FILES}


def test_w6_science_mutation_campaign_kills_all_mutants_and_restores_files() -> None:
    before = _digests()
    result = subprocess.run(
        [sys.executable, "scripts/w6_mutations.py"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=900,
    )
    after = _digests()

    assert result.returncode == 0, (
        f"mutation campaign failed (exit {result.returncode})\n{result.stdout}\n{result.stderr}"
    )
    assert "VALID_KILL" in result.stdout
    assert "20/20 mutants killed" in result.stdout
    assert before == after, "mutation campaign did not restore every target file"
