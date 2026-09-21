"""Wave 7 architecture gates: the color & delivery substrate must remain lightweight and pure.

These gates mechanically enforce the Wave 7 contract:
1. **zero heavy video/color dependencies** — no ``torch``, ``cv2``, ``scipy``,
   ``colour``, ``ffmpeg``, or external C-extensions in the pure delivery pack substrate;
2. **declarative pack purity** — ``creative/packs/delivery/`` contains only pure
   data models and handlers (stdlib + pydantic + studio);
3. **boundary isolation** — delivery pack files may only import from creative.packs
   and creative.studio;
4. **data-only manifest** — ``pack.manifest.json`` contains no executable keys
   and declares zero arbitrary execution permissions.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from nexus_ai_agent.creative.packs.delivery.models import (
    DELIVERY_PACKAGE_ID,
    OPERATION_ADJUST_EXPOSURE,
    OPERATION_APPLY_LUT,
    OPERATION_AUTO_BALANCE,
    OPERATION_EXPORT_OTIO,
    OPERATION_MAKE_PROXY,
    OPERATION_MATCH_SHOT,
    OPERATION_RENDER_MASTER_4K,
)
from nexus_ai_agent.creative.packs.manifest import CapabilityPackManifest

REPO_ROOT = Path(__file__).parents[2]
DELIVERY_PACK = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs" / "delivery"
DELIVERY_MANIFEST = DELIVERY_PACK / "pack.manifest.json"

#: Heavy video, color science, and ML libraries strictly forbidden in delivery pack substrate.
FORBIDDEN_HEAVY_DELIVERY_MODULES = {
    "colour",
    "cv2",
    "ffmpeg",
    "moviepy",
    "numpy_sugar",
    "onnxruntime",
    "scipy",
    "torch",
    "torchaudio",
    "torchvision",
}

#: Allowed top-level modules in the pure delivery pack.
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


def test_no_heavy_video_imports_in_delivery_substrate() -> None:
    """Enforce that Wave 7 delivery substrate contains zero heavy video/CV/ML imports."""
    all_files = list(DELIVERY_PACK.glob("*.py"))
    assert all_files, "expected delivery substrate files to exist"

    violations: list[str] = []
    for file_path in all_files:
        imports = _top_level_imports(file_path)
        hit = imports & FORBIDDEN_HEAVY_DELIVERY_MODULES
        if hit:
            violations.append(f"{file_path.relative_to(REPO_ROOT)} imports: {sorted(hit)}")

    assert not violations, (
        "heavy video/CV/ML imports found in Wave 7 delivery substrate:\n" + "\n".join(violations)
    )


def test_delivery_pack_substrate_is_pure_stdlib_and_pydantic() -> None:
    """Creative delivery pack layer must stay pure stdlib + pydantic."""
    for file_path in DELIVERY_PACK.glob("*.py"):
        imports = _top_level_imports(file_path)
        assert imports <= ALLOWED_PACK_TOP_LEVEL, (
            f"{file_path.relative_to(REPO_ROOT)} imports outside allowlist: "
            f"{sorted(imports - ALLOWED_PACK_TOP_LEVEL)}"
        )


def test_delivery_pack_does_not_cross_package_boundaries() -> None:
    """Delivery pack files may only import from creative.packs and creative.studio."""
    for file_path in DELIVERY_PACK.glob("*.py"):
        for module in _nexus_modules(file_path):
            assert module.startswith(
                ("nexus_ai_agent.creative.packs", "nexus_ai_agent.creative.studio")
            ), (
                f"{file_path.relative_to(REPO_ROOT)} crosses boundary via {module!r}; "
                "packs may only use studio contracts"
            )


def test_delivery_manifest_structure_and_no_executable_keys() -> None:
    """Delivery pack manifest must validate against schema and contain no executable keys."""
    assert DELIVERY_MANIFEST.is_file(), "delivery pack.manifest.json is missing"
    raw_content = DELIVERY_MANIFEST.read_text(encoding="utf-8")
    data = json.loads(raw_content)

    manifest = CapabilityPackManifest.model_validate(data)
    assert manifest.package_id == DELIVERY_PACKAGE_ID
    assert OPERATION_APPLY_LUT in manifest.capabilities
    assert OPERATION_ADJUST_EXPOSURE in manifest.capabilities
    assert OPERATION_AUTO_BALANCE in manifest.capabilities
    assert OPERATION_MATCH_SHOT in manifest.capabilities
    assert OPERATION_MAKE_PROXY in manifest.capabilities
    assert OPERATION_EXPORT_OTIO in manifest.capabilities
    assert OPERATION_RENDER_MASTER_4K in manifest.capabilities
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
