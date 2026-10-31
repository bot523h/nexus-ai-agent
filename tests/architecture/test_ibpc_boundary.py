"""Architecture guards for the IBPC slice (``nagar.ibpc``).

Mission NAGAR-IBPC-LIVE-EDITING-01 red lines, made executable against the tree:

* the slice never module-imports the queue implementation, the worker, the
  render-job adapter, the studio bus, the pack runtime or the job verifier —
  execution is reachable ONLY through the injected ``JobQueuePort``;
* the only subprocess in the whole package is the read-only footprint verifier
  (``footprint.py``); no other module may spawn a process;
* the queue-facing constants (job type / command / surface operation /
  workspace prefix) are closed literals pinned against the canonical worker's
  own map — no text- or model-derived names;
* the slice does not import the cognition or the older creative handoff
  (no second chain, no LLM in the actor/permission/queue/renderer/path);
* the provenance layer is only *referenced* (journal/passport), never
  re-implemented: no second ledger schema is defined here.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
IBPC = ROOT / "src" / "nexus_ai_agent" / "nagar" / "ibpc"

FORBIDDEN_MODULE_IMPORTS = (
    "nexus_ai_agent.adapters",
    "nexus_ai_agent.worker",
    "nexus_ai_agent.creative.render_jobs",
    "nexus_ai_agent.creative.studio.bus",
    "nexus_ai_agent.creative.studio.authorization",
    "nexus_ai_agent.creative.packs.runtime",
    "nexus_ai_agent.creative.rendering",
    "nexus_ai_agent.jobs",
    "nexus_ai_agent.nagar.cognition",
    "nexus_ai_agent.nagar.creative",
)

#: The one module allowed to spawn processes: the independent footprint
#: verifier, which only ever decodes read-only.
SUBPROCESS_ALLOWED_IN = {"footprint.py"}

FORBIDDEN_CALLS = {"system", "popen", "exec", "eval", "__import__"}


def _files() -> list[Path]:
    return sorted(IBPC.rglob("*.py"))


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append(node.module)
    return out


def _called_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                out.append(func.id)
            elif isinstance(func, ast.Attribute):
                out.append(func.attr)
    return out


def test_no_forbidden_imports_anywhere_in_ibpc() -> None:
    violations: list[str] = []
    for path in _files():
        for module in _imported_modules(path):
            for forbidden in FORBIDDEN_MODULE_IMPORTS:
                if module == forbidden or module.startswith(forbidden + "."):
                    violations.append(f"{path.name}: imports {module}")
    assert not violations, "IBPC must not import execution/authority surfaces:\n" + "\n".join(
        violations
    )


def test_subprocess_only_in_footprint() -> None:
    for path in _files():
        imports = _imported_modules(path)
        uses_subprocess = "subprocess" in imports
        calls = set(_called_names(path))
        spawns = uses_subprocess or bool(calls & FORBIDDEN_CALLS)
        if spawns and path.name not in SUBPROCESS_ALLOWED_IN:
            raise AssertionError(f"{path.name}: process spawn outside footprint.py")
        if path.name in SUBPROCESS_ALLOWED_IN:
            assert uses_subprocess, "footprint.py must be the subprocess owner"


def test_no_llm_or_model_provider_imports() -> None:
    forbidden_substrings = ("llm", "openai", "anthropic", "gemini", "groq", "model_provider")
    for path in _files():
        for module in _imported_modules(path):
            low = module.lower()
            assert not any(s in low for s in forbidden_substrings), (
                f"{path.name}: imports model provider {module}"
            )


def test_queue_constants_pinned_against_canonical_worker() -> None:
    """The literals the service enqueues with must equal the canonical worker's
    own constants (this test — not the slice — may import render_jobs)."""
    from nexus_ai_agent.creative.render_jobs import (
        CREATIVE_RENDER_JOB_TYPE,
        SURFACE_TO_CANONICAL,
        WORKSPACE_PREFIX,
    )
    from nexus_ai_agent.nagar.ibpc import service

    assert service.JOB_TYPE == CREATIVE_RENDER_JOB_TYPE
    assert service.COMMAND == "edit"
    assert service.SURFACE_OPERATION == "trim"
    assert service.CANONICAL_OPERATION == SURFACE_TO_CANONICAL[("edit", "trim")]
    assert service.WORKSPACE_PREFIX == WORKSPACE_PREFIX


def test_package_init_imports_every_module() -> None:
    """Law R20 reachability: each ibpc module is imported by name from the
    package ``__init__`` (or by tests)."""
    init = (IBPC / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(init)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "nexus_ai_agent.nagar.ibpc":
            imported.update(alias.name for alias in node.names)
    modules = {p.stem for p in _files() if p.name != "__init__.py"}
    assert modules <= imported, f"modules not imported by name: {modules - imported}"


def test_no_second_provenance_schema() -> None:
    """The capsule references the existing journal/passport; it must not define
    a competing ledger model (no CausalJournal/EventKind redefinition)."""
    for path in _files():
        text = path.read_text(encoding="utf-8")
        assert "class CausalJournal" not in text
        assert "class EventKind" not in text
        assert "class Passport" not in text
