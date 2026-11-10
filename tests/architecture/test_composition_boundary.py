"""Architecture gate — the composition layer owns DAG logic and nothing else.

ADR 0014 draws a hard line: the Composition Compiler owns the DAG and its
validation, and it owns **no** authorization, execution authority, job queue,
command bus, canonical capability registry, pack trust, artifact passport,
provenance authority or final verification authority.

These gates are written against the *rule*, not against today's file list, so
they fail for any future parallel runtime, any future shadow registry and any
future attempt to make the composition package self-authorizing.

1. the composition package holds **no** capability table of its own;
2. it holds **no** queue, bus, authorizer, verifier registry or passport
   builder of its own;
3. the composition job type is registered in the **canonical** worker map and
   has a **canonical** verifier;
4. the composition resolves against the **live** canonical registry — the same
   one the command bus authorizes against;
5. the new capability pack goes through the ordinary pack path (manifest,
   COMPOSITION entry, PACK_LIFECYCLE state, activation completeness);
6. the pack substrate stays **data-only**: the FFmpeg realization lives
   outside ``creative/packs/``.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
COMPOSITION_DIR = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "composition"
PACKS_DIR = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs"

#: Modules that may not be imported by the composition package.  Each of them
#: is a canonical authority or a parallel runtime: importing one would let the
#: composition layer *own* it.
FORBIDDEN_IMPORTS = frozenset(
    {
        "nexus_ai_agent.adapters.in_process_job_queue",
        "nexus_ai_agent.jobs.creative_passport",
        "nexus_ai_agent.provenance.passport",
    }
)

#: Names that would mean the composition package carries its own authority.
FORBIDDEN_NAMES = frozenset(
    {
        "InProcessJobQueue",
        "PassportBuilder",
        "CreativeExecutionPassport",
        "ProjectAccess",
        "CapabilityRegistry",
    }
)


def _modules() -> list[Path]:
    return sorted(path for path in COMPOSITION_DIR.rglob("*.py") if path.is_file())


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# ---------------------------------------------------------------------------
# 1. no capability table of its own
# ---------------------------------------------------------------------------


def test_composition_package_has_no_capability_table() -> None:
    """A composition may *resolve* capabilities, never *define* them."""
    offenders: list[str] = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in {
                "CapabilitySpec",
                "CapabilityTable",
                "CapabilityRegistry",
                "OperationSpec",
            }:
                offenders.append(f"{path.name}: class {node.name}")
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
                if name in {"CAPABILITIES", "CAPABILITY_SPECS", "OPERATIONS", "REGISTRY"}:
                    offenders.append(f"{path.name}: module-level table {name}")
            if isinstance(node, ast.FunctionDef) and node.name in {
                "build_capability_registry",
                "build_composition_registry",
            }:
                offenders.append(f"{path.name}: builder {node.name}")
    assert not offenders, (
        f"the composition package must not define capabilities of its own: {offenders}"
    )


def test_composition_package_owns_no_parallel_runtime() -> None:
    """No queue, no bus, no authorizer, no passport builder, no verifier map."""
    offenders: list[str] = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in FORBIDDEN_NAMES:
                offenders.append(f"{path.name}: class {node.name}")
            if isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_IMPORTS:
                offenders.append(f"{path.name}: imports {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in FORBIDDEN_IMPORTS:
                        offenders.append(f"{path.name}: imports {alias.name}")
    assert not offenders, f"the composition package must not own a runtime of its own: {offenders}"


# ---------------------------------------------------------------------------
# 2. the canonical registration
# ---------------------------------------------------------------------------


def test_composition_job_type_is_registered_in_the_canonical_worker_map() -> None:
    from nexus_ai_agent.creative.composition.job import COMPOSITION_JOB_TYPE
    from nexus_ai_agent.worker import default_job_handlers

    handlers = default_job_handlers()
    assert COMPOSITION_JOB_TYPE in handlers, (
        f"{COMPOSITION_JOB_TYPE} is not registered in worker.py::default_job_handlers()"
    )
    assert callable(handlers[COMPOSITION_JOB_TYPE])


def test_composition_job_type_has_a_canonical_verifier() -> None:
    from nexus_ai_agent.adapters.in_process_job_queue import default_artifact_verifiers
    from nexus_ai_agent.creative.composition.job import COMPOSITION_JOB_TYPE

    assert COMPOSITION_JOB_TYPE in default_artifact_verifiers()


def test_the_composition_resolves_against_the_live_canonical_registry() -> None:
    """The compiler must build on the same registry the bus authorizes against.

    The repository's canonical pattern is that each component builds its own
    registry instance from the **one** canonical builder
    (``creative/packs/runtime.py::build_runtime_registry``), so the guarantee
    is *fingerprint and surface identity*, not Python object identity.
    """
    from nexus_ai_agent.creative.composition.compiler import (
        CompositionCompiler,
        registry_fingerprint,
    )
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
    from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry

    live = build_runtime_registry()
    compiler = CompositionCompiler()
    assert compiler.registry_fingerprint == registry_fingerprint(live)
    assert isinstance(compiler.registry, CapabilityRegistry)
    assert set(compiler.registry.list_operations()) == set(live.list_operations())
    for operation_id in live.list_operations():
        assert compiler.registry.get_spec(operation_id) == live.get_spec(operation_id)

    # An explicitly supplied registry is used as-is — the compiler never
    # swaps in a private table of its own.
    explicit = CompositionCompiler(live)
    assert explicit.registry is live


def test_the_composition_package_owns_no_capability_table() -> None:
    """No ``dict[capability_id, CapabilitySpec]`` anywhere in the package."""
    package = Path("src/nexus_ai_agent/creative/composition")
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and node.annotation is not None:
                text = ast.unparse(node.annotation)
                if "CapabilitySpec" in text and "dict" in text:
                    offenders.append(f"{path.name}: {text}")
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.endswith("_CAPABILITIES"):
                        offenders.append(f"{path.name}: {target.id}")
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    if "Registry" in ast.unparse(base):
                        offenders.append(f"{path.name}: class {node.name}")
    assert not offenders, f"the composition package must not own a registry: {offenders}"


# ---------------------------------------------------------------------------
# 3. the new pack goes through the ordinary path
# ---------------------------------------------------------------------------


def test_the_media_pack_is_composed_and_lifecycle_available() -> None:
    from nexus_ai_agent.creative.packs.runtime import COMPOSITION
    from nexus_ai_agent.creative.studio.lifecycle import PACK_LIFECYCLE, pack_lifecycle

    package_ids = {entry.package_id for entry in COMPOSITION}
    assert "nexus.media.transform" in package_ids
    assert pack_lifecycle("nexus.media.transform").executable is True
    assert "nexus.media.transform" in PACK_LIFECYCLE


def test_the_media_pack_manifest_exists_and_declares_the_three_operations() -> None:
    import json

    manifest = PACKS_DIR / "media" / "pack.manifest.json"
    assert manifest.is_file()
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    assert raw["package_id"] == "nexus.media.transform"
    assert set(raw["capabilities"]) == {
        "media.resize_image",
        "media.grayscale_image",
        "media.image_to_video",
    }


def test_the_pack_substrate_stays_data_only() -> None:
    """The FFmpeg realization must NOT live inside ``creative/packs/``."""
    from nexus_ai_agent.creative.packs.media import operations

    source = (PACKS_DIR / "media" / "operations.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "Popen", "shell=True"):
        assert forbidden not in source, (
            f"the pack substrate must stay data-only; found {forbidden!r}"
        )
    # The media engine — the adapter half — lives outside the packs tree.
    engine = COMPOSITION_DIR / "engine.py"
    assert engine.is_file()
    assert "resolve_ffmpeg_bin" in engine.read_text(encoding="utf-8")
    assert operations.OPERATION_RESIZE == "media.resize_image"


def test_the_three_operations_are_registered_in_the_live_registry() -> None:
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

    registry = build_runtime_registry()
    for operation in (
        "media.resize_image",
        "media.grayscale_image",
        "media.image_to_video",
    ):
        assert operation in registry.list_operations()
        spec = registry.get_spec(operation)
        assert spec.is_composable is True
        assert spec.media_inputs and spec.media_outputs
