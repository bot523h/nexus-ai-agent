"""The default deployment may not expose the unauthenticated dashboard.

P0-5 (``docs/DECISION_LOG.md``) decided that ``/api/dashboard/*`` keeps an
*optional* bearer token: when ``NEXUS_DASHBOARD_TOKEN`` is unset the API answers
without one, and the reason that is acceptable is the second half of the same
decision — ``docker-compose.yml`` publishes the service on ``127.0.0.1`` only, so
the default deployment is not reachable from off-host.  Until this guard existed
nothing verified that half: the mitigation lived in a YAML comment and a sentence
in ``docs/architecture/SECURITY.md``.  A one-line edit
(``"127.0.0.1:8000:8000"`` → ``"8000:8000"``) would have published an API that
answers unauthenticated requests, and the suite would have stayed green.

The guard is dependency-free on purpose: PyYAML is not a declared dependency of
this project (it is present only transitively, via another package), so the
published-port block is read with a small indentation-aware reader that
understands both the short (``"IP:host:container"``) and the long
(``target:``/``published:``/``host_ip:``) compose spellings.  A compose layout
this reader cannot make sense of fails the test instead of passing it: a control
that cannot be found is not a control that is present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from nexus_ai_agent.config.settings import Settings

REPO_ROOT = Path(__file__).parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"

#: Host addresses that keep a published port off every other interface.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "[::1]", "localhost"})

_SERVICE = re.compile(r"^ {2}([A-Za-z0-9_-]+):\s*$")
_LONG_KEY = re.compile(r"(?:^|[\s,{])(host_ip|published|target|protocol|mode|name|app_protocol):")
_HOST_IP = re.compile(r"(?:^|[\s,{])host_ip:\s*([^\s,}]+)")


@dataclass(frozen=True)
class PublishedPort:
    """One entry of a service's ``ports:`` block.

    ``host_ip is None`` means the entry publishes the port on **every**
    interface, which is exactly what the P0-5 mitigation forbids.
    """

    service: str
    entry: str
    host_ip: str | None

    @property
    def loopback_only(self) -> bool:
        return self.host_ip in LOOPBACK_HOSTS


def _services(text: str) -> dict[str, list[str]]:
    """Service name -> its body lines, read from the ``services:`` block."""
    services: dict[str, list[str]] = {}
    current: str | None = None
    in_services = False
    for line in text.splitlines():
        if line.startswith("services:"):
            in_services = True
            continue
        if in_services and line and not line.startswith((" ", "#")):
            break  # a new top-level key ends the block
        match = _SERVICE.match(line)
        if in_services and match:
            current = match.group(1)
            services[current] = []
            continue
        if current is not None and line.strip():
            services[current].append(line)
    return services


def _port_entries(lines: list[str]) -> list[str]:
    """Raw ``ports:`` list entries of one service, one string per item.

    A list item may span several lines (the long syntax), so anything more
    indented than the current item is folded into it.
    """
    entries: list[str] = []
    in_ports = False
    ports_indent = 0
    item_indent = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if stripped.startswith("ports:"):
            in_ports = True
            ports_indent = indent
            continue
        if not in_ports:
            continue
        if indent <= ports_indent:
            in_ports = False
            continue
        if stripped.startswith(("- ", "-")):
            item_indent = indent
            entries.append(stripped[2:].strip())
        elif entries and indent > item_indent:
            entries[-1] = f"{entries[-1]} {stripped}"
    return entries


def _host_ip(entry: str) -> str | None:
    """Host IP a compose port entry binds to, or ``None`` when it binds all.

    Short syntax is ``container``, ``host:container`` or ``ip:host:container``;
    the first two publish on every interface.  Long syntax publishes on every
    interface unless ``host_ip`` is present.
    """
    text = entry.strip().strip("\"'")
    if not _LONG_KEY.match(text):
        if text.startswith("["):  # IPv6 host: "[::1]:8000:8000"
            return text[: text.index("]") + 1] if "]" in text else None
        parts = text.split(":")
        return parts[0] if len(parts) == 3 else None
    match = _HOST_IP.search(text)
    return match.group(1).strip("\"'") if match else None


def _published_ports(text: str) -> list[PublishedPort]:
    ports: list[PublishedPort] = []
    for service, lines in _services(text).items():
        for entry in _port_entries(lines):
            if entry:
                ports.append(PublishedPort(service, entry, _host_ip(entry)))
    return ports


def _compose_text() -> str:
    assert COMPOSE.is_file(), f"missing {COMPOSE.name} — the deployment guard has lost its subject"
    return COMPOSE.read_text(encoding="utf-8")


def test_every_published_port_is_loopback_bound() -> None:
    """No service may publish a port on every interface.

    The dashboard is the service that makes this load-bearing (it answers
    unauthenticated when no token is configured), but the rule is stated for the
    whole file so that a future service cannot inherit the exemption by
    accident.
    """
    exposed = [port for port in _published_ports(_compose_text()) if not port.loopback_only]
    assert not exposed, (
        "published ports that are not loopback-bound: "
        + "; ".join(f"{port.service}: {port.entry}" for port in exposed)
        + " — see NEXUS_DASHBOARD_TOKEN / P0-5 in docs/DECISION_LOG.md"
    )


def test_the_dashboard_service_publishes_a_loopback_port() -> None:
    """Fail-closed coupling: the P0-5 mitigation names this service.

    If the service is renamed, unpublished, or stops declaring ``ports:``, this
    test fails on purpose.  Silently passing would mean the guard stopped
    watching the only surface it was written for.
    """
    services = _services(_compose_text())
    assert "dashboard" in services, (
        "no 'dashboard' service in docker-compose.yml: if it was renamed, update this guard "
        "and the P0-5 row in docs/architecture/SECURITY.md"
    )
    ports = [port for port in _published_ports(_compose_text()) if port.service == "dashboard"]
    assert ports, "the dashboard service publishes no port — the P0-5 rationale no longer holds"
    assert all(port.loopback_only for port in ports), (
        "the dashboard is published beyond loopback: " + "; ".join(port.entry for port in ports)
    )


def test_the_optional_token_is_why_the_bind_is_load_bearing() -> None:
    """The two halves of P0-5 are asserted together, not in two places.

    ``NEXUS_DASHBOARD_TOKEN`` has no default *by design*, so the loopback bind is
    the control that keeps the default deployment private.  If the token ever
    becomes mandatory, this test fails so that whoever changed it re-reads the
    rationale and updates both the guard and the docs instead of leaving a
    comment claiming a protection that a later edit already removed.
    """
    default = Settings.model_fields["api_dashboard_token"].default
    assert default is None, (
        "NEXUS_DASHBOARD_TOKEN now has a default: if the dashboard is mandatory-token now, "
        "update this guard and SECURITY.md T3 (the loopback bind is no longer the only control)"
    )


_PROBE = """services:
  dashboard:
    build: .
    ports:
      - {entry}
"""


@pytest.mark.parametrize(
    ("entry", "loopback_only"),
    [
        ('"127.0.0.1:8000:8000"', True),
        ('"localhost:8000:8000"', True),
        ('"[::1]:8000:8000"', True),
        ('"8000:8000"', False),  # two parts ⇒ host port on every interface
        ("8000", False),  # one part ⇒ random host port on every interface
        ('"0.0.0.0:8000:8000"', False),
        ('"192.168.1.10:8000:8000"', False),
        ("target: 8000", False),  # long syntax without host_ip
        ("host_ip: 127.0.0.1", True),
    ],
)
def test_the_port_reader_can_actually_fire(entry: str, loopback_only: bool) -> None:
    """Red-proof: the reader must discriminate, not merely parse.

    Written against a synthetic compose fragment so the repository's real file
    is never edited to prove the guard works.
    """
    ports = _published_ports(_PROBE.format(entry=entry))
    assert [port.service for port in ports] == ["dashboard"], ports
    assert ports[0].loopback_only is loopback_only, ports[0]
