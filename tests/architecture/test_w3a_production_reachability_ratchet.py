"""AST-only production reachability ratchet for the W3 foundation wave.

This gate deliberately uses only the standard library. It follows static Python
imports from the six executable production roots, not imports in tests, and
allows the known orphan inventory only to shrink as future waves wire modules.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "nexus_ai_agent"
PRODUCTION_ROOTS = (
    "nexus_ai_agent.api.app",
    "nexus_ai_agent.worker",
    "nexus_ai_agent.creative.studio_cli",
    "nexus_ai_agent.creative.studio_typer",
    # The repository's executable module is `nagar/__main__.py`; it is nested
    # under the package's `nagar` namespace.
    "nexus_ai_agent.nagar.__main__",
    "nexus_ai_agent.cli",
)

# Current, reviewed orphan ceiling. New modules in these scientific packages
# cannot be added here casually: a module must first become statically reachable
# from a production root. W3-B/W3-C should remove names as they wire them.
ORPHAN_CEILING: dict[str, frozenset[str]] = {
    "creative/audio_engine": frozenset(
        {
            "nexus_ai_agent.creative.audio_engine.ambisonics_binaural_fdn",
            "nexus_ai_agent.creative.audio_engine.av_subframe_foley_sync",
            "nexus_ai_agent.creative.audio_engine.cinema_kernels",
            "nexus_ai_agent.creative.audio_engine.forensic_qc",
            # Merged from PR #353; still an explicit orphan until production wiring lands.
            "nexus_ai_agent.creative.audio_engine.master_cinema_pipeline",
            "nexus_ai_agent.creative.audio_engine.psychoacoustic_bark_mastering",
            "nexus_ai_agent.creative.audio_engine.spectral_voice_restoration",
            "nexus_ai_agent.creative.audio_engine.spine_lineage",
            "nexus_ai_agent.creative.audio_engine.stereo_hpss",
            "nexus_ai_agent.creative.audio_engine.wiener_denoise",
        }
    ),
    "creative/storyboard_compiler": frozenset(),
    "creative/hyper_compiler": frozenset(),
    "storage/personal_cloud_vault": frozenset(),
    "selfhood": frozenset(
        {
            "nexus_ai_agent.selfhood",
            "nexus_ai_agent.selfhood.amend",
            "nexus_ai_agent.selfhood.witness",
        }
    ),
}


def _module_name(path: Path) -> str:
    relative = path.relative_to(SRC_ROOT).with_suffix("")
    name = "nexus_ai_agent." + ".".join(relative.parts)
    return name.removesuffix(".__init__")


def _import_graph() -> tuple[dict[str, ast.Module], dict[str, set[str]]]:
    """Parse every package module and resolve imports to in-package modules."""
    modules_by_path: dict[str, ast.Module] = {}
    paths_by_name: dict[str, Path] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        name = _module_name(path)
        modules_by_path[name] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        paths_by_name[name] = path
    known = set(modules_by_path)
    graph: dict[str, set[str]] = {name: set() for name in known}

    for name, tree in modules_by_path.items():
        path = paths_by_name[name]
        for node in ast.walk(tree):
            candidates: list[str] = []
            if isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                package = name if path.name == "__init__.py" else name.rpartition(".")[0]
                base = (
                    importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                    if node.level
                    else (node.module or "")
                )
                candidates.append(base)
                candidates.extend(
                    f"{base}.{alias.name}" for alias in node.names if alias.name != "*"
                )

            for candidate in candidates:
                resolved = candidate
                while resolved and resolved not in known:
                    resolved = resolved.rpartition(".")[0]
                if resolved:
                    graph[name].add(resolved)
    return modules_by_path, graph


def _production_reachable(graph: dict[str, set[str]]) -> set[str]:
    reachable: set[str] = set()
    pending = list(PRODUCTION_ROOTS)
    while pending:
        current = pending.pop()
        if current in reachable or current not in graph:
            continue
        reachable.add(current)
        pending.extend(graph[current] - reachable)
    return reachable


def test_production_reachability_ratchet_has_no_new_scientific_orphans() -> None:
    modules, graph = _import_graph()
    reachable = _production_reachable(graph)

    assert set(PRODUCTION_ROOTS) <= set(modules), "a production root disappeared"
    for subsystem, ceiling in ORPHAN_CEILING.items():
        prefix = "nexus_ai_agent." + subsystem.replace("/", ".")
        package_modules = {
            module for module in modules if module == prefix or module.startswith(prefix + ".")
        }
        actual_orphans = package_modules - reachable
        unexpected = actual_orphans - ceiling
        assert not unexpected, (
            f"W3-A reachability ratchet: new unwired module(s) in {subsystem}: "
            f"{sorted(unexpected)}; wire them to a production root or justify and ratchet "
            "the reviewed ceiling explicitly."
        )
        # Exact ratchet: a module that gets wired must also leave the ceiling,
        # otherwise the reviewed inventory silently over-states the orphans.
        assert actual_orphans == ceiling, (
            f"W3-A ratchet inventory mismatch for {subsystem}: "
            f"actual={sorted(actual_orphans)} ceiling={sorted(ceiling)}; "
            "remove wired modules from ORPHAN_CEILING"
        )


def test_critical_render_verification_and_approval_modules_are_production_reachable() -> None:
    modules, graph = _import_graph()
    reachable = _production_reachable(graph)
    required = {
        "nexus_ai_agent.creative.storyboard_jobs",
        "nexus_ai_agent.creative.materializer",
        "nexus_ai_agent.creative.artifact_verifier",
        "nexus_ai_agent.creative.artifact_passport",
        "nexus_ai_agent.creative.artifact_download",
        "nexus_ai_agent.nagar.ibpc.approval",
    }
    assert required <= set(modules)
    assert required <= reachable, (
        f"critical production modules are unwired: {sorted(required - reachable)}"
    )


def test_worker_ast_registers_storyboard_render_job() -> None:
    modules, _graph = _import_graph()
    worker = modules["nexus_ai_agent.worker"]
    handler_imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "nexus_ai_agent.creative.storyboard_jobs"
        and any(alias.name == "storyboard_render_job" for alias in node.names)
        for node in ast.walk(worker)
    )
    registrations = [
        node
        for node in ast.walk(worker)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "default_job_handlers"
    ]
    assert handler_imported, "worker must import storyboard_render_job from storyboard_jobs"
    assert len(registrations) == 1
    registered = any(
        isinstance(node, ast.Dict)
        and any(
            isinstance(key, ast.Constant)
            and key.value == "storyboard_render"
            and isinstance(value, ast.Name)
            and value.id == "storyboard_render_job"
            for key, value in zip(node.keys, node.values, strict=True)
        )
        for node in ast.walk(registrations[0])
    )
    assert registered, (
        'default_job_handlers() must map "storyboard_render" to storyboard_render_job'
    )


def test_reachability_inventory_is_measurable_and_roots_are_not_stubs() -> None:
    modules, graph = _import_graph()
    reachable = _production_reachable(graph)
    # Post-#353 census on base main 1c1cb293: pin the measured tree so future
    # module additions trigger an explicit inventory review, not silent drift.
    assert len(modules) == 601
    assert len(reachable) == 409
    assert all(graph[root] for root in PRODUCTION_ROOTS)
