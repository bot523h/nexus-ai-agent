"""Deterministic logical effect-key (P4) — pure, framework-free, frozen-baseline zone.

An *effect key* names one logical external effect, not one delivery attempt.
The outbox dispatcher delivers at-least-once; the effect key is what turns a
repeated attempt into an at-most-once *logical effect*. Every attempt carries
the same key, so the despatcher and the downstream effect can recognise "this
is the same intent again" without trusting a random identifier minted per
retry.

Contract (mirrored and mutation-checked in ``tests/unit/test_effect_key.py``):

* deterministic        — same intent always yields the same key;
* stable across retry  — no attempt id, no timestamp, no randomness, no clock;
* distinct per effect  — a new logical entity/revision/destination/slot yields
  a new key (so two genuinely different effects are never collapsed);
* independent of attempt UUID — attempt identity is named separately and never
  contributes to the key;

The key material is the canonical JSON serialisation of the intent tuple.
``sort_keys`` makes the result independent of the order in which the caller
names the fields. Identity fields (``logical_entity``, ``logical_revision``,
``destination``, ``logical_slot``) must be supplied in a canonical Python type:
an ``int`` chat id and the string ``"555"`` are *different* intents, exactly as
they are different values. The call site owns that canonicalisation.

The returned key is ``sha256(canonical_json)[:60]``. It is never used as a
metric label (see ``adapters/effect/observability_backend.py``) and never logged in
raw form.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final

#: Type-native prefix of a *typed* effect key (``op.token``). Primary-key
#: component; low cardinality (operation type names), safe to index and group by.
TYPED_PREFIX_MAX_OPERATION_LENGTH: Final[int] = 80

#: Length of the hex token (full sha256 is 64 chars; the prefix carries the type).
TOKEN_LENGTH: Final[int] = 60


def effect_key(
    *,
    operation_type: str,
    logical_entity: str | int,
    logical_revision: str | int,
    destination: str | int,
    logical_slot: str | int | None = None,
) -> str:
    """Return the stable logical effect key for one intent.

    Raises ``ValueError`` when an identity field is empty (an empty identity
    would silently collapse distinct effects onto one key).
    """
    if not str(operation_type).strip():
        raise ValueError("operation_type must not be empty")
    if str(logical_entity) == "" or str(logical_revision) == "" or str(destination) == "":
        raise ValueError("logical entity, revision and destination must not be empty")
    material = {
        "operation_type": str(operation_type).strip(),
        "logical_entity": logical_entity,
        "logical_revision": logical_revision,
        "destination": destination,
        # ``None`` and a present slot stay distinguishable: an absent slot is a
        # different logical intent than an empty-string slot.
        "logical_slot": logical_slot,
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:TOKEN_LENGTH]


def serialize_effect_key(operation_type: str, token: str) -> str:
    """Compose the typed form stored under the outbox primary key (``op.token``).

    The operation type is kept in plain text (bounded, type-native) so reads
    stay inspectable and groupable; the token carries the logical identity.
    """
    operation = str(operation_type).strip()
    if not operation:
        raise ValueError("operation_type must not be empty")
    if len(operation) > TYPED_PREFIX_MAX_OPERATION_LENGTH:
        raise ValueError("operation_type too long")
    if not token:
        raise ValueError("token must not be empty")
    return f"{operation}.{token}"


def split_effect_key(typed_key: str) -> tuple[str, str]:
    """Invert :func:`serialize_effect_key` into ``(operation_type, token)``.

    The separator is the *last* dot (``rpartition``): operation types may
    themselves contain dots (``telegram.send``, ``r2.upload``) while the token
    is a hex digest that never can, so the split is unambiguous.
    Raises ``ValueError`` on a malformed key.
    """
    operation, sep, token = str(typed_key).rpartition(".")
    if not sep or not operation or not token or "." in token:
        raise ValueError(f"malformed typed effect key: {typed_key!r}")
    return operation, token


def is_effect_key_for(typed_key: str, operation_type: str) -> bool:
    """True when ``typed_key`` belongs to ``operation_type`` (routing guard)."""
    try:
        return split_effect_key(typed_key)[0] == str(operation_type)
    except ValueError:
        return False
