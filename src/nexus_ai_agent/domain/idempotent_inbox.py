"""Idempotent update intake as a monotone DFA plus an authenticated callback codec.

Inbound updates are modelled as an idempotent semigroup action: replaying the
same ``(source_scope, update_id, payload_digest)`` any number of times yields
the same sealed receipt, while reusing an id with a *different* payload digest
is a Byzantine conflict and fails closed.

The callback codec keeps inline-button state inside Telegram's 64-byte
``callback_data`` budget and carries a monotonic ``session_epoch`` so a button
from an old message cannot silently undo a newer selection.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final

__all__ = [
    "ByzantineUpdateConflictError",
    "CallbackAction",
    "CallbackActionEnvelope",
    "CallbackCodecError",
    "InboxDecision",
    "InboxReceipt",
    "InboxReceiptTamperError",
    "InboxState",
    "InvalidInboxTransitionError",
    "StaleCallbackError",
    "advance",
    "payload_digest",
]

_CALLBACK_MAX_BYTES: Final[int] = 64
_TAG_BYTES: Final[int] = 8
_SUPPORTED_CALLBACK_VERSION: Final[int] = 1


class InboxState(Enum):
    RECEIVED = "RECEIVED"
    CLAIMED = "CLAIMED"
    COMMITTED = "COMMITTED"
    QUARANTINED = "QUARANTINED"


_ALLOWED: Final[Mapping[InboxState, frozenset[InboxState]]] = MappingProxyType(
    {
        InboxState.RECEIVED: frozenset({InboxState.CLAIMED, InboxState.QUARANTINED}),
        InboxState.CLAIMED: frozenset({InboxState.COMMITTED, InboxState.QUARANTINED}),
        InboxState.COMMITTED: frozenset({InboxState.COMMITTED}),
        InboxState.QUARANTINED: frozenset({InboxState.QUARANTINED}),
    }
)


class InvalidInboxTransitionError(ValueError):
    """Raised when the DFA is asked to make an undefined state transition."""


class ByzantineUpdateConflictError(RuntimeError):
    """Raised when one update id arrives with two different payload digests."""


class InboxReceiptTamperError(RuntimeError):
    """Raised when a stored receipt no longer matches its sealed digest."""


class CallbackCodecError(ValueError):
    """Raised for malformed, oversized or unauthenticated callback payloads."""


class StaleCallbackError(CallbackCodecError):
    """Raised when a callback belongs to an older session epoch."""


def advance(current: InboxState, target: InboxState) -> InboxState:
    """Apply one DFA step, rejecting transitions outside the closed table."""

    if not isinstance(current, InboxState) or not isinstance(target, InboxState):
        raise TypeError("inbox states must be InboxState members")
    if target not in _ALLOWED[current]:
        raise InvalidInboxTransitionError(f"illegal transition {current.value} -> {target.value}")
    return target


def payload_digest(payload: bytes) -> str:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    return hashlib.sha256(bytes(payload)).hexdigest()


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{label} must be a 64-character SHA-256 hex string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be hexadecimal") from exc
    return value.lower()


@dataclass(frozen=True, slots=True)
class InboxReceipt:
    """Immutable, self-authenticating record of one inbound update."""

    source_scope: str
    update_id: int
    payload_digest: str
    state: InboxState
    sealed_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_scope, str) or not self.source_scope:
            raise ValueError("source_scope must be a non-empty string")
        if isinstance(self.update_id, bool) or not isinstance(self.update_id, int):
            raise TypeError("update_id must be an int")
        if self.update_id < 0:
            raise ValueError("update_id must be non-negative")
        if not isinstance(self.state, InboxState):
            raise TypeError("state must be an InboxState")
        object.__setattr__(
            self, "payload_digest", _require_digest(self.payload_digest, "payload_digest")
        )
        object.__setattr__(
            self, "sealed_digest", _require_digest(self.sealed_digest, "sealed_digest")
        )

    def _canonical(self) -> bytes:
        document = {
            "source_scope": self.source_scope,
            "update_id": self.update_id,
            "payload_digest": self.payload_digest,
            "state": self.state.value,
        }
        return json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def compute_sealed_digest(self) -> str:
        return hashlib.sha256(self._canonical()).hexdigest()

    def verify(self) -> None:
        if not hmac.compare_digest(self.compute_sealed_digest(), self.sealed_digest):
            raise InboxReceiptTamperError(
                f"receipt for {self.source_scope}/{self.update_id} failed its sealed digest"
            )

    @classmethod
    def create(
        cls, source_scope: str, update_id: int, payload: str, state: InboxState
    ) -> InboxReceipt:
        provisional = cls(
            source_scope=source_scope,
            update_id=update_id,
            payload_digest=_require_digest(payload, "payload_digest"),
            state=state,
            sealed_digest="0" * 64,
        )
        return cls(
            source_scope=provisional.source_scope,
            update_id=provisional.update_id,
            payload_digest=provisional.payload_digest,
            state=provisional.state,
            sealed_digest=provisional.compute_sealed_digest(),
        )

    def to_json(self) -> str:
        document = {
            "source_scope": self.source_scope,
            "update_id": self.update_id,
            "payload_digest": self.payload_digest,
            "state": self.state.value,
            "sealed_digest": self.sealed_digest,
        }
        return json.dumps(document, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> InboxReceipt:
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InboxReceiptTamperError("stored receipt is not valid JSON") from exc
        if not isinstance(document, dict):
            raise InboxReceiptTamperError("stored receipt is not an object")
        try:
            receipt = cls(
                source_scope=document["source_scope"],
                update_id=document["update_id"],
                payload_digest=document["payload_digest"],
                state=InboxState(document["state"]),
                sealed_digest=document["sealed_digest"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InboxReceiptTamperError("stored receipt fields are invalid") from exc
        receipt.verify()
        return receipt


@dataclass(frozen=True, slots=True)
class InboxDecision:
    """Outcome of an intake call: the sealed receipt plus what actually happened."""

    receipt: InboxReceipt
    replayed: bool
    applied: bool

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, InboxReceipt):
            raise TypeError("receipt must be an InboxReceipt")
        if not isinstance(self.replayed, bool) or not isinstance(self.applied, bool):
            raise TypeError("replayed and applied must be bools")
        if self.replayed and self.applied:
            raise ValueError("a decision cannot both replay and apply")


class CallbackAction(Enum):
    SET_LANGUAGE = "l"
    SET_AGENT = "a"
    SET_MODE = "m"


_CODE_TO_ACTION: Final[Mapping[str, CallbackAction]] = MappingProxyType(
    {action.value: action for action in CallbackAction}
)


@dataclass(frozen=True, slots=True)
class CallbackActionEnvelope:
    """A compact, HMAC-authenticated inline-button command."""

    action: CallbackAction
    target: str
    session_epoch: int
    version: int = _SUPPORTED_CALLBACK_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.action, CallbackAction):
            raise TypeError("action must be a CallbackAction")
        if not isinstance(self.target, str) or not self.target:
            raise CallbackCodecError("target must be a non-empty string")
        if "." in self.target:
            raise CallbackCodecError("target must not contain '.'")
        if not self.target.isascii():
            raise CallbackCodecError("target must be ASCII")
        if len(self.target) > 24:
            raise CallbackCodecError("target must be at most 24 ASCII characters")
        if isinstance(self.session_epoch, bool) or not isinstance(self.session_epoch, int):
            raise TypeError("session_epoch must be an int")
        if self.session_epoch < 0:
            raise CallbackCodecError("session_epoch must be non-negative")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("version must be an int")
        if self.version != _SUPPORTED_CALLBACK_VERSION:
            raise CallbackCodecError(f"unsupported callback version {self.version}")

    def encode(self, secret: bytes) -> str:
        if not isinstance(secret, (bytes, bytearray)) or not secret:
            raise CallbackCodecError("HMAC secret must be non-empty bytes")
        payload = f"{self.version}.{self.action.value}.{self.target}.{self.session_epoch}"
        tag = hmac.new(bytes(secret), payload.encode("ascii"), hashlib.sha256).hexdigest()[
            :_TAG_BYTES
        ]
        data = f"{payload}.{tag}"
        if len(data) > _CALLBACK_MAX_BYTES:
            raise CallbackCodecError(
                f"encoded callback is {len(data)} bytes, budget is {_CALLBACK_MAX_BYTES}"
            )
        return data

    @classmethod
    def decode(cls, data: str, secret: bytes, *, minimum_epoch: int = 0) -> CallbackActionEnvelope:
        if not isinstance(data, str):
            raise CallbackCodecError("callback data must be a string")
        if not data or len(data) > _CALLBACK_MAX_BYTES or not data.isascii():
            raise CallbackCodecError("callback data must be 1-64 ASCII bytes")
        if not isinstance(secret, (bytes, bytearray)) or not secret:
            raise CallbackCodecError("HMAC secret must be non-empty bytes")
        parts = data.split(".")
        if len(parts) != 5:
            raise CallbackCodecError("callback data must have five dot-separated fields")
        raw_version, raw_action, target, raw_epoch, tag = parts
        try:
            version = int(raw_version)
            session_epoch = int(raw_epoch)
        except ValueError as exc:
            raise CallbackCodecError("callback version and epoch must be integers") from exc
        action = _CODE_TO_ACTION.get(raw_action)
        if action is None:
            raise CallbackCodecError(f"unknown callback action {raw_action!r}")
        payload = f"{raw_version}.{raw_action}.{target}.{raw_epoch}"
        expected = hmac.new(bytes(secret), payload.encode("ascii"), hashlib.sha256).hexdigest()[
            :_TAG_BYTES
        ]
        if not hmac.compare_digest(expected, tag):
            raise CallbackCodecError("callback HMAC tag does not match")
        envelope = cls(
            action=action,
            target=target,
            session_epoch=session_epoch,
            version=version,
        )
        if isinstance(minimum_epoch, bool) or not isinstance(minimum_epoch, int):
            raise TypeError("minimum_epoch must be an int")
        if envelope.session_epoch < minimum_epoch:
            raise StaleCallbackError(
                f"session epoch {envelope.session_epoch} is older than {minimum_epoch}"
            )
        return envelope
