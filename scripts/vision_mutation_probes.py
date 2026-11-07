"""Run ten destructive Vision mutations, require RED, and restore byte-for-byte."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
MODEL = ROOT / "src/nexus_ai_agent/creative/packs/vision/models.py"
OPS = ROOT / "src/nexus_ai_agent/creative/packs/vision/operations.py"
TEST = [
    str(ROOT / ".venv/bin/python"),
    "-m",
    "pytest",
    "-q",
    str(ROOT / "tests/unit/test_vision_pack.py"),
    str(ROOT / "tests/architecture/test_vision_pack_boundary.py"),
    "-x",
]
MUTANTS = (
    (MODEL, 'extra="forbid"', 'extra="ignore"', "accept unknown field"),
    (
        MODEL,
        "le=1.0, allow_inf_nan=False",
        "le=2.0, allow_inf_nan=False",
        "accept invalid confidence",
    ),
    (
        MODEL,
        "if self.end_us <= self.start_us:",
        "if False and self.end_us <= self.start_us:",
        "skip temporal range ordering",
    ),
    (
        OPS,
        '"content_hash": f"sha256:{digest}"',
        '"content_hash": "sha256:mutated"',
        "corrupt provenance content hash",
    ),
    (OPS, "if source is None:", "if False and source is None:", "skip unknown asset validation"),
    (OPS, "if output_id in assets:", "if False and output_id in assets:", "skip output collision"),
    (
        OPS,
        '"pixel_execution": False,\n            "provenance"',
        '"pixel_execution": True,\n            "provenance"',
        "claim fake execution",
    ),
    (
        OPS,
        'provenance = {\n            "operation": operation,',
        'provenance = {\n            "operation": "mutated",',
        "remove operation provenance",
    ),
    (
        OPS,
        '"parameters": data,\n                "processor"',
        '"parameters": {},\n                "processor"',
        "remove digest parameters",
    ),
    (
        OPS,
        "m.CorrectGazeInput, PermissionLevel.CONFIRMATION",
        "m.CorrectGazeInput, PermissionLevel.REVERSIBLE",
        "bypass gaze confirmation",
    ),
)


def main() -> int:
    baseline = subprocess.run(TEST, check=False).returncode
    if baseline:
        raise SystemExit("baseline is RED")
    killed = []
    for path, old, new, name in MUTANTS:
        original = path.read_text()
        if old not in original:
            raise SystemExit(f"mutation anchor missing: {name}")
        try:
            path.write_text(original.replace(old, new, 1))
            if (
                subprocess.run(
                    TEST, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
                ).returncode
                == 0
            ):
                raise SystemExit(f"SURVIVED: {name}")
            killed.append(name)
        finally:
            path.write_text(original)
    if subprocess.run(TEST, check=False).returncode:
        raise SystemExit("restore is RED")
    print(f"GREEN -> {len(killed)}/{len(MUTANTS)} mutants RED -> restore GREEN")
    for name in killed:
        print(f"KILLED: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
