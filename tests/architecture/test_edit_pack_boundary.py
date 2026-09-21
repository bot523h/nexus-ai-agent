"""Wave 3 architecture gates: the timeline edit substrate must remain lightweight and pure.

These gates mechanically enforce the Wave 3 contract:
1. **zero heavy video/ML dependencies** — no ``torch``, ``cv2``, ``scipy``,
   ``moviepy``, or external C-extensions in the pure timeline edit pack substrate;
2. **declarative pack purity** — ``creative/packs/edit/`` contains only pure
   data models and handlers (stdlib + pydantic + studio);
3. **boundary isolation** — edit pack files may only import from creative.packs and creative.studio;
4. **data-only manifest** — ``pack.manifest.json`` contains no executable keys
   and declares zero arbitrary execution permissions.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from nexus_ai_agent.creative.packs.edit.models import (
    EDIT_PACKAGE_ID,
    OPERATION_ATTACH_B_ROLL,
    OPERATION_FREEZE_FRAME,
    OPERATION_INSERT_GAP,
    OPERATION_RETIME_TO_MUSIC,
    OPERATION_REVERSE_SEGMENT,
    OPERATION_RIPPLE_DELETE,
    OPERATION_SPEED_RAMP,
    OPERATION_TRIM,
)
from nexus_ai_agent.creative.packs.manifest import CapabilityPackManifest

REPO_ROOT = Path(__file__).parents[2]
EDIT_PACK = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs" / "edit"
EDIT_MANIFEST = EDIT_PACK / "pack.manifest.json"

#: Heavy video, editing, and ML libraries strictly forbidden in edit pack substrate.
FORBIDDEN_HEAVY_EDIT_MODULES = {
    "cv2",
    "ffmpeg",
    "moviepy",
    "onnxruntime",
    "scipy",
    "torch",
    "torchaudio",
    "torchvision",
}

#: Allowed top-level modules in the pure edit pack.
ALLOWED_PACK_TOP_LEVEL = {
    "__future__",
    "collections",
    "dataclasses",
    "datetime",
    "hashlib",
    "importlib",
    "json",
    "pathlib",
    "re",
    "typing",
    "uuid",
    "pydantic",
    "nexus_ai_agent",
}

FORBIDDEN_MANIFEST_KEYS = {
    "command",
    "entrypoint",
    "entry_points",
    "exec",
    "hooks",
    "install",
    "post_install",
    "pre_install",
    "run",
    "script",
    "shell",
}


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _nexus_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("nexus_ai"):
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(a.name for a in node.names if a.name.startswith("nexus_ai"))
    return modules


def test_no_heavy_video_imports_in_edit_substrate() -> None:
    """Enforce that Wave 3 edit substrate contains zero heavy video/ML imports."""
    all_files = list(EDIT_PACK.glob("*.py"))
    assert all_files, "expected edit substrate files to exist"

    violations: list[str] = []
    for file_path in all_files:
        imports = _top_level_imports(file_path)
        hit = imports & FORBIDDEN_HEAVY_EDIT_MODULES
        if hit:
            violations.append(f"{file_path.relative_to(REPO_ROOT)} imports: {sorted(hit)}")

    assert not violations, "heavy video/ML imports found in Wave 3 edit substrate:\n" + "\n".join(
        violations
    )


def test_edit_pack_substrate_is_pure_stdlib_and_pydantic() -> None:
    """Creative edit pack layer must stay pure stdlib + pydantic."""
    for file_path in EDIT_PACK.glob("*.py"):
        imports = _top_level_imports(file_path)
        assert imports <= ALLOWED_PACK_TOP_LEVEL, (
            f"{file_path.relative_to(REPO_ROOT)} imports outside allowlist: "
            f"{sorted(imports - ALLOWED_PACK_TOP_LEVEL)}"
        )


def test_edit_pack_does_not_cross_package_boundaries() -> None:
    """Edit pack files may only import from creative.packs and creative.studio."""
    for file_path in EDIT_PACK.glob("*.py"):
        for module in _nexus_modules(file_path):
            assert module.startswith(
                ("nexus_ai_agent.creative.packs", "nexus_ai_agent.creative.studio")
            ), (
                f"{file_path.relative_to(REPO_ROOT)} crosses boundary via {module!r}; "
                "packs may only use studio contracts"
            )


def test_edit_manifest_structure_and_no_executable_keys() -> None:
    """Edit pack manifest must validate against schema and contain no executable keys."""
    assert EDIT_MANIFEST.is_file(), "edit pack.manifest.json is missing"
    raw_content = EDIT_MANIFEST.read_text(encoding="utf-8")
    data = json.loads(raw_content)

    manifest = CapabilityPackManifest.model_validate(data)
    assert manifest.package_id == EDIT_PACKAGE_ID
    assert OPERATION_TRIM in manifest.capabilities
    assert OPERATION_RIPPLE_DELETE in manifest.capabilities
    assert OPERATION_INSERT_GAP in manifest.capabilities
    assert OPERATION_SPEED_RAMP in manifest.capabilities
    assert OPERATION_REVERSE_SEGMENT in manifest.capabilities
    assert OPERATION_FREEZE_FRAME in manifest.capabilities
    assert OPERATION_ATTACH_B_ROLL in manifest.capabilities
    assert OPERATION_RETIME_TO_MUSIC in manifest.capabilities
    assert manifest.security.allow_arbitrary_native_code is False
    assert manifest.security.allow_arbitrary_wasm_imports is False
    assert manifest.network_policy.runtime_network is False

    def walk(node: object, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in FORBIDDEN_MANIFEST_KEYS, (
                    f"forbidden executable key {key!r} at {trail or '<root>'}"
                )
                walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{trail}[{index}]")

    walk(data, "")
