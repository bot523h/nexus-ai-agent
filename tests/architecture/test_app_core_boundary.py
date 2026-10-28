"""App-first frame gates (docs/architecture/PRODUCT_FRAME.md, F1).

* the identity/channel vocabulary is transport-free (no ``telegram`` import);
* a RATCHET: the set of modules outside ``bot/`` that import ``telegram`` may
  only shrink. Remove a path from the allowlist when you extract it; never add one.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src/nexus_ai_agent"

CORE_FILES = (SRC / "application/principal.py", SRC / "application/ports/channel.py")

# Files outside bot/ that import telegram today (measured, not aspirational).
TELEGRAM_IMPORTERS_OUTSIDE_BOT = frozenset(
    {
        "agent/approval.py",
        "agent/self_monitor.py",
        "features/channel_manager.py",
        "features/force_join.py",
        "features/onboarding.py",
    }
)


def _imports_telegram(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import) and any(
            a.name == "telegram" or a.name.startswith("telegram.") for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == "telegram" or node.module.startswith("telegram."):
                return True
    return False


def test_identity_and_channel_vocabulary_is_transport_free() -> None:
    for path in CORE_FILES:
        assert path.exists(), path
        assert not _imports_telegram(path), f"{path.name} must not import telegram"


def test_telegram_importers_outside_bot_only_shrink() -> None:
    found = {
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if not p.relative_to(SRC).as_posix().startswith("bot/") and _imports_telegram(p)
    }
    added = found - TELEGRAM_IMPORTERS_OUTSIDE_BOT
    assert not added, f"new telegram coupling outside bot/: {sorted(added)}"
    stale = TELEGRAM_IMPORTERS_OUTSIDE_BOT - found
    assert not stale, f"extracted — remove from the ratchet allowlist: {sorted(stale)}"
