"""Wave 6 architecture gates: the motion graphics substrate must remain lightweight and pure.

These gates mechanically enforce the Wave 6 contract:
1. **zero heavy graphics/ML dependencies** — no ``torch``, ``cv2``, ``scipy``,
   ``wgpu``, or external C-extensions in the pure motion graphics pack substrate;
2. **declarative pack purity** — ``creative/packs/motion/`` contains only pure
   data models and handlers (stdlib + pydantic + studio);
3. **boundary isolation** — motion pack files may only import from creative.packs
   and creative.studio;
4. **data-only manifest** — ``pack.manifest.json`` contains no executable keys
   and declares zero arbitrary execution permissions.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from nexus_ai_agent.creative.packs.manifest import CapabilityPackManifest
from nexus_ai_agent.creative.packs.motion.models import (
    MOTION_PACKAGE_ID,
    OPERATION_ADD_GLOW,
    OPERATION_ADD_MOTION_BLUR,
    OPERATION_ADD_TITLE,
    OPERATION_ADD_TRANSITION,
    OPERATION_KEYFRAME_TRANSFORM,
)

REPO_ROOT = Path(__file__).parents[2]
MOTION_PACK = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs" / "motion"
MOTION_MANIFEST = MOTION_PACK / "pack.manifest.json"

#: Heavy graphics, video, and ML libraries strictly forbidden in motion pack substrate.
FORBIDDEN_HEAVY_MOTION_MODULES = {
    "cv2",
    "moviepy",
    "onnxruntime",
    "scipy",
    "torch",
    "torchaudio",
    "torchvision",
    "wgpu",
}

#: Allowed top-level modules in the pure motion pack.
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


def test_no_heavy_graphics_imports_in_motion_substrate() -> None:
    """Enforce that Wave 6 motion substrate contains zero heavy graphics/ML imports."""
    all_files = list(MOTION_PACK.glob("*.py"))
    assert all_files, "expected motion substrate files to exist"

    violations: list[str] = []
    for file_path in all_files:
        imports = _top_level_imports(file_path)
        hit = imports & FORBIDDEN_HEAVY_MOTION_MODULES
        if hit:
            violations.append(f"{file_path.relative_to(REPO_ROOT)} imports: {sorted(hit)}")

    assert not violations, (
        "heavy graphics/ML imports found in Wave 6 motion substrate:\n" + "\n".join(violations)
    )


def test_motion_pack_substrate_is_pure_stdlib_and_pydantic() -> None:
    """Creative motion pack layer must stay pure stdlib + pydantic."""
    for file_path in MOTION_PACK.glob("*.py"):
        imports = _top_level_imports(file_path)
        assert imports <= ALLOWED_PACK_TOP_LEVEL, (
            f"{file_path.relative_to(REPO_ROOT)} imports outside allowlist: "
            f"{sorted(imports - ALLOWED_PACK_TOP_LEVEL)}"
        )


def test_motion_pack_does_not_cross_package_boundaries() -> None:
    """Motion pack files may only import from creative.packs and creative.studio."""
    for file_path in MOTION_PACK.glob("*.py"):
        for module in _nexus_modules(file_path):
            assert module.startswith(
                ("nexus_ai_agent.creative.packs", "nexus_ai_agent.creative.studio")
            ), (
                f"{file_path.relative_to(REPO_ROOT)} crosses boundary via {module!r}; "
                "packs may only use studio contracts"
            )


def test_motion_manifest_structure_and_no_executable_keys() -> None:
    """Motion pack manifest must validate against schema and contain no executable keys."""
    assert MOTION_MANIFEST.is_file(), "motion pack.manifest.json is missing"
    raw_content = MOTION_MANIFEST.read_text(encoding="utf-8")
    data = json.loads(raw_content)

    manifest = CapabilityPackManifest.model_validate(data)
    assert manifest.package_id == MOTION_PACKAGE_ID
    assert OPERATION_ADD_TRANSITION in manifest.capabilities
    assert OPERATION_KEYFRAME_TRANSFORM in manifest.capabilities
    assert OPERATION_ADD_GLOW in manifest.capabilities
    assert OPERATION_ADD_MOTION_BLUR in manifest.capabilities
    assert OPERATION_ADD_TITLE in manifest.capabilities
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
