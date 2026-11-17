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

import pytest

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


def test_subprocess_import_form_is_the_plain_module_only() -> None:
    """``from subprocess import run as _r`` or ``import subprocess as sp`` would
    hide a spawn behind an alias; the boundary is only provable if every spawn is
    spelled ``subprocess.<attr>``."""
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                raise AssertionError(
                    f"{path.name}: 'from subprocess import ...' hides a spawn (line {node.lineno})"
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "subprocess" and alias.asname not in (None, "subprocess"):
                        raise AssertionError(
                            f"{path.name}: aliased subprocess import (line {node.lineno})"
                        )


def test_spawn_sites_are_exactly_the_allowlist_and_use_only_run() -> None:
    """The set of files that spawn a process equals ``SUBPROCESS_ALLOWED_IN``
    exactly (both directions), and each of them uses ``subprocess.run`` only —
    never Popen, call, check_call or check_output."""
    sites: dict[str, set[str]] = {}
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        attrs = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
        }
        if attrs:
            sites[path.name] = attrs
    assert set(sites) == SUBPROCESS_ALLOWED_IN, (
        f"spawn sites {sorted(sites)} != allowlist {sorted(SUBPROCESS_ALLOWED_IN)}"
    )
    for name, attrs in sites.items():
        assert attrs == {"run"}, f"{name}: uses subprocess.{sorted(attrs)}; only run is permitted"


def _argv_is_literal_list(tree: ast.AST, arg: ast.expr) -> bool:
    """True when the argv expression is a list literal, or a name every assignment
    of which in this module is a list literal."""
    if isinstance(arg, ast.List):
        return True
    if isinstance(arg, ast.Name):
        values = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == arg.id for t in node.targets)
        ]
        return bool(values) and all(isinstance(v, ast.List) for v in values)
    return False


def _innermost_function(tree: ast.AST, target: ast.AST) -> ast.FunctionDef | None:
    best: ast.FunctionDef | None = None
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and any(n is target for n in ast.walk(fn)):
            if best is None or fn.lineno >= best.lineno:
                best = fn
    return best


def _assert_typed_wrapper_passes_literals(tree: ast.AST, argv: ast.expr, where: str) -> None:
    """A spawn whose argv is a function parameter is provable only when that
    parameter is annotated ``list[...]`` and every call site of the wrapper in
    this module passes a list literal. Anything else fails closed."""
    assert isinstance(argv, ast.Name), f"{where}: argv is neither a list literal nor a parameter"
    fn = _innermost_function(tree, argv)
    assert fn is not None, f"{where}: argv name {argv.id!r} is not a function parameter"
    param = next((a for a in fn.args.args if a.arg == argv.id), None)
    assert (
        param is not None
        and param.annotation is not None
        and (ast.unparse(param.annotation).startswith("list"))
    ), f"{where}: argv parameter {argv.id!r} is not annotated list[...]"
    callers = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == fn.name
    ]
    assert callers, f"{where}: wrapper {fn.name} has no call site in this module to prove its argv"
    for call in callers:
        assert call.args and isinstance(call.args[0], ast.List), (
            f"{where}: call to {fn.name} at line {call.lineno} does not pass a list literal"
        )


def _spawn_violations(source: str, name: str) -> list[str]:
    """Every rule the subprocess boundary enforces, applied to one module's source.

    Pure function on purpose: the architecture test runs it on the real tree, and
    the mutation tests run it on deliberately broken sources to prove each rule
    actually fires (a checker that cannot fail proves nothing).
    """
    tree = ast.parse(source, filename=name)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            problems.append(f"{name}:{node.lineno} 'from subprocess import' hides a spawn")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess" and alias.asname not in (None, "subprocess"):
                    problems.append(f"{name}:{node.lineno} aliased subprocess import")
    for call in ast.walk(tree):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "subprocess"
        ):
            continue
        where = f"{name}:{call.lineno}"
        if call.func.attr != "run":
            problems.append(f"{where} subprocess.{call.func.attr} is not permitted; only run")
        if not call.args:
            problems.append(f"{where} spawn without argv")
        elif not _argv_is_literal_list(tree, call.args[0]):
            try:
                _assert_typed_wrapper_passes_literals(tree, call.args[0], where)
            except AssertionError as exc:
                problems.append(str(exc))
        if any(k.arg is None for k in call.keywords):
            problems.append(f"{where} **kwargs splat can smuggle shell=True")
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        shell = kw.get("shell")
        if shell is not None and not (isinstance(shell, ast.Constant) and shell.value is False):
            problems.append(f"{where} shell must be absent or False")
        timeout = kw.get("timeout")
        if timeout is None or (isinstance(timeout, ast.Constant) and timeout.value is None):
            problems.append(f"{where} spawn without a bounded timeout")
    return problems


def test_every_spawn_is_list_argv_shell_free_and_timeout_bounded() -> None:
    """Static half of the process security gate, applied to the real tree. The
    runtime half (the timeout really kills the child, missing binary, empty
    output) lives in ``test_ibpc_shadow.py`` / ``test_ibpc_signature.py``."""
    problems: list[str] = []
    for path in _files():
        problems += _spawn_violations(path.read_text(encoding="utf-8"), path.name)
    assert not problems, "\n".join(problems)


def test_checker_sees_exactly_the_three_real_spawn_sites() -> None:
    """Non-vacuity: the rules above must actually reach the spawns in the tree."""
    count = 0
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        count += sum(
            1
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "subprocess"
        )
    assert count == 3, f"expected footprint, shadow and signature spawns (3), found {count}"


_CLEAN_SPAWN = (
    "import subprocess\n"
    "def run_it(p: str):\n"
    "    return subprocess.run(['ffmpeg', p], capture_output=True, timeout=5)\n"
)
_MUTANTS = {
    "string argv with shell=True": (
        "import subprocess\nsubprocess.run('ffmpeg x', shell=True, timeout=5)\n"
    ),
    "missing timeout": "import subprocess\nsubprocess.run(['ffmpeg'], capture_output=True)\n",
    "timeout=None": "import subprocess\nsubprocess.run(['ffmpeg'], timeout=None)\n",
    "Popen": "import subprocess\nsubprocess.Popen(['ffmpeg'], timeout=5)\n",
    "from-import alias": "from subprocess import run\nrun(['ffmpeg'], timeout=5)\n",
    "module alias": "import subprocess as sp\nsp.run(['ffmpeg'], timeout=5)\n",
    "kwargs splat": "import subprocess\nopts = {}\nsubprocess.run(['ffmpeg'], timeout=5, **opts)\n",
    "untyped argv parameter": (
        "import subprocess\ndef f(x):\n    return subprocess.run(x, timeout=5)\n"
    ),
}


def test_checker_accepts_a_clean_spawn() -> None:
    assert _spawn_violations(_CLEAN_SPAWN, "clean.py") == []


@pytest.mark.parametrize("mutant", sorted(_MUTANTS))
def test_checker_catches_each_mutant(mutant: str) -> None:
    assert _spawn_violations(_MUTANTS[mutant], "mutant.py"), f"rule missed mutant: {mutant}"


def test_no_process_spawn_through_os_or_asyncio_anywhere_in_ibpc() -> None:
    """Spawn surfaces other than ``subprocess`` are banned across the slice."""
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            base = node.func.value
            if not isinstance(base, ast.Name):
                continue
            attr = node.func.attr
            if base.id == "os" and (
                attr in {"system", "popen"} or attr.startswith(("exec", "spawn", "posix_spawn"))
            ):
                raise AssertionError(
                    f"{path.name}: os.{attr} spawns a process (line {node.lineno})"
                )
            if base.id == "asyncio" and attr.startswith("create_subprocess"):
                raise AssertionError(f"{path.name}: asyncio.{attr} (line {node.lineno})")


#: The triad's own modules hold no authority beyond ffmpeg frame capture: no
#: network, no filesystem write, no dynamic code.
TRIAD_MODULES = ("delta.py", "shadow.py", "signature.py")
_NETWORK_MODULES = {
    "socket",
    "ssl",
    "urllib",
    "http",
    "requests",
    "httpx",
    "aiohttp",
    "ftplib",
    "smtplib",
}
_FS_WRITE_ATTRS = {
    "write_bytes",
    "write_text",
    "unlink",
    "rmdir",
    "rmtree",
    "mkdir",
    "touch",
    "rename",
}


def test_triad_modules_have_no_network_filesystem_write_or_dynamic_code() -> None:
    for name in TRIAD_MODULES:
        path = IBPC / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(path):
            top = module.split(".")[0]
            assert top not in _NETWORK_MODULES, f"{name}: network import {module}"
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                assert node.func.attr not in _FS_WRITE_ATTRS, (
                    f"{name}: filesystem write .{node.func.attr}() (line {node.lineno})"
                )
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)] + [
                    k.value.value
                    for k in node.keywords
                    if k.arg == "mode" and isinstance(k.value, ast.Constant)
                ]
                assert not any(isinstance(m, str) and any(c in m for c in "wax+") for m in modes), (
                    f"{name}: open() for writing (line {node.lineno})"
                )
            assert not (isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS), (
                f"{name}: dynamic code call {node.func.id} (line {node.lineno})"
            )


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
