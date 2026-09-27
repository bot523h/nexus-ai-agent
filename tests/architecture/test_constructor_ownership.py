"""Constructor-ownership ratchet (W1).

The mission forbids direct construction of the shared runtime components —
``GeminiEngine``, ``GeminiProvider``, ``ConversationStore``,
``GeminiRequestQueue``, ``SummarizerEngine`` — outside the approved
factories, *and requires it to be detectable*.  This test walks every
``src/nexus_ai_agent/**/*.py`` file with the stdlib ``ast`` module and
pins construction sites to an exact, shrink-only allow-list:

* ``application/runtime.py`` — the W1 approved factory (build_runtime);
* legacy seams kept for backward compatibility, each emitting an
  observability warning at runtime (they may only SHRINK in future waves);
* ``knowledge/knowledge_manager.py`` — file owned by another active zone
  claim; the ratchet pins its single existing site so it cannot grow.

Any new construction site ANYWHERE else fails this suite immediately.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "nexus_ai_agent"

FORBIDDEN_TYPES = (
    "GeminiEngine",
    "GeminiProvider",
    "ConversationStore",
    "GeminiRequestQueue",
    "SummarizerEngine",
)

# Approved construction counts per module, relative to src/nexus_ai_agent/.
# Green-tree expectation AFTER W1 lands; baseline (pre-W1) had extra sites
# in bot/app.py — which is exactly what this ratchet now forbids.
ALLOWED_SITES: dict[str, dict[str, int]] = {
    "application/runtime.py": {
        "GeminiEngine": 1,
        "GeminiProvider": 1,
        "ConversationStore": 1,
        "GeminiRequestQueue": 1,
        "SummarizerEngine": 1,
    },
    # Legacy fallback seams (may only shrink; each warns at runtime):
    "llm/gemini_provider.py": {"GeminiEngine": 1},
    "bot/handlers.py": {"GeminiEngine": 1, "SummarizerEngine": 1},
    "agents/store/base_agent.py": {"GeminiProvider": 1},
    "features/ai_memory.py": {"GeminiProvider": 1},
    # knowledge/ zone is claimed by another agent; pin, don't own.
    "knowledge/knowledge_manager.py": {"GeminiProvider": 1},
}


def _collect_construction_sites() -> dict[str, dict[str, int]]:
    sites: dict[str, dict[str, int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in FORBIDDEN_TYPES:
                sites.setdefault(rel, {})[name] = sites.setdefault(rel, {}).get(name, 0) + 1
    return sites


def test_no_forbidden_construction_outside_approved_seams() -> None:
    actual = _collect_construction_sites()
    assert actual == ALLOWED_SITES, (
        "constructor ownership drift detected\n"
        f"  expected exactly: {ALLOWED_SITES}\n"
        f"  found: {actual}\n"
        "New stateful LLM/DB/store/queue constructions must land in the "
        "approved runtime factory; legacy seams are shrink-only."
    )
