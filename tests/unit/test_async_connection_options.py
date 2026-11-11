"""F-001: the async PostgreSQL connection path must accept a canonical Neon URL.

Arena finding F-001: libpq-style URL parameters were forwarded verbatim to
``asyncpg.connect`` as keyword arguments.  The SQLAlchemy asyncpg dialect does
``opts.update(url.query)``, so ``?sslmode=require`` became
``asyncpg.connect(..., sslmode="require")`` — and ``asyncpg.connect`` accepts
neither ``sslmode`` nor ``channel_binding`` (it has no ``**kwargs``), so a
canonical Neon URL failed with ``TypeError: unexpected keyword argument
'sslmode'`` at connect time.

These tests drive the REAL dialect (``create_connect_args``) and bind the
resulting arguments against the REAL ``asyncpg.connect`` signature, so the
contract cannot silently drift from the driver.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.engine import make_url

from nexus_ai_agent.storage.db import (
    PgConnectConfigError,
    resolve_asyncpg_connect_args,
    to_asyncpg_url,
)

_DIALECT = PGDialect_asyncpg()
_CONNECT_SIGNATURE = inspect.signature(asyncpg.connect)


def _final_kwargs(url: str) -> dict[str, object]:
    """The keyword arguments SQLAlchemy would hand to ``asyncpg.connect``."""
    dsn, connect_args = resolve_asyncpg_connect_args(to_asyncpg_url(url))
    _positional, kwargs = _DIALECT.create_connect_args(make_url(dsn))
    kwargs.update(connect_args)
    return kwargs


def _assert_driver_accepts(url: str) -> dict[str, object]:
    kwargs = _final_kwargs(url)
    # Bind against the real signature: a forwarded-but-unknown keyword raises
    # TypeError here exactly as it would at connect time.
    _CONNECT_SIGNATURE.bind(**kwargs)
    return kwargs


def test_canonical_neon_url_sslmode_require_is_accepted() -> None:
    """The exact F-001 failure: a Neon URL with ``sslmode=require``."""
    kwargs = _assert_driver_accepts("postgresql://u:p@ep-x.neon.tech/db?sslmode=require")
    assert kwargs.get("ssl") == "require"
    assert "sslmode" not in kwargs  # never forwarded as a driver kwarg


def test_verify_full_is_not_downgraded() -> None:
    kwargs = _assert_driver_accepts("postgresql://u:p@h/db?sslmode=verify-full")
    assert kwargs.get("ssl") == "verify-full"


def test_no_query_parameters_produce_no_connect_args() -> None:
    """A plain URL needs no translation and adds no driver kwargs."""
    kwargs = _assert_driver_accepts("postgresql://u:p@h/db")
    assert "ssl" not in kwargs
    assert "sslmode" not in kwargs


def test_channel_binding_require_is_refused_not_dropped() -> None:
    """asyncpg cannot negotiate SCRAM-SHA-256-PLUS, so ``require`` must fail."""
    with pytest.raises(PgConnectConfigError) as excinfo:
        resolve_asyncpg_connect_args("postgresql://u:p@h/db?channel_binding=require")
    assert "channel_binding" in str(excinfo.value)


def test_unknown_parameter_is_refused() -> None:
    """No arbitrary libpq keyword is forwarded to the driver."""
    with pytest.raises(PgConnectConfigError) as excinfo:
        resolve_asyncpg_connect_args("postgresql://u:p@h/db?totally_unknown=1")
    assert "totally_unknown" in str(excinfo.value)


def test_unsupported_sslmode_is_refused() -> None:
    with pytest.raises(PgConnectConfigError) as excinfo:
        resolve_asyncpg_connect_args("postgresql://u:p@h/db?sslmode=maybe")
    assert "sslmode" in str(excinfo.value)


def test_pooler_endpoint_url_is_accepted() -> None:
    """A Neon ``-pooler`` endpoint carries the same TLS query parameters."""
    url = "postgresql://u:p@ep-x-pooler.us-east-2.aws.neon.tech/db?sslmode=require"
    kwargs = _assert_driver_accepts(url)
    assert kwargs.get("ssl") == "require"


def test_url_encoded_credentials_are_preserved() -> None:
    """A password with URL-encoded special characters must survive intact."""
    kwargs = _final_kwargs("postgresql://u%40x:pa%3Ass%2Fword@h/db?sslmode=require")
    assert kwargs.get("user") == "u@x"
    assert kwargs.get("password") == "pa:ss/word"


def test_sslrootcert_builds_a_verifying_context(tmp_path) -> None:
    """``sslrootcert`` must yield a real SSLContext that verifies the server.

    Uses the system CA bundle (``certifi`` is already a transitive dependency)
    so the assertion is about the context's real verification flags, not a
    mocked object and not a network call.
    """
    import ssl

    import certifi

    ca = tmp_path / "ca.pem"
    ca.write_text(Path(certifi.where()).read_text())

    _dsn, connect_args = resolve_asyncpg_connect_args(
        f"postgresql://u:p@h/db?sslrootcert={ca}&sslmode=verify-full"
    )
    ctx = connect_args["ssl"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_sslrootcert_absent_keeps_string_mode() -> None:
    """Without a CA file the mode string passes through untouched (no context)."""
    _dsn, connect_args = resolve_asyncpg_connect_args("postgresql://u:p@h/db?sslmode=require")
    assert connect_args["ssl"] == "require"


def test_dialect_would_otherwise_forward_sslmode() -> None:
    """The regression guard: the RAW dialect DOES forward ``sslmode``.

    If a future SQLAlchemy release stops forwarding it, this test fails and the
    resolver's raison d'être is re-examined — it never silently becomes dead
    code that hides a real regression.
    """
    _positional, kwargs = _DIALECT.create_connect_args(
        make_url("postgresql+asyncpg://u:p@h/db?sslmode=require")
    )
    assert "sslmode" in kwargs
    with pytest.raises(TypeError):
        _CONNECT_SIGNATURE.bind(**kwargs)
