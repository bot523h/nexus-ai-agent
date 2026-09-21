"""Wave 4a architecture gates: the caption substrate must remain lightweight and pure.

These gates mechanically enforce the Wave 4a contract:
1. **zero heavy dependencies** — no ``torch``, ``torchaudio``, ``whisper``,
   ``whisperx``, ``faster_whisper``, ``pyannote``, ``transformers``, or
   ``onnxruntime`` imports anywhere in the caption substrate;
2. **declarative pack purity** — ``creative/packs/caption/`` contains only pure
   data models, formatters and handlers (stdlib + pydantic + studio);
3. **port boundary isolation** — ``application/ports/caption_engine`` never
   imports adapters;
4. **fail-closed unavailable adapter** — installations without a caption profile
   must raise a typed ``CaptionProfileUnavailableError`` (code:
   ``caption_profile_unavailable``) and never silently degrade;
5. **data-only manifest** — ``pack.manifest.json`` contains no executable keys
   and declares zero arbitrary execution permissions.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import get_type_hints

import pytest

from nexus_ai_agent.application.ports.caption_engine import (
    CaptionEnginePort,
    CaptionProfileUnavailableError,
)
from nexus_ai_agent.creative.caption.unavailable_adapter import (
    UnavailableCaptionAdapter,
    UnavailableCaptionEngine,
)
from nexus_ai_agent.creative.packs.caption.models import (
    CAPTION_PACKAGE_ID,
    OPERATION_GENERATE_SRT,
    OPERATION_TRANSCRIBE,
)
from nexus_ai_agent.creative.packs.manifest import CapabilityPackManifest

REPO_ROOT = Path(__file__).parents[2]
CAPTION_PACK = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs" / "caption"
CAPTION_ADAPTER = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "caption"
CAPTION_PORT = REPO_ROOT / "src" / "nexus_ai_agent" / "application" / "ports" / "caption_engine.py"
CAPTION_MANIFEST = CAPTION_PACK / "pack.manifest.json"

#: Heavy speech and ML libraries strictly forbidden in Wave 4a.
FORBIDDEN_HEAVY_SPEECH_MODULES = {
    "ctranslate2",
    "faster_whisper",
    "onnxruntime",
    "pyannote",
    "scipy",
    "soundfile",
    "torch",
    "torchaudio",
    "transformers",
    "whisper",
    "whisperx",
}

#: Allowed top-level modules in the pure caption pack.
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


def test_no_heavy_speech_imports_in_substrate() -> None:
    """Enforce that Wave 4a contains zero heavy ML/speech imports."""
    all_files = [
        *CAPTION_PACK.glob("*.py"),
        *CAPTION_ADAPTER.glob("*.py"),
        CAPTION_PORT,
    ]
    assert all_files, "expected caption substrate files to exist"

    violations: list[str] = []
    for file_path in all_files:
        imports = _top_level_imports(file_path)
        hit = imports & FORBIDDEN_HEAVY_SPEECH_MODULES
        if hit:
            violations.append(f"{file_path.relative_to(REPO_ROOT)} imports: {sorted(hit)}")

    assert not violations, (
        "heavy ML/speech imports found in Wave 4a substrate (forbidden until Wave 4b):\n"
        + "\n".join(violations)
    )


def test_caption_pack_substrate_is_pure_stdlib_and_pydantic() -> None:
    """Creative pack layer must stay pure stdlib + pydantic."""
    for file_path in CAPTION_PACK.glob("*.py"):
        imports = _top_level_imports(file_path)
        assert imports <= ALLOWED_PACK_TOP_LEVEL, (
            f"{file_path.relative_to(REPO_ROOT)} imports outside allowlist: "
            f"{sorted(imports - ALLOWED_PACK_TOP_LEVEL)}"
        )


def test_caption_pack_does_not_cross_package_boundaries() -> None:
    """Pack files may only import from creative.packs and creative.studio."""
    for file_path in CAPTION_PACK.glob("*.py"):
        for module in _nexus_modules(file_path):
            assert module.startswith(
                ("nexus_ai_agent.creative.packs", "nexus_ai_agent.creative.studio")
            ), (
                f"{file_path.relative_to(REPO_ROOT)} crosses boundary via {module!r}; "
                "packs may only use studio contracts"
            )


def test_caption_engine_port_does_not_import_adapters() -> None:
    """Application ports must never import adapters or creative implementations."""
    imports = _top_level_imports(CAPTION_PORT)
    assert "adapters" not in imports
    for module in _nexus_modules(CAPTION_PORT):
        assert "adapters" not in module
        assert "creative.caption" not in module


def test_caption_engine_port_declares_typed_methods() -> None:
    """Port methods transcribe and is_available must be typed protocols."""
    transcribe_fn = CaptionEnginePort.transcribe
    hints = get_type_hints(transcribe_fn)
    assert "audio_path" in hints
    assert "return" in hints
    assert inspect.iscoroutinefunction(transcribe_fn)

    is_available_fn = CaptionEnginePort.is_available
    avail_hints = get_type_hints(is_available_fn)
    assert avail_hints.get("return") is bool


@pytest.mark.asyncio
async def test_unavailable_adapter_fails_closed_without_silent_fallback() -> None:
    """Unavailable adapter must fail closed with CaptionProfileUnavailableError."""
    adapter = UnavailableCaptionAdapter(reason="no weights loaded")
    assert adapter.is_available() is False

    with pytest.raises(CaptionProfileUnavailableError) as exc_info:
        await adapter.transcribe("tests/fixtures/sample.wav")

    assert exc_info.value.code == "caption_profile_unavailable"
    assert "caption_profile_unavailable" in str(exc_info.value)
    assert "no weights loaded" in str(exc_info.value)

    assert UnavailableCaptionEngine is UnavailableCaptionAdapter


def test_caption_manifest_structure_and_no_executable_keys() -> None:
    """Manifest must validate against the strict schema and contain no executable keys."""
    assert CAPTION_MANIFEST.is_file(), "caption pack.manifest.json is missing"
    raw_content = CAPTION_MANIFEST.read_text(encoding="utf-8")
    data = json.loads(raw_content)

    manifest = CapabilityPackManifest.model_validate(data)
    assert manifest.package_id == CAPTION_PACKAGE_ID
    assert OPERATION_TRANSCRIBE in manifest.capabilities
    assert OPERATION_GENERATE_SRT in manifest.capabilities
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
