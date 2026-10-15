"""The checkpoint schema golden is only valid for the exact dependency it was built from.

``langgraph-checkpoint-sqlite 3.1.2`` added ``writes.task_path``; the golden
fingerprint (generated against 3.1.1) then mismatched, cleanup was (correctly)
disabled, and six reconciler/lifecycle tests went red on ``main`` and every PR
without any code change.  Drift must stay a *failure, not a repair* (see
docs/DECISION_LOG.md), so the fix is determinism: the dependency that defines the
checkpoint schema has to be pinned exactly, as its postgres sibling already is.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from nexus_ai_agent.storage.checkpoint_adapter import FINGERPRINT_ALGORITHM

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def _runtime_dependencies() -> list[str]:
    block = re.search(r"^dependencies = \[(.*?)^\]", PYPROJECT, re.DOTALL | re.MULTILINE)
    assert block, "could not locate [project].dependencies"
    return re.findall(r'"([^"]+)"', block.group(1))


def test_every_checkpoint_schema_dependency_is_pinned_exactly() -> None:
    deps = {re.split(r"[=<>!~\[ ]", d, maxsplit=1)[0]: d for d in _runtime_dependencies()}
    for name in ("langgraph-checkpoint-sqlite", "langgraph-checkpoint-postgres"):
        assert name in deps, f"{name} missing from dependencies"
        assert re.fullmatch(rf"{name}==\d+(\.\d+)*", deps[name]), (
            f"{deps[name]!r}: the checkpoint schema golden is only valid for one exact "
            f"release, so this dependency must be pinned with '==' (not a range)"
        )


def test_installed_sqlite_checkpointer_matches_the_pin() -> None:
    from importlib.metadata import version

    deps = {re.split(r"[=<>!~\[ ]", d, maxsplit=1)[0]: d for d in _runtime_dependencies()}
    pinned = deps["langgraph-checkpoint-sqlite"].split("==", 1)[1]
    assert version("langgraph-checkpoint-sqlite") == pinned


def test_live_checkpoint_schema_matches_the_committed_golden() -> None:
    """Behavioural guard: the real installed schema must equal the golden fingerprint."""
    import json

    from langgraph.checkpoint.sqlite import SqliteSaver

    from nexus_ai_agent.storage.checkpoint_adapter import SQLiteCheckpointAdapter

    golden = ROOT / "src/nexus_ai_agent/storage/golden/sqlite.langgraph.json"
    assert json.loads(golden.read_text("utf-8"))["algorithm"] == FINGERPRINT_ALGORITHM
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cp.sqlite"
        with SqliteSaver.from_conn_string(str(path)) as saver:
            saver.setup()
        adapter = SQLiteCheckpointAdapter(str(path))
        try:
            adapter.assert_golden(golden)
        finally:
            adapter.close()
