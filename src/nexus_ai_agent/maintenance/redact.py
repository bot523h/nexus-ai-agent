"""Centralised credential redaction for maintenance evidence (F-003).

Arena finding F-003: database credentials could reach ``DrillReport`` JSON,
log lines, subprocess diagnostics and evidence files.  The previous code
special-cased a literal ``.replace(":pg@", ...)`` — which only catches one
spelling and misses every other URL form, DSN keyword form and encoded
password.

This module is the one place a connection string is made safe for evidence.
It is deliberately dependency-free and pure so it can be called from any layer
without importing storage.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

#: The placeholder that replaces every secret.  A stable literal (not a hash)
#: so an operator can grep evidence for it and see that redaction happened.
REDACTED = "***REDACTED***"

# ``scheme://user:password@host`` — capture the password between ``:`` and ``@``
# in the userinfo section only.
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^:/@\s]*):(?P<secret>[^/@\s]*)@"
)
# libpq/psycopg keyword form: ``password=...`` (or ``passfile``).
_DSN_KEYWORD = re.compile(r"(?i)\b(password|passfile)\s*=\s*(?:'[^']*'|\"[^\"]*\"|\S+)")


def redact_dsn(url: str | None) -> str | None:
    """Return *url* with its password replaced by :data:`REDACTED`.

    Handles the URL form (``postgresql://user:pw@host/db``), a query parameter
    password (``?password=pw``) and preserves everything else so the redacted
    string is still useful for identity comparison (host/db/port survive).
    ``None`` passes through unchanged.
    """
    if not url:
        return url
    if "://" not in url:
        return _DSN_KEYWORD.sub(lambda m: f"{m.group(1)}={REDACTED}", url)

    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, _, hostpart = netloc.rpartition("@")
        if ":" in userinfo:
            user, _, _pw = userinfo.partition(":")
            userinfo = f"{user}:{REDACTED}"
        netloc = f"{userinfo}@{hostpart}"

    query = parts.query
    if query:
        query = re.sub(r"(?i)(password=)[^&]*", rf"\1{REDACTED}", query)

    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_text(text: str | None) -> str:
    """Scrub every credential spelling out of free text (logs, stderr, argv).

    Applies the URL and keyword patterns so a leaked DSN inside a subprocess
    error message, a traceback or a log line is neutralised before it is
    stored as evidence.
    """
    if not text:
        return text or ""
    redacted = _URL_CREDENTIALS.sub(
        lambda m: f"{m.group('scheme')}{m.group('user')}:{REDACTED}@", text
    )
    redacted = _DSN_KEYWORD.sub(lambda m: f"{m.group(1)}={REDACTED}", redacted)
    return redacted


def target_identity(url: str | None) -> str | None:
    """A redacted, human-meaningful identity for a target (``host:port/db``).

    Never contains the user, password or query parameters — it exists so a
    :class:`DrillReport` can name *which* database was touched without carrying
    a credential.
    """
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.netloc:
        # Not a URL (a keyword DSN or a bare path): fall back to redaction.
        return redact_dsn(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    db = parts.path.lstrip("/")
    return f"{host}{port}/{db}" if db else f"{host}{port}"


def split_dsn_password(url: str) -> tuple[str, str | None]:
    """Return ``(url_without_password, password)`` for credential-safe argv.

    A PostgreSQL DSN on a command line is visible to every user via ``ps``.
    Callers pass the password-free URL as the argument and the password through
    the ``PGPASSWORD`` environment variable (or a mode-0600 passfile) instead.
    Non-URL or password-less inputs return the original URL and ``None``.
    """
    if "://" not in url:
        return url, None
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url, None
    userinfo, _, hostpart = parts.netloc.rpartition("@")
    if ":" not in userinfo:
        return url, None
    user, _, password = userinfo.partition(":")
    new_netloc = f"{user}@{hostpart}"
    stripped = urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))
    return stripped, password
