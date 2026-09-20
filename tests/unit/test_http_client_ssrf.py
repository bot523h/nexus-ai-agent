"""The shared egress client must carry the SSRF / DNS-rebinding guard.

``ResilientHttpClient`` is the sanctioned way to fetch URLs (free_tools,
web_trainer, wikipedia_trainer) — including user-supplied ones. It must run
the ``ssrf_guard`` pre-check (https-only, every resolved address public)
and use the connect-time re-validating transport, closing the rebinding
TOCTOU window for redirects too.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from nexus_ai_agent.core.http_client import ResilientHttpClient
from nexus_ai_agent.core.ssrf_guard import SSRFBlockError, _ValidatingNetworkBackend


@pytest.fixture()
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a test tries to open a real socket connection."""
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("network")),
    )


async def test_get_rejects_plain_http_urls_before_any_network(_no_network: None) -> None:
    client = ResilientHttpClient()

    with pytest.raises(SSRFBlockError, match="https"):
        await client.get("http://example.com/data")


async def test_get_json_blocks_metadata_and_private_targets(_no_network: None) -> None:
    client = ResilientHttpClient()
    assert client._ssrf_protect is True

    # Cloud metadata endpoint by literal IP: getaddrinfo needs no DNS, and
    # the address must be rejected by the pre-check.
    assert await client.get_json("https://169.254.169.254/latest/meta-data") == {}
    assert await client.get_json("https://127.0.0.1:8080/admin") == {}


async def test_protect_can_be_disabled_explicitly(
    _no_network: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nexus_ai_agent.core.http_client as http_client_module

    def _forbidden_validate(url: str) -> httpx.URL:
        raise AssertionError("validate_url must not run with ssrf_protect=False")

    monkeypatch.setattr(http_client_module, "validate_url", _forbidden_validate)
    client = ResilientHttpClient(ssrf_protect=False)
    client._retry.max_attempts = 1
    client._retry.base_delay = 0.0

    # Refused loopback connection: a plain ConnectError, no SSRF gate.
    with pytest.raises(httpx.ConnectError):
        await client.get("http://127.0.0.1:9/x")


async def test_precheck_rejects_when_any_resolved_address_is_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebinding defence: one private address among many ⇒ blocked."""

    def _mixed_resolution(host: str, port: object, **_kw: object) -> list[tuple]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _mixed_resolution)
    client = ResilientHttpClient()

    with pytest.raises(SSRFBlockError, match="non-public"):
        await client.get("https://rebind.example/x")


async def test_connect_time_recheck_refuses_private_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport re-resolves per connection and refuses private IPs."""

    calls: list[str] = []

    def _rebinding_resolution(host: str, port: object, **_kw: object) -> list[tuple]:
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _rebinding_resolution)

    class _FailingBackend:
        async def connect_tcp(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("inner backend reached with an unvalidated host")

        async def connect_unix_socket(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("unix sockets must be refused")

        async def sleep(self, seconds: float) -> None:
            return None

    backend = _ValidatingNetworkBackend(_FailingBackend())  # type: ignore[arg-type]
    with pytest.raises(SSRFBlockError):
        await backend.connect_tcp("public.example", 443)

    assert calls == ["public.example"]  # resolution happened at connect time
