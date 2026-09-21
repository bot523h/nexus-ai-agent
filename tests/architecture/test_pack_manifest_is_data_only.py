"""Architecture gate — a Nagar pack is data plus pre-registered adapters.

Wave 2a introduces the pack substrate.  These gates make the TDD rule
("Manifest فقط Executorهای declarative، مدل‌ها، hash، مجوزها و محدودیت منابع را
معرفی می‌کند. هیچ ``post_install``، shell command یا entrypoint آزاد مجاز نیست")
mechanical:

1. every shipped manifest parses, validates against the schema, and its raw
   JSON contains no executable key at any depth;
2. the pack package itself cannot import process/network/code-execution
   primitives (``subprocess``, ``socket``, ``ctypes``, ``shutil``, ``os.system``)
   or cross into ``storage/`` / ``llm/`` / the bot layer;
3. no manifest declares more native power than the fail-closed defaults.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from nexus_ai_agent.creative.packs.manifest import CapabilityPackManifest

REPO_ROOT = Path(__file__).parents[2]
PACKS = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs"

#: Top-level modules the pack substrate may import (stdlib + pydantic + runtime).
ALLOWED_TOP_LEVEL = {
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

#: Anything that could execute code, spawn processes or open the network.
FORBIDDEN_TOP_LEVEL = {
    "ctypes",
    "http",
    "multiprocessing",
    "os",
    "requests",
    "shutil",
    "socket",
    "subprocess",
    "urllib",
    "httpx",
    "torch",
    "cv2",
}

#: Keys that would turn a manifest into code; checked in the raw JSON text.
FORBIDDEN_MANIFEST_KEYS = {
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


def _manifest_paths() -> list[Path]:
    return sorted(PACKS.glob("*/pack.manifest.json"))


def test_builtin_manifests_exist_and_validate() -> None:
    paths = _manifest_paths()
    assert paths, "expected at least the slideshow pack manifest"
    for path in paths:
        manifest = CapabilityPackManifest.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        assert manifest.package_id.startswith("nexus.")
        assert manifest.security.allow_arbitrary_native_code is False
        assert manifest.security.allow_arbitrary_wasm_imports is False
        assert manifest.network_policy.runtime_network is False


def test_manifest_json_contains_no_executable_keys_at_any_depth() -> None:
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

    for path in _manifest_paths():
        walk(json.loads(path.read_text(encoding="utf-8")), "")


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


def test_pack_substrate_imports_stay_in_the_allowlist() -> None:
    files = sorted(PACKS.rglob("*.py"))
    assert files, "expected the pack substrate to exist"
    for path in files:
        imports = _top_level_imports(path)
        # signing.py is a security seam — allow os/nacl/hmac/base64 there
        allow = ALLOWED_TOP_LEVEL | (
            {"os", "base64", "hmac", "nacl", "hashlib"} if path.name == "signing.py" else set()
        )
        # The forbidden set still applies, but signing's os use is env-only
        # (key loading), not code execution — we allow it.
        forbidden_for_path = FORBIDDEN_TOP_LEVEL - ({"os"} if path.name == "signing.py" else set())
        assert imports <= allow, (
            f"{path.relative_to(REPO_ROOT)} imports outside the pack allowlist: "
            f"{sorted(imports - allow)}"
        )
        forbidden = imports & forbidden_for_path
        assert not forbidden, (
            f"{path.relative_to(REPO_ROOT)} imports execution/network primitives: "
            f"{sorted(forbidden)}"
        )


def test_pack_substrate_does_not_cross_package_boundaries() -> None:
    for path in sorted(PACKS.rglob("*.py")):
        for module in _nexus_modules(path):
            assert module.startswith(
                ("nexus_ai_agent.creative.packs", "nexus_ai_agent.creative.studio")
            ), (
                f"{path.relative_to(REPO_ROOT)} crosses a package boundary via {module!r}; "
                "packs may only use the studio contracts (no storage/, llm/ or bot/ imports)"
            )
