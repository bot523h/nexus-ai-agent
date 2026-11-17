"""RFC 8785 (JCS) conformance for :mod:`nexus_ai_agent.provenance.jcs`.

The expected byte strings below are independent literals taken from RFC 8785
(§3.2.2.3 number serialization, §3.2.3 sorting, Appendix B) and from the JSON
data-interchange rules — they are **not** produced by the library under test, so
a library regression cannot make them agree by construction.

Two invariants are load-bearing:

* key ordering is by **UTF-16 code unit**, not by Unicode code point, so a
  supplementary-plane key sorts *before* a BMP key whose scalar exceeds
  ``0xFFFF`` (see :func:`test_key_ordering_is_utf16_not_codepoint`);
* the boundary is **exact types only** — Python conveniences the library would
  silently coerce (``tuple``, ``IntEnum``, ``str``/``dict`` subclasses) are
  refused before canonicalization.
"""

from __future__ import annotations

import enum
import hashlib
from decimal import Decimal

import pytest

from nexus_ai_agent.provenance.jcs import (
    JcsError,
    canonical_bytes,
    canonical_text,
    jcs_digest,
)


def test_normative_ascii_example() -> None:
    assert canonical_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert canonical_text({"b": 2, "a": 1}) == '{"a":1,"b":2}'


def test_no_insignificant_whitespace_and_stable_key_order() -> None:
    value = {"z": [1, 2, {"y": "x"}], "a": None}
    assert canonical_bytes(value) == b'{"a":null,"z":[1,2,{"y":"x"}]}'


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, b"0"),
        (-0.0, b"0"),
        (1, b"1"),
        (3.0, b"3"),
        (1.5, b"1.5"),
        (-1.5, b"-1.5"),
        (1e30, b"1e+30"),
        (1e21, b"1e+21"),
        (1e-7, b"1e-7"),
        (1e-6, b"0.000001"),
        (0.1, b"0.1"),
        (2**53 - 1, b"9007199254740991"),
        (-(2**53 - 1), b"-9007199254740991"),
    ],
)
def test_number_serialization_vectors(value: object, expected: bytes) -> None:
    """RFC 8785 §3.2.2.3 / Appendix: ECMAScript ``Number::toString``."""
    assert canonical_bytes(value) == expected


def test_string_escaping_is_minimal_and_utf8() -> None:
    # 0x7F (DEL) is NOT escaped; \b \f \n \r \t use their short forms.
    assert canonical_bytes('\x0f\x0a\x09\x0d\x08\x0c"\\\x7f') == (
        b'"\\u000f\\n\\t\\r\\b\\f\\"\\\\\x7f"'
    )


def test_key_ordering_is_utf16_not_codepoint() -> None:
    """A supplementary-plane key (U+1F600) sorts before U+FB33.

    By Unicode *code point* U+FB33 < U+1F600; by UTF-16 *code unit* the
    surrogate ``0xD83D`` precedes ``0xFB33``. RFC 8785 mandates the latter, so
    this literal pins the ordering rule, not merely "some sorted order".
    """
    value = {
        "\u20ac": "Euro",
        "\r": "CR",
        "\ufb33": "Dalet",
        "1": "One",
        "\U0001f600": "Emoji",
        "\u0080": "Ctrl",
        "\u00f6": "Ouml",
    }
    assert canonical_bytes(value) == (
        b'{"\\r":"CR","1":"One","\xc2\x80":"Ctrl","\xc3\xb6":"Ouml",'
        b'"\xe2\x82\xac":"Euro","\xf0\x9f\x98\x80":"Emoji","\xef\xac\xb3":"Dalet"}'
    )


def test_unicode_is_not_normalized() -> None:
    """NFC and NFD spellings are distinct inputs (no implicit normalization)."""
    assert canonical_bytes("\u00e9") == b'"\xc3\xa9"'
    assert canonical_bytes("e\u0301") == b'"e\xcc\x81"'
    assert canonical_bytes("\u00e9") != canonical_bytes("e\u0301")


def test_digest_is_sha256_over_the_exact_canonical_bytes() -> None:
    value = {"b": 2, "a": 1}
    payload = canonical_bytes(value)
    assert jcs_digest(value) == "sha256:" + hashlib.sha256(payload).hexdigest()


def test_round_trip_of_a_composite_document() -> None:
    value = {"list": [1, "two", 3.0, True, False, None], "empty": {}}
    # 3.0 is a JSON number with no fraction in RFC 8785, so it prints as 3.
    assert canonical_text(value) == '{"empty":{},"list":[1,"two",3,true,false,null]}'


class _Color(enum.IntEnum):
    RED = 1


class _Str(str):
    pass


class _Dict(dict):
    pass


@pytest.mark.parametrize(
    "value",
    [
        (1, 2),  # tuple: the library would coerce to an array
        {1, 2},  # set: unordered, unsupported
        b"bytes",
        Decimal("1.5"),
        _Color.RED,  # IntEnum: would coerce to int
        _Str("x"),  # str subclass: would coerce to str
        _Dict({"a": 1}),  # dict subclass
        {"k": (1,)},  # nested tuple
    ],
)
def test_non_json_values_and_subclasses_are_refused(value: object) -> None:
    with pytest.raises(JcsError):
        canonical_bytes(value)


@pytest.mark.parametrize(
    "value",
    [
        {1: "a"},  # non-string key
        {"a": {2: "b"}},  # nested non-string key
    ],
)
def test_non_string_keys_are_refused(value: object) -> None:
    with pytest.raises(JcsError):
        canonical_bytes(value)


@pytest.mark.parametrize(
    "value",
    [
        2**53,  # exceeds the safe-integer domain
        -(2**53),
        float("nan"),
        float("inf"),
        float("-inf"),
        "\ud800",  # lone surrogate (not valid UTF-8)
        {"k": "\udfff"},
    ],
)
def test_domain_errors_fail_closed(value: object) -> None:
    with pytest.raises(JcsError):
        canonical_bytes(value)


def test_cyclic_structure_is_refused_not_crashed() -> None:
    value: dict[str, object] = {}
    value["self"] = value
    with pytest.raises(JcsError):
        canonical_bytes(value)


def test_jcs_error_is_a_value_error() -> None:
    assert issubclass(JcsError, ValueError)


def test_missing_dependency_is_a_typed_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``--no-deps`` environment must yield a typed JcsError, not ImportError."""
    import builtins

    real_import = builtins.__import__

    def _blocked(name: str, *args: object, **kwargs: object) -> object:
        if name == "rfc8785":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    with pytest.raises(JcsError):
        canonical_bytes({"a": 1})
