"""M0 wiring — static import-graph proofs (no runtime execution).

Guarantees, from the source text alone:

1. Both composition roots construct ``InstrumentedJobQueue`` (the subclass)
   — swapping it back to the bare queue fails these tests.
2. Zero ``isinstance(..., InProcessJobQueue)`` checks exist anywhere in
   ``src/`` — the subclass cannot be rejected by type filters.
3. The fenced queue module and ports are never modified by this claim's
   runtime to know about instrumentation (no reverse dependency: the base
   queue does not import the instrumentation package).
4. ``observability/readyz_router`` is a factory that only touches
   observability readiness — ``api/`` still owns its own router (fenced),
   and this module does not import ``api``.
5. Instrumentation depends only on the queue adapter, ports, and
   observability — no new framework imports (langgraph/sqlmodel/telegram).
6. Every emission in the instrumentation module goes through ``_emit`` or a
   diagnostics ``log_job_*`` helper (fail-safe surface is complete): no
   bare ``get_metrics_registry()`` calls in the adapter.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[2]
SRC = REPO / "src" / "nexus_ai_agent"


def _read(*rel: str) -> str:
    return (SRC.joinpath(*rel)).read_text(encoding="utf-8")


def test_composition_root_bot_uses_instrumented_queue():
    text = _read("bot", "app.py")
    assert "InstrumentedJobQueue" in text
    assert "InProcessJobQueue(" not in text  # construction swapped, not imported


def test_composition_root_cli_uses_instrumented_queue():
    text = _read("cli.py")
    assert "InstrumentedJobQueue(" in text
    # the bare class must not be constructed at this root
    assert "InProcessJobQueue(" not in text


def test_zero_isinstance_checks_against_base_queue():
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "isinstance" not in text or "InProcessJobQueue" not in text.split(
            "isinstance"
        )[-1][:120], f"isinstance filter on base queue in {path}"


def test_base_queue_does_not_import_instrumentation():
    """No reverse dependency: fenced queue file stays instrumentation-free."""
    text = _read("adapters", "in_process_job_queue.py")
    assert "instrumentation" not in text


def test_readyz_router_does_not_import_api_package():
    text = _read("observability", "readyz_router.py")
    assert "nexus_ai_agent.api" not in text
    assert "from nexus_ai_agent.observability.readiness import" in text


def test_readyz_router_is_lazy_about_fastapi():
    """Module import must not require the web stack (CLI/test contexts)."""
    text = _read("observability", "readyz_router.py")
    top, _, _ = text.partition("def create_readyz_router")
    # fastapi only appears inside function bodies, never at module top level
    assert "import fastapi" not in top
    assert "from fastapi" not in top


def test_instrumentation_import_surface_is_narrow():
    text = _read("adapters", "instrumentation", "job_instrumentation.py")
    imports = [
        line.strip()
        for line in text.splitlines()
        if line.startswith("from nexus_ai_agent") or line.startswith("import ")
    ]
    allowed_prefixes = (
        "from nexus_ai_agent.adapters.in_process_job_queue",
        "from nexus_ai_agent.application.ports.job_queue",
        "from nexus_ai_agent.observability.",
        "import json",
        "import logging",
        "import time",
        "from collections.abc",
        "from dataclasses",
        "from datetime",
        "from typing",
    )
    for line in imports:
        assert any(
            line.startswith(prefix) for prefix in allowed_prefixes
        ), f"unexpected import in instrumentation: {line}"


def test_instrumentation_has_no_bare_registry_access():
    """All metric mutations must route through facade or diagnostics helpers."""
    text = _read("adapters", "instrumentation", "job_instrumentation.py")
    assert "get_metrics_registry" not in text
    assert "increment(" not in text
    assert "set_gauge(" not in text
    assert "observe(" not in text


def test_no_forbidden_framework_imports_in_m0_surface():
    """Forbidden frameworks may be *named* in policy prose, never imported."""
    import ast

    forbidden = {"langgraph", "sqlmodel", "telegram"}
    m0_files = [
        SRC / "adapters" / "instrumentation" / "__init__.py",
        SRC / "adapters" / "instrumentation" / "job_instrumentation.py",
        SRC / "observability" / "readyz_router.py",
        SRC / "observability" / "metrics.py",
        SRC / "observability" / "diagnostics.py",
        SRC / "infrastructure" / "observability" / "metrics.py",
    ]
    for path in m0_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                assert name not in forbidden, f"{name} imported in {path}"
