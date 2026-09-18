"""DoD: raw LangGraph SQLite savers may only be touched by the composition
root (``storage/langgraph_checkpoint.py``) and ``adapters/langgraph``.

Everywhere else the runtime must see the lifecycle wrapper (or no saver at
all), which is what keeps the wrapper replaceable and the boundary auditable.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src"

RAW_SAVERS = {"SqliteSaver", "AsyncSqliteSaver"}
ALLOWED_PREFIXES = (
    "src/nexus_ai_agent/storage/langgraph_checkpoint.py",
    "src/nexus_ai_agent/adapters/langgraph/",
)


def _raw_saver_refs(path: Path) -> set[str]:
    """Collect names that are imported from a raw saver module or referenced."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("langgraph.checkpoint.sqlite") or module.endswith(
                ".checkpoint.sqlite"
            ):
                refs.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name) and node.id in RAW_SAVERS:
            refs.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in RAW_SAVERS:
            refs.add(node.attr)
    return refs & RAW_SAVERS


def test_raw_saver_only_in_composition_root_and_adapter() -> None:
    offenders: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        relative = str(path.relative_to(ROOT))
        if any(relative.startswith(prefix) for prefix in ALLOWED_PREFIXES):
            continue
        refs = _raw_saver_refs(path)
        if refs:
            offenders[relative] = refs
    assert not offenders, f"raw saver usage outside the allowed boundary: {offenders}"


def test_composition_root_is_the_only_runtime_wiring_point() -> None:
    """``LifecycleRecordingSaver`` may only be *applied* at the composition
    root.

    ``adapters/langgraph`` defines it; referencing it elsewhere in src is a
    wiring violation.  (Importing the plain contextvar from the same module
    is fine — e.g. the inspect command's ``admin`` context — and is not a
    wiring point.)
    """
    wiring: list[str] = []
    for path in SRC.rglob("*.py"):
        relative = str(path.relative_to(ROOT))
        if relative.startswith(ALLOWED_PREFIXES[1]):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("nexus_ai_agent.adapters")
            ):
                wiring.extend(
                    f"{relative}: {alias.name}"
                    for alias in node.names
                    if alias.name == "LifecycleRecordingSaver"
                )
            elif isinstance(node, ast.Name) and node.id == "LifecycleRecordingSaver":
                wiring.append(f"{relative}: <name>")
    allowed = [item for item in wiring if item.startswith(ALLOWED_PREFIXES[0])]
    assert wiring == allowed, f"unexpected wrapper wiring: {wiring}"
