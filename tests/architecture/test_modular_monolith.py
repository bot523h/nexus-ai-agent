"""Architecture gates for the frozen Modular Monolith boundary."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "nexus_ai_agent"


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _compose_services(path: Path) -> set[str]:
    """Read top-level service keys without adding a YAML dependency to tests."""
    services: set[str] = set()
    in_services = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        stripped = raw_line.strip()
        if indent == 0 and stripped == "services:":
            in_services = True
            continue
        if in_services and indent == 0:
            break
        if in_services and indent == 2 and stripped.endswith(":"):
            services.add(stripped[:-1])
    return services


def test_no_celery_or_redis_imports_in_production() -> None:
    """The production tree must not depend on a distributed queue."""
    violations: list[str] = []
    for path in SRC.rglob("*.py"):
        imported = _top_level_imports(path)
        if imported & {"celery", "redis"}:
            violations.append(f"{path.relative_to(ROOT)}: {sorted(imported & {'celery', 'redis'})}")
    assert not violations, "distributed queue imports remain:\n" + "\n".join(violations)


def test_no_distributed_queue_in_docker_compose() -> None:
    """Compose must describe one monolith and its dashboard only."""
    services = _compose_services(ROOT / "docker-compose.yml")
    assert services == {"bot", "dashboard"}


def test_no_distributed_queue_dependencies_or_dispatch() -> None:
    """No dependency or call site may silently reintroduce Celery/Redis."""
    project_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"celery' not in project_text
    assert '"redis' not in project_text

    dispatch_sites: list[str] = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if ".delay(" in text or "celery_app" in text:
            dispatch_sites.append(str(path.relative_to(ROOT)))
    assert not dispatch_sites, "distributed dispatch sites remain: " + ", ".join(dispatch_sites)
