"""Structured logging with secret redaction.

``configure_logging`` installs a redaction stage into both the structlog
pipeline and the stdlib root handlers, so bot tokens, API keys, and
``Authorization``/``Bearer`` headers never reach the logs — even when a
caller logs an object that accidentally carries them.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import structlog

#: Telegram bot token shape: "<bot id>:<35-ish secret chars>".
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,10}:[A-Za-z0-9_-]{30,}\b")
#: Bot token embedded in Telegram API URL: https://api.telegram.org/bot<TOKEN>/
_BOT_URL_TOKEN_RE = re.compile(r"(?i)(https://api\.telegram\.org/bot)\d{6,10}:[A-Za-z0-9_-]{30,}")
#: "Bearer <token>" in headers/strings.
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]{8,}={0,2}")
#: key=value / key: value / "key": "value" for secret-ish key names.
#: A value starting with ``%`` is a logging format specifier (e.g. the
#: ``token=%s`` in ``logger.warning("token=%s", ...)``) — never redact it.
_KEY_VALUE_RE = re.compile(
    r"(?i)(\"?(?:api[_-]?key|access[_-]?key(?:_id)?|secret(?:_key)?|token|"
    r"password|authorization)\"?\s*[:=]\s*)(\"[^\\\"]*\"|'[^']*'|(?![%])[^\s,;}&]+)"
)
#: Query-string key leakage (?key= , &key=, ?token= …) — bare `key` must be
#: caught even when no `api_` prefix (Gemini `?key=AIza...`). Keep the
#: delimiter, redact the value.
_QUERY_KEY_RE = re.compile(r"(?i)([?&](?:key|api[_-]?key|token|secret|password)=)[^&\s]+")
#: Header leakage for Gemini: x-goog-api-key: <value>
_GOOGLE_API_KEY_RE = re.compile(r"(?i)(x-goog-api-key\s*[:=]\s*)[^\s,;\"'}]+")
_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(token|secret|password|api_?key|access_?key|authorization|bearer|credential|key)"
)


def _redact_urls(text: str) -> str:
    """Strip user:pass credentials from URLs (mirrors infra redaction)."""

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parts = urlsplit(raw)
            if parts.username or parts.password:
                host = parts.hostname or "unknown-host"
                if parts.port:
                    host += f":{parts.port}"
                # preserve redacted query already handled by _QUERY_KEY_RE
                return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
        except ValueError:
            pass
        return raw

    return re.sub(r"https?://[^\s]+", replace, text)


def redact_secrets(text: str) -> str:
    """Mask known secret shapes inside *text*."""
    redacted = _BOT_URL_TOKEN_RE.sub(r"\1" + _REDACTED, text)
    redacted = _TELEGRAM_TOKEN_RE.sub(_REDACTED, redacted)
    redacted = _BEARER_RE.sub(r"\1 " + _REDACTED, redacted)
    redacted = _GOOGLE_API_KEY_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", redacted)
    redacted = _QUERY_KEY_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", redacted)
    redacted = _KEY_VALUE_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", redacted)
    redacted = _redact_urls(redacted)
    return redacted


def _redact_processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: redact event text and secret-ish key values."""
    for key, value in list(event_dict.items()):
        if key == "event":
            continue
        if isinstance(value, str) and _SENSITIVE_KEY_RE.search(key):
            event_dict[key] = _REDACTED
        elif isinstance(value, str):
            event_dict[key] = redact_secrets(value)
    event = event_dict.get("event")
    if isinstance(event, str):
        event_dict["event"] = redact_secrets(event)
    return event_dict


class SecretRedactionFilter(logging.Filter):
    """stdlib filter: redact the rendered message of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(str(record.msg))
        if record.args:
            record.args = tuple(
                redact_secrets(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        return True


def configure_logging(level: str = "INFO", *, redact: bool = True) -> None:
    """
    Configure stdlib logging + structlog for JSON output.

    Correlation IDs are supported via structlog.contextvars. Bind them with:
      structlog.contextvars.bind_contextvars(correlation_id="...")

    With ``redact=True`` (default) known secret shapes are masked in both
    structlog events and plain stdlib records.
    """

    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")
    if redact:
        redaction_filter = SecretRedactionFilter()
        for handler in logging.getLogger().handlers:
            if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
                handler.addFilter(redaction_filter)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if redact:
        processors.append(_redact_processor)
    processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)


__all__ = [
    "SecretRedactionFilter",
    "configure_logging",
    "get_logger",
    "redact_secrets",
]
