"""Architecture guards for the durable cognition -> creative-queue handoff.

The handoff adds exactly one property — "a free-text intent survives as a
durable job on the canonical worker path" — and it must add **no** new authority
or execution surface.  These fitness tests make that executable against the
tree:

* the ``nagar.creative`` slice never module-imports the worker, the queue, the
  pack runtime or the verifier (so it can neither execute nor build a second
  registry/queue/verifier);
* the durable ``job_type`` handed to the queue is a closed constant, never a
  model- or text-derived name (no "model output becomes a handler name");
* the queue payload is the canonical ``CreativeRenderPayload`` — ``extra=forbid``
  and no authority field;
* the slice exposes no execution primitive (no subprocess/shell/ffmpeg) and
  takes its authority only through the injected gateway;
* the one canonical bus factory (``build_job_bus``) derives the experimental
  opt-in from server policy, never from a caller.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from nexus_ai_agent.creative.render_jobs import CreativeRenderPayload, build_job_bus
from nexus_ai_agent.nagar.creative import run_free_text_intent_durable

ROOT = Path(__file__).parents[2]
CREATIVE = ROOT / "src" / "nexus_ai_agent" / "nagar" / "creative"
HANDOFF = CREATIVE / "handoff.py"
CLI = ROOT / "src" / "nexus_ai_agent" / "cli.py"

#: Execution / authority surfaces the slice must never import at module scope.
FORBIDDEN_MODULE_IMPORTS = (
    "nexus_ai_agent.creative.render_jobs",
    "nexus_ai_agent.adapters.in_process_job_queue",
    "nexus_ai_agent.creative.packs.runtime",
    "nexus_ai_agent.jobs",
    "nexus_ai_agent.creative.studio.bus",
    "nexus_ai_agent.creative.studio.authorization",
)

#: Execution primitives that would create a second path.
FORBIDDEN_CALLS = {"dispatch", "system", "popen", "run", "exec", "eval"}

AUTHORITY_FIELDS = {
    "actor",
    "permissions",
    "permission",
    "authorization",
    "authorizer",
    "allow_experimental",
    "worker",
    "handler",
    "job_status",
    "retry_count",
}


def _files() -> list[Path]:
    return sorted(CREATIVE.rglob("*.py"))


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_imports(path: Path) -> list[str]:
    tree = _tree(path)
    function_nodes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    local_ids = {
        id(node) for fn in ast.walk(tree) if isinstance(fn, function_nodes) for node in ast.walk(fn)
    }
    out: list[str] = []
    for node in ast.walk(tree):
        if id(node) in local_ids:
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
        elif isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
    return out


def test_slice_never_module_imports_an_execution_or_authority_surface() -> None:
    for path in _files():
        for module in _module_imports(path):
            assert not module.startswith(FORBIDDEN_MODULE_IMPORTS), (
                f"{path.relative_to(ROOT)} module-imports an execution surface: {module!r}"
            )


def test_slice_exposes_no_execution_primitive() -> None:
    for path in _files():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_CALLS, (
                    f"{path.relative_to(ROOT)} calls execution primitive {node.func.id!r}"
                )
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"system", "popen"}, (
                    f"{path.relative_to(ROOT)} reaches an execution primitive {node.attr!r}"
                )


def test_job_type_is_a_closed_constant_never_derived_from_text() -> None:
    # The enqueue call must name the job type by constant, not by a model value.
    calls = [
        node
        for node in ast.walk(_tree(HANDOFF))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "enqueue"
    ]
    assert calls, "expected the slice to enqueue through the injected queue"
    for call in calls:
        job_type = next((kw for kw in call.keywords if kw.arg == "job_type"), None)
        assert job_type is not None, "enqueue must pass job_type explicitly"
        assert isinstance(job_type.value, ast.Name), "job_type must be a constant, not computed"
        assert job_type.value.id == "CREATIVE_RENDER_JOB_TYPE"


def test_queue_payload_is_the_canonical_forbidden_extra_contract() -> None:
    assert CreativeRenderPayload.model_config.get("extra") == "forbid"
    fields = set(CreativeRenderPayload.model_fields)
    assert not (fields & AUTHORITY_FIELDS), fields & AUTHORITY_FIELDS


def test_slice_takes_authority_only_through_the_injected_gateway() -> None:
    params = inspect.signature(run_free_text_intent_durable).parameters
    for required in ("gateway", "queue", "project", "source_path", "workspace_dir"):
        assert required in params, f"missing injected dependency {required!r}"
        assert params[required].kind is inspect.Parameter.KEYWORD_ONLY
    for forbidden in ("actor", "project_id", "authorizer", "permissions", "allow_experimental"):
        assert forbidden not in params, f"slice must not accept {forbidden!r} directly"


def test_bus_factory_derives_the_opt_in_from_policy_not_a_caller() -> None:
    params = inspect.signature(build_job_bus).parameters
    assert "allow_experimental" not in params, (
        "a caller must not be able to pass the experimental opt-in"
    )
    source = (ROOT / "src" / "nexus_ai_agent" / "creative" / "render_jobs.py").read_text(
        encoding="utf-8"
    )
    assert "EXPERIMENTAL_OPT_IN_OPERATIONS" in source


def test_cli_entry_point_composes_the_canonical_factory() -> None:
    # The operator entry point reaches the bus through the one canonical factory
    # and the existing queue — never a bespoke bus/queue construction.
    source = CLI.read_text(encoding="utf-8")
    assert "build_job_bus(" in source
    assert "InProcessJobQueue(" in source
    assert "run_free_text_intent_durable(" in source
    # It must not dispatch inline: the durable slice persists, the worker runs.
    assert "bus.dispatch(" not in source
