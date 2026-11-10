"""NAGAR Studio static delivery (web/README.md): allow-list, CSP, no secrets in the client."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from nexus_ai_agent.api import studio_web as web


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "dashboard-secret-0123456789")
    from nexus_ai_agent.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    from nexus_ai_agent.api import app as app_module

    yield TestClient(app_module.app, base_url="https://testserver")
    settings_module.get_settings.cache_clear()


def test_every_allow_listed_asset_exists_and_is_served_with_hardening_headers(
    client: TestClient,
) -> None:
    for name, asset in web._ASSETS.items():
        assert asset.source.is_file(), name
        url = "/studio/" if name == "index.html" else f"/studio/{name}"
        response = client.get(url)
        assert response.status_code == 200, name
        assert response.headers["content-security-policy"] == web.CONTENT_SECURITY_POLICY
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cache-control"] == "no-cache"
        assert response.content, name


def test_csp_forbids_inline_script_remote_origins_and_framing() -> None:
    csp = web.CONTENT_SECURITY_POLICY
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    assert "http:" not in csp and "https:" not in csp and "*" not in csp


def test_only_listed_assets_are_reachable_and_no_path_reaches_the_filesystem(
    client: TestClient,
) -> None:
    for path in (
        "/studio/../pyproject.toml",
        "/studio/%2e%2e/pyproject.toml",
        "/studio/..%2f..%2fapi/app.py",
        "/studio/__init__.py",
        "/studio/index.html",
        "/studio/fonts/../../api/app.py",
        "/studio/missing.js",
    ):
        response = client.get(path)
        assert response.status_code in (404, 307), path
        assert b"FastAPI" not in response.content, path


def test_service_worker_scope_and_build_stamp(client: TestClient) -> None:
    response = client.get("/studio/sw.js")
    assert response.headers["service-worker-allowed"] == "/studio/"
    text = response.text
    assert "__BUILD_ID__" not in text and web.build_id() in text


def test_service_worker_never_handles_api_or_non_get_requests() -> None:
    source = (web.STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    assert 'request.method !== "GET"' in source
    assert 'url.pathname.startsWith("/studio/")' in source
    # nothing but shell paths may be listed for precaching
    listed = re.findall(r'"(/[^"]*)"', source.split("const SHELL = [")[1].split("];")[0])
    assert listed and all(p.startswith("/studio/") for p in listed)


def test_manifest_is_installable_and_scoped() -> None:
    manifest = json.loads((web.STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["display"] == "standalone" and manifest["dir"] == "rtl"
    assert manifest["scope"] == "/studio/" and manifest["start_url"].startswith("/studio/")
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in manifest["icons"]:
        assert (web.STATIC_DIR / icon["src"].rsplit("/", 1)[1]).is_file()


def test_html_has_no_inline_script_style_or_event_handler() -> None:
    html = (web.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert "<style" not in html and not re.search(r"\son[a-z]+=", html)
    assert 'lang="fa"' in html and 'dir="rtl"' in html
    assert not re.search(r'(?:src|href)="https?://', html)


def test_client_code_never_stores_or_forwards_credentials() -> None:
    sources = {p.name: p.read_text(encoding="utf-8") for p in web.STATIC_DIR.glob("*.js")}
    for name, text in sources.items():
        assert "innerHTML" not in text and "outerHTML" not in text, name
        assert "insertAdjacentHTML" not in text and "document.write" not in text, name
        assert "eval(" not in text and "new Function" not in text, name
        assert "sessionStorage" not in text and "indexedDB" not in text, name
        assert "Authorization" not in text and "Bearer" not in text, name
        assert not re.search(r"https?://", text), name
    # localStorage is limited to non-secret per-browser conveniences (theme, recent job ids).
    keys = set()
    for text in sources.values():
        for match in re.finditer(
            r"localStorage\.(?:get|set|remove)Item\(\s*([A-Z_]+|\"[^\"]+\")", text
        ):
            keys.add(match.group(1))
    assert keys <= {"THEME_KEY", "RECENT_KEY"}, keys
    # the only place a secret ever appears is the one-time login body
    assert sources["api.js"].count("token") <= 3


def test_shared_font_is_served_not_copied() -> None:
    assert web.FONT_PATH.is_file()
    assert not list(web.STATIC_DIR.glob("*.ttf")) and not list(web.STATIC_DIR.rglob("*.woff*"))
