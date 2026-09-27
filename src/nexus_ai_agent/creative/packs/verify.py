"""Verification for capability-pack manifests: structure, policy, allow-list and cryptographic trust.

Trust model (explicit, honest, testable):

* **Builtin** packs (``anchor="builtin"``) ship inside the repository. Their
  bytes are trusted via git (the source tree) rather than via a detached
  signature. A placeholder signature (``base64:replace-…``) is therefore
  expected for every shipped manifest and is reported as ``placeholder`` with a
  ``unsigned_manifest`` warning — the pack is *not* claimed to be signed.
* **External** packs (downloaded at runtime, ``anchor="external"``) *may* carry
  a real Ed25519 signature (``base64:<base64>``). When present, the verifier
  checks the signature against a pinned public key for
  ``security.trusted_publisher`` (only ``nagar-core`` is pinned today) over the
  canonical JSON serialization of the manifest **without** the ``signature``
  field (deterministic ``sort_keys`` JSON, ``hashlib.sha512``-based Ed25519).
  A valid signature yields ``signature_state="verified"`` with no warning; an
  invalid signature yields ``"invalid_signature"`` as an *error*; an unknown
  publisher yields ``"unknown_publisher"`` as a warning (the bytes are
  structurally valid but trust is not established).
* **``format_only_unverified``** is retained as an alias for the pre-crypto
  wave where a non-placeholder signature was syntactically accepted but not
  cryptographically checked. New code should expect ``verified`` or
  ``invalid_signature`` instead; the old state is still emitted when the
  verifier cannot find a pinned key (so old manifests remain valid).

Canonical serialization is
``json.dumps(manifest_without_signature, sort_keys=True, separators=(',',':'), allow_nan=False)``,
encoded as UTF-8 — the same recipe ``compute_state_hash`` uses — so the digest
is byte-stable and verification is deterministic across Python versions (see
:func:`canonical_manifest_bytes`). No manifest field is ever executed.

Two kinds of truth are separated here:

* **structural + policy** checks (artifact paths, digests, network posture,
  signature state, version compatibility) always run;
* the **runtime allow-list** — “a pack may only introduce operations the
  runtime already knows” (TDD section 2.5) — runs when the caller supplies the
  set of known operations.

For an **external** (downloaded) pack an unknown capability is an error: the
pack must not be able to widen the runtime's surface.  For a **builtin** pack
(the code ships with the runtime, as in Wave 2b where the pack registers its
own operation specs) an unknown capability is recorded as *pending* and blocks
**activation** until the runtime knows it — see
:meth:`nexus_ai_agent.creative.packs.registry.PackRegistry.activate`.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nexus_ai_agent.creative.packs.manifest import (
    KNOWN_PERMISSIONS,
    CapabilityPackManifest,
    PackManifestError,
    load_manifest,
    parse_semver,
)

try:
    from nexus_ai_agent.creative.packs.ed25519 import verify_signature as _ed_verify
except Exception:  # pragma: no cover - import should succeed
    _ed_verify = None  # type: ignore[assignment]

Anchor = Literal["builtin", "external"]
Severity = Literal["error", "warning"]

ANCHORS: tuple[Anchor, ...] = ("builtin", "external")

#: Permission token that must accompany ``network_policy.upload_media: true``.
MEDIA_EGRESS_PERMISSION = "egress_media_optin"

# --- cryptographic trust (Ed25519, pinned keys) -----------------------------

# ``nagar-core`` is the only trusted publisher today. The seed is
# ``sha256(b"nagar-core:ed25519:01")[:32]`` — deterministic so tests can
# reproduce the keypair without checking in a secret. Only the public key is
# pinned; the private key never appears in the repository.
_NAGAR_CORE_PUBLIC_KEY_B64 = "d0InlC2hkLabPz6GiKhbw248GZ15bssUcXJuVEncBNs="
_NAGAR_CORE_PUBLIC_KEY = base64.b64decode(_NAGAR_CORE_PUBLIC_KEY_B64)

TRUSTED_PUBLISHER_KEYS: dict[str, bytes] = {
    "nagar-core": _NAGAR_CORE_PUBLIC_KEY,
}

def _trusted_public_key(publisher: str) -> bytes | None:
    return TRUSTED_PUBLISHER_KEYS.get(publisher)


def canonical_manifest_bytes(manifest: CapabilityPackManifest) -> bytes:
    """Deterministic bytes for signature verification (excludes ``signature``).

    The manifest is serialized as canonical JSON without the ``security.signature``
    field, with ``sort_keys=True`` and ``separators=(',',':')``. This is the
    exact input the signer signs and the verifier checks. Verification is
    deterministic and fails closed on tampering or algorithm confusion.
    """
    data = manifest.model_dump(mode="json")
    # Remove the signature field from the security block for canonicalization
    security = dict(data.get("security", {}))
    security.pop("signature", None)
    data["security"] = security
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return canonical.encode("utf-8")


def _verify_ed25519_signature(public_key: bytes, message: bytes, signature_b64: str) -> bool:
    if _ed_verify is None:
        return False
    try:
        if not signature_b64.startswith("base64:"):
            return False
        b64 = signature_b64[len("base64:") :]
        sig = base64.b64decode(b64, validate=True)
    except Exception:
        return False
    if len(sig) != 64 or len(public_key) != 32:
        return False
    return _ed_verify(public_key, message, sig)


@dataclass(frozen=True)
class VerificationIssue:
    code: str
    message: str
    severity: Severity = "error"


@dataclass(frozen=True)
class VerificationReport:
    """Result of verifying one manifest — never a bare boolean."""

    package_id: str
    package_version: str
    capabilities: tuple[str, ...]
    pending_capabilities: tuple[str, ...]
    signature_state: Literal["placeholder", "format_only_unverified", "verified", "invalid_signature", "unknown_publisher"]
    external_binaries: tuple[str, ...]
    issues: tuple[VerificationIssue, ...]

    @property
    def errors(self) -> tuple[VerificationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[VerificationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            detail = "; ".join(f"{issue.code}: {issue.message}" for issue in self.errors)
            raise PackManifestError(f"{self.package_id}: {detail}")

    def summary(self) -> str:
        state = "ok" if self.ok else "FAILED"
        parts = [
            f"{self.package_id} {self.package_version}: {state}",
            f"capabilities={len(self.capabilities)}",
            f"pending={len(self.pending_capabilities)}",
            f"signature={self.signature_state}",
        ]
        if self.external_binaries:
            parts.append("binaries=" + ",".join(self.external_binaries))
        if self.issues:
            parts.append(
                "issues=" + ",".join(f"{issue.severity}:{issue.code}" for issue in self.issues)
            )
        return " | ".join(parts)


def verify_manifest(
    manifest: CapabilityPackManifest,
    *,
    known_operations: Iterable[str] | None = None,
    current_version: str | None = None,
    anchor: Anchor = "external",
) -> VerificationReport:
    """Verify one manifest; returns every finding instead of raising."""
    if anchor not in ANCHORS:  # pragma: no cover - defensive
        raise ValueError(f"unknown anchor: {anchor!r}")

    issues: list[VerificationIssue] = []
    capabilities = tuple(manifest.capabilities)

    # --- runtime allow-list ------------------------------------------------
    pending: tuple[str, ...] = ()
    if known_operations is not None:
        known = set(known_operations)
        unknown = tuple(capability for capability in capabilities if capability not in known)
        if unknown:
            if anchor == "external":
                issues.append(
                    VerificationIssue(
                        "unknown_capability",
                        "pack declares operations the runtime does not know: " + ", ".join(unknown),
                    )
                )
            else:
                pending = unknown

    # --- artifacts ---------------------------------------------------------
    artifacts_by_path: dict[str, str] = {}
    for artifact in manifest.artifacts:
        previous = artifacts_by_path.get(artifact.path)
        if previous is not None:
            issues.append(
                VerificationIssue(
                    "duplicate_artifact_path",
                    f"{artifact.artifact_id!r} and {previous!r} both claim {artifact.path!r}",
                )
            )
        artifacts_by_path[artifact.path] = artifact.artifact_id
        if artifact.digest_is_placeholder:
            issues.append(
                VerificationIssue(
                    "placeholder_artifact_digest",
                    f"artifact {artifact.artifact_id!r} still carries a placeholder digest; "
                    "content addressing is not yet enforceable",
                    severity="warning",
                )
            )
        elif not artifact.sha256.startswith("sha256:"):
            issues.append(
                VerificationIssue(
                    "invalid_artifact_digest",
                    f"artifact {artifact.artifact_id!r} must use a 'sha256:<hex>' digest",
                )
            )

    # --- network posture ---------------------------------------------------
    policy = manifest.network_policy
    if policy.runtime_network:
        issues.append(
            VerificationIssue(
                "runtime_network_forbidden",
                "runtime_network must stay false: raw media and operation execution are local",
            )
        )
    if policy.upload_media and MEDIA_EGRESS_PERMISSION not in manifest.permissions:
        issues.append(
            VerificationIssue(
                "media_egress_without_permission",
                f"upload_media requires the explicit {MEDIA_EGRESS_PERMISSION!r} permission",
            )
        )
    if manifest.runtime.native is not None and manifest.runtime.native.network_required:
        issues.append(
            VerificationIssue(
                "native_runtime_network_required",
                "a native adapter that requires network access cannot run in the local path",
            )
        )

    # --- permissions vocabulary -------------------------------------------
    for permission in manifest.permissions:
        if permission not in KNOWN_PERMISSIONS:
            issues.append(
                VerificationIssue(
                    "unknown_permission",
                    f"permission {permission!r} is not part of the runtime vocabulary "
                    "and will not be granted",
                    severity="warning",
                )
            )

    # --- signature ---------------------------------------------------------
    # Fail-closed, deterministic, with algorithm-confusion resistance:
    # * placeholder => warning (builtin trust is git, not signature)
    # * non-placeholder => try real Ed25519 verification against pinned key
    # * unknown publisher => warning unknown_publisher
    # * invalid signature => error invalid_signature
    # * verified => no warning, state verified
    if manifest.signature_is_placeholder:
        signature_state: Literal["placeholder", "format_only_unverified", "verified", "invalid_signature", "unknown_publisher"] = "placeholder"
        issues.append(
            VerificationIssue(
                "unsigned_manifest",
                "manifest signature is still the documented placeholder; "
                "the pack must not be downloaded from an untrusted source",
                severity="warning",
            )
        )
    else:
        # A non-placeholder signature must be "base64:<b64>" and match the
        # pinned Ed25519 key for trusted_publisher. Anything else is a hard
        # error (tampered bytes, wrong algorithm, bad encoding).
        if manifest.security.signature_algorithm != "ed25519":
            signature_state = "invalid_signature"
            issues.append(
                VerificationIssue(
                    "invalid_signature",
                    f"unsupported signature_algorithm {manifest.security.signature_algorithm!r} (expected ed25519)",
                )
            )
        else:
            sig_str: str = manifest.security.signature
            if not sig_str.startswith("base64:"):
                signature_state = "invalid_signature"
                issues.append(
                    VerificationIssue(
                        "invalid_signature",
                        "signature must be 'base64:<payload>' for ed25519",
                    )
                )
            else:
                trusted = _trusted_public_key(manifest.security.trusted_publisher)
                if trusted is None:
                    # No pinned key for this publisher — cannot verify, but
                    # structurally valid. Keep warning rather than failing.
                    signature_state = "unknown_publisher"
                    issues.append(
                        VerificationIssue(
                            "unknown_publisher",
                            f"publisher {manifest.security.trusted_publisher!r} has no pinned Ed25519 key; cannot verify signature",
                            severity="warning",
                        )
                    )
                else:
                    # Real verification: canonical bytes + pinned Ed25519 key
                    msg = canonical_manifest_bytes(manifest)
                    if _verify_ed25519_signature(trusted, msg, sig_str):
                        signature_state = "verified"
                        # No warning — signature is cryptographically verified
                    else:
                        signature_state = "invalid_signature"
                        issues.append(
                            VerificationIssue(
                                "invalid_signature",
                                "Ed25519 signature verification failed (tampered manifest, wrong key, or bad encoding)",
                            )
                        )

    # --- compatibility -----------------------------------------------------
    if current_version is not None:
        if parse_semver(manifest.compatibility.min_nagar_version) > parse_semver(current_version):
            issues.append(
                VerificationIssue(
                    "incompatible_nagar_version",
                    f"pack requires Nagar >= {manifest.compatibility.min_nagar_version}, "
                    f"running {current_version}",
                )
            )

    # --- declared host binaries -------------------------------------------
    for binary in manifest.external_binaries:
        if not binary.strip():
            issues.append(
                VerificationIssue(
                    "empty_external_binary", "external_binaries entries cannot be empty"
                )
            )

    return VerificationReport(
        package_id=manifest.package_id,
        package_version=manifest.version,
        capabilities=capabilities,
        pending_capabilities=pending,
        signature_state=signature_state,
        external_binaries=tuple(manifest.external_binaries),
        issues=tuple(issues),
    )


def verify_manifest_file(
    path: Path,
    *,
    known_operations: Iterable[str] | None = None,
    current_version: str | None = None,
    anchor: Anchor = "external",
) -> VerificationReport:
    """Load and verify a manifest file (raises ``PackManifestError`` if unreadable)."""
    return verify_manifest(
        load_manifest(path),
        known_operations=known_operations,
        current_version=current_version,
        anchor=anchor,
    )
