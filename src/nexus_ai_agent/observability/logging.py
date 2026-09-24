"""Structured logging with secret redaction.

``configure_logging`` installs a redaction stage into both the structlog
pipeline and the stdlib root handlers, so bot tokens, API keys, and
``Authorization``/``Bearer`` headers never reach the logs — even when a
caller logs an object that accidentally carries them.

The redaction runs at the *logging boundary* (stdlib filter +
structlog processor), not inside selected callers, so every surface
that renders a record — ``httpx``'s ``HTTP Request: …`` INFO lines,
exception tracebacks, JSON events — sees only masked values:

* bare Telegram bot tokens ``<id>:<secret>`` and the API-URL form
  ``https://api.telegram.org/bot<token>/…``;
* ``Bearer`` tokens and ``x-goog-api-key`` headers;
* query-parameter secrets ``?key=…``, ``?token=…`` (``&``-form too);
* ``user:password`` userinfo inside URLs;
* values stored under secret-ish keys (``api_key``, ``token``, …) in
  event dicts, including one level of nested structures.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import structlog

#: Telegram bot token shape: "<bot id>:<35-ish secret chars>".
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,10}:[A-Za-z0-9_-]{30,}\b")
#: Bot token embedded in a Telegram API URL. The ``bot`` prefix destroys
#: the ``\b`` word boundary of the bare-token pattern, so this shape
#: needs its own rule: https://api.telegram.org/bot<TOKEN>/sendMessage
_BOT_URL_TOKEN_RE = re.compile(r"(?i)(https?://api\.telegram\.org/bot)\d{6,10}:[A-Za-z0-9_-]{30,}")
#: "Bearer <token>" in headers/strings.
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]{8,}={0,2}")
#: ``Basic`` scheme credentials (RFC 7617): ``Basic <base64-credentials>``.
#: The candidate token must look credential-SHAPED — at least 14 chars of
#: the base64 alphabet, mixed case, plus a digit or ``=`` padding (checked
#: in ``_basic_repl``) — so prose such as ``basic settings here`` or
#: ``Basic Authentication flow`` is never redacted.
_BASIC_RE = re.compile(r"(?i)\b(basic)\s+([A-Za-z0-9+/=_.-]{14,})")
#: Gemini-style API-key header: x-goog-api-key: <secret> (also matches
#: the ``"x-goog-api-key": "…"`` form produced by dict reprs).
_GOOGLE_API_KEY_RE = re.compile(r"(?i)(x-goog-api-key[\"']?\s*[:=]\s*[\"']?)(?!%)[^\s,;}'\"]+")
#: Query-string secrets: ?key= , &api_key= , ?token= … — bare ``key``
#: must be caught even without an ``api`` prefix (Gemini ?key=AIza…).
_QUERY_KEY_RE = re.compile(
    r"(?i)([?&](?:key|api[_-]?key|access[_-]?key|token|secret|password)=)(?!%)[^&\s]+"
)
#: key=value / key: value / "key": "value" for secret-ish key names.
#: A value starting with ``%`` is a logging format specifier (e.g. the
#: ``token=%s`` in ``logger.warning("token=%s", ...)``) — never redact it.
_KEY_VALUE_RE = re.compile(
    r"(?i)(\"?(?:api[_-]?key|access[_-]?key(?:_id)?|secret(?:_key)?|token|"
    r"password|authorization)[\"\\']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|(?![%])[^\s,;}&]+)"
)
_REDACTED = "[REDACTED]"
#: Event-dict keys whose *values* are secret-ish. ``key``/``token``/…
#: must match as a whole ``_``/``-``-separated segment so names like
#: ``keyboard`` or ``hotkey`` are not over-redacted.
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:^|[_\-\s])(?:key|keys|token|tokens|secret|secrets|password|passwords|"
    r"credential|credentials|authorization|authorisation|bearer|api[_-]?key|api[_-]?keys|"
    r"access[_-]?key|access[_-]?keys)(?:$|[_\-\s])"
)


def _basic_repl(match: re.Match[str]) -> str:
    """Redact ``Basic <token>`` only when the token is credential-shaped."""
    token = match.group(2)
    if (
        any(c.isupper() for c in token)
        and any(c.islower() for c in token)
        and any(c.isdigit() or c == "=" for c in token)
    ):
        return f"{match.group(1)} {_REDACTED}"
    return match.group(0)


def redact_secrets(text: str) -> str:
    """Mask known secret shapes inside *text*."""
    redacted = _BOT_URL_TOKEN_RE.sub(r"\1" + _REDACTED, text)
    redacted = _TELEGRAM_TOKEN_RE.sub(_REDACTED, redacted)
    redacted = _BEARER_RE.sub(r"\1 " + _REDACTED, redacted)
    redacted = _BASIC_RE.sub(_basic_repl, redacted)
    redacted = _GOOGLE_API_KEY_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", redacted)
    redacted = _QUERY_KEY_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", redacted)
    redacted = _KEY_VALUE_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", redacted)
    return _redact_urls(redacted)


def _redact_urls(text: str) -> str:
    """Strip ``user:password`` credentials from URLs (any host)."""

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parts = urlsplit(raw)
            if parts.username or parts.password:
                host = parts.hostname or "unknown-host"
                if parts.port:
                    host += f":{parts.port}"
                return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
        except ValueError:
            pass
        return raw

    return re.sub(r"https?://[^\s]+", replace, text)


def _redact_value(value: Any) -> Any:
    """Recursively redact a structlog event value.

    Strings go through :func:`redact_secrets`; mappings redact the
    values stored under secret-ish keys outright and recurse into the
    rest (one cheap defence level for accidentally logged ``headers``
    dicts); lists/tuples recurse element-wise.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        return {
            key: (
                _REDACTED
                if _SENSITIVE_KEY_RE.search(str(key)) and isinstance(val, str)
                else _redact_value(val)
            )
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def _redact_processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: redact event text and secret-ish key values."""
    for key, value in list(event_dict.items()):
        if key == "event":
            continue
        if isinstance(value, str) and _SENSITIVE_KEY_RE.search(key):
            event_dict[key] = _REDACTED
        else:
            event_dict[key] = _redact_value(value)
    event = event_dict.get("event")
    if isinstance(event, str):
        event_dict["event"] = redact_secrets(event)
    return event_dict


class SecretRedactionFilter(logging.Filter):
    """stdlib filter: redact a record before *any* rendering happens.

    stdlib renders ``msg % args`` and ``exc_info`` lazily, at handler
    emit time — *after* filters run — so the filter must normalise every
    shape a record can carry, or the secret reaches the final line raw:

    * ``msg`` — masked through :func:`redact_secrets`;
    * ``args`` — a single *mapping* (a documented stdlib shape:
      ``logger.warning("%(password)s", {...})``) keeps its mapping type
      (destroying it into a tuple would raise ``TypeError: format
      requires a mapping`` at emit) with key-aware value redaction;
      every other arg sequence is walked element-wise through
      :func:`_redact_value` so nested dicts/lists/tuples are masked
      before ``%``-rendering;
    * ``exc_info`` — the traceback is formatted *now* (through stdlib's
      own formatter) and stored as ``exc_text``; ``Formatter.format``
      reuses pre-computed ``exc_text`` verbatim, so the redacted text is
      what actually reaches the handler;
    * ``stack_info`` — same lazy-rendering hole as tracebacks.
    """

    # Module-level formatter reused only for its formatException logic so
    # the stored text is byte-identical to what stdlib would render.
    _exc_formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(str(record.msg))
        args = record.args
        if isinstance(args, Mapping):
            # Key-aware: a value stored under a secret-ish key is replaced
            # wholesale (it may be a fresh credential matching no known
            # shape); everything else recurses.
            record.args = {
                key: (
                    _REDACTED
                    if isinstance(value, str) and _SENSITIVE_KEY_RE.search(str(key))
                    else _redact_value(value)
                )
                for key, value in args.items()
            }
        elif args:
            record.args = tuple(_redact_value(arg) for arg in args)
        if record.exc_info and not record.exc_text:
            record.exc_text = redact_secrets(
                self._exc_formatter.formatException(record.exc_info)
            )
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info)
        return True


class RedactingFormatter(logging.Formatter):
    """Last-resort boundary: redact the *fully rendered* line.

    ``SecretRedactionFilter`` normalises the record before rendering,
    but a render itself can still surface secrets from shapes the filter
    cannot rewrite in place (e.g. the ``repr()`` of an arbitrary object
    passed as a ``%s`` arg).  Wrapping the handler's formatter — the one
    boundary every stdlib line passes through — masks those residuals.
    The wrapped formatter's format/datefmt/style are preserved.
    """

    def __init__(self, base: logging.Formatter | None) -> None:
        base_style = getattr(base, "_style", None)
        style_char = getattr(base_style, "style_char", "%") if base_style is not None else "%"
        super().__init__(
            fmt=getattr(base, "_fmt", None) or "%(message)s",
            datefmt=getattr(base, "datefmt", None),
            style=style_char,
        )
        self._base = base

    def format(self, record: logging.LogRecord) -> str:
        if self._base is not None:
            rendered = self._base.format(record)
        else:
            # No wrapped formatter: use the FULL standard rendering
            # (message + traceback + stack_info) so lazily-rendered
            # exception text still passes through this boundary.
            rendered = logging.Formatter.format(self, record)
        return redact_secrets(rendered)


def configure_logging(level: str = "INFO", *, redact: bool = True) -> None:
    """
    Configure stdlib logging + structlog for JSON output.

    Correlation IDs are supported via structlog.contextvars. Bind them with:
      structlog.contextvars.bind_contextvars(correlation_id="...")

    With ``redact=True`` (default) known secret shapes are masked in both
    structlog events and plain stdlib records. The filter is attached to
    every handler that exists on the root logger at configuration time
    (idempotently — re-running ``configure_logging`` does not stack
    filters), which is the logging boundary every subsystem — ``httpx``
    request lines included — flows through.
    """

    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")
    # basicConfig silently does nothing (handlers *and* level) when the
    # root logger already has a handler — a library that attached one
    # earlier would otherwise leave the root level at WARNING and drop
    # every INFO record (httpx request lines included). Set it explicitly.
    logging.getLogger().setLevel(getattr(logging, level.upper(), logging.INFO))
    if redact:
        redaction_filter = SecretRedactionFilter()
        for handler in logging.getLogger().handlers:
            if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
                handler.addFilter(redaction_filter)
            # Belt-and-suspenders: also wrap the formatter so the final
            # rendered line (args reprs, tracebacks, anything the filter
            # cannot rewrite in place) is redacted as a last resort.
            if not isinstance(handler.formatter, RedactingFormatter):
                handler.setFormatter(RedactingFormatter(handler.formatter))

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
    "RedactingFormatter",
    "SecretRedactionFilter",
    "configure_logging",
    "get_logger",
    "redact_secrets",
]
