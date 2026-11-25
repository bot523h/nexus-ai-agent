"""Startup composition is opt-in and runs before either bot accepts traffic."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn

from nexus_ai_agent.bot import app as bot_module
from nexus_ai_agent.bot import webhook
from nexus_ai_agent.creative.assets import service as asset_service


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_bot_post_init_reconciles_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    from nexus_ai_agent.bot import access_guard, creative_surface, handlers

    events: list[str] = []
    marker, report = object(), object()

    class Queue:
        async def acquire_ownership(self) -> None:
            events.append("queue")

        async def resume_pending(self) -> None:
            events.append("resume")

    class Feature:
        def bind(self, bot: object) -> None:
            pass

        async def restore_pending(self) -> None:
            pass

    class FakeApplication:
        def __init__(self) -> None:
            self.bot_data: dict[str, Any] = {}
            self.bot = object()
            self.post_init: Any = None

        def add_handler(self, *args: Any, **kwargs: Any) -> None:
            pass

    app = FakeApplication()

    class Builder:
        def token(self, token: str) -> Builder:
            return self

        def post_init(self, callback: Any) -> Builder:
            app.post_init = callback
            return self

        def post_shutdown(self, callback: Any) -> Builder:
            return self

        def build(self) -> Any:
            return app

    async def reconcile(value: Any) -> Any:
        assert value is marker
        events.append("assets")
        return report

    monkeypatch.setattr(bot_module, "ApplicationBuilder", Builder)
    monkeypatch.setattr(
        bot_module,
        "_init_v2_engines",
        lambda settings: {
            "job_queue": Queue(),
            "feature_engines": SimpleNamespace(
                reminders=Feature(), force_join=Feature(), anon=Feature()
            ),
        },
    )
    monkeypatch.setattr(bot_module, "build_handlers", lambda *args, **kwargs: [])
    monkeypatch.setattr(access_guard, "build_access_guard", lambda settings: object())
    monkeypatch.setattr(handlers, "build_moderation_gate", lambda: object())
    monkeypatch.setattr(creative_surface, "build_creative_handlers", lambda queue: {})
    monkeypatch.setattr(asset_service, "async_reconcile_asset_library_startup", reconcile)
    settings = SimpleNamespace(
        telegram_bot_token="123:token",
        asset_library_root=None,
        asset_library_db_path=None,
        self_vitals_enabled=False,
        db_path="unused",
        self_vitals_period_s=30,
        max_ram_mb=512,
    )
    bot_module.build_application(settings, object(), storage=object(), session_factory=lambda: None)  # type: ignore[arg-type]
    if configured:
        app.bot_data["asset_library_service"] = marker
    await app.post_init(app)
    assert events == (["assets", "queue", "resume"] if configured else ["queue", "resume"])
    assert app.bot_data["startup_status"]["state"] == "ready"
    if configured:
        assert app.bot_data["asset_library_startup_report"] is report


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_webhook_startup_reconciles_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    events: list[str] = []
    marker, report = object(), object()

    class App:
        def __init__(self) -> None:
            self.bot_data: dict[str, Any] = {}
            self.bot = self

        async def initialize(self) -> None:
            events.append("initialize")

        async def start(self) -> None:
            events.append("start")

        async def set_webhook(self, **kwargs: Any) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def shutdown(self) -> None:
            pass

    class Config:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class Server:
        def __init__(self, config: Any) -> None:
            pass

        async def serve(self) -> None:
            pass

    async def reconcile(value: Any) -> Any:
        assert value is marker
        events.append("assets")
        return report

    app = App()
    if configured:
        app.bot_data["asset_library_service"] = marker
    monkeypatch.setattr(asset_service, "async_reconcile_asset_library_startup", reconcile)
    monkeypatch.setattr(uvicorn, "Config", Config)
    monkeypatch.setattr(uvicorn, "Server", Server)
    await webhook._serve_webhook(
        application=app,
        api_app=object(),
        webhook_url="https://example.test",
        webhook_secret="secret",
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
    assert events == (["initialize", "assets", "start"] if configured else ["initialize", "start"])
    if configured:
        assert app.bot_data["asset_library_startup_report"] is report
