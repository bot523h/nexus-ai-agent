"""CLI error-contract regressions: stdout/stderr, exit-code, no traceback, redaction."""

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
    source_root = tmp_path / "kb"
    source_root.mkdir()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXUS_KNOWLEDGE_SOURCE_ROOTS", str(source_root))
    monkeypatch.setenv("NEXUS_KNOWLEDGE_INDEX_PATH", str(tmp_path / "knowledge_index"))
    monkeypatch.setenv("NEXUS_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv("NEXUS_LOG_LEVEL", "WARNING")
    settings_module.get_settings.cache_clear()
    import asyncio

    from nexus_ai_agent.storage.db import create_all_tables

    asyncio.run(create_all_tables(str(tmp_path / "app.sqlite")))
    return source_root


def _run(*args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, list(args))


def test_cli_reports_typed_error_on_stderr_not_stdout(cli_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_KNOWLEDGE_ENABLED", "false")
    settings_module.get_settings.cache_clear()
    result = _run("knowledge", "index")
    assert result.exit_code == 1
    assert "error[disabled]" in result.stderr
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout
    assert result.stdout.strip() == ""


def test_cli_no_traceback_on_misconfigured(cli_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER", "cloud-vendor")
    settings_module.get_settings.cache_clear()
    result = _run("knowledge", "status")
    assert result.exit_code == 1
    assert "error[misconfigured]" in result.stderr
    assert "Traceback" not in result.output
    assert "Traceback" not in result.stderr
    assert result.stdout.strip() == ""


def test_cli_startup_failure_is_redacted_and_no_secret_in_output(
    cli_env: Path, monkeypatch
) -> None:
    # Simulate a ValidationError whose ``input`` holds a secret. The CLI's
    # ``_safe_error_detail`` must render only the field location, never the
    # secret value.
    from pydantic import ValidationError

    from nexus_ai_agent.cli import _safe_error_detail

    # Build a ValidationError via Settings so the shape matches production
    monkeypatch.setenv("NEXUS_KNOWLEDGE_MAX_FILE_BYTES", "super-secret-token-12345")
    settings_module.get_settings.cache_clear()
    try:
        settings_module.get_settings()
        assert False, "should have raised ValidationError"
    except ValidationError as ve:
        detail = _safe_error_detail(ve)
        assert "super-secret-token" not in detail
        # Location is the env field name (case may be upper)
        assert "knowledge_max_file_bytes" in detail.lower()
        assert "ValidationError" in detail

    # Also verify the CLI path redacts it — keep the invalid env in place so
    # the real _knowledge_service → get_settings raises the same VE.
    result = _run("knowledge", "status")
    assert result.exit_code == 1
    assert "error[internal]:" in result.stderr
    assert "super-secret-token" not in result.stderr
    assert "super-secret-token" not in result.stdout
    assert "knowledge_max_file_bytes" in result.stderr.lower()
    assert "Traceback" not in result.stdout
    assert "application log" in result.stderr
    assert result.stdout.strip() == ""
    # Cleanup
    monkeypatch.delenv("NEXUS_KNOWLEDGE_MAX_FILE_BYTES", raising=False)
    settings_module.get_settings.cache_clear()


def test_cli_json_mode_keeps_stdout_parseable_on_success(cli_env: Path) -> None:
    (cli_env / "doc.md").write_text("Legitimate content for json test")
    result = _run("knowledge", "index", "--json")
    assert result.exit_code == 0
    # stdout is pure JSON
    payload = json.loads(result.stdout)
    assert "ingested" in payload
    # stderr may contain warnings but not JSON
    assert "Traceback" not in result.stderr


def test_cli_json_mode_error_is_typed_on_stderr(cli_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_KNOWLEDGE_ENABLED", "false")
    settings_module.get_settings.cache_clear()
    result = _run("knowledge", "search", "hello", "--json")
    assert result.exit_code == 1
    assert "error[disabled]" in result.stderr
    assert result.stdout.strip() == ""
    assert "Traceback" not in result.stderr


def test_cli_internal_error_logs_but_shows_safe_detail(
    cli_env: Path, monkeypatch
) -> None:
    def boom():  # type: ignore[no-untyped-def]
        raise PermissionError(13, "Permission denied", str(cli_env / "knowledge_index"))

    monkeypatch.setattr(settings_module, "get_settings", boom)
    result = _run("knowledge", "status")
    assert result.exit_code == 1
    assert "error[internal]:" in result.stderr
    assert "PermissionError" in result.stderr
    assert "Permission denied" in result.stderr
    assert str(cli_env / "knowledge_index") in result.stderr
    assert "application log" in result.stderr
    assert result.stdout.strip() == ""
    assert "Traceback" not in result.stdout
    # stderr should not contain a raw Python traceback frame like 'File "'
    # (it may contain the safe one-line detail, but not a multi-line traceback)
    # The logger does emit exc_info to stderr via structlog, but CliRunner
    # captures that — we assert no "Traceback (most recent call last)" leak
    # in the user-facing line. The log traceback is allowed on stderr but
    # must be after the typed line — we check the typed line is first.
    assert result.stderr.strip().splitlines()[0].startswith("error[internal]:")


def test_cli_unconfigured_roots_shows_hint_and_exit_1(
    cli_env: Path, monkeypatch
) -> None:
    monkeypatch.setenv("NEXUS_KNOWLEDGE_SOURCE_ROOTS", "")
    settings_module.get_settings.cache_clear()
    result = _run("knowledge", "index")
    assert result.exit_code == 1
    assert "error[misconfigured]" in result.stderr
    assert "NEXUS_KNOWLEDGE_SOURCE_ROOTS" in result.stderr
    assert "hint:" in result.stderr
    assert result.stdout.strip() == ""
