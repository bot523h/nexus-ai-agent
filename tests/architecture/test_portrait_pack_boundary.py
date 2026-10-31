"""Architecture gates: the portrait pack substrate must stay lightweight and pure.

Mirrors the audio/edit/delivery pack boundaries (task-152 acceptance: گیت مرزی
پک سبز):

1. **zero heavy ML/CV dependencies** — no ``torch``, ``onnxruntime``, ``cv2``,
   ``transformers`` imports anywhere in the portrait substrate;
2. **declarative pack purity** — ``creative/packs/portrait/`` contains only pure
   data models and handlers (stdlib + pydantic + studio + vision primitives);
3. **boundary isolation** — portrait files may only import from creative.packs
   and creative.studio;
4. **data-only manifest** — ``pack.manifest.json`` contains no executable keys
   and declares zero arbitrary execution permissions.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from nexus_ai_agent.creative.packs.manifest import CapabilityPackManifest
from nexus_ai_agent.creative.packs.portrait.models import (
    OPERATION_BACKGROUND_BLUR,
    OPERATION_CORRECT_GAZE,
    OPERATION_DETECT_LANDMARKS,
    OPERATION_MASK_HAIR,
    PORTRAIT_PACKAGE_ID,
)

REPO_ROOT = Path(__file__).parents[2]
PORTRAIT_PACK = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs" / "portrait"
PORTRAIT_MANIFEST = PORTRAIT_PACK / "pack.manifest.json"

#: Heavy ML/CV libraries strictly forbidden in the portrait substrate.
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

#: Allowed top-level modules in the pure portrait pack.
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
    all_files = list(PORTRAIT_PACK.glob("*.py"))
    assert all_files, "expected portrait substrate files to exist"
    violations: list[str] = []
    for file_path in all_files:
        hit = _top_level_imports(file_path) & FORBIDDEN_HEAVY_VISION_MODULES
        if hit:
            violations.append(f"{file_path.relative_to(REPO_ROOT)} imports: {sorted(hit)}")
    assert not violations, "heavy ML/CV imports in portrait substrate:\n" + "\n".join(violations)


def test_portrait_pack_substrate_is_pure_stdlib_and_pydantic() -> None:
    for file_path in PORTRAIT_PACK.glob("*.py"):
        imports = _top_level_imports(file_path)
        assert imports <= ALLOWED_PACK_TOP_LEVEL, (
            f"{file_path.relative_to(REPO_ROOT)} imports outside allowlist: "
            f"{sorted(imports - ALLOWED_PACK_TOP_LEVEL)}"
        )


def test_portrait_pack_does_not_cross_package_boundaries() -> None:
    for file_path in PORTRAIT_PACK.glob("*.py"):
        for module in _nexus_modules(file_path):
            assert module.startswith(
                ("nexus_ai_agent.creative.packs", "nexus_ai_agent.creative.studio")
            ), (
                f"{file_path.relative_to(REPO_ROOT)} crosses boundary via {module!r}; "
                "packs may only use studio contracts and pack primitives"
            )


def test_portrait_manifest_structure_and_no_executable_keys() -> None:
    assert PORTRAIT_MANIFEST.is_file(), "portrait pack.manifest.json is missing"
    data = json.loads(PORTRAIT_MANIFEST.read_text(encoding="utf-8"))

    manifest = CapabilityPackManifest.model_validate(data)
    assert manifest.package_id == PORTRAIT_PACKAGE_ID
    assert OPERATION_DETECT_LANDMARKS in manifest.capabilities
    assert OPERATION_MASK_HAIR in manifest.capabilities
    assert OPERATION_BACKGROUND_BLUR in manifest.capabilities
    assert OPERATION_CORRECT_GAZE in manifest.capabilities
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
