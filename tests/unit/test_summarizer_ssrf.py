"""SSRF guard tests for SummarizerEngine.summarize_url (Phase 0).

No network I/O happens in these tests: DNS is monkeypatched and the
fetch itself is spied on (and must not be attempted for blocked targets).
"""

from __future__ import annotations

import socket
from typing import Any

import httpcore
import pytest

from nexus_ai_agent.core import ssrf_guard
from nexus_ai_agent.features.summarizer import SummarizerEngine

PUBLIC_IP = "93.184.216.34"


def _fake_getaddrinfo(ip: str):
    def fake(host, port=None, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 443))]

    return fake


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "127.8.8.8",
        "10.0.0.5",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",
        "0.1.2.3",
        "::1",
        "fe80::1",
        "fc00::1",
        "fd12:3456::1",
    ],
)
def test_is_public_ip_blocks_private(ip: str) -> None:
    assert ssrf_guard.is_public_ip(ip) is False


@pytest.mark.parametrize(
    "ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "172.32.0.1", "2001:4860:4860::8888"]
)
def test_is_public_ip_allows_public(ip: str) -> None:
    assert ssrf_guard.is_public_ip(ip) is True


@pytest.mark.asyncio
async def test_engine_uses_safe_transport() -> None:
    engine = SummarizerEngine(gemini_api_key="test")
    try:
        assert isinstance(engine._http._transport, ssrf_guard.SafeAsyncTransport)
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["http", "ftp", "file"])
async def test_summarize_url_rejects_non_https(monkeypatch, scheme: str) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(PUBLIC_IP))
    engine = SummarizerEngine(gemini_api_key="test")
    try:
        result = await engine.summarize_url(f"{scheme}://example.com/page")
    finally:
        await engine.close()
    assert result.error
    assert "https" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.1.2.3", "172.16.5.5", "192.168.0.1", "169.254.1.1", "0.0.0.0"]
)
async def test_summarize_url_rejects_private_ip_prefetch(monkeypatch, ip: str) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(ip))
    engine = SummarizerEngine(gemini_api_key="test")
    fetches: list[Any] = []

    async def spy_get(*args: Any, **kwargs: Any) -> Any:
        fetches.append((args, kwargs))
        raise AssertionError("no fetch should be attempted for a blocked host")

    monkeypatch.setattr(engine._http, "get", spy_get)
    try:
        result = await engine.summarize_url(f"https://host-{ip.replace('.', '_')}.example/page")
    finally:
        await engine.close()
    assert result.error
    assert "non-public" in result.error
    assert fetches == []


@pytest.mark.asyncio
async def test_summarize_url_public_host_passes_guard(monkeypatch) -> None:
    """Prefetch validation passes for a public host (no fetch performed)."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(PUBLIC_IP))
    url = ssrf_guard.validate_url("https://example.com/page")
    assert url.host == "example.com"


@pytest.mark.asyncio
async def test_connect_time_check_blocks_rebinding(monkeypatch) -> None:
    """DNS rebinding simulation.

    The first lookup (prefetch) returns a public IP; the second lookup,
    performed at connect time by the transport, returns a loopback IP.
    The fetch must be blocked before any socket is opened.
    """
    answers = iter([PUBLIC_IP, "127.0.0.1"])

    def fake(host, port=None, *args, **kwargs):
        ip = next(answers, "127.0.0.1")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    engine = SummarizerEngine(gemini_api_key="test")
    try:
        result = await engine.summarize_url("https://rebind.example.com/page")
    finally:
        await engine.close()
    assert result.error
    assert "non-public" in result.error


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    """Inner backend that records the host it is asked to connect to."""

    def __init__(self) -> None:
        self.connected: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        self.connected.append(host)
        return httpcore.AsyncNetworkStream()

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        return None


@pytest.mark.asyncio
async def test_backend_connects_to_validated_ip_literal(monkeypatch) -> None:
    """The inner backend must receive the validated IP, not the hostname."""
    inner = _RecordingBackend()
    backend = ssrf_guard._ValidatingNetworkBackend(inner)
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(PUBLIC_IP))
    await backend.connect_tcp("public.example.com", 443)
    assert inner.connected == [PUBLIC_IP]


@pytest.mark.asyncio
async def test_backend_blocks_private_at_connect(monkeypatch) -> None:
    """Redirect target resolving to a private IP is blocked at connect time."""
    inner = _RecordingBackend()
    backend = ssrf_guard._ValidatingNetworkBackend(inner)
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("192.168.1.10"))
    with pytest.raises(ssrf_guard.SSRFBlockError):
        await backend.connect_tcp("internal.example.com", 443)
    assert inner.connected == []
