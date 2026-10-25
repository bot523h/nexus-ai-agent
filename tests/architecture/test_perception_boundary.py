"""Perception lane gates: the pack stays pure, the adapter owns perception processes.

1. the pack (``creative/packs/perception``) is stdlib + pydantic + studio only and
   never spawns a process, opens a socket or touches the file system;
2. the adapter (``creative/perception``) runs FFmpeg for perception measurements, never
   uses a shell, and has no network client;
3. the manifest is data-only, declares no network, and matches the operations;
4. the pack is composed and has a lifecycle record (never dead weight).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from nexus_ai_agent.creative.packs.manifest import CapabilityPackManifest
from nexus_ai_agent.creative.packs.perception.models import PERCEPTION_PACKAGE_ID
from nexus_ai_agent.creative.packs.perception.operations import build_perception_registry
from nexus_ai_agent.creative.packs.runtime import COMPOSITION_BY_PACKAGE_ID
from nexus_ai_agent.creative.studio.lifecycle import PACK_LIFECYCLE, LifecycleState

REPO_ROOT = Path(__file__).parents[2]
CREATIVE = REPO_ROOT / "src" / "nexus_ai_agent" / "creative"
PACK = CREATIVE / "packs" / "perception"
ADAPTER = CREATIVE / "perception"
MANIFEST = PACK / "pack.manifest.json"

PACK_ALLOWED = {"__future__", "hashlib", "json", "re", "typing", "pydantic", "nexus_ai_agent"}
ADAPTER_ALLOWED = {
    "__future__",
    "collections",
    "dataclasses",
    "hashlib",
    "os",
    "pathlib",
    "re",
    "signal",
    "subprocess",
    "threading",
    "math",
    "pydantic",
    "typing",
    "nexus_ai_agent",
}
NETWORK_AND_HEAVY = {
    "aiohttp", "httpx", "requests", "socket", "urllib", "urllib3", "numpy", "scipy",
    "librosa", "torch", "cv2", "PIL", "pydub", "soundfile",
}  # fmt: skip
FORBIDDEN_MANIFEST_KEYS = {
    "command", "entrypoint", "entry_points", "exec", "hooks", "install", "post_install",
    "pre_install", "run", "script", "shell",
}  # fmt: skip


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _nexus_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("nexus_ai")
    ]


def test_pack_is_pure() -> None:
    files = sorted(PACK.glob("*.py"))
    assert files
    for path in files:
        imports = _imports(path)
        assert imports <= PACK_ALLOWED, f"{path.name}: {sorted(imports - PACK_ALLOWED)}"
        assert not imports & NETWORK_AND_HEAVY
        for module in _nexus_modules(path):
            assert module.startswith(
                ("nexus_ai_agent.creative.packs", "nexus_ai_agent.creative.studio")
            ), f"{path.name} crosses the pack boundary via {module}"


def test_perception_adapter_runs_ffmpeg_and_never_uses_a_shell() -> None:
    files = sorted(ADAPTER.glob("*.py"))
    assert {p.name for p in files} >= {"analyzer.py", "parsers.py", "__init__.py"}
    for path in files:
        imports = _imports(path)
        assert imports <= ADAPTER_ALLOWED | {"nexus_ai_agent"}, (
            f"{path.name}: {sorted(imports - ADAPTER_ALLOWED)}"
        )
        assert not imports & NETWORK_AND_HEAVY
        source = path.read_text(encoding="utf-8")
        assert "shell=True" not in source
        assert "os.system" not in source and "os.popen" not in source
        if path.name != "analyzer.py":
            assert "subprocess" not in imports, f"{path.name} must stay pure"
    # parsers are pure text -> numbers
    assert not (_imports(ADAPTER / "parsers.py") & {"subprocess", "os", "pathlib", "threading"})


def test_adapter_only_reaches_packs_the_slideshow_binary_resolver_and_itself() -> None:
    for path in ADAPTER.glob("*.py"):
        for module in _nexus_modules(path):
            assert module.startswith(
                (
                    "nexus_ai_agent.creative.packs.perception",
                    "nexus_ai_agent.creative.perception",
                    "nexus_ai_agent.creative.slideshow.ffmpeg",
                )
            ), f"{path.name} imports {module}"


def test_studio_and_other_packs_never_import_the_adapter() -> None:
    for root in (CREATIVE / "studio", CREATIVE / "packs"):
        for path in root.rglob("*.py"):
            for module in _nexus_modules(path):
                assert not module.startswith("nexus_ai_agent.creative.perception"), (
                    f"{path.relative_to(REPO_ROOT)} imports the I/O adapter"
                )


def test_manifest_is_data_only_and_offline() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest = CapabilityPackManifest.model_validate(data)
    assert manifest.package_id == PERCEPTION_PACKAGE_ID
    assert manifest.network_policy.runtime_network is False
    assert manifest.network_policy.upload_media is False
    assert manifest.security.allow_arbitrary_native_code is False
    assert list(manifest.external_binaries) == ["ffmpeg"]
    assert set(manifest.capabilities) == set(build_perception_registry().list_operations())

    def walk(node: object) -> None:
        if isinstance(node, dict):
            assert not FORBIDDEN_MANIFEST_KEYS & set(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)


def test_pack_is_composed_and_has_a_lifecycle_record() -> None:
    assert PERCEPTION_PACKAGE_ID in COMPOSITION_BY_PACKAGE_ID
    assert PACK_LIFECYCLE[PERCEPTION_PACKAGE_ID].state is LifecycleState.AVAILABLE
