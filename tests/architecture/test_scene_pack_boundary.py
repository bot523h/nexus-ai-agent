"""Architecture gates: the scene pack substrate must stay lightweight and pure.

Mirrors the portrait/audio/delivery pack boundaries (task-153 acceptance: گیت
مرزی / runtime activation gate):

1. **zero heavy ML/CV dependencies** — no ``torch``, ``onnxruntime``, ``cv2``,
   ``transformers`` imports anywhere in the scene substrate;
2. **declarative pack purity** — ``creative/packs/scene/`` contains only pure
   data models and handlers (stdlib + pydantic + studio + vision primitives);
3. **boundary isolation** — scene files may only import from creative.packs
   and creative.studio (never a sibling pack);
4. **data-only manifest** — ``pack.manifest.json`` contains no executable keys
   and declares zero arbitrary execution permissions.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from nexus_ai_agent.creative.packs.manifest import CapabilityPackManifest
from nexus_ai_agent.creative.packs.scene.models import (
    OPERATION_AUTO_REFRAME_SUBJECT,
    OPERATION_FIND_SUBJECT_MOMENT,
    OPERATION_REMOVE_OBJECT,
    OPERATION_SEGMENT_SUBJECT,
    SCENE_PACKAGE_ID,
)

REPO_ROOT = Path(__file__).parents[2]
SCENE_PACK = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs" / "scene"
SCENE_MANIFEST = SCENE_PACK / "pack.manifest.json"

#: Heavy ML/CV libraries strictly forbidden in the scene substrate.
FORBIDDEN_HEAVY_VISION_MODULES = {
    "cv2",
    "keras",
    "onnx",
    "onnxruntime",
    "scipy",
    "tensorflow",
    "torch",
    "torchaudio",
    "torchvision",
    "transformers",
    "ultralytics",
}

#: Allowed top-level modules in the pure scene pack.
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


def test_no_heavy_vision_imports_in_substrate() -> None:
    all_files = list(SCENE_PACK.glob("*.py"))
    assert all_files, "expected scene substrate files to exist"
    violations: list[str] = []
    for file_path in all_files:
        hit = _top_level_imports(file_path) & FORBIDDEN_HEAVY_VISION_MODULES
        if hit:
            violations.append(f"{file_path.relative_to(REPO_ROOT)} imports: {sorted(hit)}")
    assert not violations, "heavy ML/CV imports in scene substrate:\n" + "\n".join(violations)


def test_scene_pack_substrate_is_pure_stdlib_and_pydantic() -> None:
    for file_path in SCENE_PACK.glob("*.py"):
        imports = _top_level_imports(file_path)
        assert imports <= ALLOWED_PACK_TOP_LEVEL, (
            f"{file_path.relative_to(REPO_ROOT)} imports outside allowlist: "
            f"{sorted(imports - ALLOWED_PACK_TOP_LEVEL)}"
        )


def test_scene_pack_does_not_cross_package_boundaries() -> None:
    for file_path in SCENE_PACK.glob("*.py"):
        for module in _nexus_modules(file_path):
            assert module.startswith(
                ("nexus_ai_agent.creative.packs", "nexus_ai_agent.creative.studio")
            ), (
                f"{file_path.relative_to(REPO_ROOT)} crosses boundary via {module!r}; "
                "packs may only use studio contracts and pack primitives"
            )


def test_scene_manifest_structure_and_no_executable_keys() -> None:
    assert SCENE_MANIFEST.is_file(), "scene pack.manifest.json is missing"
    data = json.loads(SCENE_MANIFEST.read_text(encoding="utf-8"))

    manifest = CapabilityPackManifest.model_validate(data)
    assert manifest.package_id == SCENE_PACKAGE_ID
    assert OPERATION_SEGMENT_SUBJECT in manifest.capabilities
    assert OPERATION_REMOVE_OBJECT in manifest.capabilities
    assert OPERATION_FIND_SUBJECT_MOMENT in manifest.capabilities
    assert OPERATION_AUTO_REFRAME_SUBJECT in manifest.capabilities
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
