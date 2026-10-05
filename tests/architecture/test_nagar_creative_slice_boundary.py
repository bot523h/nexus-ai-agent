"""Architecture guards for the free-text slice and the cognition authority model.

The slice is trustworthy only if it stays *thin* and keeps the one execution
boundary.  These fitness tests make that executable:

* ``nagar.creative`` module-level imports stay light — a provider (``llm``) or
  settings (``config``) import may exist **only inside the composition-root
  function** ``build_provider``, never at import time, never anywhere else;
* the slice never owns a bus: it does not module-import ``studio.bus`` or
  ``studio.authorization`` (so it cannot construct a second execution path);
* runtime reflection pins that authority cannot be supplied by a caller/proposal
  and cannot be carried by a context or proposal.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from nexus_ai_agent.nagar.cognition.context import CognitionContext, ProposalSchema
from nexus_ai_agent.nagar.cognition.gateway import CognitionGateway
from nexus_ai_agent.nagar.cognition.proposal import TypedProposal
from nexus_ai_agent.nagar.creative import run_free_text_intent

ROOT = Path(__file__).parents[2]
CREATIVE = ROOT / "src" / "nexus_ai_agent" / "nagar" / "creative"

#: Top-level modules the slice may import at import time.
ALLOWED_TOP_LEVEL = {"__future__", "typing", "pydantic", "nexus_ai_agent"}

#: Packages a composition root may reach, but only lazily inside a function.
LAZY_ONLY_PREFIXES = ("nexus_ai_agent.llm", "nexus_ai_agent.config")

#: Execution surfaces the slice must never import at module scope.
FORBIDDEN_MODULE_IMPORTS = (
    "nexus_ai_agent.creative.studio.bus",
    "nexus_ai_agent.creative.studio.authorization",
)

#: Context/proposal fields that would turn data into authority.
AUTHORITY_FIELDS = {
    "actor",
    "permissions",
    "permission",
    "grants",
    "grant",
    "authorization",
    "authorizer",
    "execution_policy",
    "capability_snapshot",
    "confirmed",
    "privilege",
    "privileges",
}


def _files() -> list[Path]:
    return sorted(CREATIVE.rglob("*.py"))


def _imports(path: Path) -> list[tuple[bool, str]]:
    """Return ``(is_function_local, module_name)`` for every import node."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function_nodes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    local_ids = {
        id(node) for fn in ast.walk(tree) if isinstance(fn, function_nodes) for node in ast.walk(fn)
    }
    out: list[tuple[bool, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.append((id(node) in local_ids, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.append((id(node) in local_ids, alias.name))
    return out


def test_slice_package_exists() -> None:
    assert list(CREATIVE.glob("*.py")), "expected the nagar.creative slice package to exist"


def test_slice_module_level_imports_stay_light() -> None:
    for path in _files():
        for is_local, module in _imports(path):
            if is_local:
                continue
            top = module.split(".")[0]
            assert top in ALLOWED_TOP_LEVEL, (
                f"{path.relative_to(ROOT)} module-imports outside the allowlist: {module!r}"
            )


def test_provider_and_settings_are_lazy_and_confined_to_the_composition_root() -> None:
    # The provider/config imports must exist (the composition root is real) but
    # only inside a function — never at import time, and only in the slice's
    # modules.
    for path in _files():
        for is_local, module in _imports(path):
            if module.startswith(LAZY_ONLY_PREFIXES):
                assert is_local, (
                    f"{path.relative_to(ROOT)} imports {module!r} at module scope; "
                    "provider/settings must be lazy so the boundary stays light"
                )
    # The composition root lives in ``nagar.composition`` (host-owned) and must
    # actually construct the provider there, lazily.
    composition = ROOT / "src" / "nexus_ai_agent" / "nagar" / "composition.py"
    tree = ast.parse(composition.read_text(encoding="utf-8"))
    build = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_cognition_provider"
    )
    local_imports = {
        node.module for node in ast.walk(build) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert any(m and m.startswith("nexus_ai_agent.llm") for m in local_imports), (
        "build_cognition_provider must construct the provider (lazy import of nexus_ai_agent.llm)"
    )
    # And the slice itself must NOT build a provider: no llm import in it at all.
    for path in _files():
        for _is_local, module in _imports(path):
            assert not module.startswith("nexus_ai_agent.llm"), (
                f"{path.relative_to(ROOT)} must not build a model; use nagar.composition"
            )


def test_slice_never_module_imports_a_bus_or_authorizer() -> None:
    for path in _files():
        for is_local, module in _imports(path):
            if is_local:
                continue
            assert not module.startswith(FORBIDDEN_MODULE_IMPORTS), (
                f"{path.relative_to(ROOT)} imports an execution surface at module scope: {module!r}"
            )


def test_gateway_constructor_has_no_authority_inputs() -> None:
    params = set(inspect.signature(CognitionGateway.__init__).parameters)
    assert not (params & {"allowed_operations", "permissions", "grants", "authorizer"}), (
        f"the gateway must not accept authority from its caller; extra params: {params}"
    )
    # The bus and producer are injected — the only execution surface is passed in.
    assert {"bus", "producer", "actor", "project_id"} <= params


def test_context_and_proposal_cannot_carry_authority() -> None:
    context_fields = set(CognitionContext.model_fields) | set(ProposalSchema.model_fields)
    proposal_fields = set(TypedProposal.model_fields)
    assert not (context_fields & AUTHORITY_FIELDS), context_fields & AUTHORITY_FIELDS
    assert not (proposal_fields & AUTHORITY_FIELDS), proposal_fields & AUTHORITY_FIELDS


def test_both_models_forbid_extra_fields() -> None:
    for model in (CognitionContext, ProposalSchema, TypedProposal):
        assert model.model_config.get("extra") == "forbid", model.__name__


def test_run_free_text_intent_gets_actor_and_project_from_the_gateway() -> None:
    # The slice receives its authority *through the injected gateway* (which the
    # host composition root built with an explicit actor/project), and takes the
    # authoritative project state as an injected, keyword-only argument.  A
    # caller/model cannot pass an actor or project into the slice directly.
    params = inspect.signature(run_free_text_intent).parameters
    assert "gateway" in params
    assert params["gateway"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["gateway"].default is inspect.Parameter.empty
    assert "project" in params
    assert params["project"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["project"].default is inspect.Parameter.empty
    for forbidden in ("actor", "project_id", "authorizer", "permissions"):
        assert forbidden not in params, f"slice must not accept {forbidden!r} directly"
    # And the gateway itself exposes no caller-supplied authority on ``run``.
    run_params = inspect.signature(CognitionGateway.run).parameters
    assert not (set(run_params) & {"actor", "permissions", "grants", "authorizer"})
