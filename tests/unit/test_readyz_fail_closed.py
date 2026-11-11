"""F-008: ``/readyz`` must fail closed with a structured 503, never HTTP 500.

Arena finding F-008: a malformed ``NEXUS_DATABASE_URL`` (unsupported scheme,
missing host) raised a bare ``ValueError`` out of ``enforce_postgres_policy()``.
``_database_readiness`` only caught ``BackendPolicyError``, so the exception
escaped the endpoint and FastAPI rendered an HTTP 500 — a server-fault page for
what is really a "this deployment is not usable" condition. It also risked
echoing the credential-bearing URL back in the traceback.

Every case below must yield ``(ok=False, <sanitised detail>)`` — the signal the
``/readyz`` handler turns into a 503 — and no secret may appear in the detail.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus_ai_agent.api.app import _database_readiness

POISON = "NEXUS_SECRET_POISON_92831"


@pytest.fixture()
def _reset_settings(monkeypatch):
    def _apply(**env: str | None) -> None:
        for key in (
            "NEXUS_DATABASE_URL",
            "DATABASE_URL",
            "NEXUS_REQUIRE_POSTGRES",
            "NEXUS_DB_PATH",
        ):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            if value is not None:
                monkeypatch.setenv(key, value)
        from nexus_ai_agent.config.settings import get_settings

        get_settings.cache_clear()

    yield _apply
    from nexus_ai_agent.config.settings import get_settings

    get_settings.cache_clear()


@pytest.mark.parametrize(
    "url",
    [
        "mysql://u:p@h/db",  # unsupported scheme
        "postgresql:///db",  # missing host
        "not-a-url-at-all",  # unparseable as PostgreSQL
        f"mysql://u:{POISON}@h/db",  # unsupported scheme *with* a credential
    ],
)
def test_malformed_url_is_not_ready_never_raises(_reset_settings, url: str) -> None:
    """A bad URL must return ``ok=False`` — it must not raise (→ HTTP 500)."""
    _reset_settings(NEXUS_DATABASE_URL=url, NEXUS_REQUIRE_POSTGRES="true")
    ok, detail = asyncio.run(_database_readiness())
    assert ok is False
    assert POISON not in detail, "the credential leaked into the readiness detail"
    assert detail  # a non-empty, structured explanation


def test_missing_url_with_postgres_required_is_not_ready(_reset_settings) -> None:
    _reset_settings(NEXUS_REQUIRE_POSTGRES="true")
    ok, detail = _run()
    assert ok is False
    assert "postgresql" in detail


def test_blank_url_with_postgres_required_is_not_ready(_reset_settings) -> None:
    _reset_settings(NEXUS_DATABASE_URL="   ", NEXUS_REQUIRE_POSTGRES="true")
    ok, detail = _run()
    assert ok is False
    assert "postgresql" in detail


def test_unreachable_postgres_is_not_ready_not_a_crash(_reset_settings) -> None:
    """Connection refusal must be a clean not-ready, not an escaping exception."""
    _reset_settings(
        NEXUS_DATABASE_URL="postgresql://u:p@127.0.0.1:1/none", NEXUS_REQUIRE_POSTGRES="true"
    )
    ok, detail = _run()
    assert ok is False
    assert detail


def _run() -> tuple[bool, str]:
    return asyncio.run(_database_readiness(timeout=1.0))
