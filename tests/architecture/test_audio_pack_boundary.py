"""Wave 5 architecture gates: the audio studio substrate must remain lightweight and pure.

These gates mechanically enforce the Wave 5 contract:
1. **zero heavy DSP/ML dependencies** — no ``torch``, ``torchaudio``, ``librosa``,
   ``scipy``, ``soundfile``, ``aubio``, or ``pydub`` imports anywhere in the audio pack substrate;
2. **declarative pack purity** — ``creative/packs/audio/`` contains only pure
   data models and handlers (stdlib + pydantic + studio);
3. **boundary isolation** — audio pack files may only import from creative.packs
   and creative.studio;
4. **data-only manifest** — ``pack.manifest.json`` contains no executable keys
   and declares zero arbitrary execution permissions.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from nexus_ai_agent.creative.packs.audio.models import (
    AUDIO_PACKAGE_ID,
    OPERATION_BEAT_SYNC_CUT,
    OPERATION_DETECT_BEATS,
    OPERATION_DUCK_MUSIC,
    OPERATION_NORMALIZE_LOUDNESS,
)
from nexus_ai_agent.creative.packs.manifest import CapabilityPackManifest

REPO_ROOT = Path(__file__).parents[2]
AUDIO_PACK = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs" / "audio"
AUDIO_MANIFEST = AUDIO_PACK / "pack.manifest.json"

#: Heavy DSP, audio processing, and ML libraries strictly forbidden in audio pack substrate.
FORBIDDEN_HEAVY_AUDIO_MODULES = {
    "aubio",
    "ctranslate2",
    "faster_whisper",
    "librosa",
    "onnxruntime",
    "pydub",
    "scipy",
    "sounddevice",
    "soundfile",
    "torch",
    "torchaudio",
    "transformers",
}

#: Allowed top-level modules in the pure audio pack.
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


def test_no_heavy_audio_imports_in_substrate() -> None:
    """Enforce that Wave 5 contains zero heavy ML/DSP imports in substrate."""
    all_files = list(AUDIO_PACK.glob("*.py"))
    assert all_files, "expected audio substrate files to exist"

    violations: list[str] = []
    for file_path in all_files:
        imports = _top_level_imports(file_path)
        hit = imports & FORBIDDEN_HEAVY_AUDIO_MODULES
        if hit:
            violations.append(f"{file_path.relative_to(REPO_ROOT)} imports: {sorted(hit)}")

    assert not violations, "heavy ML/DSP imports found in Wave 5 audio substrate:\n" + "\n".join(
        violations
    )


def test_audio_pack_substrate_is_pure_stdlib_and_pydantic() -> None:
    """Creative pack layer must stay pure stdlib + pydantic."""
    for file_path in AUDIO_PACK.glob("*.py"):
        imports = _top_level_imports(file_path)
        assert imports <= ALLOWED_PACK_TOP_LEVEL, (
            f"{file_path.relative_to(REPO_ROOT)} imports outside allowlist: "
            f"{sorted(imports - ALLOWED_PACK_TOP_LEVEL)}"
        )


def test_audio_pack_does_not_cross_package_boundaries() -> None:
    """Audio pack files may only import from creative.packs and creative.studio."""
    for file_path in AUDIO_PACK.glob("*.py"):
        for module in _nexus_modules(file_path):
            assert module.startswith(
                ("nexus_ai_agent.creative.packs", "nexus_ai_agent.creative.studio")
            ), (
                f"{file_path.relative_to(REPO_ROOT)} crosses boundary via {module!r}; "
                "packs may only use studio contracts"
            )


def test_audio_manifest_structure_and_no_executable_keys() -> None:
    """Audio pack manifest must validate against schema and contain no executable keys."""
    assert AUDIO_MANIFEST.is_file(), "audio pack.manifest.json is missing"
    raw_content = AUDIO_MANIFEST.read_text(encoding="utf-8")
    data = json.loads(raw_content)

    manifest = CapabilityPackManifest.model_validate(data)
    assert manifest.package_id == AUDIO_PACKAGE_ID
    assert OPERATION_DETECT_BEATS in manifest.capabilities
    assert OPERATION_NORMALIZE_LOUDNESS in manifest.capabilities
    assert OPERATION_DUCK_MUSIC in manifest.capabilities
    assert OPERATION_BEAT_SYNC_CUT in manifest.capabilities
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
