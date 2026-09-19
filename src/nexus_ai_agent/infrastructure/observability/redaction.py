"""Redaction at the observability boundary."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_SECRET = re.compile(r"(?i)(bearer\s+|(?:token|api[_-]?key|secret|password)\s*[=:]\s*)[^\s,;&]+")
_TELEGRAM = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{35}\b")


def redact(value: object) -> str:
    text = str(value)
    text = _SECRET.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
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
                return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
        except ValueError:
            pass
        return raw

    return re.sub(r"https?://[^\s]+", replace, text)


def redact_fields(fields: dict[str, object]) -> dict[str, str]:
    return {key: redact(value) for key, value in fields.items()}
