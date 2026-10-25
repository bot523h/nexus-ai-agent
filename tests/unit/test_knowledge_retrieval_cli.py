"""CLI proof: the real ``nexus knowledge`` wiring an operator runs.

Drives the shipped Typer app end to end against an isolated SQLite database,
an isolated source root and the real LlamaIndex adapter (deterministic hash
embeddings — no model, no credentials, no network).  The assertions pin the
*contracts*: parseable ``--json`` on stdout, typed ``error[code]`` lines with
exit 1 on failure, and distinct defined states for empty outcomes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nexus_ai_agent.cli import app
from nexus_ai_agent.config import settings as settings_module

runner = CliRunner()


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated DB + source root + index dir; cached settings invalidated."""
    source_root = tmp_path / "kb"
    source_root.mkdir()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXUS_KNOWLEDGE_SOURCE_ROOTS", str(source_root))
    monkeypatch.setenv("NEXUS_KNOWLEDGE_INDEX_PATH", str(tmp_path / "knowledge_index"))
    monkeypatch.setenv("NEXUS_KNOWLEDGE_ENABLED", "true")
    # Deterministic offline provider regardless of the invoking environment:
    # an inherited NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER must not change behavior.
    monkeypatch.setenv("NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv("NEXUS_LOG_LEVEL", "WARNING")
    settings_module.get_settings.cache_clear()

    import asyncio

    from nexus_ai_agent.storage.db import create_all_tables

    asyncio.run(create_all_tables(str(tmp_path / "app.sqlite")))
    return source_root


def _run(*args: str):
    return runner.invoke(app, list(args))


def test_status_json_reports_honest_absent_state(cli_env: Path) -> None:
    result = _run("knowledge", "status", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)  # stdout must be pure JSON
    assert payload["enabled"] is True
    assert payload["configured"] is True
    assert payload["store_present"] is False
    assert payload["source_count"] == 0
    assert payload["embedding_provider"] == "hash"
    assert payload["degraded"] is False


def test_index_search_status_roundtrip_json(cli_env: Path) -> None:
    (cli_env / "runbook.md").write_text(
        "Rollback procedure: redeploy the previous image tag via GitHub Actions."
    )

    indexed = _run("knowledge", "index", "--json")
    assert indexed.exit_code == 0, indexed.output
    report = json.loads(indexed.stdout)
    assert report["ingested"] == 1 and report["failed"] == 0

    searched = _run("knowledge", "search", "rollback previous image", "--json")
    assert searched.exit_code == 0, searched.output
    response = json.loads(searched.stdout)
    assert response["state"] == "ok"
    evidence = response["evidence"][0]
    assert evidence["path"] == "runbook.md"
    assert len(evidence["sha256"]) == 64
    assert evidence["citation"] == (
        f"runbook.md@{evidence['sha256'][:12]}#{evidence['chunk_index']}"
    )
    assert "Rollback procedure" in evidence["excerpt"]

    status = json.loads(_run("knowledge", "status", "--json").stdout)
    assert status["store_present"] is True
    assert status["source_count"] == 1
    assert status["node_count"] >= 1

    listed = json.loads(_run("knowledge", "sources", "list", "--json").stdout)
    assert [row["path"] for row in listed] == ["runbook.md"]
    assert listed[0]["status"] == "ingested"


def test_search_human_output_shows_citation(cli_env: Path) -> None:
    (cli_env / "notes.txt").write_text("The retrieval subsystem lives under knowledge_retrieval.")
    assert _run("knowledge", "index").exit_code == 0

    result = _run("knowledge", "search", "retrieval subsystem")
    assert result.exit_code == 0, result.output
    assert "notes.txt" in result.output
    assert "#chunk" in result.output


def test_search_empty_query_is_defined_state_exit_zero(cli_env: Path) -> None:
    result = _run("knowledge", "search", "   ", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["state"] == "empty_query"
    assert payload["evidence"] == []


def test_search_on_empty_index_is_defined_state(cli_env: Path) -> None:
    result = _run("knowledge", "search", "anything relevant", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"] == "empty_index"


def test_disabled_subsystem_fails_closed_with_typed_error(cli_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_KNOWLEDGE_ENABLED", "false")
    settings_module.get_settings.cache_clear()

    disabled_commands = (
        ["knowledge", "index"],
        ["knowledge", "search", "q"],
        ["knowledge", "rebuild", "--yes"],
    )
    for command in disabled_commands:
        result = _run(*command)
        assert result.exit_code == 1, result.output
        assert "error[disabled]" in result.stderr

    status = _run("knowledge", "status", "--json")
    assert status.exit_code == 0
    assert json.loads(status.stdout)["enabled"] is False


def test_unconfigured_roots_fail_closed(cli_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_KNOWLEDGE_SOURCE_ROOTS", "")
    settings_module.get_settings.cache_clear()

    result = _run("knowledge", "index")
    assert result.exit_code == 1, result.output
    assert "error[misconfigured]" in result.stderr
    assert "NEXUS_KNOWLEDGE_SOURCE_ROOTS" in result.stderr


def test_unknown_provider_fails_without_traceback(cli_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER", "cloud-vendor")
    settings_module.get_settings.cache_clear()

    result = _run("knowledge", "status")
    assert result.exit_code == 1, result.output
    assert "error[misconfigured]" in result.stderr
    assert "Traceback" not in result.stderr


def test_rebuild_requires_confirmation_flag_or_prompts(cli_env: Path) -> None:
    (cli_env / "doc.md").write_text("Rebuildable knowledge content.")
    assert _run("knowledge", "index").exit_code == 0

    confirmed = _run("knowledge", "rebuild", "--yes", "--json")
    assert confirmed.exit_code == 0, confirmed.output
    payload = json.loads(confirmed.stdout)
    assert payload["rebuilt"] is True and payload["ingested"] == 1

    aborted = runner.invoke(app, ["knowledge", "rebuild"], input="n\n")
    assert aborted.exit_code != 0


def test_sources_remove_then_search_no_longer_returns_it(cli_env: Path) -> None:
    (cli_env / "old.md").write_text("Deprecated content about legacy fax gateways.")
    (cli_env / "keep.md").write_text("Current content about webhook gateways.")
    assert _run("knowledge", "index").exit_code == 0

    removed = _run("knowledge", "sources", "remove", "old.md")
    assert removed.exit_code == 0, removed.output

    after = json.loads(_run("knowledge", "search", "deprecated fax gateways", "--json").stdout)
    assert all(ev["path"] != "old.md" for ev in after["evidence"])

    missing = _run("knowledge", "sources", "remove", "never-existed.md")
    assert missing.exit_code == 1
    assert "error[not_found]" in missing.stderr


def test_index_reports_secret_exclusion_in_human_output(cli_env: Path) -> None:
    (cli_env / "doc.md").write_text("Legitimate content.")
    (cli_env / ".env").write_text("TOKEN=supersecret")

    result = _run("knowledge", "index")
    assert result.exit_code == 0, result.output
    assert ".env" in result.stderr and "secret" in result.stderr

    listed = json.loads(_run("knowledge", "sources", "list", "--json").stdout)
    assert all(".env" not in row["path"] for row in listed)


def test_startup_failure_prints_a_safe_actionable_detail(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup diagnostics: never a bare exception name, never a silent log.

    If settings/index-directory construction fails, the CLI must print the
    safe detail (type + message) and route the full traceback to the actual
    application log — stdout stays empty (no log pollution).
    """

    def boom():
        raise PermissionError(13, "Permission denied", str(cli_env / "knowledge_index"))

    monkeypatch.setattr(settings_module, "get_settings", boom)
    result = _run("knowledge", "status")
    assert result.exit_code == 1
    assert "error[internal]:" in result.stderr
    assert "PermissionError" in result.stderr
    assert "Permission denied" in result.stderr  # actionable, not just the type name
    assert "knowledge_index" in result.stderr  # the operator's configured path
    assert "application log" in result.stderr
    assert result.stdout.strip() == ""  # stdout carries no log output
