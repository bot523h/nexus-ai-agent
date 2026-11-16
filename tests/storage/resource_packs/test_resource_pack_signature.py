"""Ed25519 manifest signatures (``cryptography`` backend)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from resource_pack_testkit import PackMirror, blob, core_seed_entries, tree_snapshot  # noqa: E402

from nexus_ai_agent.storage.resource_packs import (  # noqa: E402
    LocalDirectoryPackSource,
    PackVerificationError,
    ResourcePackManifest,
    ResourcePackSyncManager,
    SignatureVerificationError,
    build_manifest,
    manifest_from_json,
    manifest_to_json,
    sign_manifest,
    verify_installed_pack,
    verify_manifest_signature,
)


def _keypair() -> tuple[bytes, bytes]:
    key = Ed25519PrivateKey.generate()  # ephemeral, never persisted
    private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, public


def _manifest(mirror: PackMirror) -> ResourcePackManifest:
    entry = mirror.add_remote("audio/ambience.ogg", blob("amb", 2048))
    return mirror.manifest("core", "1.0.0", [entry, *core_seed_entries()])


def test_signed_manifest_verifies_and_survives_json(mirror: PackMirror) -> None:
    private, public = _keypair()
    signed = sign_manifest(_manifest(mirror), private)
    assert signed.signature is not None and len(signed.signature) == 128
    verify_manifest_signature(signed, public)
    verify_manifest_signature(manifest_from_json(manifest_to_json(signed)), public)


@pytest.mark.parametrize(
    "change",
    [
        {"title_fa": "بسته جعلی"},
        {"version": "1.0.1"},
        {"min_free_space_bytes": 10**12},
        {"signature": "00" * 64},
    ],
)
def test_any_tampering_breaks_the_signature(mirror: PackMirror, change: dict[str, object]) -> None:
    private, public = _keypair()
    signed = sign_manifest(_manifest(mirror), private)
    with pytest.raises(SignatureVerificationError):
        verify_manifest_signature(replace(signed, **change), public)  # type: ignore[arg-type]


def test_tampered_entry_list_breaks_the_signature(mirror: PackMirror) -> None:
    private, public = _keypair()
    signed = sign_manifest(_manifest(mirror), private)
    forged = build_manifest(
        pack_id=signed.pack_id,
        title_fa=signed.title_fa,
        tier=signed.tier,
        version=signed.version,
        entries=signed.entries[:-1],
    )
    with pytest.raises(SignatureVerificationError):
        verify_manifest_signature(replace(forged, signature=signed.signature), public)


def test_wrong_key_unsigned_and_malformed_keys_are_rejected(mirror: PackMirror) -> None:
    private, _ = _keypair()
    _, stranger = _keypair()
    manifest = _manifest(mirror)
    signed = sign_manifest(manifest, private)
    with pytest.raises(SignatureVerificationError, match="does not verify"):
        verify_manifest_signature(signed, stranger)
    with pytest.raises(SignatureVerificationError, match="unsigned"):
        verify_manifest_signature(manifest, stranger)
    with pytest.raises(SignatureVerificationError, match="32 raw bytes"):
        verify_manifest_signature(signed, b"short")
    with pytest.raises(SignatureVerificationError, match="32 raw bytes"):
        sign_manifest(manifest, b"short")


def test_a_dishonest_manifest_is_never_signed(mirror: PackMirror) -> None:
    private, _ = _keypair()
    with pytest.raises(PackVerificationError):
        sign_manifest(replace(_manifest(mirror), total_leaves=99), private)


def test_installed_pack_verification_with_trusted_key(mirror: PackMirror, tmp_path: Path) -> None:
    private, public = _keypair()
    signed = sign_manifest(_manifest(mirror), private)
    manager = ResourcePackSyncManager(
        tmp_path / "device",
        [signed],
        LocalDirectoryPackSource(mirror.root),
        trusted_public_key=public,
        disk_free_probe=lambda _: 10**12,
    )
    result = manager.sync_pack("core")
    assert result.verification.signature_verified
    report = verify_installed_pack(signed, result.install_dir, trusted_public_key=public)
    assert report.signature_verified
    _, stranger = _keypair()
    with pytest.raises(SignatureVerificationError):
        verify_installed_pack(signed, result.install_dir, trusted_public_key=stranger)


def test_manager_refuses_unsigned_catalog_before_any_write(
    mirror: PackMirror, tmp_path: Path
) -> None:
    _, public = _keypair()
    device = tmp_path / "device"
    manager = ResourcePackSyncManager(
        device,
        [_manifest(mirror)],
        LocalDirectoryPackSource(mirror.root),
        trusted_public_key=public,
        disk_free_probe=lambda _: 10**12,
    )
    with pytest.raises(SignatureVerificationError):
        manager.sync_pack("core")
    with pytest.raises(SignatureVerificationError):
        manager.get_login_gate_status()
    assert tree_snapshot(device) == {}


# ── F2: verification is fail-closed, and "unsigned" is a decision ────────────


def _synced_pack(
    mirror: PackMirror, tmp_path: Path, *, signed: bool = True
) -> tuple[ResourcePackManifest, Path, bytes]:
    """Install a pack through the manager with the explicit unsigned opt-in."""
    private, public = _keypair()
    manifest = _manifest(mirror)
    catalog = sign_manifest(manifest, private) if signed else manifest
    manager = ResourcePackSyncManager(
        tmp_path / "device",
        [catalog],
        LocalDirectoryPackSource(mirror.root),
        allow_unsigned=True,
        disk_free_probe=lambda _: 10**12,
    )
    return catalog, manager.sync_pack("core").install_dir, public


def test_verify_installed_pack_refuses_to_run_without_a_trust_root(
    mirror: PackMirror, tmp_path: Path
) -> None:
    """No key and no opt-in is a configuration failure, not "skip the check".

    Before this change ``trusted_public_key=None`` silently skipped signature
    verification, so any caller that forgot to pass a key installed
    unauthenticated bytes and reported success.  The failure now happens before
    a single byte of the install is read.
    """
    signed, install, _ = _synced_pack(mirror, tmp_path)
    with pytest.raises(
        SignatureVerificationError,
        match="trusted_public_key is required for fail-closed pack verification",
    ):
        verify_installed_pack(signed, install)


def test_verify_installed_pack_accepts_an_explicit_unsigned_deployment(
    mirror: PackMirror, tmp_path: Path
) -> None:
    """``allow_unsigned=True`` is the only way to verify without a signature."""
    signed, install, public = _synced_pack(mirror, tmp_path)
    report = verify_installed_pack(signed, install, allow_unsigned=True)
    assert not report.signature_verified
    # A signed pack with a trusted key still verifies for real, opt-in or not.
    report = verify_installed_pack(signed, install, trusted_public_key=public, allow_unsigned=True)
    assert report.signature_verified


def test_verify_installed_pack_still_rejects_an_unsigned_manifest_with_a_key(
    mirror: PackMirror, tmp_path: Path
) -> None:
    _, public = _keypair()
    unsigned, install, _ = _synced_pack(mirror, tmp_path, signed=False)
    assert unsigned.signature is None
    with pytest.raises(SignatureVerificationError, match="unsigned"):
        verify_installed_pack(unsigned, install, trusted_public_key=public)
    # ... and allow_unsigned must not weaken a deployment that does have a key.
    with pytest.raises(SignatureVerificationError, match="unsigned"):
        verify_installed_pack(unsigned, install, trusted_public_key=public, allow_unsigned=True)


def test_the_manager_refuses_to_construct_without_a_trust_root(
    mirror: PackMirror, tmp_path: Path
) -> None:
    with pytest.raises(
        SignatureVerificationError,
        match="trusted_public_key is required for fail-closed pack verification",
    ):
        ResourcePackSyncManager(
            tmp_path / "device",
            [_manifest(mirror)],
            LocalDirectoryPackSource(mirror.root),
            disk_free_probe=lambda _: 10**12,
        )
    # The explicit opt-in is accepted and recorded on the instance.
    manager = ResourcePackSyncManager(
        tmp_path / "device",
        [_manifest(mirror)],
        LocalDirectoryPackSource(mirror.root),
        allow_unsigned=True,
        disk_free_probe=lambda _: 10**12,
    )
    assert manager.trusted_public_key is None and manager.allow_unsigned is True


def test_signature_required_is_the_single_policy_point() -> None:
    from nexus_ai_agent.storage.resource_packs.pack_manifest import signature_required

    assert signature_required(trusted_public_key=b"k" * 32) is True
    assert signature_required(trusted_public_key=b"k" * 32, allow_unsigned=True) is True
    assert signature_required(trusted_public_key=None, allow_unsigned=True) is False
    with pytest.raises(SignatureVerificationError):
        signature_required(trusted_public_key=None)
