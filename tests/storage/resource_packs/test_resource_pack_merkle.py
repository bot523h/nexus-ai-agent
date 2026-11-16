"""RFC 6962 Merkle tree, manifest model and fail-closed on-disk verification."""

from __future__ import annotations

import hashlib
import json
import os
import struct
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from resource_pack_testkit import PackMirror, blob, remote_entry

from nexus_ai_agent.storage.resource_packs import (
    ManifestValidationError,
    PackVerificationError,
    ResourceBlobEntry,
    ResourcePackManifest,
    SignatureVerificationError,
    build_manifest,
    compute_merkle_root,
    enforce_manifest_signature,
    leaf_hash,
    leaf_payload,
    manifest_from_json,
    manifest_to_dict,
    manifest_to_json,
    merkle_tree_root,
    node_hash,
    verify_installed_pack,
    verify_manifest_integrity,
)


def _rfc6962_mth(leaves: Sequence[bytes]) -> bytes:
    """Independent reference: RFC 6962 §2.1 recursive definition."""
    n = len(leaves)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return leaves[0]
    k = 1
    while k * 2 < n:
        k *= 2
    return hashlib.sha256(b"\x01" + _rfc6962_mth(leaves[:k]) + _rfc6962_mth(leaves[k:])).digest()


def _leaves(n: int) -> list[bytes]:
    return [leaf_hash(f"leaf-{i}".encode()) for i in range(n)]


def _install(manifest: ResourcePackManifest, blobs: dict[str, bytes], root: Path) -> Path:
    for entry in manifest.entries:
        target = root / entry.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blobs[entry.relative_path])
    return root


def _three_file_pack() -> tuple[ResourcePackManifest, dict[str, bytes]]:
    blobs = {
        "a/one.bin": blob("one", 300),
        "b/two.bin": blob("two", 70),
        "c.bin": blob("three", 1),
    }
    manifest = build_manifest(
        pack_id="core-pack",
        title_fa="پایه",
        tier="core",
        version="1.0.0",
        entries=[remote_entry(path, data) for path, data in blobs.items()],
    )
    return manifest, blobs


# --------------------------------------------------------------------------
# tree construction
# --------------------------------------------------------------------------


@pytest.mark.parametrize("n", list(range(0, 18)) + [31, 33, 100])
def test_level_wise_tree_equals_the_rfc6962_recursive_definition(n: int) -> None:
    leaves = _leaves(n)
    assert merkle_tree_root(leaves) == _rfc6962_mth(leaves)


def test_odd_last_node_is_promoted_never_duplicated() -> None:
    a, b, c = _leaves(3)
    promoted = node_hash(node_hash(a, b), c)
    duplicated = node_hash(node_hash(a, b), node_hash(c, c))
    assert merkle_tree_root([a, b, c]) == promoted
    assert merkle_tree_root([a, b, c]) != duplicated
    # promotion happens on every level: 5 leaves -> (((ab)(cd)) e)
    a, b, c, d, e = _leaves(5)
    assert merkle_tree_root([a, b, c, d, e]) == node_hash(
        node_hash(node_hash(a, b), node_hash(c, d)), e
    )


def test_cve_2012_2459_duplicated_tail_does_not_collide() -> None:
    """[a, b, c] vs [a, b, c, c]: the Bitcoin-style tree would give one root."""
    a, b, c = _leaves(3)
    assert merkle_tree_root([a, b, c]) != merkle_tree_root([a, b, c, c])
    triples = [("x", "0" * 64, 1), ("y", "1" * 64, 2), ("z", "2" * 64, 3)]
    assert compute_merkle_root(triples) != compute_merkle_root([*triples, triples[-1]])


def test_total_leaves_is_locked_into_the_final_root() -> None:
    triples = [("x", "0" * 64, 1), ("y", "1" * 64, 2), ("z", "2" * 64, 3)]
    honest = compute_merkle_root(triples)
    inner = merkle_tree_root([leaf_hash(leaf_payload(*t)) for t in triples])
    expected = hashlib.sha256(b"\x02" + struct.pack(">Q", 3) + inner).hexdigest()
    assert honest == expected
    assert compute_merkle_root(triples, total_leaves=4) != honest
    assert compute_merkle_root(triples, total_leaves=2) != honest


def test_leaf_and_node_hashes_are_domain_separated() -> None:
    payload = leaf_payload("dir/file.bin", "a" * 64, 7)
    assert leaf_hash(payload) == hashlib.sha256(b"\x00" + payload).digest()
    left, right = _leaves(2)
    assert node_hash(left, right) == hashlib.sha256(b"\x01" + left + right).digest()
    # a leaf whose payload *looks like* two child hashes cannot pose as a node
    assert leaf_hash(left + right) != node_hash(left, right)


def test_leaf_payload_is_unambiguous() -> None:
    assert leaf_payload("ab", "a" * 64, 1) != leaf_payload("a", "a" * 64, 1)
    assert leaf_payload("a", "a" * 64, 1) != leaf_payload("a", "a" * 64, 256)
    encoded = leaf_payload("ab", "a" * 64, 1)
    assert encoded[:4] == struct.pack(">I", 2) and len(encoded) == 4 + 2 + 32 + 8


# --------------------------------------------------------------------------
# manifest model
# --------------------------------------------------------------------------


def test_build_manifest_sorts_entries_and_derives_totals() -> None:
    manifest, blobs = _three_file_pack()
    assert [e.relative_path for e in manifest.entries] == ["a/one.bin", "b/two.bin", "c.bin"]
    assert manifest.total_leaves == 3
    assert manifest.total_installed_bytes == sum(len(b) for b in blobs.values())
    assert manifest.total_network_download_bytes == manifest.total_installed_bytes
    assert manifest.mandatory is True
    assert manifest.min_free_space_bytes == 2 * manifest.total_installed_bytes
    verify_manifest_integrity(manifest)


def test_manifest_json_round_trip_is_canonical() -> None:
    manifest, _ = _three_file_pack()
    encoded = manifest_to_json(manifest)
    assert manifest_from_json(encoded) == manifest
    assert manifest_from_json(encoded.decode()) == manifest
    assert manifest_to_json(manifest_from_json(encoded)) == encoded
    assert "signature" not in manifest_to_dict(manifest, include_signature=False)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d.update(schema=2),
        lambda d: d.update(entries="nope"),
        lambda d: d["entries"][0].update(extra=1),
        lambda d: d["entries"][0].update(size_bytes=1.5),
        lambda d: d["entries"][0].update(sha256="ZZ"),
        lambda d: d.update(total_leaves=-1),
    ],
)
def test_strict_parser_rejects_malformed_manifests(mutate: object) -> None:
    manifest, _ = _three_file_pack()
    data = json.loads(manifest_to_json(manifest))
    assert callable(mutate)
    mutate(data)
    with pytest.raises(ManifestValidationError):
        manifest_from_json(json.dumps(data))


def test_parser_rejects_invalid_json() -> None:
    with pytest.raises(ManifestValidationError, match="not valid JSON"):
        manifest_from_json(b"{not json")
    with pytest.raises(ManifestValidationError):
        manifest_from_json(b"[]")


@pytest.mark.parametrize(
    "path",
    ["", "/abs", "a/../b", "a//b", "./a", "a\\b", "c:/x", "a/\x00", "x" * 2000],
)
def test_entry_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ManifestValidationError):
        ResourceBlobEntry(path, "a" * 64, 1, "remote_blob")


def test_entry_field_validation() -> None:
    with pytest.raises(ManifestValidationError):
        ResourceBlobEntry("a", "A" * 64, 1, "remote_blob")
    with pytest.raises(ManifestValidationError):
        ResourceBlobEntry("a", "a" * 64, -1, "remote_blob")
    with pytest.raises(ManifestValidationError):
        ResourceBlobEntry("a", "a" * 64, True, "remote_blob")
    with pytest.raises(ManifestValidationError):
        ResourceBlobEntry("a", "a" * 64, 1, "torrent")  # type: ignore[arg-type]
    with pytest.raises(ManifestValidationError, match="need a seed_recipe"):
        ResourceBlobEntry("a", "a" * 64, 1, "local_dna_seed")
    with pytest.raises(ManifestValidationError, match="must not carry"):
        ResourceBlobEntry("a", "a" * 64, 1, "remote_blob", {"recipe": "x"})
    with pytest.raises(ManifestValidationError, match="floats"):
        ResourceBlobEntry("a", "a" * 64, 1, "local_dna_seed", {"gain": 1.5})
    with pytest.raises(ManifestValidationError, match="keys must be strings"):
        ResourceBlobEntry("a", "a" * 64, 1, "local_dna_seed", {"p": {1: 2}})


def test_seed_recipe_is_deeply_immutable() -> None:
    recipe: dict[str, object] = {"recipe": "lut3d_cube", "params": {"sizes": [1, 2]}}
    entry = ResourceBlobEntry("a", "a" * 64, 1, "local_dna_seed", recipe)
    recipe["recipe"] = "changed"
    assert entry.seed_recipe is not None and entry.seed_recipe["recipe"] == "lut3d_cube"
    with pytest.raises(TypeError):
        entry.seed_recipe["recipe"] = "x"  # type: ignore[index]
    assert entry.to_dict()["seed_recipe"] == {
        "recipe": "lut3d_cube",
        "params": {"sizes": [1, 2]},
    }


def test_manifest_structural_validation() -> None:
    manifest, _ = _three_file_pack()
    bad: list[dict[str, object]] = [
        {"pack_id": "Bad Id"},
        {"title_fa": "  "},
        {"tier": "ultra"},
        {"version": "../1"},
        {"mandatory": 1},
        {"mandatory": False},  # core is always mandatory
        {"entries": ()},
        {"entries": ("x",)},
        {"entries": tuple(reversed(manifest.entries))},
        {"entries": (manifest.entries[0], manifest.entries[0])},
        {"merkle_root": "nope"},
        {"signature": "abc"},
        {"min_free_space_bytes": -5},
    ]
    for change in bad:
        with pytest.raises(ManifestValidationError):
            replace(manifest, **change)  # type: ignore[arg-type]
    clash = (remote_entry("a", b"1"), remote_entry("a/b", b"2"))
    with pytest.raises(ManifestValidationError, match="nests under"):
        replace(manifest, entries=clash)
    optional = build_manifest(
        pack_id="hd", title_fa="سینمایی", tier="cinema_hd", version="2", entries=clash[:1]
    )
    assert optional.mandatory is False
    assert optional.remote_entries == clash[:1] and optional.seed_entries == ()


def test_integrity_check_reports_every_dishonest_total() -> None:
    manifest, _ = _three_file_pack()
    tampered = replace(
        manifest,
        total_installed_bytes=1,
        total_network_download_bytes=2,
        min_free_space_bytes=0,
        total_leaves=4,
    )
    with pytest.raises(PackVerificationError) as info:
        verify_manifest_integrity(tampered)
    text = " ".join(info.value.problems)
    for needle in ("total_leaves", "total_installed_bytes", "network", "min_free", "merkle_root"):
        assert needle in text


# --------------------------------------------------------------------------
# on-disk verification (fail closed)
# --------------------------------------------------------------------------


def test_verify_installed_pack_accepts_an_exact_install(tmp_path: Path) -> None:
    manifest, blobs = _three_file_pack()
    report = verify_installed_pack(
        manifest, _install(manifest, blobs, tmp_path / "ok"), allow_unsigned=True
    )
    assert report.verified_files == 3
    assert report.verified_bytes == manifest.total_installed_bytes
    assert report.merkle_root == manifest.merkle_root and not report.signature_verified


def test_verification_without_a_key_fails_closed_by_default(tmp_path: Path) -> None:
    """F1: an honest-looking but unsigned install is NOT trusted unless explicitly allowed."""
    manifest, blobs = _three_file_pack()
    install = _install(manifest, blobs, tmp_path / "ok")
    with pytest.raises(SignatureVerificationError, match="allow_unsigned=True"):
        verify_installed_pack(manifest, install)
    with pytest.raises(SignatureVerificationError):
        enforce_manifest_signature(manifest, None)
    assert enforce_manifest_signature(manifest, None, allow_unsigned=True) is False
    with pytest.raises(TypeError, match="bool"):
        enforce_manifest_signature(manifest, None, allow_unsigned="yes")  # type: ignore[arg-type]
    # the signature policy runs first: nothing on disk is even looked at
    with pytest.raises(SignatureVerificationError):
        verify_installed_pack(manifest, tmp_path / "nowhere")


def test_changing_total_leaves_fails_verification(tmp_path: Path) -> None:
    manifest, blobs = _three_file_pack()
    install = _install(manifest, blobs, tmp_path / "pack")
    for count in (2, 4):
        with pytest.raises(PackVerificationError, match="total_leaves"):
            verify_installed_pack(
                replace(manifest, total_leaves=count), install, allow_unsigned=True
            )


@pytest.mark.parametrize("victim", ["a/one.bin", "b/two.bin", "c.bin"])
def test_flipping_one_byte_in_any_file_fails_verification(tmp_path: Path, victim: str) -> None:
    manifest, blobs = _three_file_pack()
    install = _install(manifest, blobs, tmp_path / "pack")
    path = install / victim
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))
    with pytest.raises(PackVerificationError, match=f"{victim}: sha256 mismatch"):
        verify_installed_pack(manifest, install, allow_unsigned=True)


def test_missing_truncated_extra_and_symlinked_files_fail(tmp_path: Path) -> None:
    manifest, blobs = _three_file_pack()
    install = _install(manifest, blobs, tmp_path / "pack")
    (install / "c.bin").unlink()
    (install / "b/two.bin").write_bytes(b"short")
    (install / "intruder.so").write_bytes(b"\x7fELF")
    os.symlink(install / "a/one.bin", install / "link.bin")
    with pytest.raises(PackVerificationError) as info:
        verify_installed_pack(manifest, install, allow_unsigned=True)
    text = "\n".join(info.value.problems)
    assert "c.bin: missing" in text
    assert "b/two.bin: size 5" in text
    assert "intruder.so: unexpected file" in text
    assert "link.bin: symbolic links" in text


def test_missing_install_directory_fails(tmp_path: Path) -> None:
    manifest, _ = _three_file_pack()
    with pytest.raises(PackVerificationError, match="install directory missing"):
        verify_installed_pack(manifest, tmp_path / "nowhere", allow_unsigned=True)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs only")
def test_special_files_are_rejected(tmp_path: Path) -> None:
    manifest, blobs = _three_file_pack()
    install = _install(manifest, blobs, tmp_path / "pack")
    os.mkfifo(install / "pipe")
    with pytest.raises(PackVerificationError, match="pipe: not a regular file"):
        verify_installed_pack(manifest, install, allow_unsigned=True)


def test_mirror_fixture_builds_consistent_manifests(mirror: PackMirror) -> None:
    entry = mirror.add_remote("x.bin", b"payload")
    manifest = mirror.manifest("pk", "1.0.0", [entry], min_free_space_bytes=100)
    verify_manifest_integrity(manifest)
    assert manifest.min_free_space_bytes == 100


def test_missing_ed25519_backend_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``cryptography`` installed must mean "not verified", never "allowed"."""
    import importlib

    from nexus_ai_agent.storage.resource_packs import (
        SignatureBackendUnavailableError,
        SignatureVerificationError,
        sign_manifest,
        verify_manifest_signature,
    )

    real_import = importlib.import_module

    def without_cryptography(name: str, package: str | None = None) -> object:
        if name.startswith("cryptography"):
            raise ImportError(name)
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", without_cryptography)
    manifest, _ = _three_file_pack()
    signed = replace(manifest, signature="ab" * 64)
    with pytest.raises(SignatureBackendUnavailableError, match="cryptography"):
        verify_manifest_signature(signed, b"\x01" * 32)
    with pytest.raises(SignatureVerificationError):
        sign_manifest(manifest, b"\x02" * 32)
