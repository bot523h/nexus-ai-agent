"""Architecture and runtime guards for the task-181 authorization boundary."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    AuthorizationError,
    InputRef,
    InputReferenceError,
    TargetRef,
    Timeline,
    TypedCommand,
    new_project,
)

REPO_ROOT = Path(__file__).parents[2]
SERVICE_FILE = REPO_ROOT / "src/nexus_ai_agent/creative/slideshow/service.py"
UPSCALE_FILE = REPO_ROOT / "src/nexus_ai_agent/creative/slideshow/upscale.py"
RENDER_FILE = REPO_ROOT / "src/nexus_ai_agent/creative/render_jobs.py"
RUNTIME_FILES = (SERVICE_FILE, UPSCALE_FILE, RENDER_FILE)
EXPECTED_PERMISSIONS = {"project:read", "project:write"}


def _tree(source: Path) -> ast.Module:
    return ast.parse(source.read_text(encoding="utf-8"), filename=str(source))


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


def _keyword(call: ast.Call, name: str) -> ast.keyword:
    return next((item for item in call.keywords if item.arg == name), None) or pytest.fail(
        f"{name}= is required at line {call.lineno}"
    )


def _name(node: ast.AST) -> str:
    assert isinstance(node, ast.Name), f"expected a named binding, got {ast.dump(node)}"
    return node.id


def _attribute(node: ast.AST, *, attr: str) -> None:
    if isinstance(node, ast.Name) and node.id == "project_id":
        return
    assert isinstance(node, ast.Attribute) and node.attr == attr, ast.dump(node)
    names = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
    attrs = {item.attr for item in ast.walk(node) if isinstance(item, ast.Attribute)}
    assert names & {"project", "project_id"} or "project" in attrs, ast.dump(node)


def _string_set(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        assert node.func.id == "frozenset" and len(node.args) == 1
        node = node.args[0]
    assert isinstance(node, ast.Set), f"permissions must be an explicit set, got {ast.dump(node)}"
    values: set[str] = set()
    for item in node.elts:
        assert isinstance(item, ast.Constant) and isinstance(item.value, str)
        values.add(item.value)
    return values


def _assert_project_access_call(node: ast.Call, *, actor_name: str) -> None:
    assert isinstance(node.func, ast.Name) and node.func.id == "ProjectAccess"
    fields = {item.arg: item.value for item in node.keywords}
    assert _name(fields["actor"]) == actor_name
    _attribute(fields["project_id"], attr="project_id")
    assert _string_set(fields["permissions"]) == EXPECTED_PERMISSIONS


def _project_access_factory(tree: ast.AST, name: str, *, actor_name: str) -> None:
    function = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        ),
        None,
    )
    assert function is not None, f"missing ProjectAccess factory {name}"
    returns = [node for node in ast.walk(function) if isinstance(node, ast.Return)]
    assert len(returns) == 1 and isinstance(returns[0].value, ast.Call)
    _assert_project_access_call(returns[0].value, actor_name=actor_name)


def _assignments(tree: ast.AST) -> dict[str, ast.AST]:
    result: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            result[node.targets[0].id] = node.value
    return result


def _assert_service_identity(tree: ast.AST, *, actor_name: str, actor_id: str) -> None:
    assignments = _assignments(tree)
    actor_value = assignments[actor_name]
    assert isinstance(actor_value, ast.Call) and isinstance(actor_value.func, ast.Name)
    assert actor_value.func.id == "ActorIdentity"
    fields = {item.arg: item.value for item in actor_value.keywords}
    assert isinstance(fields["kind"], ast.Constant) and fields["kind"].value == "service"
    assert isinstance(fields["actor_id"], ast.Constant) and fields["actor_id"].value == actor_id


def _assert_direct_bus(source: Path, *, actor_name: str, actor_id: str, access_name: str) -> None:
    tree = _tree(source)
    _assert_service_identity(tree, actor_name=actor_name, actor_id=actor_id)
    assignments = _assignments(tree)
    access_value = assignments.get(access_name)
    if access_name == "_service_access":
        _project_access_factory(tree, access_name, actor_name=actor_name)
    elif isinstance(access_value, ast.Call):
        _assert_project_access_call(access_value, actor_name=actor_name)
    else:
        pytest.fail(f"{access_name} must be a ProjectAccess construction")

    calls = _calls(tree, "CommandBus")
    assert calls, f"{source} must construct its canonical CommandBus"
    for call in calls:
        authorizer = _keyword(call, "authorizer").value
        assert not (isinstance(authorizer, ast.Constant) and authorizer.value is None)
        if access_name == "_service_access":
            assert isinstance(authorizer, ast.Call) and isinstance(authorizer.func, ast.Name)
            assert authorizer.func.id == access_name and len(authorizer.args) == 1
            _attribute(authorizer.args[0], attr="project_id")
        else:
            assert _name(authorizer) == access_name

    command_calls = _calls(tree, "_command")
    assert command_calls
    for call in command_calls:
        project_id = _keyword(call, "project_id").value
        _attribute(project_id, attr="project_id")
        operation = call.args[0]
        if isinstance(operation, ast.Name) and operation.id.endswith("SCAN"):
            continue
        refs = _keyword(call, "input_refs").value
        assert not (isinstance(refs, ast.Tuple) and not refs.elts), (
            f"post-scan command at line {call.lineno} must carry owned input_refs"
        )


def test_direct_slideshow_buses_require_non_null_scoped_service_grants() -> None:
    _assert_direct_bus(
        SERVICE_FILE,
        actor_name="_SERVICE_ACTOR",
        actor_id="slideshow-runtime",
        access_name="_service_access",
    )
    _assert_direct_bus(
        UPSCALE_FILE,
        actor_name="_SERVICE_ACTOR",
        actor_id="slideshow-upscale-runtime",
        access_name="access",
    )


def test_render_job_factory_and_command_use_the_same_scoped_service_grant() -> None:
    tree = _tree(RENDER_FILE)
    assignments = _assignments(tree)
    _assert_service_identity(tree, actor_name="service_actor", actor_id="creative-render-runtime")
    service_access = assignments["service_access"]
    assert isinstance(service_access, ast.Call)
    _assert_project_access_call(service_access, actor_name="service_actor")

    factory_calls = _calls(tree, "build_job_bus")
    assert factory_calls
    for call in factory_calls:
        authorizer = _keyword(call, "authorizer").value
        assert not (isinstance(authorizer, ast.Constant) and authorizer.value is None)
        assert _name(authorizer) == "service_access"

    typed_commands = _calls(tree, "TypedCommand")
    assert len(typed_commands) == 1
    command = typed_commands[0]
    assert _name(_keyword(command, "actor").value) == "service_actor"
    target = _keyword(command, "target").value
    assert isinstance(target, ast.Call) and isinstance(target.func, ast.Name)
    assert target.func.id == "TargetRef"
    _attribute(
        dict((item.arg, item.value) for item in target.keywords)["project_id"], attr="project_id"
    )
    refs = _keyword(command, "input_refs").value
    assert isinstance(refs, ast.Tuple) and refs.elts
    ref = refs.elts[0]
    assert (
        isinstance(ref, ast.Call) and isinstance(ref.func, ast.Name) and ref.func.id == "InputRef"
    )
    ref_fields = {item.arg: item.value for item in ref.keywords}
    _attribute(ref_fields["project_id"], attr="project_id")
    assert _name(ref_fields["ref_id"]) == "SOURCE_ASSET_ID"


def test_each_runtime_command_declares_the_correct_target_and_post_scan_refs() -> None:
    for source in (SERVICE_FILE, UPSCALE_FILE):
        tree = _tree(source)
        for call in _calls(tree, "_command"):
            target = _keyword(call, "project_id").value
            _attribute(target, attr="project_id")
            operation = call.args[0]
            if isinstance(operation, ast.Name) and operation.id.endswith("SCAN"):
                continue
            refs = _keyword(call, "input_refs").value
            assert not (isinstance(refs, ast.Tuple) and not refs.elts)


def _bus_with_access(project_id: str = "proj-runtime") -> tuple[CommandBus, ActorIdentity]:
    project = new_project(
        project_id, "runtime-test", Timeline(timeline_id="main", duration_us=1_000_000)
    )
    actor = ActorIdentity(kind="service", actor_id="runtime-test")
    access = ProjectAccess(
        actor=actor, project_id=project_id, permissions=frozenset(EXPECTED_PERMISSIONS)
    )
    # The core studio registry (the bus default) implements ``system.undo``,
    # so this host-boundary test needs no pack dependency.
    return CommandBus(project, authorizer=access), actor


def _command(
    project_id: str, actor: ActorIdentity, refs: tuple[InputRef, ...] = ()
) -> TypedCommand:
    return TypedCommand(
        command_id="cmd-runtime-test",
        operation="system.undo",
        input={},
        actor=actor,
        target=TargetRef(project_id=project_id, track_id="main"),
        input_refs=refs,
        idempotency_key="runtime-test",
    )


def test_real_bus_rejects_actor_and_project_scope_mismatches() -> None:
    bus, actor = _bus_with_access()
    with pytest.raises(AuthorizationError):
        bus.dispatch(_command("proj-runtime", ActorIdentity(kind="service", actor_id="mallory")))
    with pytest.raises(AuthorizationError):
        bus.dispatch(_command("other-project", actor))


def test_real_bus_rejects_foreign_project_input_refs_before_handler() -> None:
    bus, actor = _bus_with_access()
    foreign_ref = InputRef(ref_type="asset", project_id="other-project", ref_id="asset-1")
    with pytest.raises(InputReferenceError):
        bus.dispatch(_command("proj-runtime", actor, (foreign_ref,)))
