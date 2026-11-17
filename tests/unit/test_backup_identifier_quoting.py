"""Regression: the SQLite inventory must survive hostile table names."""

from __future__ import annotations

import sqlite3

from nexus_ai_agent.maintenance.backup import _sqlite_inventory


def test_inventory_quotes_hostile_table_names() -> None:
    conn = sqlite3.connect(":memory:")
    hostile = 'a"b; DROP TABLE safe; --'
    conn.execute('CREATE TABLE "' + hostile.replace('"', '""') + '" (x INTEGER)')
    conn.execute("CREATE TABLE safe (x INTEGER)")
    conn.execute("INSERT INTO safe VALUES (1)")
    conn.execute('INSERT INTO "' + hostile.replace('"', '""') + '" VALUES (7), (8)')
    counts = _sqlite_inventory(None, conn)  # type: ignore[arg-type]
    assert counts == {hostile: 2, "safe": 1}
    assert conn.execute("SELECT COUNT(*) FROM safe").fetchone()[0] == 1
