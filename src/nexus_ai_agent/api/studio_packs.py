"""Studio's read-only view of the resource packs installed on this device.

`api/studio_web.py` is deliberately delivery-only — an architecture test forbids
it from importing the execution or storage layers, so the static-file shell
cannot grow a second authority.  The pack *status* projection therefore lives in
its own API module, next to `api/studio.py`, and does exactly one thing: read the
deployment's pack configuration and hand it to
:func:`nexus_ai_agent.storage.resource_packs.delivery.delivery_report`, which
re-verifies each installed pack against its signed manifest.

The configuration is read from the environment on every call rather than cached:
a key rotated or a root remounted must take effect on the next request, and a
stale `trusted_public_key` in a long-lived process is precisely the failure this
report exists to expose.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

__all__ = [
    "resource_pack_key",
    "resource_pack_report",
    "resource_pack_root",
    "unsigned_packs_allowed",
]

_ROOT_ENV = "NEXUS_RESOURCE_PACK_ROOT"
_KEY_ENV = "NEXUS_RESOURCE_PACK_PUBLIC_KEY"
_UNSIGNED_ENV = "NEXUS_RESOURCE_PACK_ALLOW_UNSIGNED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def resource_pack_root() -> Path | None:
    """The packs root this deployment verifies, if one is configured."""
    raw = os.getenv(_ROOT_ENV, "").strip()
    return Path(raw) if raw else None


def resource_pack_key() -> bytes | None:
    """The trusted Ed25519 key, as 32 raw bytes, from the environment.

    Hex only, and length-checked *here* rather than in the pack layer, so a
    malformed value cannot be mistaken for a policy decision: it returns ``None``,
    which the report treats as "no trust root" (fail closed) rather than "no
    signature needed".
    """
    raw = os.getenv(_KEY_ENV, "").strip()
    if not raw:
        return None
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        return None
    return key if len(key) == 32 else None


def unsigned_packs_allowed() -> bool:
    """``NEXUS_RESOURCE_PACK_ALLOW_UNSIGNED=1`` — the explicit unsigned policy.

    Defaults to false: without a key, every pack is reported ``signature_required``
    instead of being offered to a render.
    """
    return os.getenv(_UNSIGNED_ENV, "").strip().lower() in _TRUTHY


def resource_pack_report() -> dict[str, Any]:
    """One row per installed pack, re-verified on every call."""
    from nexus_ai_agent.storage.resource_packs import delivery_report

    key = resource_pack_key()
    return delivery_report(
        resource_pack_root(),
        trusted_public_key=key,
        # A configured key always wins: allow_unsigned can never weaken a
        # deployment that has a trust root, only stand in for the absence of one.
        allow_unsigned=unsigned_packs_allowed() if key is None else False,
    )
