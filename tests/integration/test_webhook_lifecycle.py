"""Dynamic integration proof for the custom PTB/Uvicorn webhook lifecycle.

The production lifecycle callbacks are registered on the PTB Application and
are automatically driven by ``Application.run_polling``.  The webhook adapter
uses manually-managed ``initialize/start/stop/shutdown`` calls, so it must
invoke those *same* callbacks and keep cleanup reachable after partial startup.
"""

from __future__ import annotations

import asyncio
import http.client
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
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
        self.shutdown_count = 0
        self._initialized = False

    async def initialize(self) -> None:
        self.events.append("bot.initialize")
        self._initialized = True

    async def shutdown(self) -> None:
        if not self._initialized:
            return
        self._initialized = False
        self.shutdown_count += 1
        self.events.append("bot.shutdown")

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
        self._initialized = False
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
        self.post_stop = self._post_stop
        self.post_shutdown = self._post_shutdown

    def _fail(self, stage: str) -> None:
        if self.fail_at == stage:
            raise RuntimeError(f"injected failure at {stage}")

    async def _post_init(self, application: Any) -> None:
        assert application is self
        self.events.append("hook.post_init")
        await self.job_queue.resume_pending()
        self._fail("post_init")

    async def _post_stop(self, application: Any) -> None:
        assert application is self
        self.events.append("hook.post_stop")

    async def _post_shutdown(self, application: Any) -> None:
        assert application is self
        self.events.append("hook.post_shutdown")
        self._fail("post_shutdown")
        await self.runtime.aclose()

    async def initialize(self) -> None:
        self.events.append("app.initialize")
        await self.bot.initialize()
        self._fail("initialize")
        self._initialized = True

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
        if not self._initialized:
            return
        await self.bot.shutdown()
        self._initialized = False
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
            def __init__(self) -> None:
                self._should_exit = False

            @property
            def should_exit(self) -> bool:
                return self._should_exit

            @should_exit.setter
            def should_exit(self, value: bool) -> None:
                self._should_exit = value
                if value and owner.serve_release is not None:
                    owner.serve_release.set()

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
        "bot.initialize",
        "hook.post_init",
        "job.resume",
        "app.start",
        "bot.set_webhook",
        "server.serve",
        "app.stop",
        "hook.post_stop",
        "app.shutdown",
        "bot.shutdown",
        "hook.post_shutdown",
        "runtime.close",
    ]
    assert app.job_queue.resume_count == 1
    assert app.runtime.close_count == 1
    assert app.bot.set_webhook_count == 1


@pytest.mark.parametrize(
    ("fail_at", "initialize_attempted", "start_attempted"),
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
    initialize_attempted: bool,
    start_attempted: bool,
) -> None:
    app = _Application(fail_at=fail_at)
    _install_uvicorn(monkeypatch, app.events, fail_at=fail_at)

    with pytest.raises(RuntimeError):
        await _serve(app)

    assert app.events.count("hook.post_shutdown") == 1, app.events
    assert app.runtime.close_count == 1, app.events
    assert app.events.count("app.shutdown") == int(initialize_attempted), app.events
    assert app.bot.shutdown_count == int(initialize_attempted), app.events
    assert app.events.count("app.stop") == int(start_attempted), app.events
    assert app.events.count("hook.post_stop") == int(start_attempted), app.events
    if fail_at in {"resume", "post_init", "start", "set_webhook", "serve", "stop", "shutdown"}:
        assert app.events.count("job.resume") == 1, app.events


async def test_real_ptb_partial_initialize_closes_bot_request_and_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from telegram.error import NetworkError
    from telegram.ext import ApplicationBuilder
    from telegram.request import BaseRequest

    events: list[str] = []

    class FailingGetMeRequest(BaseRequest):
        def __init__(self) -> None:
            self.initialize_count = 0
            self.shutdown_count = 0

        @property
        def read_timeout(self) -> float:
            return 5.0

        async def initialize(self) -> None:
            self.initialize_count += 1
            events.append("request.initialize")

        async def shutdown(self) -> None:
            self.shutdown_count += 1
            events.append("request.shutdown")

        async def do_request(
            self,
            url: str,
            method: str,
            request_data: Any = None,
            read_timeout: Any = None,
            write_timeout: Any = None,
            connect_timeout: Any = None,
            pool_timeout: Any = None,
        ) -> tuple[int, bytes]:
            _ = (method, request_data, read_timeout, write_timeout, connect_timeout, pool_timeout)
            if "getMe" in url:
                events.append("telegram.get_me")
                raise RuntimeError("injected getMe failure after request initialization")
            return 200, b'{"ok":true,"result":true}'

    request = FailingGetMeRequest()

    async def close_runtime(application: Any) -> None:
        _ = application
        events.append("runtime.close")

    application = (
        ApplicationBuilder()
        .token("123456:TESTTOKENYYYYYYYYYYYYYYYYYYYYYY")
        .request(request)
        .updater(None)
        .job_queue(None)
        .post_shutdown(close_runtime)
        .build()
    )
    _install_uvicorn(monkeypatch, events)

    with pytest.raises(NetworkError, match="injected getMe failure"):
        await webhook._serve_webhook(
            application=application,
            api_app=SimpleNamespace(state=SimpleNamespace()),
            webhook_url="https://example.test/telegram",
            webhook_secret="secret",
            host="127.0.0.1",
            port=8080,
            log_level="critical",
        )

    assert request.initialize_count == 1
    assert request.shutdown_count == 1
    assert events[-2:] == ["request.shutdown", "runtime.close"]


async def test_shutdown_failures_are_aggregated_without_skipping_later_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _Application(fail_at="stop")
    _install_uvicorn(monkeypatch, app.events, fail_at="serve")

    with pytest.raises(webhook.WebhookLifecycleError) as error:
        await _serve(app)

    assert isinstance(error.value.primary_failure, RuntimeError)
    assert "injected failure at serve" in str(error.value.primary_failure)
    assert [phase for phase, _ in error.value.cleanup_failures] == ["application.stop"]
    assert app.events[-6:] == [
        "app.stop",
        "hook.post_stop",
        "app.shutdown",
        "bot.shutdown",
        "hook.post_shutdown",
        "runtime.close",
    ]


async def test_post_shutdown_failure_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _Application(fail_at="post_shutdown")
    _install_uvicorn(monkeypatch, app.events)

    with pytest.raises(webhook.WebhookLifecycleError) as error:
        await _serve(app)

    assert error.value.primary_failure is None
    assert [phase for phase, _ in error.value.cleanup_failures] == ["post_shutdown"]
    assert app.events.count("hook.post_shutdown") == 1
    assert app.runtime.close_count == 0


@pytest.mark.parametrize(
    ("webhook_url", "webhook_secret", "message"),
    [
        ("", "secret", "NEXUS_WEBHOOK_URL"),
        ("https://example.test/telegram", "", "NEXUS_WEBHOOK_SECRET"),
    ],
)
def test_preflight_configuration_failure_closes_prebuilt_runtime(
    webhook_url: str,
    webhook_secret: str,
    message: str,
) -> None:
    app = _Application()
    settings = SimpleNamespace(
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        log_level="info",
    )

    with pytest.raises(webhook.WebhookConfigError, match=message):
        webhook.run_webhook(app, settings=settings)  # type: ignore[arg-type]

    assert app.events == ["hook.post_shutdown", "runtime.close"]
    assert app.runtime.close_count == 1


def test_preflight_bind_failure_closes_prebuilt_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _Application()
    settings = SimpleNamespace(
        webhook_url="https://example.test/telegram",
        webhook_secret="secret",
        log_level="info",
    )
    monkeypatch.setenv("PORT", "not-a-port")

    with pytest.raises(ValueError):
        webhook.run_webhook(app, settings=settings)  # type: ignore[arg-type]

    assert app.events == ["hook.post_shutdown", "runtime.close"]
    assert app.runtime.close_count == 1


async def test_registered_hooks_use_the_shared_dispatch_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _Application()
    _install_uvicorn(monkeypatch, app.events)
    original = webhook._WebhookLifecycle.invoke_hook
    dispatched: list[str] = []

    async def observe_dispatch(lifecycle: Any, hook_name: str) -> None:
        dispatched.append(hook_name)
        await original(lifecycle, hook_name)

    monkeypatch.setattr(webhook._WebhookLifecycle, "invoke_hook", observe_dispatch)

    await _serve(app)

    assert dispatched == ["post_init", "post_stop", "post_shutdown"]


async def test_repeated_shutdown_dispatch_is_idempotent() -> None:
    app = _Application()
    lifecycle = webhook._WebhookLifecycle(app)

    await lifecycle.invoke_hook("post_shutdown")
    await lifecycle.invoke_hook("post_shutdown")

    assert app.events == ["hook.post_shutdown", "runtime.close"]
    assert app.runtime.close_count == 1


async def test_failed_shutdown_hook_can_retry_and_then_becomes_idempotent() -> None:
    app = _Application(fail_at="post_shutdown")
    lifecycle = webhook._WebhookLifecycle(app)

    with pytest.raises(RuntimeError, match="injected failure at post_shutdown"):
        await lifecycle.invoke_hook("post_shutdown")

    app.fail_at = None
    await lifecycle.invoke_hook("post_shutdown")
    await lifecycle.invoke_hook("post_shutdown")

    assert app.events.count("hook.post_shutdown") == 2
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


def test_cli_webhook_mode_passes_its_application_to_webhook_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from telegram.ext import ApplicationBuilder

    from nexus_ai_agent import cli
    from nexus_ai_agent.bot import app as bot_app
    from nexus_ai_agent.bot import webhook as webhook_module
    from nexus_ai_agent.config import settings as settings_module
    from nexus_ai_agent.memory import long_term
    from nexus_ai_agent.observability import logging as logging_module
    from nexus_ai_agent.orchestration import graph as graph_module
    from nexus_ai_agent.storage import langgraph_checkpoint, migrations
    from nexus_ai_agent.tools import files as files_module
    from nexus_ai_agent.tools import registry as registry_module

    settings = SimpleNamespace(
        log_level="INFO",
        db_path=str(tmp_path / "data" / "app.sqlite"),
        enable_shell=False,
        vector_path=str(tmp_path / "data" / "vector.sqlite"),
        checkpoint_path=str(tmp_path / "data" / "checkpoints.sqlite"),
        workspace_root=str(tmp_path),
    )
    application = (
        ApplicationBuilder()
        .token("123456:TESTTOKENYYYYYYYYYYYYYYYYYYYYYY")
        .updater(None)
        .job_queue(None)
        .build()
    )
    received: list[Any] = []

    class _Registry:
        def __init__(self, **kwargs: Any) -> None:
            self.tools: list[Any] = []

        def register(self, tool: Any) -> None:
            self.tools.append(tool)

    class _Tool:
        pass

    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    monkeypatch.setattr(logging_module, "configure_logging", lambda _level: None)
    monkeypatch.setattr(
        migrations, "ensure_startup_schema", lambda: {"backend": "sqlite", "source": "test"}
    )
    monkeypatch.setattr(cli, "build_llm_provider", lambda _settings: (object(), "test-llm"))
    monkeypatch.setattr(long_term, "LongTermMemory", lambda *_args: object())
    monkeypatch.setattr(langgraph_checkpoint, "get_checkpointer", lambda _path: object())
    monkeypatch.setattr(graph_module, "compile_graph", lambda *_args: object())
    monkeypatch.setattr(registry_module, "ToolRegistry", _Registry)
    monkeypatch.setattr(files_module, "ReadFileTool", _Tool)
    monkeypatch.setattr(files_module, "WriteFileTool", _Tool)
    monkeypatch.setattr(files_module, "ListDirTool", _Tool)
    monkeypatch.setattr(bot_app, "build_application", lambda *_args: application)
    monkeypatch.setattr(webhook_module, "run_webhook", received.append)
    monkeypatch.setenv("NEXUS_WORKSPACE_ROOT", "before-test")

    cli.run_bot(mode="webhook")

    assert received == [application]


def test_sigterm_from_cli_drives_real_ptb_and_uvicorn_to_runtime_close(
    tmp_path: Path,
) -> None:
    """Exercise CLI → webhook adapter → Uvicorn/SIGTERM → PTB cleanup."""
    repository = Path(__file__).resolve().parents[2]
    trace_path = tmp_path / "webhook-lifecycle.txt"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    child = textwrap.dedent(
        r"""
        import json
        import os

        from telegram.ext import ApplicationBuilder
        from telegram.request import BaseRequest

        from nexus_ai_agent import cli
        from nexus_ai_agent.bot import app as bot_app
        from nexus_ai_agent.config import settings as settings_module
        from nexus_ai_agent.config.settings import Settings
        from nexus_ai_agent.memory import long_term
        from nexus_ai_agent.observability import logging as logging_module
        from nexus_ai_agent.orchestration import graph as graph_module
        from nexus_ai_agent.storage import langgraph_checkpoint, migrations
        from nexus_ai_agent.tools import files as files_module
        from nexus_ai_agent.tools import registry as registry_module

        def mark(event):
            with open(os.environ["NEXUS_TEST_LIFECYCLE_TRACE"], "a", encoding="utf-8") as stream:
                stream.write(event + "\n")

        class InMemoryTelegramAPI(BaseRequest):
            @property
            def read_timeout(self):
                return 5.0

            async def initialize(self):
                mark("request.initialize")

            async def shutdown(self):
                mark("request.shutdown")

            async def do_request(
                self,
                url,
                method,
                request_data=None,
                read_timeout=None,
                write_timeout=None,
                connect_timeout=None,
                pool_timeout=None,
            ):
                if "getMe" in url:
                    result = {"id": 42, "is_bot": True, "first_name": "N", "username": "nexus"}
                else:
                    mark("telegram.webhook_registered")
                    result = True
                return 200, json.dumps({"ok": True, "result": result}).encode()

        async def post_init(application):
            mark("post_init")

        async def post_stop(application):
            mark("post_stop")

        async def post_shutdown(application):
            mark("runtime.close")

        application = (
            ApplicationBuilder()
            .token("123456:TESTTOKENYYYYYYYYYYYYYYYYYYYYYY")
            .request(InMemoryTelegramAPI())
            .updater(None)
            .job_queue(None)
            .post_init(post_init)
            .post_stop(post_stop)
            .post_shutdown(post_shutdown)
            .build()
        )

        def observe_method(name, method):
            async def observed(*args, **kwargs):
                mark(name)
                return await method(*args, **kwargs)
            return observed

        application.initialize = observe_method("application.initialize", application.initialize)
        application.start = observe_method("application.start", application.start)
        application.stop = observe_method("application.stop", application.stop)
        application.shutdown = observe_method("application.shutdown", application.shutdown)

        settings = Settings(
            log_level="INFO",
            db_path="/tmp/nexus-webhook-cli.sqlite",
            enable_shell=False,
            vector_path="/tmp/nexus-webhook-vector.sqlite",
            checkpoint_path="/tmp/nexus-webhook-checkpoints.sqlite",
            workspace_root="/tmp",
            webhook_url="https://example.test/telegram",
            webhook_secret="secret",
        )

        class Registry:
            def __init__(self, **kwargs):
                self.tools = []

            def register(self, tool):
                self.tools.append(tool)

        class Tool:
            pass

        settings_module.get_settings = lambda: settings
        logging_module.configure_logging = lambda _level: None
        migrations.ensure_startup_schema = lambda: {"backend": "sqlite", "source": "test"}
        cli.build_llm_provider = lambda _settings: (object(), "test-llm")
        long_term.LongTermMemory = lambda *_args: object()
        langgraph_checkpoint.get_checkpointer = lambda _path: object()
        graph_module.compile_graph = lambda *_args: object()
        registry_module.ToolRegistry = Registry
        files_module.ReadFileTool = Tool
        files_module.WriteFileTool = Tool
        files_module.ListDirTool = Tool
        bot_app.build_application = lambda *_args: application

        from nexus_ai_agent.api.app import app as api_app

        api_app.router.on_shutdown.append(lambda: mark("asgi.shutdown"))
        cli.run_bot(mode="webhook")
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(repository / "src"), environment.get("PYTHONPATH", "")]
    )
    environment["PORT"] = str(port)
    environment["DASHBOARD_HOST"] = "127.0.0.1"
    environment.pop("NEXUS_DATABASE_URL", None)
    environment.pop("DATABASE_URL", None)
    environment["NEXUS_WEBHOOK_URL"] = "https://example.test/telegram"
    environment["NEXUS_WEBHOOK_SECRET"] = "secret"
    environment["NEXUS_TEST_LIFECYCLE_TRACE"] = str(trace_path)
    environment["PYTHONUNBUFFERED"] = "1"

    process = subprocess.Popen(
        [sys.executable, "-c", child],
        cwd=repository,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        ready = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                body = response.read()
                connection.close()
                if response.status == 200 and body:
                    ready = True
                    break
            except OSError:
                time.sleep(0.05)
        if not ready:
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"webhook subprocess failed to become ready: {stdout}\n{stderr}")

        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(f"webhook subprocess did not shut down: {stdout}\n{stderr}")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == -signal.SIGTERM, f"stdout={stdout}\nstderr={stderr}"
    assert trace_path.read_text(encoding="utf-8").splitlines() == [
        "application.initialize",
        "request.initialize",
        "post_init",
        "application.start",
        "telegram.webhook_registered",
        "asgi.shutdown",
        "application.stop",
        "post_stop",
        "application.shutdown",
        "request.shutdown",
        "runtime.close",
    ]
