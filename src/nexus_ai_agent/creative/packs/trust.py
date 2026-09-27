"""Pack trust root: where a capability pack's *authority* actually comes from.

A manifest is a **claim**.  ``security.trusted_publisher`` is part of that
claim, so it can never make the publisher trusted, and
``security.signature`` being structurally parseable can never make the pack
verified.  Authority must come from **outside** the artifact being judged —
that outside thing is the *trust root* implemented here.

Layers (never allowed to self-authorise each other)
---------------------------------------------------
``claim``      the manifest bytes, including the publisher name it asserts
``evidence``   an Ed25519 signature over the canonical claim bytes
``authority``  a public key listed in the trust root for that publisher
``decision``   :class:`TrustDecision` — one explicit state, never a bool

Architecture (ADR: docs/architecture/adr/0006-capability-pack-trust-root.md)
----------------------------------------------------------------------------
TUF-inspired, deliberately reduced:

* an explicit, versioned root document listing publishers → public keys, with
  per-key ``status`` (``active``/``revoked``) — the TUF idea that trust is a
  *pinned list with revocation*, not a name in the payload;
* signature threshold is fixed at 1 and recorded in the document, so raising
  it later is a data change, not a code change;
* **no** online roles (timestamp/snapshot/targets), because packs in this
  project are not fetched through a live repository: there is no rollback or
  freeze surface to defend, and the runtime must work fully offline;
* **no** Sigstore/Fulcio/Rekor: keyless signing needs an online CA and
  transparency log, which contradicts the local-first constraint.

Cryptography
------------
Verification uses **exactly one** implementation, always active:
:mod:`nexus_ai_agent.creative.packs.ed25519`, a pure-Python RFC 8032 verifier.
There is deliberately **no** ``cryptography``/PyNaCl backend.  An optional
backend would mean two implementations that can disagree on exactly the edge
cases that module pins — canonical ``S``, canonical point encodings, small-order
keys — plus a "library missing" path that decays into "not verified, but
allowed".  (An earlier revision of this docstring advertised such an optional
backend.  It never existed; ``tests/architecture/test_pack_trust_boundary.py``
now fails if one is introduced.)

Verification touches only *public* data with *public* keys — there is no secret
in this process, and **no signing code ships in production**: this module can
only say yes or no, it can never mint authority.

Fail-closed by construction
---------------------------
Every way of *not* being verified is its own state
(:class:`TrustState`), and :attr:`TrustDecision.trusted` is true for exactly
one of them.  The trust root shipped in this repository contains **zero**
publisher keys, so today every external pack resolves to
``NO_TRUSTED_KEYS`` → not trusted → not activatable.  Adding a real publisher
is a data change to ``trust_root.json``; it requires no code change and, in
particular, requires no private key to exist anywhere in this repository.
"""

from __future__ import annotations

import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.packs.ed25519 import Ed25519Error, verify_ed25519
from nexus_ai_agent.creative.packs.manifest import (
    CapabilityPackManifest,
    PackManifestError,
)

#: The trust root that ships with the runtime.
#:
#: Deliberately **not** overridable through an environment variable: a trust
#: root that any process-local env var can swap is not a root of trust, it is a
#: downgrade switch.  A caller that legitimately owns a different root passes
#: it explicitly (``TrustRoot.load(path)`` / ``PackRegistry(trust_root=...)``),
#: which keeps the substitution visible in code rather than in the environment.
DEFAULT_TRUST_ROOT_PATH = Path(__file__).with_name("trust_root.json")

#: Only algorithm the verifier understands.  Anything else fails closed.
SUPPORTED_ALGORITHM = "ed25519"

#: Transport prefix for real signatures: ``ed25519:<128 hex chars>``.
SIGNATURE_PREFIX = "ed25519:"

SIGNATURE_BYTES = 64
PUBLIC_KEY_BYTES = 32


class TrustState(str, Enum):
    """Exactly one outcome per manifest — never collapsed into a boolean."""

    #: Signature is the documented ``base64:replace-…`` placeholder.
    PLACEHOLDER = "placeholder"
    #: ``security.signature_algorithm`` is not Ed25519.
    UNSUPPORTED_ALGORITHM = "unsupported_algorithm"
    #: Signature is not ``ed25519:<128 hex>``.
    MALFORMED_SIGNATURE = "malformed_signature"
    #: No trust-root document could be read.
    NO_TRUST_ROOT = "no_trust_root"
    #: The trust root knows nothing about the publisher the manifest claims.
    UNKNOWN_PUBLISHER = "unknown_publisher"
    #: The publisher exists but every key for it is revoked/inactive.
    NO_TRUSTED_KEYS = "no_trusted_keys"
    #: Signature only validates under a key that has been revoked.
    REVOKED_KEY = "revoked_key"
    #: Signature does not validate under any active key for the publisher.
    SIGNATURE_INVALID = "signature_invalid"
    #: Verified against an active key of the claimed publisher.
    VERIFIED = "verified"


#: The single state that grants trust.  Anything not in here is refused.
TRUSTED_STATES: frozenset[TrustState] = frozenset({TrustState.VERIFIED})


class TrustRootError(PackManifestError):
    """The trust-root document itself is unusable (never silently ignored)."""


@dataclass(frozen=True)
class TrustDecision:
    """Why a pack is (not) trusted — a state plus a human reason."""

    state: TrustState
    reason: str
    publisher: str
    key_id: str | None = None

    @property
    def trusted(self) -> bool:
        return self.state in TRUSTED_STATES


@dataclass(frozen=True)
class PublisherKey:
    key_id: str
    public_key: bytes
    status: str  # "active" | "revoked"

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class TrustRoot:
    """Pinned publishers → keys.  Empty is a valid — and safe — trust root."""

    version: int
    threshold: int
    publishers: Mapping[str, tuple[PublisherKey, ...]]
    source: str

    def keys_for(self, publisher: str) -> tuple[PublisherKey, ...]:
        return tuple(self.publishers.get(publisher, ()))

    @property
    def is_empty(self) -> bool:
        return not any(self.publishers.values())

    # -- loading ------------------------------------------------------------
    @classmethod
    def from_mapping(cls, raw: Any, *, source: str) -> TrustRoot:
        if not isinstance(raw, dict):
            raise TrustRootError(f"trust root must be a JSON object: {source}")
        schema = raw.get("schema")
        if schema != "nexus.pack-trust-root.v1":
            raise TrustRootError(f"unknown trust-root schema {schema!r} in {source}")
        try:
            version = int(raw["version"])
            threshold = int(raw.get("threshold", 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise TrustRootError(f"trust root {source} has no valid version: {exc}") from exc
        if threshold != 1:
            # Raising the threshold is a deliberate future change; refusing now
            # is safer than silently verifying with one signature.
            raise TrustRootError(
                f"trust root {source} declares threshold={threshold}; "
                "this runtime implements threshold=1 only and refuses to pretend otherwise"
            )
        publishers_raw = raw.get("publishers", {})
        if not isinstance(publishers_raw, dict):
            raise TrustRootError(f"trust root {source}: 'publishers' must be an object")
        publishers: dict[str, tuple[PublisherKey, ...]] = {}
        for name, entries in publishers_raw.items():
            if not isinstance(entries, list):
                raise TrustRootError(f"trust root {source}: publisher {name!r} must hold a list")
            keys: list[PublisherKey] = []
            for entry in entries:
                keys.append(cls._parse_key(entry, publisher=name, source=source))
            publishers[str(name)] = tuple(keys)
        return cls(version=version, threshold=threshold, publishers=publishers, source=source)

    @staticmethod
    def _parse_key(entry: Any, *, publisher: str, source: str) -> PublisherKey:
        if not isinstance(entry, dict):
            raise TrustRootError(
                f"trust root {source}: key entry for {publisher!r} is not an object"
            )
        key_id = entry.get("key_id")
        hex_key = entry.get("public_key")
        status = entry.get("status", "active")
        algorithm = entry.get("algorithm", SUPPORTED_ALGORITHM)
        if algorithm != SUPPORTED_ALGORITHM:
            raise TrustRootError(
                f"trust root {source}: key {key_id!r} uses unsupported algorithm {algorithm!r}"
            )
        if not isinstance(key_id, str) or not key_id:
            raise TrustRootError(f"trust root {source}: a key of {publisher!r} has no key_id")
        if status not in ("active", "revoked"):
            raise TrustRootError(
                f"trust root {source}: key {key_id!r} has invalid status {status!r}"
            )
        if not isinstance(hex_key, str):
            raise TrustRootError(f"trust root {source}: key {key_id!r} has no public_key")
        try:
            raw_key = bytes.fromhex(hex_key)
        except ValueError as exc:
            raise TrustRootError(f"trust root {source}: key {key_id!r} is not hex: {exc}") from exc
        if len(raw_key) != PUBLIC_KEY_BYTES:
            raise TrustRootError(
                f"trust root {source}: key {key_id!r} must be {PUBLIC_KEY_BYTES} bytes, "
                f"got {len(raw_key)}"
            )
        return PublisherKey(key_id=key_id, public_key=raw_key, status=status)

    @classmethod
    def load(cls, path: Path | None = None) -> TrustRoot:
        """Load the trust root from *path*, or the one shipped with the runtime.

        An unreadable trust root raises: "the authority could not be read" must
        never silently become "there is no authority, carry on".
        """
        if path is None:
            path = DEFAULT_TRUST_ROOT_PATH
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise TrustRootError(f"trust root not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise TrustRootError(f"trust root is not valid JSON: {path}: {exc}") from exc
        return cls.from_mapping(raw, source=str(path))


# ---------------------------------------------------------------------------
# Canonical signing bytes — one function, used by signer and verifier alike
# ---------------------------------------------------------------------------


def canonical_signing_bytes(manifest: CapabilityPackManifest) -> bytes:
    """Return the exact bytes a publisher signs (and the verifier checks).

    Rules, all of which exist to remove a drift class:

    * the payload is the **whole** manifest as validated by the models, with
      ``security.signature`` removed — a signature cannot cover itself — and
      every other field, including ``trusted_publisher``, ``capabilities``,
      ``permissions``, ``artifacts`` and ``compatibility``, included; a pack
      cannot gain a capability or a permission after signing;
    * key order is sorted, separators are tight, and the output is pure ASCII
      (``ensure_ascii=True``) so no Unicode normalisation or encoding choice
      can change the bytes;
    * floats are serialised through :func:`repr`-stable JSON on both sides —
      both sides call *this* function, so there is exactly one representation;
    * a schema tag is prefixed so bytes signed for this scheme can never be
      replayed as bytes for another scheme.
    """
    payload = manifest.model_dump(mode="json")
    security = dict(payload.get("security", {}))
    security.pop("signature", None)
    payload["security"] = security
    body = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return b"nexus.capability-pack.signature.v1\n" + body.encode("ascii")


def decode_signature(signature: str) -> bytes | None:
    """Decode ``ed25519:<128 hex>``; ``None`` when the shape is wrong."""
    if not signature.startswith(SIGNATURE_PREFIX):
        return None
    hex_part = signature[len(SIGNATURE_PREFIX) :]
    if len(hex_part) != SIGNATURE_BYTES * 2:
        return None
    try:
        return binascii.unhexlify(hex_part)
    except (binascii.Error, ValueError):
        return None


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def evaluate_trust(
    manifest: CapabilityPackManifest,
    trust_root: TrustRoot | None,
) -> TrustDecision:
    """Decide whether *manifest* is cryptographically trusted.

    ``trust_root=None`` means "no authority is available" and yields
    :attr:`TrustState.NO_TRUST_ROOT` — never an implicit pass.
    """
    publisher = manifest.security.trusted_publisher

    if manifest.security.signature_algorithm != SUPPORTED_ALGORITHM:  # pragma: no cover
        # The model pins Literal["ed25519"], so this is defence in depth
        # against the Literal ever being widened without touching this file.
        return TrustDecision(
            TrustState.UNSUPPORTED_ALGORITHM,
            f"signature algorithm {manifest.security.signature_algorithm!r} is not supported",
            publisher,
        )

    if manifest.signature_is_placeholder:
        return TrustDecision(
            TrustState.PLACEHOLDER,
            "manifest carries the documented unsigned placeholder signature",
            publisher,
        )

    signature = decode_signature(manifest.security.signature)
    if signature is None:
        return TrustDecision(
            TrustState.MALFORMED_SIGNATURE,
            f"signature must be '{SIGNATURE_PREFIX}<{SIGNATURE_BYTES * 2} hex chars>'",
            publisher,
        )

    if trust_root is None:
        return TrustDecision(
            TrustState.NO_TRUST_ROOT,
            "no trust root is available; a signature without an authority proves nothing",
            publisher,
        )

    keys = trust_root.keys_for(publisher)
    if not keys:
        return TrustDecision(
            TrustState.UNKNOWN_PUBLISHER,
            f"publisher {publisher!r} is not in trust root {trust_root.source} "
            "(a manifest cannot nominate its own publisher)",
            publisher,
        )

    message = canonical_signing_bytes(manifest)
    active = [key for key in keys if key.active]
    if not active:
        return TrustDecision(
            TrustState.NO_TRUSTED_KEYS,
            f"every key for publisher {publisher!r} is revoked",
            publisher,
        )

    for key in active:
        if _verifies(message, signature, key.public_key):
            return TrustDecision(
                TrustState.VERIFIED,
                f"signature verified against {key.key_id!r}",
                publisher,
                key_id=key.key_id,
            )

    for key in keys:
        if not key.active and _verifies(message, signature, key.public_key):
            return TrustDecision(
                TrustState.REVOKED_KEY,
                f"signature only verifies against revoked key {key.key_id!r}",
                publisher,
                key_id=key.key_id,
            )

    return TrustDecision(
        TrustState.SIGNATURE_INVALID,
        f"signature does not verify against any active key of {publisher!r}",
        publisher,
    )


def _verifies(message: bytes, signature: bytes, public_key: bytes) -> bool:
    try:
        verify_ed25519(public_key, message, signature)
    except Ed25519Error:
        return False
    return True
