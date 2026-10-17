"""The Studio API authorization gate (audit 2026-10-16, module ``api/studio.py``).

``api/studio.py`` is mounted by ``api/app.py`` and read the module docstring
claims two things that must be *proved*, not asserted in prose: the gate runs
before any reader, and an unconfigured project is indistinguishable from an
unauthorized one (no existence oracle).  The module also refuses to invent a
second token system — it reuses the dashboard principal — and that reuse is the
thing a future change could silently break, so it is pinned here.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from nexus_ai_agent.api import studio
from nexus_ai_agent.config import settings as settings_module

TOKEN = "s3cret-studio-token"
PROJECT = "proj-1"


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", TOKEN)
    monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", f" {PROJECT} , other ,, ")
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


def _call(project_id: str, authorization: str | None) -> None:
    studio.require_studio_project(project_id, authorization)


def test_without_a_configured_token_the_surface_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_DASHBOARD_TOKEN", raising=False)
    settings_module.get_settings.cache_clear()
    with pytest.raises(HTTPException) as excinfo:
        _call(PROJECT, None)
    assert excinfo.value.status_code == 503
    settings_module.get_settings.cache_clear()


def test_a_missing_or_wrong_bearer_token_is_rejected_before_project_checks(
    configured: None,
) -> None:
    for header in (None, "Bearer wrong", TOKEN, "Basic " + TOKEN):
        with pytest.raises(HTTPException) as excinfo:
            _call(PROJECT, header)
        assert excinfo.value.status_code == 401, header


def test_only_configured_projects_pass(configured: None) -> None:
    assert _call(PROJECT, f"Bearer {TOKEN}") is None
    assert _call("other", f"Bearer {TOKEN}") is None


@pytest.mark.parametrize("project_id", ["", " ", "../../etc", "a" * 200, "x/y", "proj id"])
def test_malformed_project_ids_are_a_bad_request(configured: None, project_id: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        _call(project_id, f"Bearer {TOKEN}")
    assert excinfo.value.status_code == 400


def test_unknown_project_is_not_distinguishable_from_an_unauthorized_one(
    configured: None,
) -> None:
    statuses = []
    for project_id in ("never-configured", "proj-1-but-not-really"):
        with pytest.raises(HTTPException) as excinfo:
            _call(project_id, f"Bearer {TOKEN}")
        statuses.append((excinfo.value.status_code, excinfo.value.detail))
    assert statuses == [
        (404, "studio project not found"),
        (404, "studio project not found"),
    ], "the same (status, detail) pair must be returned: no existence oracle"


def test_project_list_parsing_ignores_blank_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", " ,, ")
    assert studio._configured_projects() == frozenset()
    monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "a,b")
    assert studio._configured_projects() == frozenset({"a", "b"})


def test_the_gate_reuses_the_dashboard_principal_and_runs_before_project_checks(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.api.dashboard import require_dashboard_token

    calls: list[Any] = []
    monkeypatch.setattr(studio, "require_dashboard_token", lambda header=None: calls.append(header))

    _call(PROJECT, "Bearer whatever")

    assert calls == ["Bearer whatever"], "the studio surface must call the dashboard gate itself"
    assert studio.require_dashboard_token is not require_dashboard_token  # patched for the test

    studio.require_dashboard_token = require_dashboard_token  # type: ignore[assignment]
    with pytest.raises(HTTPException) as excinfo:
        _call("bad id", f"Bearer {TOKEN}")
    assert excinfo.value.status_code == 400
    assert calls == ["Bearer whatever"], "a malformed id must be rejected after authentication ran"
