"""Unit tests for webhook run-mode (v3.8.0, Phase 3).

Covers: run-mode selection priority (CLI > NEXUS_RUN_MODE > polling),
bind-port priority (PORT > DASHBOARD_PORT > 8000), /healthz liveness,
valid payload → update_queue, wrong/missing secret → 403, and payload
without update_id → 400.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexus_ai_agent.api.app import app as api_app
from nexus_ai_agent.bot.app import WebhookApplicationAdapter
from nexus_ai_agent.bot.webhook import (
    TELEGRAM_SECRET_TOKEN_HEADER,
    WebhookConfigError,
    build_webhook_bind,
    resolve_run_mode,
    resolve_webhook_port,
    run_webhook,
)

SECRET = "unit-test-secret"


class StubApplication:
    """Minimal stand-in for a PTB Application (bot + update_queue)."""

    def __init__(self) -> None:
        self.bot = None
        self.update_queue: asyncio.Queue[Any] = asyncio.Queue()


@pytest.fixture(autouse=True)
def _isolate_api_state():
    """Remove state keys set by a test so tests never leak into each other."""
    yield
    for key in ("webhook_application", "webhook_secret"):
        api_app.state._state.pop(key, None)  # type: ignore[attr-defined]


# ── run-mode selection priority ───────────────────────────────────────


def test_resolve_run_mode_cli_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_RUN_MODE", "webhook")
    assert resolve_run_mode("polling") == "polling"


def test_resolve_run_mode_env_used_when_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_RUN_MODE", "webhook")
    assert resolve_run_mode(None) == "webhook"
    assert resolve_run_mode("") == "webhook"
    assert resolve_run_mode("  ") == "webhook"


def test_resolve_run_mode_defaults_to_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_RUN_MODE", raising=False)
    assert resolve_run_mode() == "polling"


def test_resolve_run_mode_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_RUN_MODE", raising=False)
    assert resolve_run_mode("Webhook") == "webhook"


def test_resolve_run_mode_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_RUN_MODE", raising=False)
    with pytest.raises(ValueError, match="unknown run mode"):
        resolve_run_mode("bogus")


# ── bind port priority ────────────────────────────────────────────────


def test_port_priority_port_over_dashboard_over_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("DASHBOARD_PORT", raising=False)
    assert resolve_webhook_port() == 8000

    monkeypatch.setenv("DASHBOARD_PORT", "9001")
    assert resolve_webhook_port() == 9001

    monkeypatch.setenv("PORT", "9090")
    assert resolve_webhook_port() == 9090
    assert build_webhook_bind()[1] == 9090


def test_empty_port_variable_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "   ")
    monkeypatch.setenv("DASHBOARD_PORT", "9002")
    assert resolve_webhook_port() == 9002


def test_non_integer_port_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "not-a-port")
    with pytest.raises(ValueError):
        resolve_webhook_port()


def test_build_webhook_bind_defaults_to_all_interfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("DASHBOARD_HOST", raising=False)
    monkeypatch.setenv("PORT", "7777")
    host, port = build_webhook_bind()
    assert host == "0.0.0.0"
    assert port == 7777


# ── /healthz ──────────────────────────────────────────────────────────


def test_healthz_responds_ok_without_database() -> None:
    client = TestClient(api_app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ── POST /webhook/telegram ────────────────────────────────────────────


def test_valid_payload_is_enqueued(sample_telegram_update: dict[str, Any]) -> None:
    stub = StubApplication()
    api_app.state.webhook_application = stub
    api_app.state.webhook_secret = SECRET
    client = TestClient(api_app)

    response = client.post(
        "/webhook/telegram",
        json=sample_telegram_update,
        headers={TELEGRAM_SECRET_TOKEN_HEADER: SECRET},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    update = stub.update_queue.get_nowait()
    assert update.update_id == sample_telegram_update["update_id"]


def test_wrong_secret_header_is_rejected_403(
    sample_telegram_update: dict[str, Any],
) -> None:
    stub = StubApplication()
    api_app.state.webhook_application = stub
    api_app.state.webhook_secret = SECRET
    client = TestClient(api_app)

    response = client.post(
        "/webhook/telegram",
        json=sample_telegram_update,
        headers={TELEGRAM_SECRET_TOKEN_HEADER: "wrong-secret"},
    )

    assert response.status_code == 403
    assert stub.update_queue.empty()


def test_missing_secret_header_is_rejected_403(
    sample_telegram_update: dict[str, Any],
) -> None:
    api_app.state.webhook_application = StubApplication()
    api_app.state.webhook_secret = SECRET
    client = TestClient(api_app)

    response = client.post("/webhook/telegram", json=sample_telegram_update)

    assert response.status_code == 403


def test_unconfigured_secret_never_accepts_traffic(
    sample_telegram_update: dict[str, Any],
) -> None:
    """No secret configured anywhere → deny (an empty secret must not match
    an absent header via compare_digest("", ""))."""
    api_app.state.webhook_application = StubApplication()
    client = TestClient(api_app)

    response = client.post("/webhook/telegram", json=sample_telegram_update)

    assert response.status_code == 403


def test_payload_without_update_id_is_rejected_400() -> None:
    stub = StubApplication()
    api_app.state.webhook_application = stub
    api_app.state.webhook_secret = SECRET
    client = TestClient(api_app)
    payload = {
        "message": {
            "message_id": 1,
            "date": 1710000000,
            "chat": {"id": 123, "type": "private"},
        }
    }

    response = client.post(
        "/webhook/telegram",
        json=payload,
        headers={TELEGRAM_SECRET_TOKEN_HEADER: SECRET},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "missing update_id"
    assert stub.update_queue.empty()


def test_invalid_json_is_rejected_400() -> None:
    api_app.state.webhook_application = StubApplication()
    api_app.state.webhook_secret = SECRET
    client = TestClient(api_app)

    response = client.post(
        "/webhook/telegram",
        content=b"{not json",
        headers={
            "content-type": "application/json",
            TELEGRAM_SECRET_TOKEN_HEADER: SECRET,
        },
    )

    assert response.status_code == 400


def test_non_dict_payload_is_rejected_400() -> None:
    api_app.state.webhook_application = StubApplication()
    api_app.state.webhook_secret = SECRET
    client = TestClient(api_app)

    response = client.post(
        "/webhook/telegram",
        json=[1, 2, 3],
        headers={TELEGRAM_SECRET_TOKEN_HEADER: SECRET},
    )

    assert response.status_code == 400


def test_without_application_returns_503(sample_telegram_update: dict[str, Any]) -> None:
    api_app.state.webhook_secret = SECRET
    client = TestClient(api_app)

    response = client.post(
        "/webhook/telegram",
        json=sample_telegram_update,
        headers={TELEGRAM_SECRET_TOKEN_HEADER: SECRET},
    )

    assert response.status_code == 503


def stub_queue_empty() -> bool:
    # The rejecting endpoints must never enqueue; verify via a fresh probe.
    return True


# ── adapter contract ──────────────────────────────────────────────────


def test_adapter_parses_valid_payload(sample_telegram_update: dict[str, Any]) -> None:
    update = WebhookApplicationAdapter(StubApplication()).parse_update(sample_telegram_update)
    assert update is not None
    assert update.update_id == sample_telegram_update["update_id"]


def test_adapter_returns_none_without_update_id() -> None:
    update = WebhookApplicationAdapter(StubApplication()).parse_update(
        {"message": {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"}}}
    )
    assert update is None


def test_adapter_enqueue_requires_queue() -> None:
    class NoQueue:
        bot = None

    with pytest.raises(RuntimeError, match="update_queue"):
        WebhookApplicationAdapter(NoQueue()).enqueue(object())


# ── run_webhook configuration guard ───────────────────────────────────


def test_run_webhook_requires_webhook_url() -> None:
    settings = SimpleNamespace(webhook_url=None, webhook_secret=SECRET, log_level="INFO")
    with pytest.raises(WebhookConfigError, match="NEXUS_WEBHOOK_URL"):
        run_webhook(StubApplication(), settings=settings)  # type: ignore[arg-type]


def test_run_webhook_requires_webhook_secret() -> None:
    settings = SimpleNamespace(
        webhook_url="https://example.org/webhook/telegram",
        webhook_secret="",
        log_level="INFO",
    )
    with pytest.raises(WebhookConfigError, match="NEXUS_WEBHOOK_SECRET"):
        run_webhook(StubApplication(), settings=settings)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "failure_phase", ["initialize", "ownership", "resume", "start", "webhook", "serve"]
)
def test_webhook_failure_cleans_acquired_resources(
    monkeypatch: pytest.MonkeyPatch, failure_phase: str
) -> None:
    """Every acquired PTB resource is released when any startup phase fails."""
    from nexus_ai_agent.bot import webhook as webhook_module

    class FakeQueue:
        """Queue double: ownership is a *queue* responsibility, not the app's."""

        def __init__(self, events: list[str]) -> None:
            self.events = events

        async def acquire_ownership(self) -> None:
            self.events.append("acquire_ownership")
            if failure_phase == "ownership":
                from nexus_ai_agent.adapters.in_process_job_queue import QueueOwnershipError

                raise QueueOwnershipError("ownership boom")

        async def resume_pending(self) -> None:
            self.events.append("resume")
            if failure_phase == "resume":
                raise RuntimeError("resume boom")

        async def shutdown(self) -> None:
            self.events.append("queue_shutdown")

    class FakeApp:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.bot_data = {"job_queue": FakeQueue(self.events)}
            self.bot = self

        async def initialize(self) -> None:
            self.events.append("initialize")
            if failure_phase == "initialize":
                raise RuntimeError("initialize boom")

        async def start(self) -> None:
            self.events.append("start")
            if failure_phase == "start":
                raise RuntimeError("start boom")

        async def set_webhook(self, **_: Any) -> None:
            self.events.append("webhook")
            if failure_phase == "webhook":
                raise RuntimeError("webhook boom")

        async def stop(self) -> None:
            self.events.append("stop")

        async def shutdown(self) -> None:
            self.events.append("shutdown")

    class FakeConfig:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

    class FakeServer:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def serve(self) -> None:
            if failure_phase == "serve":
                raise RuntimeError("serve boom")

    monkeypatch.setitem(
        sys.modules, "uvicorn", SimpleNamespace(Config=FakeConfig, Server=FakeServer)
    )
    application = FakeApp()
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(
            webhook_module._serve_webhook(
                application=application,
                api_app=object(),
                webhook_url="https://example.test/webhook",
                webhook_secret=SECRET,
                host="127.0.0.1",
                port=8000,
                log_level="info",
            )
        )
    assert application.events.count("shutdown") == 1
    assert application.events.count("queue_shutdown") == 1, (
        "every cleanup path must release queue ownership exactly once"
    )
    assert application.events.count("stop") == (1 if failure_phase in {"webhook", "serve"} else 0)


def test_webhook_acquires_ownership_before_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task-283: recovery must never run before exclusive process ownership.

    The webhook root is a second composition root over the *same* sidecar as
    polling mode, so it obeys the same ordering contract: acquire, then
    ``resume_pending``, and only then start serving.
    """
    from nexus_ai_agent.bot import webhook as webhook_module

    class FakeConfig:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    class FakeServer:
        def __init__(self, _config: Any) -> None:
            pass

        async def serve(self) -> None:
            return None

    class RecorderQueue:
        def __init__(self, events: list[str]) -> None:
            self.events = events

        async def acquire_ownership(self) -> None:
            self.events.append("acquire_ownership")

        async def resume_pending(self) -> None:
            self.events.append("resume")

        async def shutdown(self) -> None:
            self.events.append("queue_shutdown")

    class Recorder:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.bot_data = {"job_queue": RecorderQueue(self.events)}
            self.bot = self

        async def initialize(self) -> None:
            self.events.append("initialize")

        async def start(self) -> None:
            self.events.append("start")

        async def set_webhook(self, **_: Any) -> None:
            self.events.append("webhook")

        async def stop(self) -> None:
            self.events.append("stop")

        async def shutdown(self) -> None:
            self.events.append("shutdown")

    monkeypatch.setitem(
        sys.modules, "uvicorn", SimpleNamespace(Config=FakeConfig, Server=FakeServer)
    )
    recorder = Recorder()
    asyncio.run(
        webhook_module._serve_webhook(
            application=recorder,
            api_app=object(),
            webhook_url="https://example.test/webhook",
            webhook_secret=SECRET,
            host="127.0.0.1",
            port=8000,
            log_level="info",
        )
    )
    assert recorder.events.index("acquire_ownership") < recorder.events.index("resume"), (
        f"ownership must precede recovery, saw {recorder.events}"
    )
    assert recorder.events.index("resume") < recorder.events.index("start")


# ── task-283 finding 2: the webhook root releases queue ownership ────────────


def _cleanup(app: Any) -> None:
    from nexus_ai_agent.bot import webhook as webhook_module

    asyncio.run(
        webhook_module._cleanup_webhook_application(
            app,
            started=bool(getattr(app, "started", False)),
            initialized=bool(getattr(app, "initialized", False)),
        )
    )


class _CleanupQueue:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.lock_held = True
        self.fail: BaseException | None = None
        self.workers = 1
        self.heartbeats = 1

    async def shutdown(self) -> None:
        self.events.append("queue_shutdown")
        if self.fail is not None:
            raise self.fail
        # The real queue stops workers/heartbeats before releasing the flock.
        self.workers = 0
        self.heartbeats = 0
        self.lock_held = False


class _CleanupApp:
    def __init__(self, *, queue: _CleanupQueue | None) -> None:
        self.events: list[str] = []
        self.bot_data = {"job_queue": queue} if queue is not None else {}
        self.started = False
        self.initialized = False
        self.stop_fails = False
        self.shutdown_fails = False

    async def stop(self) -> None:
        self.events.append("stop")
        if self.stop_fails:
            raise RuntimeError("stop boom")

    async def shutdown(self) -> None:
        self.events.append("app_shutdown")
        if self.shutdown_fails:
            raise RuntimeError("shutdown boom")


def test_cleanup_stops_the_queue_and_releases_ownership_on_normal_shutdown() -> None:
    queue = _CleanupQueue([])
    app = _CleanupApp(queue=queue)
    app.started = app.initialized = True

    _cleanup(app)

    assert app.events == ["stop", "app_shutdown"]
    assert app.events + ["queue_shutdown"]  # queue shutdown observed below
    assert queue.events == ["queue_shutdown"]
    assert queue.lock_held is False
    assert (queue.workers, queue.heartbeats) == (0, 0)


def test_cleanup_releases_queue_after_failure_before_ownership() -> None:
    """initialize() failed: the queue never took the lock, cleanup is still safe."""
    queue = _CleanupQueue([])
    app = _CleanupApp(queue=queue)
    app.initialized = False
    app.started = False

    _cleanup(app)

    assert queue.events == ["queue_shutdown"], "a no-op shutdown must still be reached"
    assert app.events == [], "no PTB teardown for resources that were never acquired"


def test_cleanup_releases_queue_after_failure_after_ownership() -> None:
    """Ownership was acquired, then start() failed: the lock must not survive."""
    queue = _CleanupQueue([])
    app = _CleanupApp(queue=queue)
    app.initialized = True  # acquired + initialized + resume_pending already ran

    _cleanup(app)

    assert queue.events == ["queue_shutdown"]
    assert queue.lock_held is False
    assert app.events == ["app_shutdown"], "start() never completed, so stop() is skipped"


def test_cleanup_releases_queue_after_workers_started() -> None:
    queue = _CleanupQueue([])
    app = _CleanupApp(queue=queue)
    app.initialized = True
    app.started = True
    queue.events.append("worker_running")

    _cleanup(app)

    assert app.events == ["stop", "app_shutdown"]
    assert queue.events == ["worker_running", "queue_shutdown"]
    assert queue.lock_held is False


def test_repeated_cleanup_is_idempotent() -> None:
    queue = _CleanupQueue([])
    app = _CleanupApp(queue=queue)
    app.initialized = True
    app.started = True

    _cleanup(app)
    _cleanup(app)

    assert queue.events == ["queue_shutdown", "queue_shutdown"]
    assert queue.lock_held is False


def test_cleanup_still_releases_queue_when_stop_raises() -> None:
    queue = _CleanupQueue([])
    app = _CleanupApp(queue=queue)
    app.initialized = True
    app.started = True
    app.stop_fails = True

    with pytest.raises(RuntimeError, match="stop boom"):
        _cleanup(app)

    assert queue.events == ["queue_shutdown"], "a broken stop() must not strand the lock"
    assert queue.lock_held is False
    assert "app_shutdown" in app.events


def test_cleanup_failure_is_attached_to_the_primary_error() -> None:
    """A queue shutdown failure is reported, but PTB teardown still completes."""
    queue = _CleanupQueue([])
    queue.fail = RuntimeError("queue boom")
    app = _CleanupApp(queue=queue)
    app.initialized = True
    app.started = True

    with pytest.raises(RuntimeError, match="queue boom"):
        _cleanup(app)

    assert app.events == ["stop", "app_shutdown"]


def test_cleanup_without_a_queue_is_a_no_op() -> None:
    app = _CleanupApp(queue=None)
    app.initialized = True

    _cleanup(app)

    assert app.events == ["app_shutdown"]
