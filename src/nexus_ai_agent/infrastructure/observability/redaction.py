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

Two invariants this module is held to, both pinned by
``tests/unit/test_redaction_boundary.py``:

**Fail closed.** A URL that cannot be parsed is never emitted unvetted — its
authority is withheld instead.  The previous ``except ValueError: pass``
returned the raw text, leaking ``user:password@`` for a bad port or a malformed
IPv6 literal.

**Encoding is not a licence.** A value is redacted whatever its percent-
encoding.  The removed ``(?!%)`` lookaheads suppressed redaction for values
beginning with a percent-encoded byte.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_SECRET = re.compile(r"(?i)(bearer\s+|(?:token|api[_-]?key|secret|password)\s*[=:]\s*)[^\s,;&]+")
_TELEGRAM = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{35}\b")
_BOT_URL_TOKEN = re.compile(r"(?i)(https?://api\.telegram\.org/bot)\d{6,10}:[A-Za-z0-9_-]{30,}")
# NOTE: no ``(?!%)`` lookahead here, and none in ``_QUERY_KEY`` below.  Those
# lookaheads suppressed redaction purely because a value *began* with a
# percent-encoded byte, so ``?key=%53ECRETVALUE`` and ``?access_key=%2Fbcdef``
# passed through untouched.  Percent-encoding is a property of transport, not
# evidence that a value is not a secret — declining to redact because of it is
# backwards.  The accepted cost is over-redaction of URL *templates* such as
# ``?key=%s``, which is a readability loss and not a leak.  At a boundary whose
# contract is "callers do not have to remember anything", that is the only safe
# direction to be wrong in.
_GOOGLE_API_KEY = re.compile(r"(?i)(x-goog-api-key[\"']?\s*[:=]\s*[\"']?)[^\s,;}'\"]+")
_QUERY_KEY = re.compile(
    r"(?i)([?&](?:key|api[_-]?key|access[_-]?key|token|secret|password)=)[^&\s]+"
)
#: The authority component of an http(s) URL: the only part that can carry a
#: credential, used to fail closed when the URL cannot be parsed.
_AUTHORITY = re.compile(r"(?i)^(https?://)[^/?#\s]*")

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
            # FAIL CLOSED.  ``urlsplit`` raises ``ValueError`` for a malformed
            # IPv6 literal, and ``parts.port`` raises for a non-numeric or
            # out-of-range port.  Both are ordinary typos, and a malformed
            # connection string is exactly what gets logged when a connection
            # fails — so this branch runs precisely when a credential is most
            # likely to be present.  Returning ``raw`` here (the previous
            # behaviour) emitted ``user:password@`` verbatim:
            #
            #     https://user:hunter2@example.com:notaport/api  -> unchanged
            #     https://user:hunter2@[::1/x                    -> unchanged
            #
            # The whole authority is withheld rather than surgically stripped,
            # because a string we cannot parse is a string whose userinfo
            # boundary we cannot locate reliably.  Scheme, path, query and
            # fragment survive, so the log still says which endpoint failed.
            return _AUTHORITY.sub(r"\1[UNPARSEABLE-AUTHORITY]", raw, count=1)
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
