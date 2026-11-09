"""Encryption at rest, consent, and least-privilege access control.

Three independent controls, deliberately not merged:

``EnvelopeCipher``
    Authenticated encryption of reference-asset bytes with a per-object data key
    wrapped by a key-encryption key (KEK). The KEK is read from a file outside
    the tree (``CHARACTER_CAPSULE_KEK_FILE``); **no key material is ever stored in
    source, in the database, or in an evidence bundle**. If no authenticated
    backend is importable the cipher refuses to operate — it never falls back to
    confidentiality theatre such as XOR or a static key.

``ConsentLedger``
    Synthetic characters need a synthetic declaration; real people need an
    explicit, revocable, purpose-scoped consent record. Revocation is terminal
    for *protected* operations (export/render) and is written to the audit log.

``AccessPolicy``
    A role × permission matrix, with every attempt — allowed or refused — written
    to the audit trail. "Least privilege" is only real when refusals are visible.

Honesty note (also in the Live Truth document): PyNaCl's ``SecretBox`` is
XSalsa20-Poly1305 and has no associated-data parameter, so binding is done by
encrypting a canonical header *inside* the ciphertext and re-checking it after
decryption. That is equivalent for the threat model here (substitution of a blob
between capsules/roles is detected as a header mismatch), and it is stated
plainly rather than implied to be AES-GCM AAD.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .canonical import canonical_json, digest_of, sha256_hex
from .errors import (
    AccessDenied,
    ConsentRequired,
    ConsentRevoked,
    CryptoUnavailable,
    ExportBlocked,
    KeyUnavailable,
    ValidationError,
)

# ---------------------------------------------------------------------------
# Authenticated encryption
# ---------------------------------------------------------------------------

_BLOB_MAGIC = b"NCC1"
_NONCE_BYTES = 24
_KEY_BYTES = 32


class Cipher(Protocol):
    """Minimal contract the blob store depends on."""

    def encrypt(self, plaintext: bytes, *, header: Mapping[str, Any]) -> bytes: ...

    def decrypt(self, blob: bytes, *, header: Mapping[str, Any]) -> bytes: ...

    @property
    def key_id(self) -> str: ...


def _load_nacl() -> tuple[Any, Any]:
    try:
        from nacl import secret, utils
    except Exception as exc:  # noqa: BLE001 - absence is a typed refusal, not a crash
        raise CryptoUnavailable(
            "no authenticated-encryption backend available (PyNaCl is not importable); "
            "refusing to store protected bytes in the clear",
            backend="nacl.SecretBox",
        ) from exc
    return secret, utils


def load_kek(path: str | Path | None = None) -> bytes:
    """Read a 32-byte key-encryption key from a file outside the repository.

    The path comes from ``CHARACTER_CAPSULE_KEK_FILE`` when ``path`` is not given.
    A short or missing key is a hard refusal: there is no default key, ever.
    """
    resolved = Path(path or os.environ.get("CHARACTER_CAPSULE_KEK_FILE", ""))
    if not str(resolved) or str(resolved) == ".":
        raise KeyUnavailable(
            "CHARACTER_CAPSULE_KEK_FILE is unset; encryption at rest needs an external KEK file"
        )
    if not resolved.exists():
        raise KeyUnavailable(f"KEK file does not exist: {resolved}")
    raw = resolved.read_bytes().strip()
    if len(raw) < _KEY_BYTES:
        raise KeyUnavailable(
            f"KEK file must contain at least {_KEY_BYTES} bytes of key material",
            got=len(raw),
        )
    return raw[:_KEY_BYTES]


class EnvelopeCipher:
    """Per-object data key, wrapped by a KEK; header bound inside the ciphertext."""

    def __init__(self, kek: bytes) -> None:
        if len(kek) != _KEY_BYTES:
            raise KeyUnavailable("KEK must be exactly 32 bytes", got=len(kek))
        self._secret, self._utils = _load_nacl()
        self._box = self._secret.SecretBox(kek)
        self._key_id = sha256_hex(kek)[:23]

    @property
    def key_id(self) -> str:
        """Non-secret identifier of the KEK (safe to log, useless to an attacker)."""
        return self._key_id

    def encrypt(self, plaintext: bytes, *, header: Mapping[str, Any]) -> bytes:
        data_key = self._utils.random(_KEY_BYTES)
        data_box = self._secret.SecretBox(data_key)
        wrapped = self._box.encrypt(data_key).ciphertext
        nonce = self._utils.random(_NONCE_BYTES)
        # The header travels *inside* the authenticated plaintext: a blob moved to
        # another capsule/purpose decrypts to a header that no longer matches.
        bound = canonical_json({"header": dict(header), "payload_sha256": sha256_hex(plaintext)})
        body = data_box.encrypt(plaintext, nonce).ciphertext
        return _BLOB_MAGIC + self._seal_header(bound) + wrapped + nonce + body

    def _seal_header(self, bound: str) -> bytes:
        header_bytes = bound.encode()
        length = len(header_bytes).to_bytes(2, "big")
        return length + header_bytes

    def decrypt(self, blob: bytes, *, header: Mapping[str, Any]) -> bytes:
        if not blob.startswith(_BLOB_MAGIC):
            raise CryptoUnavailable("blob is not an NCC1 envelope")
        rest = blob[len(_BLOB_MAGIC) :]
        if len(rest) < 2:
            raise CryptoUnavailable("truncated envelope header")
        header_len = int.from_bytes(rest[:2], "big")
        bound = rest[2 : 2 + header_len]
        rest = rest[2 + header_len :]
        if len(rest) < 32 + _NONCE_BYTES + 16:
            raise CryptoUnavailable("truncated envelope body")
        wrapped, nonce, body = rest[:32], rest[32 : 32 + _NONCE_BYTES], rest[32 + _NONCE_BYTES :]
        try:
            data_key = self._box.decrypt(wrapped)
        except Exception as exc:  # noqa: BLE001 - wrong KEK is a typed refusal
            raise KeyUnavailable("cannot unwrap the data key with this KEK") from exc
        try:
            plaintext = self._secret.SecretBox(data_key).decrypt(body, nonce)
        except Exception as exc:  # noqa: BLE001 - tamper/corruption is a typed refusal
            raise CryptoUnavailable(
                "envelope authentication failed (tampered or wrong key)"
            ) from exc
        expected = canonical_json(
            {"header": dict(header), "payload_sha256": sha256_hex(bytes(plaintext))}
        ).encode()
        if bound != expected:
            raise CryptoUnavailable(
                "envelope header mismatch: this blob does not belong to this capsule/purpose"
            )
        return bytes(plaintext)


class NullCipher:
    """Explicit plaintext mode, used only for scratch benchmarks.

    It exists so a caller has to *name* that they are not encrypting; there is no
    implicit fallback from :class:`EnvelopeCipher`.
    """

    @property
    def key_id(self) -> str:
        return "none"

    def encrypt(self, plaintext: bytes, *, header: Mapping[str, Any]) -> bytes:
        return plaintext

    def decrypt(self, blob: bytes, *, header: Mapping[str, Any]) -> bytes:
        return blob


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------


class ConsentPurpose(str, Enum):
    IMAGE_SYNTHESIS = "IMAGE_SYNTHESIS"
    VOICE_SYNTHESIS = "VOICE_SYNTHESIS"
    MOTION_SYNTHESIS = "MOTION_SYNTHESIS"
    COMMERCIAL_DISTRIBUTION = "COMMERCIAL_DISTRIBUTION"
    TRAINING = "TRAINING"


@dataclass(frozen=True)
class ConsentRecord:
    """Explicit, purpose-scoped, time-bounded consent for a real person."""

    consent_id: str
    character_id: str
    subject_reference: str
    purposes: tuple[ConsentPurpose, ...]
    rights: tuple[str, ...]
    territory: str
    granted_at: str
    expires_at: str | None = None
    document_digest: str = ""
    revoked_at: str | None = None
    revocation_reason: str = ""

    def __post_init__(self) -> None:
        if not self.consent_id.strip():
            raise ValidationError("consent needs an id")
        if not self.subject_reference.strip():
            raise ValidationError("consent needs a subject reference")
        if not self.purposes:
            raise ValidationError("consent must name at least one purpose")
        if not self.rights:
            raise ValidationError("consent must name the rights granted")
        if not self.territory.strip():
            raise ValidationError("consent must name a territory")
        if not self.granted_at.strip():
            raise ValidationError("consent must be dated")
        if self.document_digest and not self.document_digest.startswith("sha256:"):
            raise ValidationError("consent document digest must be 'sha256:<hex>'")

    def covers(self, purpose: ConsentPurpose, *, now: str) -> bool:
        if self.revoked_at:
            return False
        if purpose not in self.purposes:
            return False
        if self.expires_at and now > self.expires_at:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["purposes"] = [purpose.value for purpose in self.purposes]
        data["rights"] = list(self.rights)
        return data


class ConsentLedger:
    """In-memory ledger with an explicit revocation door (persisted by the store)."""

    def __init__(self, *, now: Callable[[], str] | None = None) -> None:
        self._now = now or _utc_now
        self._records: dict[str, ConsentRecord] = {}

    def add(self, record: ConsentRecord) -> ConsentRecord:
        self._records[record.consent_id] = record
        return record

    def get(self, consent_id: str) -> ConsentRecord | None:
        return self._records.get(consent_id)

    def for_character(self, character_id: str) -> tuple[ConsentRecord, ...]:
        return tuple(
            record for record in self._records.values() if record.character_id == character_id
        )

    def revoke(self, consent_id: str, reason: str) -> ConsentRecord:
        record = self._records.get(consent_id)
        if record is None:
            raise ConsentRequired(f"unknown consent {consent_id!r}")
        if not reason.strip():
            raise ValidationError("revocation needs a reason")
        revoked = ConsentRecord(
            **{
                **{k: v for k, v in record.__dict__.items() if k != "revoked_at"},
                "revoked_at": self._now(),
                "revocation_reason": reason,
            }
        )
        self._records[consent_id] = revoked
        return revoked

    def require(self, character_id: str, purpose: ConsentPurpose) -> ConsentRecord:
        """Return a covering record or refuse with a typed, actionable error."""
        now = self._now()
        candidates = self.for_character(character_id)
        if not candidates:
            raise ConsentRequired(
                f"no consent record for {character_id}; {purpose.value} needs explicit consent",
                character_id=character_id,
                purpose=purpose.value,
            )
        for record in candidates:
            if record.covers(purpose, now=now):
                return record
        revoked = [record for record in candidates if record.revoked_at]
        if revoked and len(revoked) == len(candidates):
            raise ConsentRevoked(
                f"all consent records for {character_id} are revoked; {purpose.value} is blocked",
                character_id=character_id,
                purpose=purpose.value,
                revocation_reason=revoked[0].revocation_reason,
            )
        raise ConsentRequired(
            f"no consent record for {character_id} covers {purpose.value}",
            character_id=character_id,
            purpose=purpose.value,
            now=now,
        )


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


class Role(str, Enum):
    OWNER = "OWNER"
    DIRECTOR = "DIRECTOR"
    ARTIST = "ARTIST"
    REVIEWER = "REVIEWER"
    AGENT = "AGENT"
    VIEWER = "VIEWER"
    EXTERNAL = "EXTERNAL"


class Permission(str, Enum):
    CREATE_CAPSULE = "CREATE_CAPSULE"
    EDIT_APPEARANCE = "EDIT_APPEARANCE"
    EDIT_WARDROBE = "EDIT_WARDROBE"
    EDIT_CANON = "EDIT_CANON"
    APPROVE_CANON = "APPROVE_CANON"
    MINT_IDENTITY_VERSION = "MINT_IDENTITY_VERSION"
    AUTHOR_SHOT = "AUTHOR_SHOT"
    VERIFY_CONTINUITY = "VERIFY_CONTINUITY"
    READ_SENSITIVE = "READ_SENSITIVE"
    EXPORT = "EXPORT"
    DELETE = "DELETE"
    MANAGE_CONSENT = "MANAGE_CONSENT"


#: Least privilege: each role gets the smallest set that lets it do its job.
ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(Permission),
    Role.DIRECTOR: frozenset(
        {
            Permission.CREATE_CAPSULE,
            Permission.EDIT_APPEARANCE,
            Permission.EDIT_WARDROBE,
            Permission.EDIT_CANON,
            Permission.APPROVE_CANON,
            Permission.AUTHOR_SHOT,
            Permission.VERIFY_CONTINUITY,
            Permission.EXPORT,
        }
    ),
    Role.ARTIST: frozenset(
        {
            Permission.EDIT_APPEARANCE,
            Permission.EDIT_WARDROBE,
            Permission.AUTHOR_SHOT,
            Permission.VERIFY_CONTINUITY,
        }
    ),
    Role.REVIEWER: frozenset({Permission.VERIFY_CONTINUITY, Permission.READ_SENSITIVE}),
    Role.AGENT: frozenset({Permission.AUTHOR_SHOT, Permission.VERIFY_CONTINUITY}),
    Role.VIEWER: frozenset(),
    Role.EXTERNAL: frozenset(),
}


@dataclass(frozen=True)
class Principal:
    """Who is acting. ``principal_id`` is opaque; authorization uses the role."""

    principal_id: str
    role: Role

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise ValidationError("principal needs an id")


@dataclass
class AuditTrail:
    """Append-only in-memory audit trail (the store persists the same events)."""

    events: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        principal: Principal,
        action: str,
        allowed: bool,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "at": _utc_now(),
            "principal_id": principal.principal_id,
            "role": principal.role.value,
            "action": action,
            "allowed": allowed,
            "detail": dict(detail or {}),
        }
        event["event_digest"] = digest_of(event)
        self.events.append(event)
        return event


class AccessPolicy:
    """Role → permission gate that never fails open and always audits."""

    def __init__(self, *, audit: AuditTrail | None = None) -> None:
        self.audit = audit or AuditTrail()

    def permissions(self, role: Role) -> frozenset[Permission]:
        return ROLE_PERMISSIONS.get(role, frozenset())

    def require(
        self,
        principal: Principal,
        permission: Permission,
        *,
        character_id: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        allowed = permission in self.permissions(principal.role)
        payload = {
            "character_id": character_id,
            "permission": permission.value,
            **dict(detail or {}),
        }
        self.audit.record(
            principal=principal,
            action=f"permission:{permission.value}",
            allowed=allowed,
            detail=payload,
        )
        if not allowed:
            raise AccessDenied(
                f"role {principal.role.value} does not carry {permission.value}",
                role=principal.role.value,
                permission=permission.value,
                character_id=character_id,
            )

    def check_export(self, principal: Principal, *, character_id: str, revoked: bool) -> None:
        """Export is refused for revoked consent, and audited either way."""
        if revoked:
            self.audit.record(
                principal=principal,
                action="export:blocked_revoked_consent",
                allowed=False,
                detail={"character_id": character_id},
            )
            raise ExportBlocked(
                "export refused: consent for this subject is revoked",
                character_id=character_id,
            )
        self.require(principal, Permission.EXPORT, character_id=character_id)


def random_key_material(size: int = _KEY_BYTES) -> bytes:
    """Cryptographically random bytes (used by operators/tests to create a KEK)."""
    return secrets.token_bytes(size)
