"""Wave 2 architecture gates: the pack stays declarative, the adapter does the I/O.

Four rules, each closing a way the slideshow feature could quietly violate the
TDD's Pack/Manifest contract:

1. **manifest ↔ code coherence** — the capabilities a pack declares must be
   exactly the operations its own registration function adds (no declared-but-
   missing operation, no hidden extra surface), and the pack must activate;
2. **heavy imports stay behind the adapter** — ``numpy``/``PIL``/``httpx``/
   ``subprocess``/``wave`` may not appear in ``creative/packs`` or
   ``creative/studio``; the pure layers stay stdlib + pydantic;
3. **the adapter never bypasses the bus** — the orchestration layer dispatches
   typed commands and never touches private bus state (``bus._project``);
4. **tone templates are data** — no executable key or embedded filtergraph at
   any depth of the shipped template library.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from nexus_ai_agent.creative.packs.registry import PackRegistry
from nexus_ai_agent.creative.packs.slideshow.operations import build_slideshow_registry
from nexus_ai_agent.creative.packs.slideshow.templates import TEMPLATES_PATH

REPO_ROOT = Path(__file__).parents[2]
PACKS = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs"
STUDIO = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "studio"
ADAPTER = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "slideshow"
MANIFEST = PACKS / "slideshow" / "pack.manifest.json"

PURE_DIRECTORIES = (PACKS, STUDIO)

#: Anything that reads pixels/audio, opens sockets, spawns processes or writes
#: files outside the state store.
ADAPTER_ONLY_MODULES = {
    "PIL",
    "httpx",
    "numpy",
    "os",
    "shutil",
    "socket",
    "subprocess",
    "wave",
}

FORBIDDEN_TEMPLATE_KEYS = {
    "command",
    "entrypoint",
    "exec",
    "filter",
    "filter_complex",
    "run",
    "script",
    "shell",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_declared_capabilities_match_the_registered_operations() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    declared = set(manifest["capabilities"])
    registry = build_slideshow_registry()
    registered = {
        operation for operation in registry.list_operations() if operation.startswith("slideshow.")
    }
    assert declared == registered, (
        "the manifest and the pack's registration function disagree: "
        f"declared-only={sorted(declared - registered)} "
        f"registered-only={sorted(registered - declared)}"
    )


def test_the_pack_activates_against_its_own_manifest() -> None:
    registry = PackRegistry(build_slideshow_registry(), current_version=None)
    packs = registry.register_builtin(root=PACKS)
    pack = next(p for p in packs if p.package_id == "nexus.slideshow.compose")
    assert registry.activate(pack.package_id).active is True
    assert registry.unknown_capabilities(pack.package_id) == ()


def test_pure_layers_do_not_import_media_or_network_primitives() -> None:
    for directory in PURE_DIRECTORIES:
        files = sorted(directory.rglob("*.py"))
        assert files
        for path in files:
            # signing.py is a security seam (env key) — os is env-only, not media
            if path.name == "signing.py":
                allowed = {"os"}
                offenders = (_imports(path) & ADAPTER_ONLY_MODULES) - allowed
            else:
                offenders = _imports(path) & ADAPTER_ONLY_MODULES
            assert not offenders, (
                f"{path.relative_to(REPO_ROOT)} must stay pure; "
                f"move {sorted(offenders)} behind the adapter boundary"
            )


def test_the_adapter_is_where_media_imports_live() -> None:
    adapter_files = sorted(ADAPTER.rglob("*.py"))
    assert adapter_files, "expected the slideshow adapter package to exist"
    imported: set[str] = set()
    for path in adapter_files:
        imported |= _imports(path)
    assert {"numpy", "PIL"} <= imported
    assert "wave" in imported


def test_the_adapter_dispatches_commands_instead_of_mutating_state() -> None:
    sources = {path: path.read_text(encoding="utf-8") for path in sorted(ADAPTER.rglob("*.py"))}
    service = sources[ADAPTER / "service.py"]
    assert ".dispatch(" in service, "the adapter must go through the command bus"
    for path, source in sources.items():
        assert "bus._" not in source, f"{path.relative_to(REPO_ROOT)} pokes private bus state"
        assert "_project =" not in source, (
            f"{path.relative_to(REPO_ROOT)} assigns project state directly; the bus owns it"
        )


def test_tone_templates_are_data_only() -> None:
    payload = json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))
    offenders: list[str] = []

    def walk(node: object, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in FORBIDDEN_TEMPLATE_KEYS:
                    offenders.append(f"{trail}.{key}")
                walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{trail}[{index}]")
        elif isinstance(node, str) and ("ffmpeg" in node.lower() or node.strip().startswith("-")):
            offenders.append(f"{trail}={node!r}")

    walk(payload, "")
    assert not offenders, f"template library contains execution-shaped content: {offenders}"


def test_only_the_render_lane_spawns_a_process() -> None:
    """Wave 2c: exactly one module may run a binary, and only through argv.

    The pack plans, the adapter encodes.  Anything else spawning a process — or
    handing a string to a shell — would put an unauditable side effect inside
    the boundary the manifest describes.
    """
    spawners = {
        path.relative_to(ADAPTER).as_posix()
        for path in ADAPTER.rglob("*.py")
        if "subprocess" in _imports(path)
    }
    assert spawners == {"ffmpeg.py"}, (
        f"expected exactly one adapter module to spawn FFmpeg, found {sorted(spawners)}"
    )
    source = (ADAPTER / "ffmpeg.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    run_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
    ]
    assert len(run_calls) == 1, "the render lane must run the encoder in one place"
    assert not any(keyword.arg == "shell" for keyword in run_calls[0].keywords), (
        "the encoder must never be handed to a shell"
    )
    # The encoder always writes the staging file; publishing is a rename.
    assert "output_path=staging" in source
    assert "replace(destination)" in source
