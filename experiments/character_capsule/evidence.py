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

#: Verification states of a bundle. Exactly one is returned, and the vocabulary is
#: closed so a consumer can switch on it exhaustively.
#:
#: ``VALID``             every recorded artifact matched and the signature verified
#:                       against a key supplied from *outside* the bundle
#: ``INVALID``           an artifact changed, or the signature does not verify
#: ``UNSIGNED``          nothing was signed: integrity checkable, authenticity absent
#: ``UNTRUSTED_SIGNER``  signed, but no independent anchor vouches for the key
#: ``INCOMPLETE``        not every recorded artifact was supplied for checking
#: ``NOT_VERIFIABLE``    the environment cannot check a signature (no crypto backend)
BUNDLE_VALID = "VALID"
BUNDLE_INVALID = "INVALID"
BUNDLE_UNSIGNED = "UNSIGNED"
BUNDLE_UNTRUSTED_SIGNER = "UNTRUSTED_SIGNER"
BUNDLE_INCOMPLETE = "INCOMPLETE"
BUNDLE_NOT_VERIFIABLE = "NOT_VERIFIABLE"

#: Most severe first. ``verify_bundle`` never reports a weaker state than one it
#: has already proven.
BUNDLE_STATE_SEVERITY = (
    BUNDLE_INVALID,
    BUNDLE_UNTRUSTED_SIGNER,
    BUNDLE_NOT_VERIFIABLE,
    BUNDLE_INCOMPLETE,
    BUNDLE_UNSIGNED,
    BUNDLE_VALID,
)


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
class TrustAnchor:
    """A public key supplied by the *consumer*, never read from the bundle.

    This is the whole point of separating integrity from authenticity: a bundle
    that carries its own key proves only that whoever wrote it could also sign it.
    """

    public_key_hex: str
    key_id: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        try:
            raw = bytes.fromhex(self.public_key_hex)
        except ValueError as exc:
            raise ValidationError("trust anchor public key must be hex") from exc
        if len(raw) != 32:
            raise ValidationError("an Ed25519 public key is 32 bytes", got=len(raw))

    @classmethod
    def from_file(cls, path: str | Path, *, label: str = "") -> TrustAnchor:
        raw = Path(path).read_bytes().strip()
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            text = raw.hex()
        return cls(public_key_hex=text.strip(), label=label or str(path))

    def to_dict(self) -> dict[str, str]:
        return {"public_key_hex": self.public_key_hex, "key_id": self.key_id, "label": self.label}


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
    """Result of verifying a bundle against the bytes on disk.

    ``integrity_ok`` answers "are these the bytes that were recorded?" and
    ``authenticity`` answers "who vouches for them?" — two different questions
    that the previous single boolean conflated.
    """

    status: str
    checks: Mapping[str, bool]
    reasons: tuple[str, ...] = ()
    integrity_ok: bool = False
    authenticity: str = BUNDLE_UNSIGNED
    missing_records: tuple[str, ...] = ()
    signer_key_id: str = ""
    trust_anchor_key_id: str = ""

    @property
    def ok(self) -> bool:
        """Strict: only a fully checked, externally anchored bundle is "ok"."""
        return self.status == BUNDLE_VALID and all(self.checks.values())

    @property
    def integrity_verified(self) -> bool:
        return self.integrity_ok and self.status != BUNDLE_INVALID

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "integrity_ok": self.integrity_ok,
            "authenticity": self.authenticity,
            "checks": dict(self.checks),
            "reasons": list(self.reasons),
            "missing_records": list(self.missing_records),
            "signer_key_id": self.signer_key_id,
            "trust_anchor_key_id": self.trust_anchor_key_id,
        }


def verify_bundle(
    bundle: EvidenceBundle,
    *,
    files: Mapping[str, Path] | None = None,
    trust_anchor: TrustAnchor | None = None,
    public_key_hex: str | None = None,
) -> BundleVerification:
    """Re-check structure, artifact bytes and signature; report exactly one state.

    ``files`` maps a record ``name`` to the path that currently holds the bytes.
    **Every** recorded artifact must be supplied — a partial check is reported as
    ``INCOMPLETE`` rather than passed. ``trust_anchor`` (or an explicit
    ``public_key_hex``) is the key the *consumer* trusts; the key embedded in the
    bundle is evidence about the signer, never a reason to trust it.
    """
    checks: dict[str, bool] = {}
    reasons: list[str] = []
    missing: list[str] = []

    # -- 1. structure ------------------------------------------------------
    checks["hard_binding_matches_records"] = (
        bundle.hard_binding.digest == _hard_binding_for(bundle.records).digest
    )
    if not checks["hard_binding_matches_records"]:
        reasons.append("hard binding does not match the recorded artifacts")
    checks["lineage_present"] = bool(bundle.lineage)
    if not checks["lineage_present"]:
        reasons.append("bundle has no lineage")
    names = [record.name for record in bundle.records]
    checks["records_unique"] = len(set(names)) == len(names)
    if not checks["records_unique"]:
        reasons.append("bundle names the same artifact more than once")
    structure_ok = all(checks.values())

    # -- 2. artifact bytes -------------------------------------------------
    integrity_ok = False
    if files is None:
        checks["all_artifacts_supplied"] = False
        reasons.append(
            "no artifacts were supplied: completeness cannot be established "
            "(the bundle is INCOMPLETE, not valid)"
        )
    else:
        expected = {record.name: record for record in bundle.records}
        supplied = set(files)
        missing = sorted(set(expected) - supplied)
        extra = sorted(supplied - set(expected))
        checks["all_artifacts_supplied"] = not missing
        if missing:
            reasons.append("recorded artifacts were not supplied: " + ", ".join(missing))
        if extra:
            reasons.append("supplied files are not described by the bundle: " + ", ".join(extra))
        mismatched: list[str] = []
        for name in sorted(set(expected) & supplied):
            actual = sha256_hex(Path(files[name]).read_bytes())
            if actual != expected[name].sha256:
                mismatched.append(name)
        checks["files_match"] = not mismatched
        if mismatched:
            reasons.append("content changed since the bundle was written: " + ", ".join(mismatched))
        integrity_ok = structure_ok and not missing and not mismatched

    # -- 3. signature ------------------------------------------------------
    anchor = trust_anchor
    if anchor is None and public_key_hex:
        anchor = TrustAnchor(public_key_hex=public_key_hex)
    authenticity = BUNDLE_UNSIGNED
    signer_key_id = ""
    if bundle.signature is None:
        checks["signature_present"] = False
        reasons.append(
            "bundle is unsigned: integrity is checkable, authenticity is not established"
        )
    else:
        checks["signature_present"] = True
        signer_key_id = bundle.signature.key_id
        if anchor is None:
            # No independent key: the signature can still be checked against the key
            # the bundle carries. That detects an edited claim (the signature no longer
            # matches) without establishing trust — the two are reported separately.
            checks["trust_anchor_supplied"] = False
            checking_key_hex = bundle.signature.public_key_hex
            reasons.append(
                "bundle is signed, but no independently trusted public key was supplied: "
                "the signature is checked against its own key, which proves possession, "
                "not trust"
            )
            authenticity_default = BUNDLE_UNTRUSTED_SIGNER
        elif anchor.public_key_hex.lower() != bundle.signature.public_key_hex.lower():
            checks["trust_anchor_matches_signer"] = False
            checking_key_hex = ""
            authenticity = BUNDLE_UNTRUSTED_SIGNER
            reasons.append("the supplied trust anchor is not the key that signed this bundle")
        elif anchor.key_id and signer_key_id and anchor.key_id != signer_key_id:
            # A pinned key id is part of the anchor: the same bytes with a different
            # declared id is a different key generation as far as rotation/revocation
            # policy is concerned.
            checks["trust_anchor_matches_signer"] = True
            checks["trust_anchor_key_id_matches"] = False
            checking_key_hex = ""
            authenticity = BUNDLE_UNTRUSTED_SIGNER
            reasons.append(
                "the trust anchor's key id does not match the signing key id",
            )
        else:
            checks["trust_anchor_matches_signer"] = True
            checking_key_hex = anchor.public_key_hex
            authenticity_default = BUNDLE_VALID
        if checking_key_hex:
            try:
                from nacl import signing
            except Exception:  # noqa: BLE001 - absence is a typed state, not a crash
                authenticity = BUNDLE_NOT_VERIFIABLE
                checks["signature_checked"] = False
                reasons.append(
                    "no Ed25519 backend is importable, so the signature cannot be checked "
                    "in this environment"
                )
            else:
                try:
                    signing.VerifyKey(bytes.fromhex(checking_key_hex)).verify(
                        canonical_json(bundle.claim).encode("utf-8"),
                        bytes.fromhex(bundle.signature.signature_hex),
                    )
                except Exception:  # noqa: BLE001 - a failed check is INVALID, not a crash
                    authenticity = BUNDLE_INVALID
                    checks["signature_checked"] = True
                    checks["signature_valid"] = False
                    reasons.append("signature does not verify against the claim")
                else:
                    authenticity = authenticity_default
                    checks["signature_checked"] = True
                    checks["signature_valid"] = True

    # -- 4. one state, most severe first -----------------------------------
    if not checks.get("files_match", True) or authenticity == BUNDLE_INVALID:
        status = BUNDLE_INVALID
    elif authenticity == BUNDLE_UNTRUSTED_SIGNER:
        status = BUNDLE_UNTRUSTED_SIGNER
    elif authenticity == BUNDLE_NOT_VERIFIABLE:
        status = BUNDLE_NOT_VERIFIABLE
    elif files is None or missing:
        status = BUNDLE_INCOMPLETE
    elif authenticity == BUNDLE_UNSIGNED:
        status = BUNDLE_UNSIGNED
    elif not structure_ok:
        status = BUNDLE_INVALID
    else:
        status = BUNDLE_VALID

    return BundleVerification(
        status=status,
        checks=dict(sorted(checks.items())),
        reasons=tuple(reasons),
        integrity_ok=integrity_ok and structure_ok,
        authenticity=authenticity,
        missing_records=tuple(missing),
        signer_key_id=signer_key_id,
        trust_anchor_key_id="" if anchor is None else anchor.key_id,
    )


__all__ = [
    "BUNDLE_INCOMPLETE",
    "BUNDLE_INVALID",
    "BUNDLE_NOT_VERIFIABLE",
    "BUNDLE_STATE_SEVERITY",
    "BUNDLE_UNSIGNED",
    "BUNDLE_UNTRUSTED_SIGNER",
    "BUNDLE_VALID",
    "SIGNING_KEY_ENV",
    "BundleVerification",
    "EvidenceBundle",
    "EvidenceRecord",
    "HardBinding",
    "Signature",
    "TrustAnchor",
    "build_bundle",
    "bundle_from_files",
    "load_signing_key",
    "sign_bundle",
    "verify_bundle",
]
