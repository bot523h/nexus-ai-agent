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
    explicit, revocable, purpose-scoped consent record. A consent record is only
    authorization when *all* of its scope matches the requested use: purpose,
    right, territory and time. Revocation is terminal: a revoked id is never
    revived, and revocation history survives a restart because the store
    persists it (see :class:`~experiments.character_capsule.storage.CapsuleRepository`).

``AccessPolicy``
    A role × permission matrix, with every attempt — allowed or refused — written
    to the audit trail. "Least privilege" is only real when refusals are visible.

Two design notes that were corrected after review
-------------------------------------------------
1. **Envelope format (NCC2).** The wrap step now stores the nonce *and* the
   authentication tag that ``SecretBox.encrypt`` produced. The previous format
   kept only ``.ciphertext`` and threw the nonce away, which made every encrypted
   asset unreadable — a defect, not a design choice. The format is versioned and
   a legacy ``NCC1`` blob is refused explicitly instead of being guessed at.
2. **Time is parsed, never compared as text.** ``2026-06-01T00:00:00+05:00`` and
   ``2026-05-31T19:00:00Z`` are the same instant; lexicographic comparison says
   otherwise and let expired consent stay valid.

Honesty note (also in the Live Truth document): PyNaCl's ``SecretBox`` is
XSalsa20-Poly1305 and has no associated-data parameter, so binding is done by
encrypting a canonical header *inside* the ciphertext and re-checking it after
decryption. That is equivalent for the threat model here (substitution of a blob
between purposes is detected as a header mismatch), and it is stated plainly
rather than implied to be AES-GCM AAD.
"""

from __future__ import annotations

import hmac
import os
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .canonical import canonical_json, digest_of, sha256_hex
from .errors import (
    AccessDenied,
    ConsentRequired,
    ConsentRevoked,
    CryptoUnavailable,
    KeyUnavailable,
    ValidationError,
)

# ---------------------------------------------------------------------------
# Authenticated encryption
# ---------------------------------------------------------------------------

_ENVELOPE_MAGIC = b"NCC2"
_LEGACY_MAGIC = b"NCC1"
_ENVELOPE_VERSION = 2
_NONCE_BYTES = 24
_KEY_BYTES = 32
_TAG_BYTES = 16


class Cipher(Protocol):
    """Minimal contract the blob store depends on."""

    def encrypt(self, plaintext: bytes, *, header: Mapping[str, Any]) -> bytes: ...

    def decrypt(self, blob: bytes, *, header: Mapping[str, Any]) -> bytes: ...

    @property
    def key_id(self) -> str: ...

    @property
    def provides_confidentiality(self) -> bool: ...


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


def crypto_backend_available() -> bool:
    """True when an authenticated cipher can actually be constructed."""

    try:
        _load_nacl()
    except CryptoUnavailable:
        return False
    return True


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
    """Per-object data key wrapped by a KEK; header bound inside the ciphertext.

    Layout (all lengths in bytes, big-endian where numeric)::

        "NCC2" | version(1) | header_len(2) | header | kek_nonce(24) |
        wrapped_key(48 = 32 key + 16 tag) | data_nonce(24) | body(...)

    The wrapped key is stored *with* its nonce and its 16-byte Poly1305 tag, which
    is what makes unwrapping possible at all.
    """

    wrapped_key_bytes = _KEY_BYTES + _TAG_BYTES

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

    @property
    def provides_confidentiality(self) -> bool:
        return True

    @property
    def envelope_version(self) -> int:
        return _ENVELOPE_VERSION

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _bound(header: Mapping[str, Any], plaintext: bytes) -> bytes:
        return canonical_json(
            {"header": dict(header), "payload_sha256": sha256_hex(plaintext)}
        ).encode("utf-8")

    # -- API --------------------------------------------------------------
    def encrypt(self, plaintext: bytes, *, header: Mapping[str, Any]) -> bytes:
        data_key = self._utils.random(_KEY_BYTES)
        kek_nonce = self._utils.random(_NONCE_BYTES)
        wrapped = self._box.encrypt(data_key, kek_nonce)
        if len(wrapped.ciphertext) != self.wrapped_key_bytes:
            raise CryptoUnavailable("wrapped data key has an unexpected length")
        data_nonce = self._utils.random(_NONCE_BYTES)
        body = self._secret.SecretBox(data_key).encrypt(plaintext, data_nonce).ciphertext
        # The header travels *inside* the authenticated plaintext: a blob moved to
        # another purpose decrypts to a header that no longer matches.
        bound = self._bound(header, plaintext)
        return b"".join(
            [
                _ENVELOPE_MAGIC,
                bytes([_ENVELOPE_VERSION]),
                len(bound).to_bytes(2, "big"),
                bound,
                wrapped.nonce,
                wrapped.ciphertext,
                data_nonce,
                body,
            ]
        )

    def decrypt(self, blob: bytes, *, header: Mapping[str, Any]) -> bytes:
        if blob[:4] == _LEGACY_MAGIC:
            raise CryptoUnavailable(
                "legacy NCC1 envelope: the wrapped data key was stored without its nonce, so "
                "this blob was never readable; re-encrypt it with the current cipher",
                envelope_version=1,
            )
        if not blob.startswith(_ENVELOPE_MAGIC):
            raise CryptoUnavailable("blob is not an NCC2 envelope")
        if len(blob) < len(_ENVELOPE_MAGIC) + 3:
            raise CryptoUnavailable("truncated envelope")
        version = blob[len(_ENVELOPE_MAGIC)]
        if version != _ENVELOPE_VERSION:
            raise CryptoUnavailable("unsupported envelope version", got=version)
        rest = blob[len(_ENVELOPE_MAGIC) + 1 :]
        header_len = int.from_bytes(rest[:2], "big")
        rest = rest[2:]
        bound = rest[:header_len]
        rest = rest[header_len:]
        minimum = _NONCE_BYTES + self.wrapped_key_bytes + _NONCE_BYTES + _TAG_BYTES
        if len(bound) != header_len or len(rest) < minimum:
            raise CryptoUnavailable("truncated envelope (header or body)")
        kek_nonce = rest[:_NONCE_BYTES]
        wrapped = rest[_NONCE_BYTES : _NONCE_BYTES + self.wrapped_key_bytes]
        data_nonce = rest[
            _NONCE_BYTES + self.wrapped_key_bytes : _NONCE_BYTES * 2 + self.wrapped_key_bytes
        ]
        body = rest[_NONCE_BYTES * 2 + self.wrapped_key_bytes :]
        try:
            data_key = self._box.decrypt(wrapped, kek_nonce)
        except Exception as exc:  # noqa: BLE001 - wrong KEK is a refusal, not a crash
            raise KeyUnavailable("cannot unwrap the data key with this KEK") from exc
        try:
            plaintext = bytes(self._secret.SecretBox(data_key).decrypt(body, data_nonce))
        except Exception as exc:  # noqa: BLE001 - tamper/corruption is a refusal
            raise CryptoUnavailable(
                "envelope authentication failed (tampered ciphertext or wrong key)"
            ) from exc
        if not hmac.compare_digest(bound, self._bound(header, plaintext)):
            raise CryptoUnavailable(
                "envelope header mismatch: this blob does not belong to this purpose/asset"
            )
        return plaintext


class PlaintextCipher:
    """Explicit, recorded plaintext mode.

    It exists so a caller has to *name* that they are not encrypting. There is no
    implicit fallback from :class:`EnvelopeCipher`, and a repository refuses to
    run in this mode without a written reason (``allow_plaintext="..."``) which is
    stored with the database and surfaced in ``storage_policy()``.
    """

    def __init__(self, *, reason: str) -> None:
        if not reason.strip():
            raise ValidationError("plaintext storage needs an explicit reason")
        self.reason = reason.strip()

    @property
    def key_id(self) -> str:
        return "none"

    @property
    def provides_confidentiality(self) -> bool:
        return False

    def encrypt(self, plaintext: bytes, *, header: Mapping[str, Any]) -> bytes:
        return plaintext

    def decrypt(self, blob: bytes, *, header: Mapping[str, Any]) -> bytes:
        return blob


# Backwards-compatible alias: the old name was misleading (it implied "no
# cipher" rather than "explicitly unencrypted"), but callers may still use it.
NullCipher = PlaintextCipher


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str, *, field_name: str = "timestamp") -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Accepts ``Z`` or an explicit offset. Naive timestamps are refused: assuming a
    timezone is exactly how a consent comparison becomes wrong.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must be an ISO-8601 timestamp", value=repr(value))
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field_name} must be an ISO-8601 timestamp", value=value) from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field_name} must carry an explicit timezone offset", value=value)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------


class ConsentPurpose(str, Enum):
    """What a consent record authorizes. Export is its own purpose on purpose."""

    IMAGE_SYNTHESIS = "IMAGE_SYNTHESIS"
    VOICE_SYNTHESIS = "VOICE_SYNTHESIS"
    MOTION_SYNTHESIS = "MOTION_SYNTHESIS"
    RENDER = "RENDER"
    EXPORT = "EXPORT"
    COMMERCIAL_DISTRIBUTION = "COMMERCIAL_DISTRIBUTION"
    TRAINING = "TRAINING"

    @classmethod
    def coerce(cls, value: str | ConsentPurpose) -> ConsentPurpose:
        if isinstance(value, ConsentPurpose):
            return value
        try:
            return cls(str(value))
        except ValueError as exc:
            raise ValidationError(
                "unknown consent purpose",
                value=value,
                known=sorted(purpose.value for purpose in cls),
            ) from exc


#: Rights a consent record can grant. ``identity`` is the right to use the
#: person's likeness at all; the narrower rights gate specific operations.
CONSENT_RIGHTS = frozenset(
    {"identity", "likeness", "voice", "motion", "wardrobe", "export", "distribution"}
)


@dataclass(frozen=True)
class ConsentRecord:
    """Explicit, purpose-scoped, territory-scoped, time-bounded consent."""

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
        unknown = sorted(set(self.rights) - CONSENT_RIGHTS)
        if unknown:
            raise ValidationError(
                "consent names unknown rights", unknown=unknown, known=sorted(CONSENT_RIGHTS)
            )
        if not self.territory.strip():
            raise ValidationError("consent must name a territory")
        if self.document_digest and not self.document_digest.startswith("sha256:"):
            raise ValidationError("consent document digest must be 'sha256:<hex>'")
        # Parse eagerly: a malformed date must never reach a comparison.
        granted = parse_utc(self.granted_at, field_name="granted_at")
        if self.expires_at is not None:
            expires = parse_utc(self.expires_at, field_name="expires_at")
            if expires <= granted:
                raise ValidationError(
                    "consent expires_at must be later than granted_at",
                    granted_at=self.granted_at,
                    expires_at=self.expires_at,
                )
        if self.revoked_at is not None:
            parse_utc(self.revoked_at, field_name="revoked_at")

    # -- scope ------------------------------------------------------------
    @property
    def is_revoked(self) -> bool:
        return bool(self.revoked_at)

    def territory_covers(self, territory: str | None) -> bool:
        if territory is None:
            return True
        granted = {item.strip().upper() for item in self.territory.split(",") if item.strip()}
        if granted & {"*", "WORLDWIDE", "GLOBAL"}:
            return True
        return territory.strip().upper() in granted

    def right_covers(self, right: str | None) -> bool:
        if right is None:
            return True
        return right in self.rights

    def covers(
        self,
        purpose: ConsentPurpose | str,
        *,
        now: str,
        right: str | None = None,
        territory: str | None = None,
    ) -> bool:
        """True only when every scope dimension allows this use at ``now``."""
        return self.decision(purpose, now=now, right=right, territory=territory)[0]

    def decision(
        self,
        purpose: ConsentPurpose | str,
        *,
        now: str,
        right: str | None = None,
        territory: str | None = None,
    ) -> tuple[bool, str]:
        """``(allowed, reason)`` — the reason is what an audit log should say."""
        target = ConsentPurpose.coerce(purpose)
        moment = parse_utc(now, field_name="now")
        if self.is_revoked:
            return False, f"revoked at {self.revoked_at}: {self.revocation_reason}".strip()
        if target not in self.purposes:
            return False, f"consent {self.consent_id} does not cover {target.value}"
        if moment < parse_utc(self.granted_at, field_name="granted_at"):
            return False, f"consent {self.consent_id} is not yet in force"
        if self.expires_at is not None and moment >= parse_utc(
            self.expires_at, field_name="expires_at"
        ):
            return False, f"consent {self.consent_id} expired at {self.expires_at}"
        if not self.right_covers(right):
            return False, f"consent {self.consent_id} does not grant right {right!r}"
        if not self.territory_covers(territory):
            return False, (
                f"consent {self.consent_id} covers territory {self.territory!r}, not {territory!r}"
            )
        return True, f"consent {self.consent_id} covers {target.value}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["purposes"] = [purpose.value for purpose in self.purposes]
        data["rights"] = list(self.rights)
        return data

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ConsentRecord:
        return cls(
            consent_id=str(payload["consent_id"]),
            character_id=str(payload["character_id"]),
            subject_reference=str(payload["subject_reference"]),
            purposes=tuple(ConsentPurpose.coerce(value) for value in payload["purposes"]),
            rights=tuple(str(value) for value in payload["rights"]),
            territory=str(payload["territory"]),
            granted_at=str(payload["granted_at"]),
            expires_at=None if payload.get("expires_at") is None else str(payload["expires_at"]),
            document_digest=str(payload.get("document_digest", "")),
            revoked_at=None if payload.get("revoked_at") is None else str(payload["revoked_at"]),
            revocation_reason=str(payload.get("revocation_reason", "")),
        )


@dataclass(frozen=True)
class ConsentDecision:
    """The auditable result of asking the ledger whether a use is allowed."""

    allowed: bool
    reason: str
    character_id: str
    purpose: str
    consent_id: str = ""
    right: str = ""
    territory: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ConsentLedger:
    """Consent records with terminal revocation and an append-only event log.

    The ledger itself is in-memory; :class:`CapsuleRepository` persists every
    record and every revocation and rehydrates the ledger on open, so a restart
    cannot resurrect revoked consent.
    """

    def __init__(self, *, now: Callable[[], str] | None = None) -> None:
        self._now = now or _utc_now
        self._records: dict[str, ConsentRecord] = {}
        self._events: list[dict[str, Any]] = []

    # -- population -------------------------------------------------------
    def add(self, record: ConsentRecord) -> ConsentRecord:
        """Record a consent. A revoked id can never be reused, by anyone."""
        existing = self._records.get(record.consent_id)
        if existing is not None:
            if existing.is_revoked:
                raise ConsentRevoked(
                    f"consent {record.consent_id} was revoked and cannot be re-issued under the "
                    "same id; a new consent must carry a new id",
                    consent_id=record.consent_id,
                    revoked_at=existing.revoked_at,
                )
            if existing == record:
                return existing
            raise ValidationError(
                "consent id already exists with different content",
                consent_id=record.consent_id,
            )
        self._records[record.consent_id] = record
        self._event("granted", record, detail={"purposes": [p.value for p in record.purposes]})
        return record

    def hydrate(self, records: Iterable[ConsentRecord]) -> None:
        """Load persisted records without re-emitting events (restart path)."""
        for record in records:
            self._records[record.consent_id] = record

    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._events)

    def _event(self, kind: str, record: ConsentRecord, *, detail: Mapping[str, Any]) -> None:
        event = {
            "at": self._now(),
            "kind": kind,
            "consent_id": record.consent_id,
            "character_id": record.character_id,
            "detail": dict(detail),
        }
        event["event_digest"] = digest_of(event)
        self._events.append(event)

    # -- accessors --------------------------------------------------------
    def get(self, consent_id: str) -> ConsentRecord | None:
        return self._records.get(consent_id)

    def records(self) -> tuple[ConsentRecord, ...]:
        return tuple(sorted(self._records.values(), key=lambda record: record.consent_id))

    def for_character(self, character_id: str) -> tuple[ConsentRecord, ...]:
        return tuple(record for record in self.records() if record.character_id == character_id)

    # -- revocation -------------------------------------------------------
    def revoke(self, consent_id: str, reason: str) -> ConsentRecord:
        record = self._records.get(consent_id)
        if record is None:
            raise ConsentRequired(f"unknown consent {consent_id!r}")
        if not reason.strip():
            raise ValidationError("revocation needs a reason")
        if record.is_revoked:
            raise ConsentRevoked(
                f"consent {consent_id} is already revoked (terminal)",
                consent_id=consent_id,
                revoked_at=record.revoked_at,
            )
        revoked = ConsentRecord(
            **{
                **{k: v for k, v in record.__dict__.items() if k != "revoked_at"},
                "revoked_at": self._now(),
                "revocation_reason": reason.strip(),
            }
        )
        self._records[consent_id] = revoked
        self._event("revoked", revoked, detail={"reason": reason.strip()})
        return revoked

    # -- decisions --------------------------------------------------------
    def decide(
        self,
        character_id: str,
        purpose: ConsentPurpose | str,
        *,
        right: str | None = None,
        territory: str | None = None,
    ) -> ConsentDecision:
        target = ConsentPurpose.coerce(purpose)
        now = self._now()
        candidates = self.for_character(character_id)
        if not candidates:
            return ConsentDecision(
                allowed=False,
                reason=(
                    f"no consent record for {character_id}; {target.value} needs explicit consent"
                ),
                character_id=character_id,
                purpose=target.value,
                right=right or "",
                territory=territory or "",
            )
        reasons: list[str] = []
        for record in candidates:
            allowed, reason = record.decision(target, now=now, right=right, territory=territory)
            if allowed:
                return ConsentDecision(
                    allowed=True,
                    reason=reason,
                    character_id=character_id,
                    purpose=target.value,
                    consent_id=record.consent_id,
                    right=right or "",
                    territory=territory or "",
                )
            reasons.append(reason)
        refused = ConsentDecision(
            allowed=False,
            reason="; ".join(reasons),
            character_id=character_id,
            purpose=target.value,
            right=right or "",
            territory=territory or "",
        )
        return refused

    def require(
        self,
        character_id: str,
        purpose: ConsentPurpose | str,
        *,
        right: str | None = None,
        territory: str | None = None,
    ) -> ConsentRecord:
        """Return the covering record or raise a typed, actionable refusal."""
        decision = self.decide(character_id, purpose, right=right, territory=territory)
        if decision.allowed:
            assert decision.consent_id  # noqa: S101 - invariant of an allowed decision
            record = self._records[decision.consent_id]
            return record
        candidates = self.for_character(character_id)
        revoked = [record for record in candidates if record.is_revoked]
        if candidates and len(revoked) == len(candidates):
            raise ConsentRevoked(
                f"all consent records for {character_id} are revoked;"
                f" {ConsentPurpose.coerce(purpose).value} is blocked",
                character_id=character_id,
                purpose=ConsentPurpose.coerce(purpose).value,
                revocation_reason=revoked[0].revocation_reason,
            )
        raise ConsentRequired(
            decision.reason,
            character_id=character_id,
            purpose=ConsentPurpose.coerce(purpose).value,
            right=right,
            territory=territory,
        )


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
            Permission.READ_SENSITIVE,
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

    def to_dict(self) -> dict[str, str]:
        return {"principal_id": self.principal_id, "role": self.role.value}


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

    def allows(self, principal: Principal, permission: Permission) -> bool:
        return permission in self.permissions(principal.role)

    def require(
        self,
        principal: Principal,
        permission: Permission,
        *,
        character_id: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        allowed = self.allows(principal, permission)
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


def random_key_material(size: int = _KEY_BYTES) -> bytes:
    """Cryptographically random bytes (used by operators/tests to create a KEK)."""
    return secrets.token_bytes(size)


__all__ = [
    "CONSENT_RIGHTS",
    "ROLE_PERMISSIONS",
    "AccessPolicy",
    "AuditTrail",
    "Cipher",
    "ConsentDecision",
    "ConsentLedger",
    "ConsentPurpose",
    "ConsentRecord",
    "EnvelopeCipher",
    "NullCipher",
    "Permission",
    "PlaintextCipher",
    "Principal",
    "Role",
    "crypto_backend_available",
    "load_kek",
    "parse_utc",
    "random_key_material",
]
