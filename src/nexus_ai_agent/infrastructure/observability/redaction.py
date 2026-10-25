"""Redaction at the observability boundary.

Used by :mod:`nexus_ai_agent.infrastructure.observability.structured`
(``log_lifecycle_event``): every field is redacted *here*, at the
boundary, before it reaches a log line or a metric — callers do not
have to remember anything. Covered shapes:

* ``Bearer`` tokens and ``x-goog-api-key`` headers (quoted dict-repr
  form included);
* bare Telegram bot tokens and the API-URL form
  ``https://api.telegram.org/bot<token>/…``;
* query-parameter secrets ``?key=…`` / ``&token=…`` (bare ``key``
  included);
* ``user:password`` userinfo inside URLs;
* values stored under secret-ish *field names* (``api_key``, ``token``,
  ``password`` …) are replaced wholesale, even when the value itself
  matches no known secret shape.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_SECRET = re.compile(r"(?i)(bearer\s+|(?:token|api[_-]?key|secret|password)\s*[=:]\s*)[^\s,;&]+")
_TELEGRAM = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{35}\b")
_BOT_URL_TOKEN = re.compile(r"(?i)(https?://api\.telegram\.org/bot)\d{6,10}:[A-Za-z0-9_-]{30,}")
_GOOGLE_API_KEY = re.compile(r"(?i)(x-goog-api-key[\"']?\s*[:=]\s*[\"']?)(?!%)[^\s,;}'\"]+")
_QUERY_KEY = re.compile(
    r"(?i)([?&](?:key|api[_-]?key|access[_-]?key|token|secret|password)=)(?!%)[^&\s]+"
)
#: Field names whose values are replaced wholesale even when the value
#: matches no recognizable secret shape (segment-bounded so e.g.
#: ``keyboard`` is not matched).
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:^|[_\-\s])(?:key|keys|token|tokens|secret|secrets|password|passwords|"
    r"credential|credentials|authorization|authorisation|bearer|api[_-]?key|api[_-]?keys|"
    r"access[_-]?key|access[_-]?keys)(?:$|[_\-\s])"
)


def redact(value: object) -> str:
    text = str(value)
    text = _BOT_URL_TOKEN.sub(lambda m: f"{m.group(1)}[REDACTED_BOT_TOKEN]", text)
    text = _SECRET.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    text = _GOOGLE_API_KEY.sub(lambda m: f"{m.group(1)}[REDACTED]", text)
    text = _QUERY_KEY.sub(lambda m: f"{m.group(1)}[REDACTED]", text)
    text = _TELEGRAM.sub("[REDACTED_BOT_TOKEN]", text)
    return _redact_urls(text)


def _redact_urls(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parts = urlsplit(raw)
            if parts.username or parts.password:
                host = parts.hostname or "unknown-host"
                if parts.port:
                    host += f":{parts.port}"
                query = _QUERY_KEY.sub(lambda m: f"{m.group(1)}[REDACTED]", parts.query)
                return urlunsplit((parts.scheme, host, parts.path, query, parts.fragment))
        except ValueError:
            pass
        return raw

    return re.sub(r"https?://[^\s]+", replace, text)


def redact_fields(fields: dict[str, object]) -> dict[str, str]:
    """Redact a structured key/value payload at the boundary.

    A value stored under a secret-ish *key* is replaced outright — the
    value itself may be a freshly generated credential that matches no
    known shape, and the key already told us what it is.
    """
    safe: dict[str, str] = {}
    for key, value in fields.items():
        if _SENSITIVE_KEY.search(str(key)):
            safe[str(key)] = "[REDACTED]"
        else:
            safe[str(key)] = redact(value)
    return safe
