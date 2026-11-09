"""Evidence bundles: content-addressed artifacts, lineage, and a detached signature.

Why this module exists
----------------------
A continuity verdict without evidence is an opinion. Every report this prototype
produces is therefore paired with a bundle that answers four questions without
trusting the process that wrote it:

1. **what** was measured — content digests of the frames, the audio, the
   calibration artifact and the report itself;
2. **from what** — a lineage chain back to the capsule digest and the corpus
   digest (the "ingredients" chain);
3. **by which rule set** — the calibration digest, including its thresholds;
4. **who says so** — a detached Ed25519 signature over the canonical claim, from
   a key that is *never* read from the repository.

C2PA compatibility, stated precisely
------------------------------------
This is **not** a C2PA implementation and does not claim to be one. It is a
*compatible shape*: a bundle carries assertions, a claim and one **hard binding**
— a digest over the asset bytes with explicit exclusions, the same structure as
``c2pa.hash.data`` in C2PA 2.x. A future integration would map:

===================================  ==========================================
this module                          C2PA 2.x
===================================  ==========================================
``EvidenceRecord.sha256``            assertion hashes (``c2pa.actions`` payloads)
``HardBinding.digest``               ``c2pa.hash.data`` (one hard binding only)
``HardBinding.exclusions``           ``exclusions`` byte ranges of the manifest
``Claim.assertions``                 the assertion store
``bundle.signature``                 ``claim_signature`` (COSE_Sign1)
``lineage``                          ingredient manifests
===================================  ==========================================

Two C2PA facts drive the design and are respected here: a manifest has exactly
one hard binding that excludes its own bytes, and the *absence* of a manifest
proves nothing — which is why :func:`verify_bundle` returns ``UNSIGNED`` as its
own state instead of an implicit pass.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import canonical_json, digest_of, sha256_hex
from .errors import ValidationError

#: Hard-binding algorithm identifiers this module will emit or accept.
HARD_BINDING_ALGORITHMS = ("sha256",)

#: Signing-key environment variable. There is no default key and no fallback.
SIGNING_KEY_ENV = "CHARACTER_CAPSULE_SIGNING_KEY_FILE"

#: Verification states of a bundle. ``UNSIGNED`` is deliberately distinct from
#: ``VALID``: an unsigned bundle is *unverified*, not verified-by-default.
BUNDLE_VALID = "VALID"
BUNDLE_UNSIGNED = "UNSIGNED"
BUNDLE_INVALID = "INVALID"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRecord:
    """One content-addressed input or output of a verification run."""

    name: str
    role: str
    media_type: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValidationError("evidence records need a name")
        if not self.sha256.startswith("sha256:"):
            raise ValidationError("evidence digests must be sha256:<hex>", value=self.sha256)
        if self.size_bytes < 0:
            raise ValidationError("evidence size must be non-negative", got=self.size_bytes)
        if self.media_type not in _MEDIA_TYPES:
            raise ValidationError("unsupported evidence media type", value=self.media_type)

    @property
    def digest(self) -> str:
        return digest_of(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_bytes(cls, *, name: str, role: str, media_type: str, data: bytes) -> EvidenceRecord:
        return cls(
            name=name,
            role=role,
            media_type=media_type,
            sha256=sha256_hex(data),
            size_bytes=len(data),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceRecord:
        return cls(
            name=str(payload["name"]),
            role=str(payload["role"]),
            media_type=str(payload["media_type"]),
            sha256=str(payload["sha256"]),
            size_bytes=int(payload["size_bytes"]),
        )


_MEDIA_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "audio/wav",
        "application/json",
        "text/markdown",
        "application/octet-stream",
    }
)


@dataclass(frozen=True)
class HardBinding:
    """A digest over the protected bytes, with the exclusions C2PA requires.

    ``exclusions`` are ``(offset, length)`` byte ranges that were *not* hashed —
    in a real C2PA manifest that range is the manifest itself. Modelling it here
    forces the exclusion to be explicit instead of silently forgotten.
    """

    algorithm: str
    digest: str
    exclusions: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        if self.algorithm not in HARD_BINDING_ALGORITHMS:
            raise ValidationError(
                "unsupported hard-binding algorithm",
                value=self.algorithm,
                supported=list(HARD_BINDING_ALGORITHMS),
            )
        if not self.digest.startswith("sha256:"):
            raise ValidationError("hard-binding digest must be sha256:<hex>", value=self.digest)
        for offset, length in self.exclusions:
            if offset < 0 or length < 0:
                raise ValidationError("exclusions must be non-negative ranges")

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "digest": self.digest,
            "exclusions": [[offset, length] for offset, length in self.exclusions],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> HardBinding:
        return cls(
            algorithm=str(payload["algorithm"]),
            digest=str(payload["digest"]),
            exclusions=tuple(
                (int(pair[0]), int(pair[1])) for pair in payload.get("exclusions", [])
            ),
        )


@dataclass(frozen=True)
class Signature:
    """A detached Ed25519 signature over the canonical claim."""

    algorithm: str
    key_id: str
    public_key_hex: str
    signature_hex: str
    signed_at: str

    def __post_init__(self) -> None:
        if self.algorithm != "ed25519":
            raise ValidationError("only ed25519 signatures are supported", value=self.algorithm)
        for name in ("public_key_hex", "signature_hex"):
            value = getattr(self, name)
            try:
                bytes.fromhex(value)
            except ValueError as exc:
                raise ValidationError(f"{name} must be hex", value=value) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "public_key_hex": self.public_key_hex,
            "signature_hex": self.signature_hex,
            "signed_at": self.signed_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Signature:
        return cls(
            algorithm=str(payload["algorithm"]),
            key_id=str(payload["key_id"]),
            public_key_hex=str(payload["public_key_hex"]),
            signature_hex=str(payload["signature_hex"]),
            signed_at=str(payload["signed_at"]),
        )


@dataclass(frozen=True)
class EvidenceBundle:
    """Assertions + one hard binding + lineage (+ signature when a key exists)."""

    bundle_id: str
    subject: Mapping[str, Any]
    assertions: Mapping[str, Any]
    records: tuple[EvidenceRecord, ...]
    hard_binding: HardBinding
    lineage: tuple[str, ...] = ()
    signature: Signature | None = None
    created_at: str = ""
    disclosure: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.lineage:
            raise ValidationError("an evidence bundle needs a lineage (capsule and corpus digests)")
        for link in self.lineage:
            if not link.startswith("sha256:"):
                raise ValidationError("lineage entries must be sha256:<hex>", value=link)

    # -- digests --------------------------------------------------------
    @property
    def claim(self) -> dict[str, Any]:
        """The signed claim: everything except the signature itself."""
        return {
            "bundle_id": self.bundle_id,
            "subject": dict(self.subject),
            "assertions": dict(self.assertions),
            "records": [record.to_dict() for record in self.records],
            "hard_binding": self.hard_binding.to_dict(),
            "lineage": list(self.lineage),
            "created_at": self.created_at,
            "disclosure": dict(self.disclosure),
        }

    @property
    def claim_digest(self) -> str:
        return digest_of(self.claim)

    @property
    def digest(self) -> str:
        return digest_of(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        payload = self.claim
        payload["signature"] = None if self.signature is None else self.signature.to_dict()
        payload["claim_digest"] = self.claim_digest
        return payload

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceBundle:
        signature = payload.get("signature")
        return cls(
            bundle_id=str(payload["bundle_id"]),
            subject=dict(payload["subject"]),
            assertions=dict(payload["assertions"]),
            records=tuple(EvidenceRecord.from_dict(item) for item in payload["records"]),
            hard_binding=HardBinding.from_dict(payload["hard_binding"]),
            lineage=tuple(str(link) for link in payload["lineage"]),
            signature=None if signature is None else Signature.from_dict(signature),
            created_at=str(payload.get("created_at", "")),
            disclosure=dict(payload.get("disclosure", {})),
        )

    # -- C2PA-shaped view ------------------------------------------------
    def c2pa_manifest_shape(self) -> dict[str, Any]:
        """A mapping (not an implementation) of this bundle onto C2PA 2.x fields."""
        return {
            "claim_generator": "nagar-character-capsule-prototype/1",
            "title": self.bundle_id,
            "format": self.subject.get("media_type", "image/png"),
            "assertions": [dict(self.assertions)],
            "ingredients": [{"hash": link} for link in self.lineage],
            "hard_binding": {
                "alg": self.hard_binding.algorithm,
                "hash": self.hard_binding.digest,
                "exclusions": self.hard_binding.to_dict()["exclusions"],
            },
            "claim_signature": None if self.signature is None else self.signature.to_dict(),
            "note": (
                "shape only: no C2PA SDK, no JUMBF container and no trust list are used; "
                "validate with verify_bundle() instead"
            ),
        }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def _hard_binding_for(records: Sequence[EvidenceRecord]) -> HardBinding:
    """One digest over the ordered record digests (the asset set as one asset)."""
    payload = canonical_json([record.to_dict() for record in records]).encode("utf-8")
    return HardBinding(algorithm="sha256", digest=sha256_hex(payload), exclusions=())


def build_bundle(
    *,
    bundle_id: str,
    subject: Mapping[str, Any],
    assertions: Mapping[str, Any],
    records: Iterable[EvidenceRecord],
    lineage: Sequence[str],
    created_at: str,
    disclosure: Mapping[str, Any] | None = None,
) -> EvidenceBundle:
    """Assemble a bundle; signing is a separate, explicit step."""
    ordered = tuple(sorted(records, key=lambda record: (record.role, record.name)))
    if not ordered:
        raise ValidationError("an evidence bundle with no records is not evidence")
    return EvidenceBundle(
        bundle_id=bundle_id,
        subject=dict(subject),
        assertions=dict(assertions),
        records=ordered,
        hard_binding=_hard_binding_for(ordered),
        lineage=tuple(lineage),
        created_at=created_at,
        disclosure=dict(disclosure or {}),
    )


def bundle_from_files(
    *,
    bundle_id: str,
    subject: Mapping[str, Any],
    assertions: Mapping[str, Any],
    files: Mapping[str, tuple[str, str]],
    lineage: Sequence[str],
    created_at: str,
    disclosure: Mapping[str, Any] | None = None,
) -> EvidenceBundle:
    """Build a bundle from ``{path: (role, media_type)}`` on disk.

    Reading the bytes here (rather than trusting a caller-supplied digest) is the
    whole point: the digest is computed from the artifact that exists now.
    """
    records = []
    for path_text, (role, media_type) in sorted(files.items()):
        path = Path(path_text)
        if not path.is_file():
            raise ValidationError("evidence input is missing", path=str(path))
        records.append(
            EvidenceRecord.from_bytes(
                name=path.name, role=role, media_type=media_type, data=path.read_bytes()
            )
        )
    return build_bundle(
        bundle_id=bundle_id,
        subject=subject,
        assertions=assertions,
        records=records,
        lineage=lineage,
        created_at=created_at,
        disclosure=disclosure,
    )


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def load_signing_key(path: str | Path | None = None) -> Any:
    """Load an Ed25519 signing key from a file outside the repository.

    Raises :class:`ValidationError` when no key is configured: an unsigned bundle
    is honest, a bundle signed with a checked-in key is not.
    """
    from .errors import KeyUnavailable

    resolved = Path(path or os.environ.get(SIGNING_KEY_ENV, ""))
    if not str(resolved) or str(resolved) == ".":
        raise KeyUnavailable(
            f"{SIGNING_KEY_ENV} is unset; evidence signing needs an external key file"
        )
    if not resolved.is_file():
        raise KeyUnavailable(f"signing key file does not exist: {resolved}")
    raw = resolved.read_bytes().strip()
    try:
        seed = bytes.fromhex(raw.decode("ascii")) if len(raw) == 64 else raw
    except (UnicodeDecodeError, ValueError) as exc:
        raise KeyUnavailable("signing key file must be 32 raw bytes or 64 hex chars") from exc
    if len(seed) != 32:
        raise KeyUnavailable("signing key must be exactly 32 bytes", got=len(seed))
    from nacl import signing

    return signing.SigningKey(seed)


def sign_bundle(bundle: EvidenceBundle, *, signing_key: Any, signed_at: str) -> EvidenceBundle:
    """Return a copy of ``bundle`` with a detached signature over its claim."""
    key_id = sha256_hex(bytes(signing_key.verify_key)).split(":", 1)[-1][:16]
    signature = signing_key.sign(canonical_json(bundle.claim).encode("utf-8"))
    return EvidenceBundle(
        bundle_id=bundle.bundle_id,
        subject=bundle.subject,
        assertions=bundle.assertions,
        records=bundle.records,
        hard_binding=bundle.hard_binding,
        lineage=bundle.lineage,
        signature=Signature(
            algorithm="ed25519",
            key_id=key_id,
            public_key_hex=bytes(signing_key.verify_key).hex(),
            signature_hex=signature.signature.hex(),
            signed_at=signed_at,
        ),
        created_at=bundle.created_at,
        disclosure=bundle.disclosure,
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BundleVerification:
    """Result of verifying a bundle against the bytes on disk."""

    status: str
    checks: Mapping[str, bool]
    reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in {BUNDLE_VALID, BUNDLE_UNSIGNED} and all(self.checks.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": dict(self.checks),
            "reasons": list(self.reasons),
        }


def verify_bundle(
    bundle: EvidenceBundle,
    *,
    files: Mapping[str, Path] | None = None,
    public_key_hex: str | None = None,
) -> BundleVerification:
    """Re-check structure, recorded digests, hard binding and signature.

    ``files`` maps a record ``name`` to the path that currently holds the bytes;
    absent names are skipped and reported in ``checks["files_present"]`` so a
    partial check can never look like a full pass.
    """
    checks: dict[str, bool] = {}
    reasons: list[str] = []

    checks["hard_binding_matches_records"] = (
        bundle.hard_binding.digest == _hard_binding_for(bundle.records).digest
    )
    if not checks["hard_binding_matches_records"]:
        reasons.append("hard binding does not match the recorded artifacts")

    checks["lineage_present"] = bool(bundle.lineage)

    if files:
        expected = {record.name: record.sha256 for record in bundle.records}
        matched = 0
        mismatched: list[str] = []
        for name, path in files.items():
            if name not in expected:
                reasons.append(f"file {name!r} is not described by the bundle")
                continue
            actual = sha256_hex(Path(path).read_bytes())
            if actual == expected[name]:
                matched += 1
            else:
                mismatched.append(name)
        if mismatched:
            reasons.append(
                "content changed since the bundle was written: " + ", ".join(sorted(mismatched))
            )
        checks["files_match"] = not mismatched
        checks["files_present"] = matched > 0

    status: str
    if bundle.signature is None:
        status = BUNDLE_UNSIGNED
        reasons.append(
            "bundle is unsigned: integrity is checkable, authenticity is not established"
        )
    else:
        key_hex = public_key_hex or bundle.signature.public_key_hex
        from nacl import signing

        try:
            verify_key = signing.VerifyKey(bytes.fromhex(key_hex))
            verify_key.verify(
                canonical_json(bundle.claim).encode("utf-8"),
                bytes.fromhex(bundle.signature.signature_hex),
            )
            checks["signature_valid"] = True
            status = BUNDLE_VALID
        except Exception:  # noqa: BLE001 - any verification failure is INVALID, not a crash
            checks["signature_valid"] = False
            reasons.append("signature does not verify against the claim")
            status = BUNDLE_INVALID

    if status == BUNDLE_VALID and not all(checks.values()):
        status = BUNDLE_INVALID
    return BundleVerification(
        status=status, checks=dict(sorted(checks.items())), reasons=tuple(reasons)
    )


__all__ = [
    "BUNDLE_INVALID",
    "BUNDLE_UNSIGNED",
    "BUNDLE_VALID",
    "SIGNING_KEY_ENV",
    "BundleVerification",
    "EvidenceBundle",
    "EvidenceRecord",
    "HardBinding",
    "Signature",
    "build_bundle",
    "bundle_from_files",
    "load_signing_key",
    "sign_bundle",
    "verify_bundle",
]
