from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
PRODUCT = ROOT / "src/nexus_ai_agent/product"
API_STUDIO = ROOT / "src/nexus_ai_agent/api/studio.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            values.add(node.module)
    return values


def test_product_read_surface_has_no_storage_or_execution_imports() -> None:
    imports = set().union(*(_imports(path) for path in PRODUCT.glob("*.py")))
    forbidden = {
        "nexus_ai_agent.storage",
        "nexus_ai_agent.creative.studio.bus",
        "nexus_ai_agent.creative.rendering.executor",
    }
    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )


def test_studio_api_exposes_only_get_route() -> None:
    source = API_STUDIO.read_text(encoding="utf-8")
    assert "@router.get(" in source
    assert "@router.post(" not in source
    assert "from nexus_ai_agent.product.studio_read import" in source
    assert "from nexus_ai_agent.api.dashboard import require_dashboard_token" in source
