"""RFC 8785 (JSON Canonicalization Scheme) — one explicit, additive digest API.

This module is a **new, additive** canonicalization surface. It does not
replace, wrap, or migrate the repository's historical provenance spelling
(:func:`nexus_ai_agent.provenance.models.canonical_json` / ``digest_of``, which
uses ``sort_keys`` + compact separators + ``ensure_ascii=False``). Those
historical hashes are a frozen contract for the causal ledger, the Artifact
Passport and the IBPC re-exports; JCS is a *different* spelling and must never
be wired into those call sites (enforced by
``tests/architecture/test_jcs_legacy_isolation.py``).

Why a separate spelling at all: RFC 8785 pins number formatting (ECMAScript
``Number::toString``), UTF-16 code-unit key ordering, and escaping to a single
interoperable byte sequence. It is the right basis for cross-system digests;
it is the wrong basis for re-hashing an existing ledger.

Fail-closed boundary. ``rfc8785.dumps`` silently coerces Python conveniences
(``tuple`` -> array, ``IntEnum`` -> int, ``str``/``dict`` subclasses -> their
base). This module refuses them *before* calling the library: the accepted
domain is exactly the seven JSON value types by exact type — ``dict``, ``list``,
``str``, ``int``, ``float``, ``bool``, ``None`` — with string-only object keys,
no subclasses, no tuples/sets, no ``Decimal``/``bytes``. Everything else raises
:class:`JcsError`. Non-finite floats, unsafe integers and lone surrogates are
refused by the library and translated to the same :class:`JcsError`.

The ``rfc8785`` import is deliberately **lazy** (inside the call): the
``--no-deps`` CI lanes that install the package without its dependency closure
still import :mod:`nexus_ai_agent.provenance` cleanly and get a typed
:class:`JcsError` only if they actually try to canonicalize.
"""

from __future__ import annotations

import hashlib
from typing import Any, NoReturn

__all__ = [
    "JcsError",
    "canonical_bytes",
    "canonical_text",
    "jcs_digest",
]

#: The repository's digest spelling: ``sha256:<lowercase-hex>``.
_SHA256_PREFIX = "sha256:"


class JcsError(ValueError):
    """A value was refused by the RFC 8785 boundary (fail closed, typed)."""


def _fail(reason: str) -> NoReturn:
    raise JcsError(reason)


def _enter(value: Any, seen: set[int], path: str) -> None:
    ident = id(value)
    if ident in seen:
        _fail(f"cyclic structure at {path}: JCS cannot canonicalize a cycle")
    seen.add(ident)


def _check(value: Any, seen: set[int], path: str) -> None:
    """Assert ``value`` is an exact JSON value, recursively; else raise JcsError.

    Exact-type checks (``type(x) is ...``) are deliberate: a subclass such as
    ``IntEnum`` or a custom ``str`` would otherwise be accepted by the library
    through an implicit conversion this boundary exists to forbid.
    """
    kind = type(value)
    if value is None or kind is bool or kind is str or kind is int or kind is float:
        return
    if kind is list:
        _enter(value, seen, path)
        for index, item in enumerate(value):
            _check(item, seen, f"{path}[{index}]")
        seen.discard(id(value))
        return
    if kind is dict:
        _enter(value, seen, path)
        for key, item in value.items():
            if type(key) is not str:
                _fail(f"object keys must be strings, got {type(key).__name__} at {path}")
            _check(item, seen, f"{path}.{key}")
        seen.discard(id(value))
        return
    _fail(
        f"unsupported type {kind.__name__} at {path}: the JCS boundary accepts "
        "only the exact JSON value types (dict, list, str, int, float, bool, None)"
    )


def _canonicalize(value: Any) -> bytes:
    try:
        import rfc8785
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise JcsError(
            "the pinned 'rfc8785' dependency is not installed; RFC 8785 "
            "canonicalization is unavailable in this environment"
        ) from exc
    try:
        return rfc8785.dumps(value)
    except rfc8785.CanonicalizationError as exc:
        raise JcsError(f"RFC 8785 refused the value: {exc}") from exc


def canonical_bytes(value: Any) -> bytes:
    """Return the RFC 8785 canonical UTF-8 bytes of ``value`` (fail closed)."""
    _check(value, set(), "$")
    return _canonicalize(value)


def canonical_text(value: Any) -> str:
    """Return the RFC 8785 canonical form of ``value`` as a ``str``."""
    return canonical_bytes(value).decode("utf-8")


def jcs_digest(value: Any) -> str:
    """Return ``sha256:<hex>`` over the exact canonical bytes of ``value``.

    The digest is computed over the bytes returned by :func:`canonical_bytes`
    and over nothing else, so it binds to the RFC 8785 spelling rather than to
    any storage whitespace.
    """
    return _SHA256_PREFIX + hashlib.sha256(canonical_bytes(value)).hexdigest()
