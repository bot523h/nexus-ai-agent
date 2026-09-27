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


def _constructions_in_source(source: str, filename: str = "<unknown>") -> dict[str, int]:
    """Count forbidden constructions in one module's source.

    W1 recovery: the original scanner matched the *textual* call name only,
    so ``from ... import GeminiEngine as GE; GE()`` bypassed the ratchet.
    This resolves the smallest robust alias set (stdlib ``ast`` only):

    * ``from M import N as A`` — ``A()`` resolves to ``N``;
    * ``from M import N`` — ``N()`` is already the canonical name;
    * ``import M [as A]`` + ``A.N()`` / ``M.N()`` — attribute calls resolve
      by ``attr`` regardless of the module alias, so no mapping is needed;
    * simple local aliases — ``Alias = GeminiEngine`` or
      ``Alias = module.GeminiEngine`` (including chains like ``B = A``)
      resolve to the forbidden type.

    Deliberately out of scope (documented, not silently missed):
    ``from M import *``, ``__import__``/``importlib``, ``getattr``/``eval``
    indirection, and conditional/attribute-target assignments.  Those require
    execution, not syntax, and no production site uses them.
    """
    tree = ast.parse(source, filename=filename)
    # 1. Import aliases: local name -> original imported name.
    alias_map: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.asname:
                    alias_map[a.asname] = a.name
        # `import X [as Y]` binds a module; class construction through it is
        # always an Attribute call (Y.Foo / X.Foo) resolved by attr below.

    # 2. Simple local aliases: `Alias = <forbidden or its alias>`.
    # Iterate to a fixpoint so chains (B = A = GeminiEngine) resolve.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            target = node.targets[0].id
            if target in alias_map and alias_map[target] in FORBIDDEN_TYPES:
                continue
            resolved: str | None = None
            value = node.value
            if isinstance(value, ast.Name):
                candidate = alias_map.get(value.id, value.id)
                if candidate in FORBIDDEN_TYPES:
                    resolved = candidate
            elif isinstance(value, ast.Attribute):
                if value.attr in FORBIDDEN_TYPES:
                    resolved = value.attr
            if resolved is not None and alias_map.get(target) != resolved:
                alias_map[target] = resolved
                changed = True

    # 3. Count calls through the alias map.
    counts: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name: str | None = None
        if isinstance(func, ast.Name):
            name = alias_map.get(func.id, func.id)
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name in FORBIDDEN_TYPES:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _collect_construction_sites() -> dict[str, dict[str, int]]:
    sites: dict[str, dict[str, int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        counts = _constructions_in_source(path.read_text(encoding="utf-8"), filename=rel)
        if counts:
            sites[rel] = counts
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


# ── Adversarial alias probes (W1 recovery) ─────────────────────────────
# Each proves the ratchet sees through one textual disguise.  They run the
# same helper the tree-wide guard uses, so a scanner regression fails here
# first with a pinpointed case instead of a whole-tree diff.


def test_ratchet_sees_direct_construction() -> None:
    src = "from nexus_ai_agent.features.ai_chat import GeminiEngine\nGeminiEngine(api_key='k')\n"
    assert _constructions_in_source(src) == {"GeminiEngine": 1}


def test_ratchet_sees_from_import_alias() -> None:
    src = "from nexus_ai_agent.features.ai_chat import GeminiEngine as GE\nGE(api_key='k')\n"
    assert _constructions_in_source(src) == {"GeminiEngine": 1}


def test_ratchet_sees_module_alias_qualified_access() -> None:
    src = "import nexus_ai_agent.features.ai_chat as chat\nchat.GeminiEngine(api_key='k')\n"
    assert _constructions_in_source(src) == {"GeminiEngine": 1}


def test_ratchet_sees_plain_module_qualified_access() -> None:
    src = (
        "import nexus_ai_agent.features.ai_chat\n"
        "nexus_ai_agent.features.ai_chat.GeminiEngine(api_key='k')\n"
    )
    assert _constructions_in_source(src) == {"GeminiEngine": 1}


def test_ratchet_sees_reexported_alias() -> None:
    src = (
        "from nexus_ai_agent.application.runtime import GeminiEngine as RTEngine\n"
        "RTEngine(api_key='k')\n"
    )
    assert _constructions_in_source(src) == {"GeminiEngine": 1}


def test_ratchet_sees_local_alias_and_chains() -> None:
    src = (
        "from nexus_ai_agent.features.ai_chat import GeminiEngine\n"
        "GE = GeminiEngine\n"
        "GE2 = GE\n"
        "GE(api_key='a')\n"
        "GE2(api_key='b')\n"
    )
    assert _constructions_in_source(src) == {"GeminiEngine": 2}


def test_ratchet_sees_local_alias_from_qualified_access() -> None:
    src = (
        "import nexus_ai_agent.features.ai_chat as chat\nGE = chat.GeminiEngine\nGE(api_key='k')\n"
    )
    assert _constructions_in_source(src) == {"GeminiEngine": 1}


def test_ratchet_ignores_unrelated_calls() -> None:
    src = "from somewhere import SomethingElse\nSomethingElse()\nhelper(GeminiEngine)\n"
    # A bare reference (not a call of the type) and an unrelated call count 0.
    # `helper(GeminiEngine)` passes the class as an argument — the Call func
    # is `helper`, not the forbidden type.
    assert _constructions_in_source(src) == {}


def test_ratchet_counts_each_forbidden_type_through_aliases() -> None:
    src = (
        "from a import GeminiProvider as P\n"
        "from b import ConversationStore as S\n"
        "from c import GeminiRequestQueue as Q\n"
        "from d import SummarizerEngine as Z\n"
        "P()\nS()\nQ()\nZ()\n"
    )
    assert _constructions_in_source(src) == {
        "GeminiProvider": 1,
        "ConversationStore": 1,
        "GeminiRequestQueue": 1,
        "SummarizerEngine": 1,
    }
