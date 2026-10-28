"""Principal-first identity: who is acting, independent of the channel they use.

The project is an application whose Telegram bot is one connector (see
``docs/architecture/PRODUCT_FRAME.md``). A **Principal** is the canonical
identity of a person; a **ChannelBinding** ties one external account
(a Telegram user id, a device key, ...) to exactly one Principal. Core code
must hold a ``PrincipalId``, never a Telegram id.

Pure stdlib: this module must not import ``telegram`` or any transport.
This step adds the vocabulary and a reference in-memory directory only; it
changes no storage schema and rewires no handler.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field

_PRINCIPAL_PREFIX = "prn_"
_ID_HEX_LEN = 32
_HEX = frozenset("0123456789abcdef")
MAX_EXTERNAL_ID_LEN = 256
MAX_CHANNEL_LEN = 32


class BindingConflictError(ValueError):
    """The external account is already bound to a different Principal."""


@dataclass(frozen=True, order=True)
class PrincipalId:
    """Opaque canonical identity: ``prn_`` + 32 lowercase hex characters."""

    value: str

    def __post_init__(self) -> None:
        value = self.value
        if not isinstance(value, str) or not value.startswith(_PRINCIPAL_PREFIX):
            raise ValueError("invalid PrincipalId")
        body = value[len(_PRINCIPAL_PREFIX) :]
        if len(body) != _ID_HEX_LEN or not _HEX.issuperset(body):
            raise ValueError("invalid PrincipalId")

    @classmethod
    def new(cls) -> PrincipalId:
        return cls(_PRINCIPAL_PREFIX + secrets.token_hex(_ID_HEX_LEN // 2))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ChannelBinding:
    """One external account on one channel, e.g. ``("telegram", "123456")``.

    ``channel`` is a lowercase ASCII slug; ``external_id`` is an opaque
    non-empty string the channel owns. Equality is exact: ``"007"`` and
    ``"7"`` are different accounts.
    """

    channel: str
    external_id: str

    def __post_init__(self) -> None:
        channel_ok = (
            isinstance(self.channel, str)
            and 0 < len(self.channel) <= MAX_CHANNEL_LEN
            and self.channel[0].isalpha()
            and all(c.islower() or c.isdigit() or c in "-_" for c in self.channel)
            and self.channel.isascii()
        )
        if not channel_ok:
            raise ValueError("invalid channel slug")
        if (
            not isinstance(self.external_id, str)
            or not self.external_id.strip()
            or len(self.external_id) > MAX_EXTERNAL_ID_LEN
        ):
            raise ValueError("invalid external id")


@dataclass
class PrincipalDirectory:
    """Reference in-memory directory (thread-safe). Storage adapters follow later.

    Invariants: a binding maps to at most one Principal; re-binding the same
    binding to the same Principal is idempotent; moving a binding to another
    Principal is refused (an explicit, audited transfer is a later, human-gated
    operation — never an implicit overwrite).
    """

    _by_binding: dict[ChannelBinding, PrincipalId] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def resolve(self, binding: ChannelBinding) -> PrincipalId | None:
        with self._lock:
            return self._by_binding.get(binding)

    def bind(self, principal: PrincipalId, binding: ChannelBinding) -> None:
        with self._lock:
            current = self._by_binding.get(binding)
            if current is not None and current != principal:
                raise BindingConflictError("binding already belongs to another principal")
            self._by_binding[binding] = principal

    def resolve_or_create(self, binding: ChannelBinding) -> PrincipalId:
        """First contact on a channel mints a Principal atomically."""
        with self._lock:
            current = self._by_binding.get(binding)
            if current is None:
                current = PrincipalId.new()
                self._by_binding[binding] = current
            return current

    def bindings_of(self, principal: PrincipalId) -> frozenset[ChannelBinding]:
        with self._lock:
            return frozenset(b for b, p in self._by_binding.items() if p == principal)


__all__ = [
    "MAX_CHANNEL_LEN",
    "MAX_EXTERNAL_ID_LEN",
    "BindingConflictError",
    "ChannelBinding",
    "PrincipalDirectory",
    "PrincipalId",
]
