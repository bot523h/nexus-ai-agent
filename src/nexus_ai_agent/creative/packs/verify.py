"""Verification for capability-pack manifests: structure, policy, allow-list.

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

Anchor = Literal["builtin", "external"]
Severity = Literal["error", "warning"]

ANCHORS: tuple[Anchor, ...] = ("builtin", "external")

#: Every signature state :func:`verify_manifest` can emit today.
SignatureState = Literal["placeholder", "format_only_unverified"]

#: Signature states that grant an **external** pack activation trust
#: (PACK-SEC-001).  EMPTY by construction: this wave ships no Ed25519
#: verifier (no cryptography dependency, no trusted publisher key chain), so
#: no state the verifier can produce is a verified state and every external
#: pack is refused at activation.  A future real verifier must add its new
#: state to BOTH :data:`SignatureState` and this set in the same change;
#: ``tests/unit/test_pack_manifest_verify.py`` pins that the set stays
#: disjoint from every state the current verifier can emit.
TRUSTED_SIGNATURE_STATES: frozenset[str] = frozenset()

#: Permission token that must accompany ``network_policy.upload_media: true``.
MEDIA_EGRESS_PERMISSION = "egress_media_optin"


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
    signature_state: SignatureState
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
    if manifest.signature_is_placeholder:
        signature_state: SignatureState = "placeholder"
        issues.append(
            VerificationIssue(
                "unsigned_manifest",
                "manifest signature is still the documented placeholder; "
                "the pack must not be downloaded from an untrusted source",
                severity="warning",
            )
        )
    else:
        signature_state = "format_only_unverified"
        issues.append(
            VerificationIssue(
                "signature_not_verified",
                "signature format accepted, but Ed25519 verification is not enabled in this "
                "wave (no cryptography dependency); treat the pack as unverified",
                severity="warning",
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
