"""The opt-in tracing bootstrap is fail-closed and never fatal (task-274).

The rule under test is the one every observability path in this repository
follows: *a tracing failure can never break the caller's work*.  These tests
pin the four outcomes of ``configure_tracing`` — disabled, enabled,
SDK-missing, key-missing — plus the "never raises" contract of
``bootstrap_tracing``, by driving the real functions (no SDK is mocked; the
``sdk_missing`` path is proven by construction with a fake module finder).
"""

from __future__ import annotations

import importlib
import importlib.util
import os

import pytest

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.observability import tracing

LANGSMITH_ENV = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
    "LANGSMITH_ENDPOINT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LANGSMITH_ENV:
        monkeypatch.delenv(name, raising=False)


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
    assert state == tracing.TracingState(active=True, reason="enabled", project="nexus")
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
# 3. the real SDK path (skips visibly when the extra is absent)
# --------------------------------------------------------------------------- #
def test_real_sdk_path_exports_and_is_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    if not _sdk_present():
        pytest.skip(
            "langsmith not installed; the real-SDK path is proven by the "
            "`extras-matrix` tracing CI leg (NEXUS_EXTRA_LEG=tracing)"
        )
    importlib.import_module("langsmith")  # a real import, not just find_spec
    state = tracing.configure_tracing(enabled=True, api_key="k", project="real")
    assert state.active is True
    # The SDK reads these at import time; assert the exact names it consumes.
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "k"
