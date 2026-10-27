"""The opt-in tracing bootstrap is fail-closed and never fatal (task-274).

The rule under test is the one every observability path in this repository
follows: *a tracing failure can never break the caller's work*.  These tests
pin the four outcomes of ``configure_tracing`` — disabled, active,
SDK-missing, key-missing — plus the "never raises" contract of
``bootstrap_tracing``, by driving the real functions (no SDK is mocked; the
``sdk_missing`` path is proven by construction with a monkeypatched probe).

``langsmith`` ships with the core install (transitively, via
``langgraph`` → ``langchain-core``), so there is no ``[tracing]`` extra and the
real-SDK path is provable on every leg.
"""

from __future__ import annotations

import importlib
import importlib.util
import os

import pytest

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.observability import tracing

#: Every name the SDK resolves to decide whether tracing is on.  ``langsmith``
#: looks up ``TRACING_V2``/``TRACING`` under both the ``LANGSMITH_`` and
#: ``LANGCHAIN_`` namespaces; ``LANGCHAIN_HANDLER`` is the legacy V1 flag.
LANGSMITH_ENV = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
    "LANGSMITH_ENDPOINT",
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_HANDLER",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> object:
    for name in LANGSMITH_ENV:
        # setenv first so monkeypatch records "unset" as the state to restore;
        # ``configure_tracing`` writes ``os.environ`` directly, and a bare
        # ``delenv`` on an absent name records no undo.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    # The SDK's process-global override is a real module global and its env read
    # is ``lru_cache``d; reset both on each side so neither this module nor a
    # neighbour can leak "off"/"on" state.
    _reset_sdk_override()
    _clear_sdk_env_cache()
    yield
    _reset_sdk_override()
    _clear_sdk_env_cache()


def _reset_sdk_override() -> None:
    try:
        context = importlib.import_module("langsmith._internal._context")
        context._GLOBAL_TRACING_ENABLED = None
    except Exception:  # noqa: BLE001 - the SDK may be absent; nothing to reset
        pass


def _clear_sdk_env_cache() -> None:
    """Drop the SDK's ``lru_cache``d env reads so a test controls what it sees."""
    try:
        importlib.import_module("langsmith.utils").get_env_var.cache_clear()
    except Exception:  # noqa: BLE001 - the SDK may be absent
        pass


def _sdk_present() -> bool:
    return importlib.util.find_spec("langsmith") is not None


# --------------------------------------------------------------------------- #
# 1. the four outcomes
# --------------------------------------------------------------------------- #
def test_disabled_exports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    state = tracing.configure_tracing(enabled=False, api_key="k", project="p")
    assert state == tracing.TracingState(active=False, reason="disabled")
    for name in LANGSMITH_ENV:
        assert name not in os.environ, f"{name} must not be exported when tracing is off"


def test_key_missing_is_a_typed_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_langsmith_available", lambda: True)
    state = tracing.configure_tracing(enabled=True, api_key=None, project="p")
    assert state.active is False and state.reason == "key_missing"
    assert "LANGSMITH_TRACING" not in os.environ


def test_sdk_missing_is_a_typed_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_langsmith_available", lambda: False)
    state = tracing.configure_tracing(enabled=True, api_key="k", project="p")
    assert state.active is False and state.reason == "sdk_missing"
    assert "LANGSMITH_TRACING" not in os.environ


def test_enabled_exports_the_full_langsmith_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_langsmith_available", lambda: True)
    state = tracing.configure_tracing(
        enabled=True, api_key="secret", project="nexus", endpoint="https://example.test"
    )
    assert state == tracing.TracingState(active=True, reason="active", project="nexus")
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "secret"
    assert os.environ["LANGSMITH_PROJECT"] == "nexus"
    assert os.environ["LANGSMITH_ENDPOINT"] == "https://example.test"


def test_endpoint_defaults_when_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_langsmith_available", lambda: True)
    tracing.configure_tracing(enabled=True, api_key="k", project="p")
    assert os.environ["LANGSMITH_ENDPOINT"] == tracing.DEFAULT_ENDPOINT


# --------------------------------------------------------------------------- #
# 2. never fatal (the observability contract)
# --------------------------------------------------------------------------- #
def test_bootstrap_never_raises_on_a_broken_settings_object() -> None:
    """A settings object that explodes on attribute access must not break startup."""

    class Exploding:
        @property
        def tracing_enabled(self) -> bool:
            raise RuntimeError("boom")

    state = tracing.bootstrap_tracing(Exploding())
    assert state == tracing.TracingState(active=False, reason="disabled")


def test_bootstrap_reads_settings_and_respects_default_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tracing, "_langsmith_available", lambda: True)
    # ``tracing_*`` fields carry a validation_alias that does not include the
    # field name, so they are alias-only — the repo-wide convention for the
    # other 16 such fields (see test_knowledge_retrieval_identity.py).
    default = Settings(_env_file=None)
    assert tracing.bootstrap_tracing(default).reason == "disabled"

    enabled = Settings(
        _env_file=None,
        NEXUS_TRACING_ENABLED="true",
        NEXUS_TRACING_API_KEY="k",
    )
    state = tracing.bootstrap_tracing(enabled)
    assert state.active is True and state.project == "nexus-ai-agent"


# --------------------------------------------------------------------------- #
# 3. the real SDK path (the SDK ships with the core install)
# --------------------------------------------------------------------------- #
def test_real_sdk_path_exports_and_is_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    if not _sdk_present():
        pytest.skip(
            "langsmith not installed; it ships with the core install "
            "(langgraph -> langchain-core), so this never skips in a normal install"
        )
    importlib.import_module("langsmith")  # a real import, not just find_spec
    state = tracing.configure_tracing(enabled=True, api_key="k", project="real")
    assert state.active is True
    # The SDK reads these at import time; assert the exact names it consumes.
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "k"


def test_disabled_bootstrap_stops_a_real_sdk_that_was_already_tracing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch wins over a warm (stale) SDK env cache, in both directions.

    ``langsmith.tracing_is_enabled`` reads its env flags through a
    ``functools.lru_cache``, so an earlier read (in this test or a neighbour)
    freezes the env answer.  The bootstrap must therefore drive the SDK's
    process-global override, which is checked *before* the env — otherwise a
    disabled bootstrap could not turn a primed SDK back off.
    """
    if not _sdk_present():
        pytest.skip("langsmith not installed; it ships with the core install")
    from importlib import import_module

    tracing_is_enabled = import_module("langsmith.utils").tracing_is_enabled

    # Prime the SDK's cached env read as "on" (this is what happens when the
    # ambient environment has a stale LANGSMITH_TRACING=true).
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    _clear_sdk_env_cache()
    assert tracing_is_enabled() is True

    # A disabled bootstrap must now force it back off, cache notwithstanding.
    assert tracing.configure_tracing(enabled=False, api_key="k", project="p").reason == "disabled"
    assert tracing_is_enabled() is False

    # ...and an enabled bootstrap must force it on again, symmetrically.
    assert tracing.configure_tracing(enabled=True, api_key="k", project="p").active is True
    assert tracing_is_enabled() is True


# --------------------------------------------------------------------------- #
# 2. the default-off security boundary is real
# --------------------------------------------------------------------------- #
def test_disabled_clears_a_stale_environment_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-existing ``LANGSMITH_TRACING=true`` must not survive a disabled bootstrap."""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_HANDLER", "true")
    state = tracing.configure_tracing(enabled=False, api_key="k", project="p")
    assert state.reason == "disabled"
    for name in (
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_TRACING",
        "LANGCHAIN_HANDLER",
    ):
        assert name not in os.environ, f"{name} must be cleared when tracing is off"


def test_incomplete_configuration_clears_stale_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabling without a key must still disable any stale flag — no partial activation."""
    monkeypatch.setattr(tracing, "_langsmith_available", lambda: True)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    state = tracing.configure_tracing(enabled=True, api_key=None, project="p")
    assert state.reason == "key_missing"
    assert "LANGSMITH_TRACING" not in os.environ


def test_active_path_clears_legacy_v1_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabling must leave exactly one switch on: the current ``LANGSMITH_`` one."""
    monkeypatch.setattr(tracing, "_langsmith_available", lambda: True)
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_HANDLER", "true")
    state = tracing.configure_tracing(enabled=True, api_key="k", project="p")
    assert state.active is True
    assert os.environ["LANGSMITH_TRACING"] == "true"
    for name in ("LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING", "LANGCHAIN_HANDLER"):
        assert name not in os.environ, f"{name} (legacy) must be cleared when tracing activates"


def test_bootstrap_is_idempotent_and_redetermines_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-running the bootstrap must reflect the *current* settings, not history."""
    monkeypatch.setattr(tracing, "_langsmith_available", lambda: True)
    on = Settings(_env_file=None, NEXUS_TRACING_ENABLED="true", NEXUS_TRACING_API_KEY="k")
    off = Settings(_env_file=None)

    assert tracing.bootstrap_tracing(on).active is True
    assert os.environ["LANGSMITH_TRACING"] == "true"
    # A later disabled bootstrap in the same process must fully disable tracing.
    assert tracing.bootstrap_tracing(off).reason == "disabled"
    assert "LANGSMITH_TRACING" not in os.environ


# --------------------------------------------------------------------------- #
# 5. safe diagnostics (never leak the key, URL or payload)
# --------------------------------------------------------------------------- #
def test_sanitize_exception_reports_class_and_status_only() -> None:
    class FakeAPIStatusError(Exception):
        status_code = 403

    text = tracing.sanitize_exception(FakeAPIStatusError("secret-key http://u:p@h/x payload"))
    assert text == "FakeAPIStatusError status=403"
    assert "secret" not in text and "http" not in text and "payload" not in text


def test_sanitize_exception_never_echoes_the_message() -> None:
    exc = RuntimeError("api_key=sk-live-DEADBEEF at https://x.test?a=b payload=hi")
    assert tracing.sanitize_exception(exc) == "RuntimeError"


def test_sanitize_exception_rejects_an_unsafe_class_name() -> None:
    BadName = type("BadName", (Exception,), {})
    BadName.__name__ = "Bad Name\nwith newline"
    assert tracing.sanitize_exception(BadName("boom")) == "Exception"


def test_sanitize_status_drops_urls_and_long_tokens() -> None:
    assert tracing._sanitize_status("https://api.smith.langchain.com/runs") is None
    assert tracing._sanitize_status("x" * 64) is None
    assert tracing._sanitize_status(429) == "429"
    assert tracing._sanitize_status("invalid_api_key") == "invalid_api_key"


# --------------------------------------------------------------------------- #
# 6. the override reports whether it actually applied (fail-closed evidence)
# --------------------------------------------------------------------------- #
def test_set_sdk_override_reports_success_and_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The off switch must report honestly: ``False`` on SDK drift (no silent
    false-green), ``True`` when the SDK's global is really set."""
    import importlib

    if not _sdk_present():
        pytest.skip("langsmith not installed; it ships with the core install")
    context = importlib.import_module("langsmith._internal._context")

    assert tracing._set_sdk_override(False) is True
    assert context._GLOBAL_TRACING_ENABLED is False

    # Scope the delattr to its own context so its restoration happens *before*
    # the autouse ``_clean_env`` teardown resets the override; otherwise pytest's
    # reverse-order teardown would restore the saved ``False`` afterwards.
    with monkeypatch.context() as m:
        m.delattr(context, "_GLOBAL_TRACING_ENABLED", raising=False)
        assert tracing._set_sdk_override(False) is False
        # ...and it did not silently re-create an unused global.
        assert not hasattr(context, "_GLOBAL_TRACING_ENABLED")
