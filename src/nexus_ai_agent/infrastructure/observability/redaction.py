"""Redaction at the observability boundary."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_SECRET = re.compile(r"(?i)(bearer\s+|(?:token|api[_-]?key|secret|password)\s*[=:]\s*)[^\s,;&]+")
_TELEGRAM = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
_BOT_URL_TOKEN = re.compile(r"(?i)(https://api\.telegram\.org/bot)\d{6,10}:[A-Za-z0-9_-]{30,}")
_QUERY_KEY = re.compile(r"(?i)([?&](?:key|api[_-]?key|token|secret|password)=)[^&\s]+")
_GOOGLE_API_KEY = re.compile(r"(?i)(x-goog-api-key\s*[:=]\s*)[^\s,;&\"'}]+")


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
                # Redact query string secrets inside URL as well
                query = _QUERY_KEY.sub(lambda m: f"{m.group(1)}[REDACTED]", parts.query)
                return urlunsplit((parts.scheme, host, parts.path, query, parts.fragment))
        except ValueError:
            pass
        return raw

    # First pass: strip credentials, second pass already handled query redaction inside replace,
    # but also need to handle raw query leakage outside urlsplit context (plain text)
    text = re.sub(r"https?://[^\s]+", replace, text)
    return text


def redact_fields(fields: dict[str, object]) -> dict[str, str]:
    return {key: redact(value) for key, value in fields.items()}
