"""Tests for the settings.auto_update gate on the self-update path."""

from __future__ import annotations

import pytest

from nexus_ai_agent.agent.approval import ApprovalSystem
from nexus_ai_agent.agent.self_monitor import SelfMonitor
from nexus_ai_agent.agent.updater import AutoUpdater


@pytest.fixture()
def monitor_env(monkeypatch, tmp_path):
    """Fresh settings per test (AUTO_UPDATE controlled per test)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.delenv("AUTO_UPDATE", raising=False)
    monkeypatch.delenv("NEXUS_AUTO_UPDATE", raising=False)

    from nexus_ai_agent.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    return settings_module.get_settings()


@pytest.mark.asyncio
async def test_update_check_disabled_by_default(monitor_env, monkeypatch) -> None:
    called = False

    async def fake_check(self):  # pragma: no cover - must not be reached
        nonlocal called
        called = True
        return False, None

    monkeypatch.setattr(AutoUpdater, "check_for_update", fake_check)

    monitor = SelfMonitor()
    assert monitor.settings.auto_update is False
    result = await monitor.check_for_update("v3.0.0")
    assert result is None  # disabled -> no check performed at all
    assert called is False

    ok, detail = await monitor.perform_update("v3.0.0", ApprovalSystem())
    assert ok is False
    assert "disabled" in detail


@pytest.mark.asyncio
async def test_update_check_enabled_passes_through(monitor_env, monkeypatch) -> None:
    monkeypatch.setenv("AUTO_UPDATE", "true")
    from nexus_ai_agent.config import settings as settings_module

    settings_module.get_settings.cache_clear()

    async def fake_check(self):
        return True, "v9.9.9"

    monkeypatch.setattr(AutoUpdater, "check_for_update", fake_check)

    monitor = SelfMonitor()
    assert monitor.settings.auto_update is True
    result = await monitor.check_for_update("v3.0.0")
    assert result == (True, "v9.9.9")


@pytest.mark.asyncio
async def test_perform_update_enabled_delegates_with_approval(monitor_env, monkeypatch) -> None:
    monkeypatch.setenv("AUTO_UPDATE", "true")
    from nexus_ai_agent.config import settings as settings_module

    settings_module.get_settings.cache_clear()

    async def fake_do_update(self, approval):
        return True, "update completed"

    monkeypatch.setattr(AutoUpdater, "do_update", fake_do_update)

    monitor = SelfMonitor()
    ok, detail = await monitor.perform_update("v3.0.0", ApprovalSystem())
    assert ok is True
    assert detail == "update completed"
