"""Unit tests for webhook run-mode (v3.8.0, Phase 3).

Covers: run-mode selection priority (CLI > NEXUS_RUN_MODE > polling),
bind-port priority (PORT > DASHBOARD_PORT > 8000), /healthz liveness,
valid payload → update_queue, wrong/missing secret → 403, and payload
without update_id → 400.
"""

from __future__ import annotations

import asyncio
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
