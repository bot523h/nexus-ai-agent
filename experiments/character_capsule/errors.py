"""Typed failures for the Character Capsule island.

Every refusal in this prototype is a named error with a stable ``code``; no code
path raises a bare ``ValueError``/``KeyError`` for a policy decision. That is
what lets the tests assert *why* something was refused, and what lets the
continuity verifier report a machine-readable reason instead of a phrase.

The hierarchy is shallow on purpose: one root (``CapsuleError``) plus one class
per refusal family. Callers catch the family they can act on and let the rest
propagate (fail closed).
"""

from __future__ import annotations

from typing import Any


class CapsuleError(Exception):
    """Root of every typed refusal in this island."""

    code: str = "capsule_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(CapsuleError):
    """A payload is structurally invalid (schema, range, enum, unknown field)."""

    code = "validation_error"


class ScopeViolation(CapsuleError):
    """A change targets a field at a narrower scope than the field allows.

    Example: editing ``face.canon_eye_colour`` inside a capsule revision when
    the field is declared ``IDENTITY`` — that requires a new identity version,
    never a revision (visual drift hiding in a revision is exactly the failure
    mode this island exists to prevent).
    """

    code = "scope_violation"


class PinMismatch(ValidationError):
    """A shot's pinned capsule revision/digest/module digest is not the capsule supplied.

    Distinct from a generic validation failure because it is the specific attack
    of verifying a shot against a capsule it was never authored against.
    """

    code = "pin_mismatch"


class UnknownReference(CapsuleError):
    """A reference points at something that does not exist in this store."""

    code = "unknown_reference"


class DuplicateIdentity(CapsuleError):
    """A character id (or module revision id) is already taken."""

    code = "duplicate_identity"


class RevisionConflict(CapsuleError):
    """Compare-and-swap failed: the caller edited a stale revision."""

    code = "revision_conflict"


class IntegrityError(CapsuleError):
    """Measured bytes do not match the recorded digest/size (tamper or corruption)."""

    code = "integrity_error"


class AssetRejected(CapsuleError):
    """An asset was refused before storage (type, size, name, content mismatch)."""

    code = "asset_rejected"


class MediaUnsupported(CapsuleError):
    """A media container/processing operation is outside the supported allowlist."""

    code = "media_unsupported"


class MediaLimitExceeded(MediaUnsupported):
    """A declared media resource cap was exceeded (pixels, bytes, decompression).

    Separate from :class:`MediaUnsupported` so a caller can tell "wrong format"
    from "hostile size" without parsing a message.
    """

    code = "media_limit_exceeded"


class ConsentRequired(CapsuleError):
    """A real-person capsule has no valid consent record for the requested use."""

    code = "consent_required"


class ConsentRevoked(CapsuleError):
    """Consent was revoked; the protected operation is refused from now on."""

    code = "consent_revoked"


class AccessDenied(CapsuleError):
    """The principal's role does not carry the permission the operation needs."""

    code = "access_denied"


class CryptoUnavailable(CapsuleError):
    """No authenticated-encryption backend is importable — fail closed."""

    code = "crypto_unavailable"


class KeyUnavailable(CapsuleError):
    """The key-encryption key is missing or malformed — fail closed."""

    code = "key_unavailable"


class ExportBlocked(CapsuleError):
    """Export is refused (revoked consent, unresolved canon conflict, or policy)."""

    code = "export_blocked"


class ContractViolation(CapsuleError):
    """An integration-contract message is invalid or from an unsupported version."""

    code = "contract_violation"


class ThresholdsMissing(CapsuleError):
    """Continuity evaluation was requested without a calibration artifact."""

    code = "thresholds_missing"
