"""S5: SSRF hardening of the legacy video_url download path.

``POST /creative/video-edit`` (deprecated lane, ADR 0006) accepts an
attacker-controlled ``video_url``. These tests prove the *real* path

    user URL → validate_url (fail-fast, no job row / no temp file)
            → SafeAsyncTransport (re-resolves + re-checks EVERY
              connection, redirect hops included, DNS-pinned)

against the classic SSRF vector list: localhost/loopback, RFC1918,
cloud metadata, IPv6 loopback, IPv4-mapped IPv6, plain http scheme,
userinfo tricks, DNS rebinding at connect time, public→private
redirects, and temp-file litter on refusal.

Only the TCP stream itself is faked (an in-memory httpcore backend for
the redirect test); ``validate_url``, ``SafeAsyncTransport``,
``_ValidatingNetworkBackend``, the httpx redirect machinery and the
route handler are the real production code paths. No test
monkeypatches the guard logic itself — that would be a fake success.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.core.ssrf_guard import SafeAsyncTransport, SSRFBlockError, validate_url

# ------------------------------------------------------------------ #
# fixtures
# ------------------------------------------------------------------ #


@pytest.fixture()
def _fresh_settings_cache():
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


@pytest.fixture()
def api_client(_fresh_settings_cache) -> TestClient:
    from nexus_ai_agent.api.app import app

    return TestClient(app)


@pytest.fixture()
def temp_download_dir(
    _fresh_settings_cache: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point creative_temp_dir at a scratch dir so we can inspect litter."""
    import nexus_ai_agent.api.app as app_module

    target = tmp_path / "creative-tmp"
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: settings_module.Settings(creative_temp_dir=str(target)),
    )
    return target


def _signed_form_headers(key: str, form: dict[str, str]) -> dict[str, str]:
    """HMAC-sign a urlencoded form body exactly as the endpoint verifies it."""
    body = urlencode(form).encode()
    timestamp = str(int(time.time()))
    message = f"{timestamp}:".encode() + body
    signature = hmac.new(key.encode(), message, hashlib.sha256).hexdigest()
    return {
        "X-NEXUS-Timestamp": timestamp,
        "X-NEXUS-Signature": signature,
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _public_getaddrinfo(host: str, port: Any = None, *a: Any, **k: Any) -> list[tuple[Any, ...]]:
    """Resolve every name to a public IP (tests never touch real DNS)."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]


BLOCKED_URLS = [
    "https://localhost/x.mp4",
    "https://127.0.0.1/x.mp4",
    "https://10.0.0.5/x.mp4",
    "https://172.16.0.9/x.mp4",
    "https://192.168.1.10/x.mp4",
    "https://169.254.169.254/latest/meta-data/",
    "https://0.0.0.0/x.mp4",
    "https://[::1]/x.mp4",
    "https://[::ffff:169.254.169.254]/x.mp4",
    "https://[::ffff:127.0.0.1]/x.mp4",
    "http://example.com/x.mp4",
    "https://evil.com@127.0.0.1/x.mp4",
]


# ------------------------------------------------------------------ #
# 1. validate_url matrix (pure guard — no fakes involved)
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("url", BLOCKED_URLS)
def test_validate_url_blocks_every_ssrf_vector(url: str) -> None:
    with pytest.raises(SSRFBlockError):
        validate_url(url)


def test_validate_url_allows_public_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    parsed = validate_url("https://cdn.example.com/video.mp4")
    assert parsed.host == "cdn.example.com"


# ------------------------------------------------------------------ #
# 2. Route-level fail-fast: unsafe video_url → 400, no job row
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("url", BLOCKED_URLS)
def test_video_edit_rejects_unsafe_url_with_400(
    api_client: TestClient, url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXUS_API_HMAC_KEY", "test-key")
    settings_module.get_settings.cache_clear()
    headers = _signed_form_headers("test-key", {"video_url": url})
    response = api_client.post(
        "/creative/video-edit", data=urlencode({"video_url": url}), headers=headers
    )
    assert response.status_code == 400, f"{url} must be rejected before a job is created"
    assert "rejected" in response.json()["detail"]


def test_video_edit_accepts_safe_url_shape(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public https URL passes the fail-fast gate (job is created; the
    background download would then still be connect-time guarded)."""
    monkeypatch.setenv("NEXUS_API_HMAC_KEY", "test-key")
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    headers = _signed_form_headers("test-key", {"video_url": "https://cdn.example.com/video.mp4"})
    response = api_client.post(
        "/creative/video-edit",
        data=urlencode({"video_url": "https://cdn.example.com/video.mp4"}),
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "pending"


# ------------------------------------------------------------------ #
# 3. Download path: preflight refusal leaves no temp file
# ------------------------------------------------------------------ #


async def test_download_refuses_private_url_before_temp_file(temp_download_dir: Path) -> None:
    from nexus_ai_agent.api.app import _download_video_to_temp

    with pytest.raises(SSRFBlockError):
        await _download_video_to_temp("https://169.254.169.254/latest/meta-data/")
    temp_download_dir.mkdir(parents=True, exist_ok=True)
    # refused URL must not leave a temp file
    assert not list(temp_download_dir.glob("creative-url-*"))


async def test_download_cleans_temp_file_when_connect_is_refused(
    temp_download_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DNS rebinding: preflight resolves public, connect-time resolves
    private → the *connect-time* check must refuse (not preflight-only)
    and the partially-created temp file must be removed."""
    from nexus_ai_agent.api.app import _download_video_to_temp

    resolves = {"count": 0}

    def rebind_getaddrinfo(host: str, port: Any = None, *a: Any, **k: Any) -> list[tuple[Any, ...]]:
        resolves["count"] += 1
        if resolves["count"] == 1:
            ip = "93.184.216.34"  # preflight: public
        else:
            ip = "169.254.169.254"  # connect time: rebound to metadata IP
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", rebind_getaddrinfo)
    with pytest.raises(httpx.ConnectError) as excinfo:
        await _download_video_to_temp("https://rebind.example.com/video.mp4")
    # The error must come from the *validating* transport, not from a raw
    # TCP/TLS failure — that is the proof the download path is guarded at
    # connect time.
    assert "Blocked non-public address" in str(excinfo.value)
    # blocked download must clean its temp file
    assert not list(temp_download_dir.glob("creative-url-*"))


# ------------------------------------------------------------------ #
# 4. Redirect validation: public host 302→ private target is refused
# ------------------------------------------------------------------ #


class _ScriptedStream(httpcore.AsyncNetworkStream):
    """An in-memory TLS-already-established stream that serves one
    scripted HTTP/1.1 response."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._sent = False

    async def read(self, amount: int, timeout: Any = None) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return self._payload

    async def write(self, data: bytes, timeout: Any = None) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def start_tls(self, server_hostname: str, ssl_context: Any, timeout: Any = None) -> Any:
        return self  # "TLS" already established in fake-land

    def get_extra_info(self, info: str) -> Any:
        return None


class _ScriptedBackend(httpcore.AsyncNetworkBackend):
    """Returns a scripted 302 response for the first connection."""

    def __init__(self, response: bytes) -> None:
        self._response = response
        self.connects: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Any = None,
        local_address: Any = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connects.append(f"{host}:{port}")
        return _ScriptedStream(self._response)

    async def connect_unix_socket(
        self, path: str, timeout: Any = None, socket_options: Any = None
    ) -> Any:
        raise AssertionError("unix sockets must not be used")

    async def sleep(self, seconds: float) -> None:
        return None


async def test_public_to_private_redirect_is_refused_at_connect_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public host that answers 302 → https://169.254.169.254/ must be
    refused when httpx follows the redirect: the redirect hop's connect
    goes through the same validating backend (re-resolution + IP pin)."""
    import ipaddress

    def literal_aware_getaddrinfo(
        host: str, port: Any = None, *a: Any, **k: Any
    ) -> list[tuple[Any, ...]]:
        # IP literals resolve to themselves; names resolve to a public IP.
        try:
            ipaddress.ip_address(host)
            addr: str = host
        except ValueError:
            addr = "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port or 443))]

    redirect_response = (
        b"HTTP/1.1 302 Found\r\n"
        b"Location: https://169.254.169.254/latest/meta-data/\r\n"
        b"Content-Length: 0\r\n"
        b"\r\n"
    )
    backend = _ScriptedBackend(redirect_response)
    transport = SafeAsyncTransport()
    # Swap only the innermost TCP layer with an in-memory fake; the
    # validating wrapper stays real (it still resolves + checks + pins).
    monkeypatch.setattr(transport._pool._network_backend, "_inner", backend)
    monkeypatch.setattr(socket, "getaddrinfo", literal_aware_getaddrinfo)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(httpx.ConnectError) as excinfo:
            await client.get("https://public.example.com/video.mp4")
    assert "Blocked non-public address" in str(excinfo.value)
    # Hop 1 connected (scripted public), hop 2 (metadata) was refused
    # before any TCP connection: it never appears in backend.connects.
    assert backend.connects == ["93.184.216.34:443"]


# ------------------------------------------------------------------ #
# 5. The download path really uses SafeAsyncTransport (not preflight-only)
# ------------------------------------------------------------------ #


def test_download_transport_is_the_validating_one() -> None:
    """Static proof on the production code: the AsyncClient in
    ``_download_video_to_temp`` is built with SafeAsyncTransport."""
    import inspect

    from nexus_ai_agent.api import app as app_module

    source = inspect.getsource(app_module._download_video_to_temp)
    assert "SafeAsyncTransport()" in source, "download client must use the validating transport"
    assert "follow_redirects=True" in source


# ------------------------------------------------------------------ #
# 6. Adversarial closure (S5): CGNAT, scheme-downgrade redirects,
#    malformed-host contract — each pinned by a real runtime proof.
# ------------------------------------------------------------------ #


def _closure_getaddrinfo(host: str, port: Any = None, *a: Any, **k: Any) -> list[tuple[Any, ...]]:
    """IP literals resolve to themselves; names resolve to a public IP."""
    try:
        ipaddress.ip_address(host)
        addr: str = host
    except ValueError:
        addr = "93.184.216.34"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port or 443))]


class _ClosureScriptedBackend(httpcore.AsyncNetworkBackend):
    """Serves a *queue* of raw responses, one per connection (the PR's
    single-response backend cannot drive a multi-hop scripted exchange)."""

    def __init__(self, responses: list[bytes]) -> None:
        self._responses = list(responses)
        self.connects: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Any = None,
        local_address: Any = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connects.append(f"{host}:{port}")
        payload = (
            self._responses.pop(0)
            if self._responses
            else b"HTTP/1.1 500 X\r\nContent-Length: 0\r\n\r\n"
        )
        return _ScriptedStream(payload)

    async def connect_unix_socket(
        self, path: str, timeout: Any = None, socket_options: Any = None
    ) -> Any:
        raise AssertionError("unix sockets must not be used")

    async def sleep(self, seconds: float) -> None:
        return None


@pytest.mark.parametrize(
    ("url", "label"),
    [
        ("https://100.64.0.1/x.mp4", "CGNAT low edge"),
        ("https://100.100.100.100/latest/meta-data/", "CGNAT metadata (alibaba)"),
        ("https://[::ffff:100.64.0.1]/x.mp4", "IPv4-mapped CGNAT"),
    ],
)
def test_validate_url_blocks_cgnat(url: str, label: str) -> None:
    """100.64.0.0/10 must be blocked explicitly — stdlib ``is_private``
    coverage for CGNAT is Python-version dependent."""
    with pytest.raises(SSRFBlockError):
        validate_url(url)


def test_validate_url_reports_malformed_host_as_ssrf_block() -> None:
    """Hosts httpx refuses to parse (octal IPv4 ``0177.0.0.1``) must fail
    closed through the contract exception, not leak ``InvalidURL``."""
    with pytest.raises(SSRFBlockError):
        validate_url("https://0177.0.0.1/x.mp4")


async def test_https_to_http_redirect_is_refused_not_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 302 to an http:// URL must NOT be followed: no second (plaintext)
    connection may be established and no body may be consumed."""
    hop1 = (
        b"HTTP/1.1 302 Found\r\n"
        b"Location: http://93.184.216.34:80/payload.bin\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    hop2_payload = b"DOWNGRADED-CONTENT"
    hop2 = (
        b"HTTP/1.1 200 OK\r\nContent-Length: "
        + str(len(hop2_payload)).encode()
        + b"\r\n\r\n"
        + hop2_payload
    )
    backend = _ClosureScriptedBackend([hop1, hop2])
    transport = SafeAsyncTransport()
    transport._pool._network_backend._inner = backend
    monkeypatch.setattr(socket, "getaddrinfo", _closure_getaddrinfo)
    try:
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            with pytest.raises(httpx.ConnectError) as excinfo:
                await client.get("https://public.example.com/v.mp4", timeout=5.0)
        assert "Only https" in str(excinfo.value)
        # hop 1 only — the http hop never even attempted a TCP connect
        assert backend.connects == ["93.184.216.34:443"]
        assert hop2_payload not in (str(excinfo.value),)
    finally:
        await transport.aclose()


def test_transport_refuses_plain_http_at_request_time() -> None:
    """Connect-time scheme gate: an http:// request through the safe
    transport is refused before any backend usage (redirect hops and
    direct abuse of the transport both flow through here)."""
    import pytest as _pytest

    class _Never(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("no connection may be attempted for http scheme")

    transport = SafeAsyncTransport()
    transport._pool._network_backend._inner = _Never()

    async def _run() -> None:
        try:
            async with httpx.AsyncClient(transport=transport) as client:
                with _pytest.raises(httpx.ConnectError) as excinfo:
                    await client.get("http://93.184.216.34/x", timeout=5.0)
            assert "Only https" in str(excinfo.value)
        finally:
            await transport.aclose()

    asyncio.run(_run())
