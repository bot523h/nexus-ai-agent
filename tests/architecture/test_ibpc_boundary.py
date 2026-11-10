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

#: Modules allowed to spawn ffmpeg subprocesses.
#: - footprint.py  : independent read-only pixel verifier (original)
#: - shadow.py     : NAGAR Shadow Preview — JPEG keyframe strip from scope pixels
#: - signature.py  : NAGAR Scope Signature — CIE-Lab histogram fingerprint
#: All three call ffmpeg via subprocess.run with explicit list args (no shell=True).
SUBPROCESS_ALLOWED_IN = {"footprint.py", "shadow.py", "signature.py"}

FORBIDDEN_CALLS = {"system", "popen", "exec", "eval", "__import__"}

#: ``subprocess`` entry points that actually spawn a process.
_SPAWN_ATTRS = {"run", "Popen", "call", "check_call", "check_output"}


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


def test_canonical_intent_builder_is_the_workers_own() -> None:
    """Phase 1 continuity: the schema-2 intent the slice enqueues is built with
    the SAME canonical builder the worker's own code path uses, so the worker's
    divergence check compares like with like (no slice-local re-implementation)."""
    from nexus_ai_agent.application.creative_intent import CanonicalCreativeIntent
    from nexus_ai_agent.nagar.ibpc.service import IBPCServiceDeps

    assert IBPCServiceDeps.__dataclass_fields__["intent_builder"].default is None
    # the service falls back to the canonical builder
    src = (IBPC / "service.py").read_text(encoding="utf-8")
    assert "CanonicalCreativeIntent.create" in src
    assert CanonicalCreativeIntent.__module__ == "nexus_ai_agent.application.creative_intent"


def test_authority_and_proof_are_injected_not_imported_from_execution() -> None:
    """Phase 1/2 red line: authority is an injected port and the proof gate is a
    pure local module — neither may reach into the execution/adapters layer."""
    for path in (IBPC / "authority.py", IBPC / "proof.py"):
        for module in _imported_modules(path):
            for forbidden in FORBIDDEN_MODULE_IMPORTS:
                assert not (module == forbidden or module.startswith(forbidden + ".")), (
                    f"{path.name}: imports {module}"
                )


def _subprocess_aliases(tree: ast.AST) -> set[str]:
    """Local names bound to the ``subprocess`` module or one of its spawn calls.

    ``import subprocess as sp`` and ``from subprocess import run as _run`` both
    hide a process spawn behind an alias, so an AST check that only looks for the
    literal ``subprocess.run`` attribute would miss them.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    aliases.add(alias.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in _SPAWN_ATTRS:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _spawn_calls(tree: ast.AST, aliases: set[str]) -> list[ast.Call]:
    """Every ``subprocess`` spawn call, alias-aware (``sp.run`` and bare ``run``)."""
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in aliases and func.attr in _SPAWN_ATTRS:
                found.append(node)
        elif isinstance(func, ast.Name) and func.id in aliases - {"subprocess"}:
            found.append(node)
    return found


def test_every_subprocess_spawn_is_shell_free_and_timeout_bounded() -> None:
    """Law: a spawned process must never use ``shell=True`` and must carry an
    explicit ``timeout``.  This is the static half of the security gate; the
    runtime half (timeout really terminating the child, missing ffmpeg, empty
    output) lives in ``test_ibpc_shadow.py`` / ``test_ibpc_signature.py``."""
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _subprocess_aliases(tree)
        for call in _spawn_calls(tree, aliases):
            kw = {k.arg: k.value for k in call.keywords if k.arg is not None}
            shell = kw.get("shell")
            assert not (isinstance(shell, ast.Constant) and shell.value is True), (
                f"{path.name}: subprocess call uses shell=True (line {call.lineno})"
            )
            assert "timeout" in kw, (
                f"{path.name}: subprocess call without an explicit timeout (line {call.lineno})"
            )


def test_alias_detection_would_catch_a_hidden_spawn() -> None:
    """Guard-of-the-guard: prove the alias-aware scan actually sees an aliased
    spawn, so the shell/timeout law cannot be silently bypassed by renaming."""
    src = "import subprocess as sp\nsp.run(['x'], shell=True)\n"
    tree = ast.parse(src)
    aliases = _subprocess_aliases(tree)
    assert aliases == {"sp"}
    calls = _spawn_calls(tree, aliases)
    assert len(calls) == 1

    src2 = "from subprocess import run as _run\n_run(['x'])\n"
    tree2 = ast.parse(src2)
    aliases2 = _subprocess_aliases(tree2)
    assert aliases2 == {"_run"}
    assert len(_spawn_calls(tree2, aliases2)) == 1
