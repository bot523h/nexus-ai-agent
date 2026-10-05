#!/usr/bin/env python3
"""Scaffold a new Nagar pack operation, and statically check pack/manifest coherence.

Given a small spec, print the exact edits for the six-step recipe
(see ../SKILL.md), so no file is forgotten. Also perform a lightweight,
dependency-free coherence check between a pack's ``OPERATION_*`` constants in
``models.py`` and the ``capabilities`` list in ``pack.manifest.json`` — the
static half of law R8.

Usage:
    python .agents/skills/nexus-nagar-pack-operation/scripts/scaffold_pack_operation.py \
        --pack edit --operation timeline.deband_denoise --level B \
        --fields "clip_asset_id:str,strength:float"

    # coherence check only (no --operation):
    python .../scaffold_pack_operation.py --pack edit --check

Exit code is 1 when a requested coherence check finds a mismatch, else 0.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
PACKS_ROOT = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs"

LEVELS = {"A": "IMMEDIATE", "B": "REVERSIBLE", "C": "CONFIRMED", "D": "DENIED"}


def _pack_dir(pack: str) -> Path:
    d = PACKS_ROOT / pack
    if not d.is_dir():
        sys.exit(f"unknown pack: {pack} (looked in {PACKS_ROOT})")
    return d


def _declared_operations(pack_dir: Path) -> list[str]:
    models = (pack_dir / "models.py").read_text(encoding="utf-8")
    return sorted(set(re.findall(r'^OPERATION_[A-Z0-9_]+\s*=\s*"([^"]+)"', models, re.MULTILINE)))


def _manifest_capabilities(pack_dir: Path) -> list[str]:
    manifest = json.loads((pack_dir / "pack.manifest.json").read_text(encoding="utf-8"))
    return sorted(manifest.get("capabilities", []))


def check_coherence(pack: str) -> int:
    pack_dir = _pack_dir(pack)
    declared = _declared_operations(pack_dir)
    caps = _manifest_capabilities(pack_dir)
    only_code = sorted(set(declared) - set(caps))
    only_manifest = sorted(set(caps) - set(declared))
    print(f"pack {pack}: {len(declared)} operation constants, {len(caps)} manifest capabilities")
    if only_code:
        print(f"  in models.py but NOT in manifest (add to capabilities): {only_code}")
    if only_manifest:
        print(f"  in manifest but NOT a models.py constant: {only_manifest}")
    if only_code or only_manifest:
        print("  MISMATCH — law R8 (manifest <-> code coherence) would be red.")
        return 1
    print("  coherent.")
    return 0


def _operation_constant(operation: str) -> str:
    return "OPERATION_" + re.sub(r"[^A-Za-z0-9]+", "_", operation).upper().strip("_")


def _class_name(operation: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", operation)) + "Input"


def _field_lines(fields: str) -> list[str]:
    lines = []
    for raw in filter(None, (f.strip() for f in fields.split(","))):
        name, _, typ = raw.partition(":")
        typ = typ or "str"
        lines.append(f"    {name}: {typ}")
    return lines


def print_recipe(pack: str, operation: str, level: str, fields: str) -> None:
    pack_dir = _pack_dir(pack)
    const = _operation_constant(operation)
    cls = _class_name(operation)
    level_name = LEVELS.get(level.upper(), "REVERSIBLE")
    print(f"# New operation: {operation}  (pack={pack}, level {level.upper()} {level_name})\n")

    print(f"## 1. {pack_dir.relative_to(REPO_ROOT)}/models.py\n")
    print(f'{const} = "{operation}"\n')
    print(f"class {cls}(BaseModel):")
    print('    model_config = ConfigDict(extra="forbid", frozen=True)')
    for line in _field_lines(fields):
        print(line)
    print('    # add a @model_validator(mode="after") for cross-field invariants\n')

    print(f"## 2. {pack_dir.relative_to(REPO_ROOT)}/operations.py\n")
    handler = "_" + re.sub(r"[^a-z0-9]+", "_", operation.lower())
    print(f"def {handler}(project: Project, context: OperationContext) -> OperationOutcome:")
    print(f"    payload = {cls}.model_validate(context.input_data)")
    print("    # pure: derive new AssetRecord(s); return OperationOutcome; no I/O, no subprocess")
    print("    ...\n")
    print("register it in register_<pack>_operations with an OperationSpec(")
    print(
        f"    operation={const}, permission_level=PermissionLevel.{level_name},"
        " schema_version=1, ...)\n"
    )

    print(f"## 3. {pack_dir.relative_to(REPO_ROOT)}/pack.manifest.json\n")
    print(f'add "{operation}" to "capabilities" (keeps manifest <-> code coherent, law R8).\n')

    print("## 4. tests/unit/test_<pack>_pack.py\n")
    print(f"def test_{re.sub(r'[^a-z0-9]+', '_', operation.lower())}():")
    print("    # build a Project, dispatch a TypedCommand through a real CommandBus,")
    print("    # assert the outcome and the resulting state. No bus/registry mocks.\n")

    print("## 5. tests/architecture/test_<pack>_pack_boundary.py\n")
    print("if the gate enumerates operation ids, add the new one; then run:")
    print(f"    python -m pytest -q tests/architecture/test_{pack}_pack_boundary.py\n")

    print("## 6. real bytes? extend the render lane, never the handler\n")
    print("    creative/rendering/ir.py + compiler.py (LaneIR -> filtergraph -> argv -> FFmpeg).\n")

    print("## After")
    print("    docs/architecture/CREATIVE_STUDIO.md §5 coverage table")
    print("    + docs/NAGAR_70_OPERATIONS_TDD.md\n")
    print("## Guard against the coverage-contract trap")
    print("    If a NEW test file imports this pack, classify it in")
    print("    src/nexus_ai_agent/continuum/pack_coverage.py")
    print("    (PACK_TEST_TARGETS or HOST_LAYER_PACK_IMPORTERS),")
    print("    or use build_wave1_registry() in pack-free tests.\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold a Nagar pack operation")
    parser.add_argument("--pack", required=True, help="pack directory name, e.g. edit")
    parser.add_argument("--operation", help="operation id, e.g. timeline.deband_denoise")
    parser.add_argument("--level", default="B", choices=list(LEVELS), help="permission level")
    parser.add_argument("--fields", default="", help="comma-separated name:type input fields")
    parser.add_argument("--check", action="store_true", help="run the coherence check only")
    args = parser.parse_args()

    rc = 0
    if args.check or not args.operation:
        rc = check_coherence(args.pack)
        if not args.operation:
            return rc
    print()
    print_recipe(args.pack, args.operation, args.level, args.fields)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
