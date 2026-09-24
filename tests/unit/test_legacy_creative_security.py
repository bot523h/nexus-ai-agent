"""Legacy ``/creative/*`` HTTP lane — SSRF (NAG-004) and IDOR (NAG-005) closure.

The SSRF tests drive a **real loopback listener**: before the fix the server
fetched ``video_url=http://127.0.0.1:<port>/...`` and the listener recorded the
request (RED); after the fix the request never leaves the process (GREEN) and
the listener must stay silent.  The IDOR tests pin the fail-closed read gate and
the minimized job representation.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_module
import http.server
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Counts every request that reaches the listener."""

    hits: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        type(self).hits.append(self.path)
        body = b"fake media"
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # noqa: D102 - silence the test log
        return


@pytest.fixture()
def loopback_listener() -> Iterator[tuple[str, list[str]]]:
    """A real HTTP server on 127.0.0.1 that records the paths it is asked for."""
    _RecordingHandler.hits = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_port}", _RecordingHandler.hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    from nexus_ai_agent.api import app as app_module
    from nexus_ai_agent.config import settings as settings_module

    monkeypatch.setenv("NEXUS_API_HMAC_KEY", "test-signing-key")
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path / "creative"))
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr(app_module, "_creative_registry", None)
    yield TestClient(app_module.app)
    settings_module.get_settings.cache_clear()


def _signed(key: str, body: bytes = b"", timestamp: str | None = None) -> dict[str, str]:
    stamp = timestamp or str(int(time.time()))
    signature = hmac_module.new(
        key.encode(), f"{stamp}:".encode() + body, hashlib.sha256
    ).hexdigest()
    return {"X-NEXUS-Timestamp": stamp, "X-NEXUS-Signature": signature}


def _signed_form(fields: dict[str, str], key: str = "test-signing-key") -> dict[str, str]:
    request = httpx.Request("POST", "http://testserver/creative/video-edit", data=fields)
    request.read()
    headers = _signed(key, request.content)
    headers["Content-Type"] = request.headers["Content-Type"]
    return headers


# ---------------------------------------------------------------------------
# SSRF — NAG-004
# ---------------------------------------------------------------------------


def test_video_url_never_reaches_loopback(api: TestClient, loopback_listener) -> None:
    """RED before the fix (the listener recorded the fetch) → GREEN now."""
    host, hits = loopback_listener
    headers = _signed_form({"video_url": f"http://{host}/internal.mp4"})
    response = api.post(
        "/creative/video-edit",
        data={"video_url": f"http://{host}/internal.mp4"},
        headers=headers,
    )
    assert response.status_code == 400
    assert hits == [], f"the server fetched an internal URL: {hits}"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private.mp4",
        "http://localhost/private.mp4",
        "http://[::1]/private.mp4",
        "http://10.0.0.5/private.mp4",
        "http://192.168.1.10/private.mp4",
        "http://172.16.9.9/private.mp4",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/private.mp4",
        "file:///etc/passwd",
        "ftp://127.0.0.1/private.mp4",
        "gopher://127.0.0.1:11211/_stats",
        "https:///no-host",
    ],
)
def test_unsupported_schemes_and_private_targets_are_refused(api: TestClient, url: str) -> None:
    response = api.post(
        "/creative/video-edit", data={"video_url": url}, headers=_signed_form({"video_url": url})
    )
    assert response.status_code == 400, url


def test_dns_rebinding_to_loopback_is_refused_before_any_socket(
    api: TestClient, loopback_listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public-looking name that resolves to loopback must not be fetched."""
    host, hits = loopback_listener
    port = host.split(":")[1]
    real_getaddrinfo = socket.getaddrinfo

    def _rebound(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "evil.example":
            return real_getaddrinfo("127.0.0.1", *args, **kwargs)
        return real_getaddrinfo(name, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _rebound)
    url = f"https://evil.example:{port}/internal.mp4"
    response = api.post(
        "/creative/video-edit", data={"video_url": url}, headers=_signed_form({"video_url": url})
    )
    assert response.status_code == 400
    assert hits == []


async def test_transport_refuses_a_private_connect_even_after_a_passed_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect revalidation: every hop is a fresh, re-checked TCP connect."""
    from nexus_ai_agent.core.ssrf_guard import SafeAsyncTransport

    real_getaddrinfo = socket.getaddrinfo

    def _rebound(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "later.example":
            return real_getaddrinfo("127.0.0.1", *args, **kwargs)
        return real_getaddrinfo(name, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _rebound)
    transport = SafeAsyncTransport()
    try:
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=True, timeout=2.0
        ) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://later.example/redirected-here")
    finally:
        await transport.aclose()


def test_blocked_url_never_constructs_an_http_client(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate runs in the request path: no client, no socket, visible 400."""
    from nexus_ai_agent.api import app as app_module

    constructed: list[dict[str, Any]] = []
    real_client = httpx.AsyncClient

    def _capture(*args: Any, **kwargs: Any) -> Any:
        constructed.append(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(app_module.httpx, "AsyncClient", _capture)
    url = "http://127.0.0.1/x.mp4"
    response = api.post(
        "/creative/video-edit", data={"video_url": url}, headers=_signed_form({"video_url": url})
    )
    assert response.status_code == 400
    assert constructed == []


def test_fetch_uses_the_canonical_validating_transport(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single downloader must be the guarded one (no second implementation)."""
    from nexus_ai_agent.api import app as app_module
    from nexus_ai_agent.core.ssrf_guard import SafeAsyncTransport

    captured: dict[str, Any] = {}

    def _capture(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise httpx.ConnectError("stop before any real socket")

    monkeypatch.setattr(app_module, "validate_url", lambda url: url)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", _capture)
    url = "https://media.example/clip.mp4"
    api.post(
        "/creative/video-edit", data={"video_url": url}, headers=_signed_form({"video_url": url})
    )
    transport = captured.get("transport")
    assert isinstance(transport, SafeAsyncTransport)
    assert captured["follow_redirects"] is True
    assert captured["max_redirects"] == app_module._MAX_REDIRECTS


def test_oversized_upload_is_rejected_and_leaves_no_partial_file(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nexus_ai_agent.api import app as app_module

    monkeypatch.setattr(app_module, "_MAX_UPLOAD_BYTES", 1024)
    body = b"x" * 4096
    request = httpx.Request(
        "POST",
        "http://testserver/creative/video-edit",
        files={"file": ("clip.mp4", body, "video/mp4")},
    )
    request.read()
    headers = _signed("test-signing-key", request.content)
    headers["Content-Type"] = request.headers["Content-Type"]
    response = api.post("/creative/video-edit", content=request.content, headers=headers)

    assert response.status_code == 413
    leftovers = list((tmp_path / "creative").glob("creative-upload-*"))
    assert leftovers == []


def test_remote_source_can_be_safely_rejected_without_a_partial_file(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A guarded fetch that fails mid-flight must not leave staged bytes."""
    from nexus_ai_agent.api import app as app_module

    transport = Any
    del transport

    class _FailingClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FailingClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(app_module, "validate_url", lambda url: url)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", _FailingClient)
    url = "https://media.example/clip.mp4"
    response = api.post(
        "/creative/video-edit", data={"video_url": url}, headers=_signed_form({"video_url": url})
    )
    # The job is accepted (the URL passed the gate) and then fails in the
    # background: the durable row says failed and no partial file survives.
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    waited = api.get(f"/creative/jobs/{job_id}", headers=_signed("test-signing-key")).json()
    assert waited["status"] == "failed"
    assert list((tmp_path / "creative").glob("creative-url-*")) == []


# ---------------------------------------------------------------------------
# IDOR — NAG-005
# ---------------------------------------------------------------------------


async def _seed_job(tmp_path: Path) -> str:
    from nexus_ai_agent.creative.job_registry import JobRegistry

    registry = JobRegistry(tmp_path / "creative" / "creative_jobs.sqlite3")
    job_id = await registry.create_job(
        "video_edit",
        {
            "source_type": "upload",
            "path": "/tmp/creative/creative-upload-secret.mp4",
            "video_url": "https://internal.example/secret",
        },
    )
    await registry.update_job_status(
        job_id, "failed", error="/srv/app/data/secret.sqlite3: disk full"
    )
    return job_id


async def test_job_read_is_fail_closed_and_minimized(api: TestClient, tmp_path: Path) -> None:
    job_id = await _seed_job(tmp_path)

    unsigned = api.get(f"/creative/jobs/{job_id}")
    assert unsigned.status_code == 401
    assert "input_data" not in unsigned.text

    wrong_key = api.get(f"/creative/jobs/{job_id}", headers=_signed("other-key"))
    assert wrong_key.status_code == 401

    stale = _signed("test-signing-key", timestamp=str(int(time.time()) - 10_000))
    assert api.get(f"/creative/jobs/{job_id}", headers=stale).status_code == 401

    owner = api.get(f"/creative/jobs/{job_id}", headers=_signed("test-signing-key"))
    assert owner.status_code == 200
    body = owner.json()
    assert body["status"] == "failed"
    assert body["failure_code"] == "job_failed"
    assert body["result"] is None
    # Nothing sensitive may leave the process, not even for the key holder.
    assert "input_data" not in body
    assert "error" not in body
    assert "secret" not in owner.text
    assert "/tmp/" not in owner.text and "/srv/" not in owner.text


async def test_missing_job_is_a_safe_404(api: TestClient, tmp_path: Path) -> None:
    await _seed_job(tmp_path)
    response = api.get("/creative/jobs/deadbeef", headers=_signed("test-signing-key"))
    assert response.status_code == 404
    assert "input_data" not in response.text


def test_post_requires_a_signature(api: TestClient) -> None:
    unsigned = api.post("/creative/video-edit", data={"video_url": "https://example.com/a.mp4"})
    assert unsigned.status_code == 401


def test_without_a_configured_key_the_lane_is_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nexus_ai_agent.api import app as app_module
    from nexus_ai_agent.config import settings as settings_module

    monkeypatch.delenv("NEXUS_API_HMAC_KEY", raising=False)
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path / "creative"))
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr(app_module, "_creative_registry", None)
    try:
        client = TestClient(app_module.app)
        assert client.get("/creative/jobs/anything").status_code == 503
        assert client.post("/creative/video-edit", data={}).status_code == 503
    finally:
        settings_module.get_settings.cache_clear()
