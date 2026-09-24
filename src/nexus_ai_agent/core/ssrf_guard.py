"""SSRF protection for outbound URL fetching.

Threat model
------------
A bot that fetches a user-supplied URL can be abused to probe or attack
internal networks (SSRF): cloud metadata endpoints, loopback services,
RFC1918 hosts, or DNS-rebinding attacks that pass a pre-check and then
resolve to a private address at connect time.

Defence in depth applied here:

1. :func:`validate_url` — before any fetch: only ``https`` is allowed and
   *every* address returned by ``getaddrinfo`` must be public.
2. :class:`SafeAsyncTransport` — the httpx transport used for the fetch
   wraps an httpcore network backend that re-resolves and re-checks the
   address at the moment of *each* TCP connection.  Redirects therefore
   trigger fresh checks, and because the inner backend is asked to connect
   to the already-validated IP literal, no second DNS lookup can swap the
   address (closes the TOCTOU / DNS-rebinding window).

Blocked address ranges: 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12,
192.168.0.0/16, 169.254.0.0/16 (cloud metadata), 0.0.0.0/8, ::1, fe80::/10,
fc00::/7 — plus the standard private/reserved/loopback/link-local checks.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import AsyncIterable
from typing import Any, cast

import httpcore
import httpx

# Networks that must never be reachable through outbound fetches.
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),  # IPv4 loopback
    ipaddress.ip_network("10.0.0.0/8"),  # RFC1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC1918
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata
    ipaddress.ip_network("0.0.0.0/8"),  # "this network"
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
)


class SSRFBlockError(ValueError):
    """Raised when a URL or resolved address is not safe to fetch."""


def is_public_ip(ip: str) -> bool:
    """Return True only for addresses safe to connect to."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_private
    ):
        return False
    return not any(addr in net for net in _BLOCKED_NETWORKS if net.version == addr.version)


def _resolve_public_ip(host: str) -> str:
    """Resolve *host* and return the first address that passes the check.

    Raises:
        SSRFBlockError: if the host is empty, does not resolve, or any
            resolved address is not public.
    """
    if not host:
        raise SSRFBlockError("URL has no host")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SSRFBlockError(f"DNS resolution failed for {host!r}: {exc}") from exc
    if not infos:
        raise SSRFBlockError(f"No addresses resolved for {host!r}")
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = str(sockaddr[0])
        if not is_public_ip(ip):
            raise SSRFBlockError(f"Blocked non-public address for {host!r}: {ip}")
    return str(infos[0][4][0])


def validate_url(url: str) -> httpx.URL:
    """Validate *url* before fetching: https-only and public addresses only."""
    parsed = httpx.URL(url)
    if parsed.scheme != "https":
        raise SSRFBlockError(f"Only https URLs may be fetched (got scheme {parsed.scheme!r})")
    if not parsed.host:
        raise SSRFBlockError("URL has no host")
    _resolve_public_ip(parsed.host)
    return parsed


class _ValidatingNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore backend that re-checks the resolved IP at connect time.

    The inner backend is handed the validated IP literal instead of the
    hostname, so the actual TCP connect goes to exactly the address that
    passed the SSRF check.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend) -> None:
        self._inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        ip = await asyncio.to_thread(_resolve_public_ip, host)
        return await self._inner.connect_tcp(
            ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        raise SSRFBlockError("Unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _build_ssl_context(verify: bool = True) -> ssl.SSLContext:
    if verify:
        return ssl.create_default_context()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class SafeAsyncTransport(httpx.AsyncBaseTransport):
    """httpx transport that only ever connects to public addresses.

    Every connection (including every redirect target) is resolved and
    checked at connect time; blocked targets surface as ``httpx.ConnectError``.
    """

    def __init__(self, verify: bool = True) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=_build_ssl_context(verify),
            network_backend=_ValidatingNetworkBackend(httpcore.AnyIOBackend()),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            with httpx._transports.default.map_httpcore_exceptions():
                resp = await self._pool.handle_async_request(req)
        except SSRFBlockError as exc:
            raise httpx.ConnectError(str(exc), request=request) from exc
        # Wrap the httpcore stream exactly like httpx's own default
        # transport does (AsyncClient asserts the response stream is an
        # httpx.AsyncByteStream — the raw httpcore stream is not one, so
        # a *successful* fetch would crash without this wrapper).
        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=httpx._transports.default.AsyncResponseStream(
                cast(AsyncIterable[bytes], resp.stream)
            ),
            extensions=resp.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()
