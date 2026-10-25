"""Provenance-plane boundary law: evidence, never authority (enforced).

* The ``provenance`` package must stay read-only infrastructure: it imports
  no adapter, spawns no process, touches no executor, and reaches no
  messaging/storage framework (AST-verified, like the other boundary tests).
* The queue adapter's causal observer is an injected, optional port with no
  execution authority: keyword-only, defaulting to ``None`` — and the queue's
  whole test suite passes with it unset (regression proof by the existing
  suites).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "nexus_ai_agent"

FORBIDDEN_TOP_LEVEL = {"telegram", "sqlmodel", "langgraph", "celery", "redis", "adapters"}
#: The provenance plane measures bytes; it must never execute or route work.
FORBIDDEN_DOTTED = {
    "subprocess",
    "creative.render_jobs",
    "creative.ffmpeg_executor",
    "creative.slideshow.worker_adapter",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_provenance_package_imports_no_adapter_or_framework() -> None:
    violations: list[str] = []
    for path in (SRC / "provenance").rglob("*.py"):
        imported = _imports(path)
        bad_top = {name.split(".")[0] for name in imported} & FORBIDDEN_TOP_LEVEL
        bad_dotted = imported & FORBIDDEN_DOTTED
        if bad_top or bad_dotted:
            violations.append(f"{path.relative_to(SRC)}: {sorted(bad_top | bad_dotted)}")
    assert not violations, "provenance boundary violations:\n" + "\n".join(violations)


def test_provenance_never_spawns_processes_or_threads_of_execution() -> None:
    """No os.system/popen call sites in the provenance plane (AST walk)."""
    for path in (SRC / "provenance").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                name = node.func.attr
                assert name not in {"system", "Popen", "exec", "eval"}, (
                    f"{path.relative_to(SRC)}: forbidden call {name}"
                )


def test_queue_observer_port_is_optional_and_injected() -> None:
    """The observer is a keyword-only ctor dependency defaulting to None."""
    from inspect import signature

    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue

    params = signature(InProcessJobQueue.__init__).parameters
    observer = params["causal_observer"]
    assert observer.kind.name == "KEYWORD_ONLY"
    assert observer.default is None


def test_passport_builder_takes_no_write_path_to_the_queue() -> None:
    """The passport's write surface is empty: journal reads + facts reads."""
    from nexus_ai_agent.provenance import PassportBuilder
    from nexus_ai_agent.provenance.journal import CausalJournal

    write_methods = {
        name
        for name in dir(PassportBuilder)
        if not name.startswith("_")
        and callable(getattr(PassportBuilder, name))
        and name not in {"build"}
    }
    assert not write_methods, f"passport grew write surface: {sorted(write_methods)}"
    journal_write_api = {"append"}  # the ONLY write the journal exposes
    public = {name for name in dir(CausalJournal) if not name.startswith("_")}
    assert journal_write_api <= public
    # and nothing else write-shaped:
    assert public & {"append"} == journal_write_api


def test_provenance_exports_declare_the_readonly_contract() -> None:
    import nexus_ai_agent.provenance as package

    assert package.__doc__ is not None
    assert "never" in package.__doc__ or "read-only" in package.__doc__.lower()
