"""The redaction boundary must fail closed (task-203).

``infrastructure/observability/redaction.py`` is the single choke point that
scrubs every structured log field before it reaches a log line or a metric —
callers are documented as not having to remember anything.  A boundary with
that contract has exactly one safe default: **never emit input it has not
vetted.**  Two of its rules were permissive instead, and both were reproduced
on ``e5b326b``.

Defect 1 — fail-open on an unparseable URL
------------------------------------------
``_redact_urls`` catches ``ValueError`` and returns the *raw* match.
``urlsplit`` raises ``ValueError`` for an invalid port and for a malformed IPv6
literal, and both are ordinary typos — a malformed ``NEXUS_DATABASE_URL`` port
is precisely what gets logged when a connection fails:

    redact("https://user:hunter2@example.com:notaport/api")
        -> "https://user:hunter2@example.com:notaport/api"     # password verbatim
    redact("https://user:hunter2@[::1/x")
        -> "https://user:hunter2@[::1/x"                       # password verbatim

``cat``-style controls in the same file prove the intent: the well-formed
``https://user:hunter2@example.com/api`` correctly collapses to
``https://example.com/api``.  The asymmetry is the defect.

Defect 2 — redaction suppressed by percent-encoding
---------------------------------------------------
``_QUERY_KEY`` and ``_GOOGLE_API_KEY`` carry a ``(?!%)`` negative lookahead, so
a value is *not* redacted merely because it begins with a percent-encoded
byte.  ``_SECRET`` covers ``token``/``api_key``/``secret``/``password``, which
masks most of the surface; the reachable hole is the parameter names that only
``_QUERY_KEY`` knows about:

    redact("https://x.test/v1?key=%53ECRETVALUE")          -> unchanged
    redact("https://x.test/v1?access_key=%53ECRET")        -> unchanged

Encoding is a property of transport, not a statement that a value is not a
secret.  Declining to redact because of it is backwards.

Neither behaviour is pinned by any existing test or explained by any comment.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.infrastructure.observability.redaction import redact, redact_fields

PASSWORD = "hunter2"


# --------------------------------------------------------------------------- #
# Defect 1: an unparseable authority must not be emitted unvetted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        # urlsplit accepts this, then parts.port raises ValueError
        f"https://user:{PASSWORD}@example.com:notaport/api",
        # urlsplit itself raises ValueError: invalid IPv6 literal
        f"https://user:{PASSWORD}@[::1/x",
        # the same class with a port that is numeric-looking but out of range
        f"https://user:{PASSWORD}@example.com:99999999/api",
    ],
)
def test_unparseable_url_never_leaks_the_password(url: str) -> None:
    out = redact(url)
    assert PASSWORD not in out, f"the password survived redaction: {out!r}"


@pytest.mark.parametrize(
    "url",
    [
        f"https://user:{PASSWORD}@example.com:notaport/api",
        f"https://user:{PASSWORD}@[::1/x",
    ],
)
def test_unparseable_url_never_leaks_through_the_log_boundary(url: str) -> None:
    """The real entry point: ``redact_fields`` is what ``log_lifecycle_event`` uses."""
    fields = redact_fields({"db_url": url, "unrelated": "nothing to see"})
    blob = repr(fields)
    assert PASSWORD not in blob, f"the password reached a log field: {fields!r}"


def test_a_well_formed_url_with_userinfo_is_still_scrubbed() -> None:
    """Control: this already worked and must keep working."""
    out = redact(f"https://user:{PASSWORD}@example.com/api")
    assert PASSWORD not in out
    assert out == "https://example.com/api"


def test_a_failed_url_still_keeps_its_scheme_and_path() -> None:
    """Failing closed must not mean failing useless.

    Operators still need to know which endpoint blew up; only the authority —
    the one part that can carry a credential — is withheld.
    """
    out = redact(f"https://user:{PASSWORD}@example.com:notaport/api/v1/items?x=1")
    assert PASSWORD not in out
    assert out.startswith("https://")
    assert "/api/v1/items" in out


def test_a_url_without_credentials_is_not_mangled_by_the_fail_closed_path() -> None:
    """No false positives: a bad port alone must not trigger authority removal."""
    url = "https://example.com:notaport/api/v1"
    assert redact(url) == url


# --------------------------------------------------------------------------- #
# Defect 2: percent-encoding must not suppress redaction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "param",
    ["key", "api_key", "apikey", "access_key", "accesskey", "token", "secret", "password"],
)
@pytest.mark.parametrize(
    "value",
    [
        "PLAINSECRETVALUE",  # control: always redacted
        "%53ECRETVALUE",  # percent-encoded first byte
        "%2Fbcdef",  # what a base64 key beginning with '/' looks like in a URL
        "%2Bbcdef",  # what a base64 key beginning with '+' looks like in a URL
    ],
)
def test_query_secret_is_redacted_whatever_its_encoding(param: str, value: str) -> None:
    url = f"https://x.test/v1?{param}={value}&next=1"
    out = redact(url)
    tail = value.lstrip("%")
    assert tail not in out, f"?{param}={value} survived redaction: {out!r}"
    assert "[REDACTED]" in out


def test_google_api_key_header_is_redacted_when_percent_encoded() -> None:
    out = redact('x-goog-api-key: "%53ECRETVALUE"')
    assert "ECRETVALUE" not in out, out


def test_the_query_net_runs_inside_the_fail_closed_path_too() -> None:
    """A URL that cannot be parsed must not smuggle its query string through."""
    url = f"https://user:{PASSWORD}@example.com:notaport/api?key=PLAINSECRETVALUE"
    out = redact(url)
    assert PASSWORD not in out
    assert "PLAINSECRETVALUE" not in out


# --------------------------------------------------------------------------- #
# No false green: the shapes that already worked must keep working
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "forbidden"),
    [
        ("Authorization: Bearer SECRETTOKEN123", "SECRETTOKEN123"),
        ("token=SECRETTOKEN123", "SECRETTOKEN123"),
        ("api_key: SECRETTOKEN123", "SECRETTOKEN123"),
        ("password=SECRETTOKEN123", "SECRETTOKEN123"),
        ("https://api.telegram.org/bot123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/x", "AAAAAAA"),
        ("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "AAAAAAA"),
        ("https://example.com/api?key=SECRETTOKEN123", "SECRETTOKEN123"),
        ("https://example.com/a?b=1&token=SECRETTOKEN123", "SECRETTOKEN123"),
    ],
)
def test_previously_redacted_shapes_stay_redacted(text: str, forbidden: str) -> None:
    assert forbidden not in redact(text)


def test_redaction_is_idempotent() -> None:
    """Running the boundary twice must not change a redacted string."""
    once = redact("https://example.com/api?key=SECRETTOKEN123")
    assert redact(once) == once


def test_benign_text_is_left_alone() -> None:
    text = "the keyboard shortcut is ctrl-k and the key idea is small keys"
    assert redact(text) == text


def test_sensitive_field_names_are_replaced_wholesale() -> None:
    fields = redact_fields({"api_key": "brand-new-credential", "model": "gemini-2.0-flash"})
    assert fields["api_key"] == "[REDACTED]"
    assert fields["model"] == "gemini-2.0-flash"
