"""Principal-first identity vocabulary (app-first frame, F1 step 1)."""

from __future__ import annotations

import threading

import pytest

from nexus_ai_agent.application.ports.channel import ChannelPort, OutboundMessage
from nexus_ai_agent.application.principal import (
    BindingConflictError,
    ChannelBinding,
    PrincipalDirectory,
    PrincipalId,
)


def test_new_principal_ids_are_well_formed_and_unique() -> None:
    ids = {PrincipalId.new() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(i.value) == 36 and i.value.startswith("prn_") for i in ids)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "prn_",
        "prn_" + "a" * 31,
        "prn_" + "a" * 33,
        "prn_" + "A" * 32,
        "usr_" + "a" * 32,
        "prn_" + "g" * 32,
    ],
)
def test_malformed_principal_ids_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        PrincipalId(bad)


@pytest.mark.parametrize("channel", ["", "Telegram", "1tg", "tele gram", "تلگرام", "a" * 33])
def test_bad_channel_slugs_are_refused(channel: str) -> None:
    with pytest.raises(ValueError):
        ChannelBinding(channel, "1")


@pytest.mark.parametrize("external", ["", "   ", "x" * 257])
def test_bad_external_ids_are_refused(external: str) -> None:
    with pytest.raises(ValueError):
        ChannelBinding("telegram", external)


def test_binding_equality_is_exact() -> None:
    assert ChannelBinding("telegram", "7") != ChannelBinding("telegram", "007")
    assert ChannelBinding("telegram", "7") != ChannelBinding("web", "7")


def test_first_contact_mints_one_principal_and_is_stable() -> None:
    directory = PrincipalDirectory()
    binding = ChannelBinding("telegram", "42")
    assert directory.resolve(binding) is None
    first = directory.resolve_or_create(binding)
    assert directory.resolve_or_create(binding) == first == directory.resolve(binding)


def test_one_principal_can_hold_many_channels() -> None:
    directory = PrincipalDirectory()
    principal = directory.resolve_or_create(ChannelBinding("telegram", "42"))
    directory.bind(principal, ChannelBinding("web", "device-key-1"))
    directory.bind(principal, ChannelBinding("web", "device-key-1"))  # idempotent
    assert len(directory.bindings_of(principal)) == 2


def test_a_binding_is_never_silently_moved_to_another_principal() -> None:
    directory = PrincipalDirectory()
    binding = ChannelBinding("telegram", "42")
    owner = directory.resolve_or_create(binding)
    with pytest.raises(BindingConflictError):
        directory.bind(PrincipalId.new(), binding)
    assert directory.resolve(binding) == owner


def test_concurrent_first_contact_yields_exactly_one_principal() -> None:
    directory = PrincipalDirectory()
    binding = ChannelBinding("telegram", "99")
    seen: list[PrincipalId] = []

    def worker() -> None:
        seen.append(directory.resolve_or_create(binding))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(seen)) == 1


def test_a_transport_can_implement_channel_port_structurally() -> None:
    class Fake:
        channel = "telegram"

        async def send(self, principal: PrincipalId, message: OutboundMessage) -> bool:
            return True

    assert isinstance(Fake(), ChannelPort)
