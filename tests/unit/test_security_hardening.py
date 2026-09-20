"""Security hardening tests: CORS lock-down, HMAC endpoint auth, redaction.

Covers the hardening of the dashboard API surface:

* CORS is an explicit allowlist (empty by default) — never ``*`` with
  credentials.
* ``POST /creative/video-edit`` verifies an HMAC-SHA256 request signature
  when ``NEXUS_API_HMAC_KEY`` is set (constant-time, ±300 s freshness).
* The Telegram webhook stays fail-closed on secret mismatch.
* Logs never carry raw bot tokens / bearer / key-value secrets.
"""

from __future__ import annotations

import logging
import time

import pytest
from fastapi.testclient import TestClient

from nexus_ai_agent.api.app import parse_cors_origins
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.observability.logging import (
    SecretRedactionFilter,
    configure_logging,
    redact_secrets,
)

FAKE_BOT_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefg"


@pytest.fixture()
def _fresh_settings_cache():
    """Keep the settings cache clean around env-mutating tests."""
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


@pytest.fixture()
def api_client(_fresh_settings_cache) -> TestClient:
    from nexus_ai_agent.api.app import app

    return TestClient(app)


# ── CORS ────────────────────────────────────────────────────────────────


def test_parse_cors_origins_splits_and_strips() -> None:
    assert parse_cors_origins("https://a.com, https://b.org ,") == [
        "https://a.com",
        "https://b.org",
    ]
    assert parse_cors_origins("") == []
    assert parse_cors_origins("  ") == []


def test_default_cors_grants_no_cross_origin_access(_fresh_settings_cache) -> None:
    from nexus_ai_agent.api.app import app

    client = TestClient(app)
    preflight = client.options(
        "/stats",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight.headers.get("access-control-allow-origin") is None
    plain = client.get("/stats", headers={"Origin": "https://evil.example"})
    assert plain.headers.get("access-control-allow-origin") is None


def test_configured_cors_allows_only_listed_origins(_fresh_settings_cache) -> None:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    # The real app evaluates CORS once at import; the parsing+middleware
    # wiring it uses is exercised here against a fresh app.
    origins = parse_cors_origins("https://good.example")
    probe = FastAPI()
    probe.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=bool(origins),
        allow_methods=["GET", "POST", "OPTIONS"],
    )

    @probe.get("/x")
    async def _x() -> dict[str, str]:
        return {"ok": "1"}

    client = TestClient(probe)
    allowed = client.get("/x", headers={"Origin": "https://good.example"})
    assert allowed.headers.get("access-control-allow-origin") == "https://good.example"
    denied = client.get("/x", headers={"Origin": "https://evil.example"})
    assert denied.headers.get("access-control-allow-origin") is None


# ── HMAC endpoint auth ──────────────────────────────────────────────────


def _signed_headers(key: str, timestamp: str, body: bytes) -> dict[str, str]:
    import hashlib
    import hmac

    message = f"{timestamp}:".encode() + body
    signature = hmac.new(key.encode(), message, hashlib.sha256).hexdigest()
    return {"X-NEXUS-Timestamp": timestamp, "X-NEXUS-Signature": signature}


def test_hmac_missing_or_wrong_signature_rejected(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_API_HMAC_KEY", "test-key")
    settings_module.get_settings.cache_clear()

    missing = api_client.post("/creative/video-edit", data={})
    assert missing.status_code == 401
    assert "signature" in missing.json()["detail"]

    stale_ts = str(int(time.time()) - 4000)
    stale = api_client.post(
        "/creative/video-edit", data={}, headers=_signed_headers("test-key", stale_ts, b"")
    )
    assert stale.status_code == 401
    assert "stale" in stale.json()["detail"]

    now = str(int(time.time()))
    wrong = api_client.post(
        "/creative/video-edit",
        data={},
        headers=_signed_headers("attacker-key", now, b""),
    )
    assert wrong.status_code == 401
    assert "invalid signature" in wrong.json()["detail"]


def test_hmac_valid_signature_reaches_handler(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_API_HMAC_KEY", "test-key")
    settings_module.get_settings.cache_clear()

    body = b""
    headers = _signed_headers("test-key", str(int(time.time())), body)
    # The signature gates the endpoint; an empty form is then rejected by
    # normal request validation (400) — proving the handler was reached.
    ok = api_client.post("/creative/video-edit", content=body, headers=headers)
    assert ok.status_code == 400
    assert "Provide either file or video_url" in ok.json()["detail"]


def test_no_hmac_key_keeps_legacy_behaviour_and_warns(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import nexus_ai_agent.api.app as app_module

    monkeypatch.delenv("NEXUS_API_HMAC_KEY", raising=False)
    settings_module.get_settings.cache_clear()
    app_module._hmac_warning_emitted = False

    with caplog.at_level(logging.WARNING):
        response = api_client.post("/creative/video-edit", data={})

    assert response.status_code == 400  # reached handler validation
    assert any("NEXUS_API_HMAC_KEY" in record.message for record in caplog.records)


# ── Telegram webhook stays fail-closed ─────────────────────────────────


def test_webhook_rejects_wrong_or_missing_secret(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_ai_agent.bot.webhook import TELEGRAM_SECRET_TOKEN_HEADER

    monkeypatch.delenv("NEXUS_WEBHOOK_SECRET", raising=False)
    settings_module.get_settings.cache_clear()
    no_secret = api_client.post(
        "/webhook/telegram", json={"update_id": 1}, headers={TELEGRAM_SECRET_TOKEN_HEADER: "x"}
    )
    assert no_secret.status_code == 403

    monkeypatch.setenv("NEXUS_WEBHOOK_SECRET", "real-secret")
    settings_module.get_settings.cache_clear()
    wrong = api_client.post(
        "/webhook/telegram",
        json={"update_id": 1},
        headers={TELEGRAM_SECRET_TOKEN_HEADER: "wrong"},
    )
    assert wrong.status_code == 403

    correct = api_client.post(
        "/webhook/telegram",
        json={"update_id": 1},
        headers={TELEGRAM_SECRET_TOKEN_HEADER: "real-secret"},
    )
    # Auth passed → the request now fails on "application not ready" (503),
    # never on the secret.
    assert correct.status_code == 503


# ── Log redaction ───────────────────────────────────────────────────────


def test_redact_secrets_masks_known_shapes() -> None:
    text = f"started bot with {FAKE_BOT_TOKEN} ok"
    assert FAKE_BOT_TOKEN not in redact_secrets(text)
    assert "[REDACTED]" in redact_secrets(text)

    assert "Bearer supersecret123" not in redact_secrets("auth: Bearer supersecret123")
    redacted_kv = redact_secrets('{"api_key": "hunter2", "token": "abc123456"}')
    assert "hunter2" not in redacted_kv
    assert "abc123456" not in redacted_kv


def test_structlog_events_are_redacted(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging("INFO")

    from nexus_ai_agent.observability.logging import get_logger

    with caplog.at_level(logging.INFO):
        get_logger("redaction.test").info("bot started", bot_token=FAKE_BOT_TOKEN)

    assert FAKE_BOT_TOKEN not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_stdlib_records_are_redacted() -> None:
    import io

    configure_logging("INFO")

    # configure_logging must have wired the filter into the root handlers.
    root = logging.getLogger()
    assert any(
        isinstance(f, SecretRedactionFilter) for handler in root.handlers for f in handler.filters
    )

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactionFilter())
    logger = logging.getLogger("plain.redaction.direct")
    logger.handlers = [handler]
    logger.propagate = False
    logger.warning("token=%s", FAKE_BOT_TOKEN)

    output = stream.getvalue()
    assert FAKE_BOT_TOKEN not in output
    assert "[REDACTED]" in output


def test_redaction_filter_handles_plain_records() -> None:
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="connect %s",
        args=(f"Bearer {FAKE_BOT_TOKEN}",),
        exc_info=None,
    )
    assert SecretRedactionFilter().filter(record) is True
    rendered = record.getMessage()
    assert FAKE_BOT_TOKEN not in rendered
    assert "[REDACTED]" in rendered
