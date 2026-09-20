"""R-001 / R-026 — the runtime is a modular monolith; no distributed queue.

``docs/architecture/PORTS.md`` fixes the Stage 0 contract: *"JobQueue starts
with an in-process implementation; Redis/Celery are not part of Stage 0."*
These fitness tests are the executable form of that ban.  They scan the real
tree (AST for Python, a dependency-free reader for ``docker-compose.yml`` and
``pyproject.toml``) so a re-introduction anywhere in ``src/`` turns CI red.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import tomllib

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src"

#: Top-level distributions/modules that would re-introduce a broker topology.
#: ``kombu``/``billiard`` are Celery internals; banning them closes the
#: "import the transport, not the framework" loophole.
BANNED_MODULES = frozenset({"celery", "redis", "kombu", "billiard"})

#: Celery's remote-invocation surface.  ``.delay()`` / ``.apply_async()`` /
#: ``.send_task()`` are how work used to leave the process.
BANNED_REMOTE_CALLS = frozenset({"delay", "apply_async", "send_task"})

#: Environment aliases that used to advertise a broker in ``Settings``.
BANNED_SETTINGS_FIELDS = frozenset({"redis_url", "celery_broker_url", "celery_result_backend"})

#: D2 — no scheduler either.  The nightly channel task never had a schedule
#: and was removed as dead code; channel management stays simulated until
#: R-031.  Adding a scheduler is a contract change, so it must edit this test.
BANNED_SCHEDULER_MODULES = frozenset({"apscheduler", "schedule", "croniter", "rocketry"})


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def _remote_calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in BANNED_REMOTE_CALLS
    }


def _compose_services(text: str) -> set[str]:
    """Return the top-level service names of a compose file.

    Deliberately dependency-free (PyYAML is not a declared dependency).  A
    service is a ``name:`` line at the first child indentation level found
    under the top-level ``services:`` key, whatever that indentation is.
    """
    services: set[str] = set()
    in_services = False
    child_indent: int | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^\S", line):
            in_services = line.startswith("services:")
            child_indent = None
            continue
        if not in_services:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if child_indent is None:
            child_indent = indent
        if indent == child_indent:
            match = re.match(r"^\s*['\"]?([A-Za-z0-9_.-]+)['\"]?:\s*$", line)
            if match:
                services.add(match.group(1))
    return services


def test_compose_service_parser_is_indentation_agnostic() -> None:
    """Guard the guard: a broker hidden under 4-space indentation is still seen."""
    sample = (
        "version: '3'\n"
        "services:\n"
        "    bot:\n"
        "        image: x\n"
        "    'rabbitmq':\n"
        "        image: rabbitmq\n"
        "volumes:\n"
        "    data: {}\n"
    )
    assert _compose_services(sample) == {"bot", "rabbitmq"}
    assert _compose_services("services:\n  bot:\n    build: .\n") == {"bot"}


def test_no_celery_imports_in_production() -> None:
    """No production module may import Celery/Redis (or their transports)."""
    offenders = {
        str(path.relative_to(ROOT)): sorted(imported & BANNED_MODULES)
        for path in sorted(SRC.rglob("*.py"))
        if (imported := _top_level_imports(path)) & BANNED_MODULES
    }
    assert not offenders, f"distributed-queue imports in production code: {offenders}"


def test_no_remote_task_invocation_in_production() -> None:
    """Background work must go through ``JobQueuePort.enqueue``; never ``.delay()``."""
    offenders = {
        str(path.relative_to(ROOT)): sorted(calls)
        for path in sorted(SRC.rglob("*.py"))
        if (calls := _remote_calls(path))
    }
    assert not offenders, f"Celery remote invocation in production code: {offenders}"


def test_no_celery_worker_module() -> None:
    """The Celery app module is gone; job functions live in ``jobs.py``."""
    assert not (SRC / "nexus_ai_agent" / "worker.py").exists()
    assert (SRC / "nexus_ai_agent" / "jobs.py").is_file()


def test_no_distributed_queue_dependencies_declared() -> None:
    """``pyproject.toml`` must not pull a broker client into the runtime."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared: list[str] = list(data["project"]["dependencies"])
    for extra in data["project"].get("optional-dependencies", {}).values():
        declared.extend(extra)
    names = {re.split(r"[\[<>=!~ ;]", dep, maxsplit=1)[0].strip().lower() for dep in declared}
    banned = sorted(names & BANNED_MODULES)
    assert not banned, f"broker dependencies declared: {banned}"

    overrides: list[str] = []
    for override in data.get("tool", {}).get("mypy", {}).get("overrides", []):
        overrides.extend(override.get("module", []))
    assert not set(overrides) & BANNED_MODULES, "dead mypy overrides for banned modules"


def test_no_distributed_queue_in_docker_compose() -> None:
    """The deployment topology is exactly ``bot`` + ``dashboard``."""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert _compose_services(text) == {"bot", "dashboard"}
    lowered = text.lower()
    assert "celery" not in lowered
    assert "redis" not in lowered


def test_settings_do_not_advertise_a_broker() -> None:
    """``Settings`` must not expose broker URLs for infrastructure that does not exist."""
    from nexus_ai_agent.config.settings import Settings

    exposed = set(Settings.model_fields) & BANNED_SETTINGS_FIELDS
    assert not exposed, f"broker settings still exposed: {sorted(exposed)}"


def test_no_scheduler_in_production() -> None:
    """D2: no periodic scheduler in the monolith; the dead nightly task is gone."""
    offenders = {
        str(path.relative_to(ROOT)): sorted(imported & BANNED_SCHEDULER_MODULES)
        for path in sorted(SRC.rglob("*.py"))
        if (imported := _top_level_imports(path)) & BANNED_SCHEDULER_MODULES
    }
    assert not offenders, f"scheduler imports in production code: {offenders}"
    dead = {"run_nightly_tasks", "nightly_channel_management"}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert not defined & dead, f"{path.relative_to(ROOT)} still defines {defined & dead}"
