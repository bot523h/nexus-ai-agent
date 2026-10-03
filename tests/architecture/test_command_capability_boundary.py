"""Gate 2 (D-0013): the real AI -> command -> bus -> handler boundary.

Structural pins for the reconciled canonical contract: a single envelope,
a single registry, a single bus-owned handler invocation edge, and an
authorization seam that points inward only. Behavioural gate order and
denial-before-handler are tested in
``tests/unit/test_command_capability_contract.py``.

These guards inspect the production edges already in this repository. They do
not claim generic tools/shells elsewhere in the agent are Nagar operations.
Runtime-owned roots (``creative/slideshow/*``, ``creative/render_jobs.py``)
are included in the authority inventory and must bind explicit service grants.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
CREATIVE = ROOT / "src/nexus_ai_agent/creative"
STUDIO = CREATIVE / "studio"
BUS = STUDIO / "bus.py"
AI_AREAS = (
    ROOT / "src/nexus_ai_agent/agents",
    ROOT / "src/nexus_ai_agent/agent",
    ROOT / "src/nexus_ai_agent/orchestration",
    ROOT / "src/nexus_ai_agent/llm",
)


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_only_command_bus_invokes_an_operation_handler() -> None:
    """No adapter, AI module or pack may call a registered handler directly."""
    direct: list[str] = []
    for path in sorted(CREATIVE.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "handler"
            ):
                direct.append(str(path.relative_to(ROOT)))
    assert direct == [str(BUS.relative_to(ROOT))]


def test_ai_layers_cannot_import_nagar_executors_or_pack_handlers() -> None:
    forbidden = (
        "nexus_ai_agent.creative.rendering",
        "nexus_ai_agent.creative.slideshow.ffmpeg",
        "nexus_ai_agent.creative.ffmpeg_executor",
        "nexus_ai_agent.creative.packs.",
    )
    for directory in AI_AREAS:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.py")):
            for module in _imports(_tree(path)):
                assert not module.startswith(forbidden), (
                    f"{path.relative_to(ROOT)} imports a creative executor/pack directly: {module}"
                )


def test_studio_core_cannot_execute_shell_media_or_ui_automation() -> None:
    """Narrow structural supplement to test_nagar_studio_isolation.py."""
    forbidden = {
        "subprocess",
        "os",
        "shutil",
        "socket",
        "telegram",
        "fastapi",
        "playwright",
        "selenium",
        "nexus_ai_agent.tools.system_shell",
        "nexus_ai_agent.creative.rendering",
    }
    for path in sorted(STUDIO.rglob("*.py")):
        imports = _imports(_tree(path))
        assert not imports & forbidden, f"{path.relative_to(ROOT)} imports {imports & forbidden}"


def test_authorization_seam_points_inward_only() -> None:
    """The bus consumes the seam; the seam depends only on studio models."""
    bus_imports = _imports(_tree(BUS))
    assert "nexus_ai_agent.creative.studio.authorization" in bus_imports
    seam_imports = _imports(_tree(STUDIO / "authorization.py"))
    nexus_imports = {m for m in seam_imports if m.startswith("nexus_ai_agent")}
    assert nexus_imports == {"nexus_ai_agent.creative.studio.models"}, nexus_imports


def test_single_canonical_envelope_and_protocol() -> None:
    """One envelope (studio TypedCommand), one protocol id, no v2 protocol."""
    from nexus_ai_agent.creative.studio.models import (
        COMMAND_SCHEMA_VERSION,
        PROTOCOL_VERSION,
        TypedCommand,
    )

    assert PROTOCOL_VERSION == "nagar.command.v1"
    assert COMMAND_SCHEMA_VERSION == 2
    assert "schema_version" in TypedCommand.model_fields
    assert "protocol_version" in TypedCommand.model_fields

    envelope_definitions: list[str] = []
    v2_protocol_hits: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in {
                "CommandEnvelope",
                "TypedCommand",
            }:
                envelope_definitions.append(f"{path.relative_to(ROOT)}::{node.name}")
        if "nagar.command.v2" in path.read_text(encoding="utf-8"):
            v2_protocol_hits.append(str(path.relative_to(ROOT)))
    assert envelope_definitions == ["src/nexus_ai_agent/creative/studio/models.py::TypedCommand"], (
        envelope_definitions
    )
    assert v2_protocol_hits == [], (
        "a nagar.command.v2 protocol id must never enter src/ "
        f"(reconciliation decision D-0013): {v2_protocol_hits}"
    )


# STOP-C: production reachability and minimum project permissions. These are
# the only three production composition roots; tests may create other buses.
REACHABLE_OPERATION_AUTHORITY: dict[str, dict[str, frozenset[str]]] = {
    "render_jobs.py": {
        "timeline.trim": frozenset({"project:write"}),
        "timeline.speed_ramp": frozenset({"project:write"}),
        "timeline.reverse_segment": frozenset({"project:write"}),
        "caption.transcribe": frozenset({"project:read"}),
        "color.adjust_exposure": frozenset({"project:write"}),
        "delivery.make_proxy_480p": frozenset({"project:read"}),
        "delivery.export_otio": frozenset({"project:write"}),
    },
    "slideshow/service.py": {
        "slideshow.scan_assets": frozenset({"project:write"}),
        "slideshow.score_images": frozenset({"project:read"}),
        "slideshow.suggest_tone": frozenset({"project:read"}),
        "slideshow.compose": frozenset({"project:write"}),
        "slideshow.render": frozenset({"project:write"}),
    },
    "slideshow/upscale.py": {
        "slideshow.scan_assets": frozenset({"project:write"}),
        "slideshow.upscale": frozenset({"project:write"}),
    },
}


def _production_command_bus_sites() -> list[tuple[Path, ast.Call]]:
    sites: list[tuple[Path, ast.Call]] = []
    for path in sorted(CREATIVE.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "CommandBus"
            ):
                sites.append((path, node))
    return sites


def _command_helper_operations(path: Path, operation_constants: object) -> set[str]:
    tree = _tree(path)
    operations: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_command"
            and node.args
        ):
            continue
        operation = node.args[0]
        if isinstance(operation, ast.Constant) and isinstance(operation.value, str):
            operations.add(operation.value)
        elif isinstance(operation, ast.Name):
            value = getattr(operation_constants, operation.id, None)
            assert isinstance(value, str), (
                f"{path.relative_to(ROOT)} dispatches untracked operation constant {operation.id}"
            )
            operations.add(value)
        else:
            raise AssertionError(
                f"{path.relative_to(ROOT)} has a dynamic _command operation; "
                "the authority matrix must remain closed"
            )
    return operations


def test_every_production_command_bus_root_has_a_trusted_authorizer() -> None:
    expected = {
        CREATIVE / "render_jobs.py",
        CREATIVE / "slideshow" / "service.py",
        CREATIVE / "slideshow" / "upscale.py",
    }
    sites = _production_command_bus_sites()
    assert {path for path, _ in sites} == expected
    for path, call in sites:
        authorizer = next((kw.value for kw in call.keywords if kw.arg == "authorizer"), None)
        assert authorizer is not None, (
            f"{path.relative_to(ROOT)} constructs CommandBus without an explicit trusted authorizer"
        )

        def is_project_access(value: ast.AST) -> bool:
            return (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "ProjectAccess"
            )

        project_scoped = is_project_access(authorizer)
        if isinstance(authorizer, ast.Name):
            tree = _tree(path)
            project_scoped = any(
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == authorizer.id
                    for target in node.targets
                )
                and is_project_access(node.value)
                for node in ast.walk(tree)
            )
        assert project_scoped, (
            f"{path.relative_to(ROOT)} authorizer must be a project-scoped trusted grant"
        )


def test_every_production_dispatch_envelope_has_a_service_identity() -> None:
    roots = {
        CREATIVE / "render_jobs.py": "_dispatch",
        CREATIVE / "slideshow" / "service.py": "_command",
        CREATIVE / "slideshow" / "upscale.py": "_command",
    }
    for path, helper_name in roots.items():
        tree = _tree(path)
        helper = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == helper_name
        )
        has_actor_claim = any(
            (
                isinstance(node, ast.Dict)
                and any(isinstance(key, ast.Constant) and key.value == "actor" for key in node.keys)
            )
            or (
                isinstance(node, ast.Call)
                and any(keyword.arg == "actor" for keyword in node.keywords)
            )
            for node in ast.walk(helper)
        )
        assert has_actor_claim, (
            f"{path.relative_to(ROOT)} builds a production command without an actor claim"
        )
        assert "ActorIdentity" in path.read_text(encoding="utf-8"), (
            f"{path.relative_to(ROOT)} does not bind a typed service actor"
        )


def test_reachable_operation_authority_matrix_matches_dispatch_and_registry() -> None:
    from nexus_ai_agent.creative.packs import runtime as runtime_module
    from nexus_ai_agent.creative.packs import slideshow as slideshow_operations
    from nexus_ai_agent.creative.render_jobs import (
        RENDER_JOB_OPERATION_PERMISSIONS,
        SURFACE_TO_CANONICAL,
    )
    from nexus_ai_agent.creative.slideshow.service import SLIDESHOW_SERVICE_OPERATION_PERMISSIONS
    from nexus_ai_agent.creative.slideshow.upscale import SLIDESHOW_UPSCALE_OPERATION_PERMISSIONS

    expected = REACHABLE_OPERATION_AUTHORITY
    declared_permissions = {
        "render_jobs.py": RENDER_JOB_OPERATION_PERMISSIONS,
        "slideshow/service.py": SLIDESHOW_SERVICE_OPERATION_PERMISSIONS,
        "slideshow/upscale.py": SLIDESHOW_UPSCALE_OPERATION_PERMISSIONS,
    }
    assert declared_permissions == expected

    actual = {
        "render_jobs.py": set(SURFACE_TO_CANONICAL.values()),
        "slideshow/service.py": _command_helper_operations(
            CREATIVE / "slideshow" / "service.py", slideshow_operations
        ),
        "slideshow/upscale.py": _command_helper_operations(
            CREATIVE / "slideshow" / "upscale.py", slideshow_operations
        ),
    }
    assert {root: set(permissions) for root, permissions in expected.items()} == actual

    registry = runtime_module.build_runtime_registry()
    for root, operation_permissions in expected.items():
        for operation, required_permissions in operation_permissions.items():
            spec_permissions = frozenset(registry.get_spec(operation).effective_permissions)
            assert spec_permissions == required_permissions, (
                f"{root} authority drift for {operation}: "
                f"expected {sorted(required_permissions)}, got {sorted(spec_permissions)}"
            )
