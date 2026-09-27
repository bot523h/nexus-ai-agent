"""Dynamic integration proof for the custom PTB/Uvicorn webhook lifecycle.

The production lifecycle callbacks are registered on the PTB Application and
are automatically driven by ``Application.run_polling``.  The webhook adapter
uses manually-managed ``initialize/start/stop/shutdown`` calls, so it must
invoke those *same* callbacks and keep cleanup reachable after partial startup.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.bot import webhook


class _Runtime:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1
        self.events.append("runtime.close")


class _JobQueue:
    def __init__(self, events: list[str], fail_at: str | None) -> None:
        self.events = events
        self.fail_at = fail_at
        self.resume_count = 0

    async def resume_pending(self) -> None:
        self.resume_count += 1
        self.events.append("job.resume")
        if self.fail_at == "resume":
            raise RuntimeError("injected failure at resume")


class _Bot:
    def __init__(self, events: list[str], fail_at: str | None) -> None:
        self.events = events
        self.fail_at = fail_at
        self.set_webhook_count = 0

    async def set_webhook(self, *, url: str, secret_token: str) -> None:
        assert url == "https://example.test/telegram"
        assert secret_token == "secret"
        self.set_webhook_count += 1
        self.events.append("bot.set_webhook")
        if self.fail_at == "set_webhook":
            raise RuntimeError("injected failure at set_webhook")


class _Application:
    """Faithful PTB lifecycle test double with explicitly registered hooks."""

    def __init__(
        self,
        *,
        fail_at: str | None = None,
        stop_started: asyncio.Event | None = None,
        stop_release: asyncio.Event | None = None,
    ) -> None:
        self.events: list[str] = []
        self.fail_at = fail_at
        self.stop_started = stop_started
        self.stop_release = stop_release
        self.runtime = _Runtime(self.events)
        self.job_queue = _JobQueue(self.events, fail_at)
        self.bot = _Bot(self.events, fail_at)
        self.bot_data: dict[str, Any] = {
            "job_queue": self.job_queue,
            "runtime": self.runtime,
        }
        # PTB ApplicationBuilder stores these callbacks on the application.
        # run_polling invokes them; a custom adapter must dispatch the same hooks.
        self.post_init = self._post_init
        self.post_shutdown = self._post_shutdown

    def _fail(self, stage: str) -> None:
        if self.fail_at == stage:
            raise RuntimeError(f"injected failure at {stage}")

    async def _post_init(self, application: Any) -> None:
        assert application is self
        self.events.append("hook.post_init")
        await self.job_queue.resume_pending()
        self._fail("post_init")

    async def _post_shutdown(self, application: Any) -> None:
        assert application is self
        self.events.append("hook.post_shutdown")
        await self.runtime.aclose()

    async def initialize(self) -> None:
        self.events.append("app.initialize")
        self._fail("initialize")

    async def start(self) -> None:
        self.events.append("app.start")
        self._fail("start")

    async def stop(self) -> None:
        self.events.append("app.stop")
        if self.stop_started is not None:
            self.stop_started.set()
        if self.stop_release is not None:
            await self.stop_release.wait()
        self._fail("stop")

    async def shutdown(self) -> None:
        self.events.append("app.shutdown")
        self._fail("shutdown")


class _ServerFactory:
    def __init__(
        self,
        events: list[str],
        *,
        fail_at: str | None = None,
        serve_started: asyncio.Event | None = None,
        serve_release: asyncio.Event | None = None,
    ) -> None:
        self.events = events
        self.fail_at = fail_at
        self.serve_started = serve_started
        self.serve_release = serve_release

    def config(self, app: Any, **kwargs: Any) -> object:
        assert app is not None
        if self.fail_at == "config":
            raise RuntimeError("injected failure at config")
        return object()

    def server(self, config: object) -> Any:
        owner = self

        class _Server:
            async def serve(self) -> None:
                owner.events.append("server.serve")
                if owner.serve_started is not None:
                    owner.serve_started.set()
                if owner.serve_release is not None:
                    await owner.serve_release.wait()
                if owner.fail_at == "serve":
                    raise RuntimeError("injected failure at serve")

        return _Server()


def _install_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    fail_at: str | None = None,
    serve_started: asyncio.Event | None = None,
    serve_release: asyncio.Event | None = None,
) -> None:
    factory = _ServerFactory(
        events,
        fail_at=fail_at,
        serve_started=serve_started,
        serve_release=serve_release,
    )
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(Config=factory.config, Server=factory.server),
    )


def _serve(app: _Application) -> Any:
    return webhook._serve_webhook(
        application=app,
        api_app=SimpleNamespace(state=SimpleNamespace()),
        webhook_url="https://example.test/telegram",
        webhook_secret="secret",
        host="127.0.0.1",
        port=8080,
        log_level="info",
    )


async def test_webhook_runs_the_registered_lifecycle_once_in_dependency_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _Application()
    _install_uvicorn(monkeypatch, app.events)

    await _serve(app)

    assert app.events == [
        "app.initialize",
        "hook.post_init",
        "job.resume",
        "app.start",
        "bot.set_webhook",
        "server.serve",
        "app.stop",
        "app.shutdown",
        "hook.post_shutdown",
        "runtime.close",
    ]
    assert app.job_queue.resume_count == 1
    assert app.runtime.close_count == 1
    assert app.bot.set_webhook_count == 1


@pytest.mark.parametrize(
    ("fail_at", "initialized", "start_attempted"),
    [
        ("config", False, False),
        ("initialize", True, False),
        ("post_init", True, False),
        ("resume", True, False),
        ("start", True, True),
        ("set_webhook", True, True),
        ("serve", True, True),
        ("stop", True, True),
        ("shutdown", True, True),
    ],
)
async def test_partial_startup_always_attempts_reachable_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    fail_at: str,
    initialized: bool,
    start_attempted: bool,
) -> None:
    app = _Application(fail_at=fail_at)
    _install_uvicorn(monkeypatch, app.events, fail_at=fail_at)

    with pytest.raises(BaseException):
        await _serve(app)

    assert app.events.count("hook.post_shutdown") == 1, app.events
    assert app.runtime.close_count == 1, app.events
    assert app.events.count("app.shutdown") == int(initialized), app.events
    assert app.events.count("app.stop") == int(start_attempted), app.events
    if fail_at in {"resume", "post_init", "start", "set_webhook", "serve", "stop", "shutdown"}:
        assert app.events.count("job.resume") == 1, app.events


def test_preflight_configuration_failure_closes_prebuilt_runtime() -> None:
    app = _Application()
    settings = SimpleNamespace(webhook_url="", webhook_secret="secret", log_level="info")

    with pytest.raises(webhook.WebhookConfigError, match="NEXUS_WEBHOOK_URL"):
        webhook.run_webhook(app, settings=settings)  # type: ignore[arg-type]

    assert app.events == ["hook.post_shutdown", "runtime.close"]
    assert app.runtime.close_count == 1


async def test_repeated_sigterm_cancellation_finishes_cleanup_then_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_started = asyncio.Event()
    serve_release = asyncio.Event()
    stop_started = asyncio.Event()
    stop_release = asyncio.Event()
    app = _Application(stop_started=stop_started, stop_release=stop_release)
    _install_uvicorn(
        monkeypatch,
        app.events,
        serve_started=serve_started,
        serve_release=serve_release,
    )

    task = asyncio.create_task(_serve(app))
    await asyncio.wait_for(serve_started.wait(), timeout=5)
    task.cancel()  # SIGTERM-style cancellation of the serving task.
    await asyncio.wait_for(stop_started.wait(), timeout=5)
    task.cancel()  # adversarial repeated cancellation during cleanup.
    stop_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert app.events.count("app.stop") == 1
    assert app.events.count("app.shutdown") == 1
    assert app.events.count("hook.post_shutdown") == 1
    assert app.runtime.close_count == 1
