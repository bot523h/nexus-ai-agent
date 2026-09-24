"""Observability boundary tests: metrics label hygiene + secret redaction.

The redaction assertions run at two levels:

1. pure-function level — ``redact_secrets`` (structlog/stdlib pipeline)
   and ``redact``/``redact_fields`` (infrastructure lifecycle pipeline);
2. **captured-log level** — records are actually logged through the
   configured stdlib + structlog pipelines and the *rendered output*
   is asserted to contain no raw secrets. This is the proof that the
   redaction lives at the logging boundary, not inside selected
   callers: an ``httpx``-style INFO line or an exception string that
   carries a token must come out masked.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from nexus_ai_agent.infrastructure.observability.metrics import MetricsRegistry
from nexus_ai_agent.infrastructure.observability.redaction import redact, redact_fields
from nexus_ai_agent.observability.logging import configure_logging, redact_secrets

BOT_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawk"
GEMINI_KEY = "AIzaSyA1234567890abcdefghijk"
BEARER_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"

#: (label, text, secrets that must not survive)
REDACTION_CASES = [
    (
        "bare bot token",
        f"auth failed for {BOT_TOKEN}",
        [BOT_TOKEN],
    ),
    (
        "bot token inside telegram API URL",
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        [BOT_TOKEN],
    ),
    (
        "bearer header",
        f"Authorization: Bearer {BEARER_JWT}",
        [BEARER_JWT],
    ),
    (
        "gemini key as query param (httpx INFO line)",
        "HTTP Request: POST https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_KEY} HTTP/1.1",
        [GEMINI_KEY],
    ),
    (
        "token as query param",
        "GET https://example.com/api?token=supersecret123&x=1",
        ["supersecret123"],
    ),
    (
        "x-goog-api-key header",
        f"x-goog-api-key: {GEMINI_KEY}",
        [GEMINI_KEY],
    ),
    (
        "x-goog-api-key inside a dict repr",
        f"{{'x-goog-api-key': '{GEMINI_KEY}'}}",
        [GEMINI_KEY],
    ),
    (
        "userinfo credentials in URL",
        "https://user:supersecretPW@downloads.example.com/file.bin",
        ["supersecretPW"],
    ),
    (
        "api_key in key=value structure",
        f"api_key = '{GEMINI_KEY}'",
        [GEMINI_KEY],
    ),
]


@pytest.mark.parametrize(
    ("label", "text", "secrets"),
    REDACTION_CASES,
    ids=[case[0] for case in REDACTION_CASES],
)
def test_redact_secrets_masks_every_shape(label: str, text: str, secrets: list[str]) -> None:
    out = redact_secrets(text)
    for secret in secrets:
        assert secret not in out, f"{label}: raw secret survived structlog/stdlib redaction"
    assert out != text, f"{label}: nothing was redacted"


@pytest.mark.parametrize(
    ("label", "text", "secrets"),
    REDACTION_CASES,
    ids=[case[0] for case in REDACTION_CASES],
)
def test_infra_redact_masks_every_shape(label: str, text: str, secrets: list[str]) -> None:
    out = redact(text)
    for secret in secrets:
        assert secret not in out, f"{label}: raw secret survived lifecycle redaction"
    assert out != text, f"{label}: nothing was redacted"


def test_redaction_keeps_non_secret_text_usable() -> None:
    """Redaction must not shred ordinary words (no over-redaction)."""
    text = "keyboard layout=colemak hotkey=4 monkey=done ok=1"
    assert redact_secrets(text) == text
    assert redact(text) == text


def test_redact_fields_replaces_sensitive_keys_wholesale() -> None:
    """A value stored under a secret-ish key is dropped even when the
    value itself matches no known secret shape."""
    fields = {
        "api_key": "brand-new-unshaped-secret",
        "keyboard": "layout-1",
        "token": "xyz",
        "url": f"https://api.telegram.org/bot{BOT_TOKEN}/getMe",
    }
    safe = redact_fields(fields)
    assert safe["api_key"] == "[REDACTED]"
    assert safe["token"] == "[REDACTED]"
    assert safe["keyboard"] == "layout-1"
    assert BOT_TOKEN not in safe["url"]


# --------------------------------------------------------------------- #
# Captured-log tests — the rendered output of the real pipelines
# --------------------------------------------------------------------- #


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture()
def captured_root_logs():
    """Configure the real logging pipelines and capture rendered output."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    capture = _Capture()
    root.addHandler(capture)
    try:
        configure_logging(level="INFO")
        yield capture
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_captured_stdlib_log_has_no_raw_secrets(captured_root_logs: _Capture) -> None:
    """httpx-style INFO lines and error strings flow through the stdlib
    root handlers in production — the boundary filter must mask them."""
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.info(
        "HTTP Request: POST https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_KEY} HTTP/1.1"
    )
    httpx_logger.info(f"HTTP Request: GET https://api.telegram.org/bot{BOT_TOKEN}/getMe HTTP/1.1")
    logging.getLogger("urllib3").error(
        "<urlopen error permission denied for https://user:secretPW@internal.example.com/x"
        "?token=supersecret123>"
    )
    rendered = "\n".join(captured_root_logs.lines)
    for secret in (GEMINI_KEY, BOT_TOKEN, "secretPW", "supersecret123"):
        assert secret not in rendered, "raw secret reached a rendered stdlib log record"
    assert "[REDACTED]" in rendered


def test_captured_structlog_event_has_no_raw_secrets(captured_root_logs: _Capture) -> None:
    """structlog events (JSON) with sensitive keys/nested dicts are
    redacted by the processor before rendering."""
    import structlog

    logger = structlog.get_logger("test-redaction-boundary")
    logger.info(
        "outbound_request",
        url=f"https://generativelanguage.googleapis.com/v1beta/models/g:generateContent?key={GEMINI_KEY}",
        api_key=GEMINI_KEY,
        headers={"x-goog-api-key": GEMINI_KEY, "accept": "application/json"},
        keyboard="colemak",
    )
    rendered = "\n".join(captured_root_logs.lines)
    assert GEMINI_KEY not in rendered, "raw API key reached a rendered structlog event"
    assert "[REDACTED]" in rendered
    # non-sensitive fields keep their values — redaction is surgical.
    assert "colemak" in rendered
    assert "application/json" in rendered


def test_structlog_event_redacts_nested_mapping_values() -> None:
    from nexus_ai_agent.observability.logging import _redact_processor

    event = {
        "event": "request",
        "headers": {"x-goog-api-key": GEMINI_KEY, "authorization": f"Bearer {BEARER_JWT}"},
        "list": [f"https://x.example/?key={GEMINI_KEY}"],
    }
    out: dict[str, Any] = _redact_processor(None, "info", dict(event))
    rendered = str(out)
    assert GEMINI_KEY not in rendered
    assert BEARER_JWT not in rendered


# --------------------------------------------------------------------- #
# pre-existing assertions (unchanged)
# --------------------------------------------------------------------- #


def test_redaction_removes_credentials() -> None:
    value = (
        "Bearer abc123 password=secret https://u:p@example.com/x "
        "123456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJK"
    )
    result = redact(value)
    assert "abc123" not in result
    assert "secret" not in result
    assert "u:p" not in result
    assert "REDACTED" in result


def test_metrics_allow_only_low_cardinality_labels() -> None:
    metrics = MetricsRegistry()
    metrics.increment("nexus_touch_failures_total", labels={"backend": "sqlite"})
    assert metrics.snapshot()['nexus_touch_failures_total{backend="sqlite"}'] == 1
    with pytest.raises(ValueError):
        metrics.increment("bad", labels={"thread_id": "secret"})
