"""PR3 C3 — golden management: human-triggered only, never automatic.

``nexus checkpoints golden update`` is the *only* code path allowed to
write a schema golden.  reconcile only asserts; CI never updates.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest
from typer.testing import CliRunner

from nexus_ai_agent.cli import app
from nexus_ai_agent.storage.checkpoint_adapter import FINGERPRINT_ALGORITHM


def _seeded_sqlite(tmp_path) -> str:
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = tmp_path / "goldenc.sqlite"
    conn = sqlite3.connect(path)
    SqliteSaver(conn).setup()
    conn.execute(
        "INSERT INTO checkpoints "
        "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
        "type, checkpoint, metadata) VALUES ('t1', '', 'cp1', NULL, 'json', '{}', '{}')"
    )
    conn.commit()
    conn.close()
    return str(path)


def test_golden_update_requires_confirmation(tmp_path) -> None:
    from nexus_ai_agent.config.settings import get_settings

    db = _seeded_sqlite(tmp_path)
    out = tmp_path / "out.json"
    get_settings.cache_clear()
    result = CliRunner().invoke(
        app,
        [
            "checkpoints",
            "golden",
            "update",
            "--backend",
            "sqlite",
            "--path",
            db,
            "--output",
            str(out),
        ],
    )
    get_settings.cache_clear()
    assert result.exit_code == 0, result.output
    assert "would write" in result.output
    assert "--yes" in result.output
    assert not out.exists()  # nothing written without confirmation


def test_golden_update_writes_and_is_idempotent(tmp_path) -> None:
    from nexus_ai_agent.config.settings import get_settings

    db = _seeded_sqlite(tmp_path)
    out = tmp_path / "out.json"
    get_settings.cache_clear()
    for _ in range(2):
        result = CliRunner().invoke(
            app,
            [
                "checkpoints",
                "golden",
                "update",
                "--backend",
                "sqlite",
                "--path",
                db,
                "--output",
                str(out),
                "--yes",
            ],
        )
        get_settings.cache_clear()
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["algorithm"] == FINGERPRINT_ALGORITHM
    assert len(payload["fingerprint"]) == 64

    # Idempotent: a second confirmed run writes byte-identical content and
    # prints no change warning.
    assert "CHANGES" not in result.output


def test_golden_update_warns_on_fingerprint_change(tmp_path) -> None:
    from nexus_ai_agent.config.settings import get_settings

    db = _seeded_sqlite(tmp_path)
    out = tmp_path / "out.json"
    get_settings.cache_clear()
    CliRunner().invoke(
        app,
        [
            "checkpoints",
            "golden",
            "update",
            "--backend",
            "sqlite",
            "--path",
            db,
            "--output",
            str(out),
            "--yes",
        ],
    )
    get_settings.cache_clear()

    # Schema state changes (a core stamp appears) ⇒ the fingerprint must
    # change, and the command must show old/new before the confirmed write.
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    conn.execute("INSERT INTO alembic_version (version_num) VALUES ('abc123')")
    conn.commit()
    conn.close()

    result = CliRunner().invoke(
        app,
        [
            "checkpoints",
            "golden",
            "update",
            "--backend",
            "sqlite",
            "--path",
            db,
            "--output",
            str(out),
            "--yes",
        ],
    )
    get_settings.cache_clear()
    assert result.exit_code == 0, result.output
    assert "CHANGES" in result.output
    assert "old:" in result.output and "new:" in result.output


def test_golden_update_pg_backend_without_url_fails_cleanly(tmp_path) -> None:
    from nexus_ai_agent.config.settings import get_settings

    get_settings.cache_clear()
    saved = os.environ.pop("NEXUS_DATABASE_URL", None)
    try:
        result = CliRunner().invoke(
            app, ["checkpoints", "golden", "update", "--backend", "postgres"]
        )
    finally:
        if saved is not None:
            os.environ["NEXUS_DATABASE_URL"] = saved
        get_settings.cache_clear()
    assert result.exit_code == 2
    assert "no PostgreSQL URL" in result.output


@pytest.mark.skipif(not os.getenv("NEXUS_DATABASE_URL"), reason="requires PostgreSQL")
def test_golden_update_pg_against_live_db(tmp_path) -> None:
    """The live PG fingerprint must equal the committed golden (schema-v1-pg)."""
    from nexus_ai_agent.config.settings import get_settings
    from nexus_ai_agent.storage.checkpoint_pg_adapter import (
        DEFAULT_PG_GOLDEN,
    )
    from nexus_ai_agent.storage.checkpoint_pg_adapter import (
        FINGERPRINT_ALGORITHM as PG_ALGORITHM,
    )

    out = tmp_path / "pg-out.json"
    get_settings.cache_clear()
    result = CliRunner().invoke(
        app,
        ["checkpoints", "golden", "update", "--backend", "postgres", "--output", str(out), "--yes"],
    )
    get_settings.cache_clear()
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["algorithm"] == PG_ALGORITHM
    committed = json.loads(DEFAULT_PG_GOLDEN.read_text(encoding="utf-8"))
    assert payload["fingerprint"] == committed["fingerprint"]
    # And the committed golden passes the change-detection diff (no warning).
    assert "CHANGES" not in result.output
