"""Local, atomic, verifiable artifact storage built on erasure-coded shards."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .galois_erasure import ErasureShard, encode, reconstruct


def _leaf(value: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + value).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def merkle_root(leaves: list[bytes] | tuple[bytes, ...]) -> str:
    if not leaves:
        return hashlib.sha256(b"").hexdigest()
    level = [_leaf(bytes(leaf)) for leaf in leaves]
    while len(level) > 1:
        level = [
            _node(level[i], level[i + 1]) if i + 1 < len(level) else level[i]
            for i in range(0, len(level), 2)
        ]
    return level[0].hex()


def merkle_proof(leaves: list[bytes] | tuple[bytes, ...], index: int) -> list[tuple[str, str]]:
    if not 0 <= index < len(leaves):
        raise IndexError("leaf index out of range")
    level = [_leaf(bytes(leaf)) for leaf in leaves]
    proof: list[tuple[str, str]] = []
    while len(level) > 1:
        sibling = index - 1 if index % 2 else index + 1
        if sibling < len(level):
            proof.append(("left" if index % 2 else "right", level[sibling].hex()))
        index //= 2
        level = [
            _node(level[i], level[i + 1]) if i + 1 < len(level) else level[i]
            for i in range(0, len(level), 2)
        ]
    return proof


def verify_merkle_proof(
    leaf: bytes,
    index: int,
    proof: list[tuple[str, str]],
    root: str,
    *,
    total_leaves: int | None = None,
) -> bool:
    """Verify an RFC 6962 inclusion proof, binding the leaf position to ``index``.

    When ``total_leaves`` is given, the exact tree shape is replayed.  A trailing odd
    node is promoted without a proof step, a ``"left"`` sibling requires an odd index
    (right child), a ``"right"`` sibling requires an even index (left child), and the
    proof must consume exactly the steps that topology requires.  Without
    ``total_leaves`` the promotion levels are unknown, so only index sign and step
    labels are checked, and the parity rule is not applied.
    """
    if index < 0 or (total_leaves is not None and not 0 <= index < total_leaves):
        return False
    current = _leaf(bytes(leaf))
    idx = index
    count = total_leaves
    for side, sibling_hex in proof:
        if count is not None:
            while count > 1 and idx == count - 1 and count % 2 == 1:
                idx //= 2
                count = (count + 1) // 2  # promoted: no sibling, no proof step
            if count <= 1 or (idx + 1 if idx % 2 == 0 else idx - 1) >= count:
                return False
            count = (count + 1) // 2
        if side not in ("left", "right"):
            return False
        if count is not None and (
            (side == "left" and idx % 2 == 0) or (side == "right" and idx % 2 == 1)
        ):
            return False
        sibling = bytes.fromhex(sibling_hex)
        current = _node(sibling, current) if side == "left" else _node(current, sibling)
        idx //= 2
    if count is not None:
        while count > 1:
            if not (idx == count - 1 and count % 2 == 1):
                return False
            idx //= 2
            count = (count + 1) // 2
        if idx != 0:
            return False
    return current.hex() == root


compute_merkle_root = merkle_root
build_merkle_proof = merkle_proof
verify_proof = verify_merkle_proof


@dataclass(frozen=True, slots=True)
class ArtifactReceipt:
    artifact_id: str
    manifest_hash: str
    merkle_root: str
    verified: bool
    healed: tuple[int, ...] = ()
    timestamp: float = 0.0

    @property
    def root(self) -> str:
        return self.merkle_root


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    artifact_id: str
    original_size: int
    k: int
    m: int
    shard_size: int
    shard_hashes: tuple[str, ...]
    merkle_root: str


class ArtifactVault:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, artifact_id: str) -> Path:
        if (
            not artifact_id
            or "/" in artifact_id
            or "\\" in artifact_id
            or artifact_id in {".", ".."}
        ):
            raise ValueError("invalid artifact id")
        return self.root / artifact_id

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def store_artifact(
        self, artifact_id: str, data: bytes, k: int = 4, m: int = 2
    ) -> ArtifactReceipt:
        shards = encode(data, k, m)
        hashes = tuple(hashlib.sha256(s.data).hexdigest() for s in shards)
        manifest = ArtifactManifest(
            artifact_id,
            len(data),
            k,
            m,
            len(shards[0].data),
            hashes,
            merkle_root([s.data for s in shards]),
        )
        directory = self._dir(artifact_id)
        directory.mkdir(parents=True, exist_ok=True)
        for shard in shards:
            self._atomic_write(directory / f"shard-{shard.index:04d}.bin", shard.data)
        payload = json.dumps(asdict(manifest), sort_keys=True, separators=(",", ":")).encode()
        self._atomic_write(directory / "manifest.json", payload)
        return ArtifactReceipt(
            artifact_id,
            hashlib.sha256(payload).hexdigest(),
            manifest.merkle_root,
            True,
            (),
            time.time(),
        )

    put_artifact = store_artifact

    def load_manifest(self, artifact_id: str) -> ArtifactManifest:
        raw = json.loads((self._dir(artifact_id) / "manifest.json").read_text())
        return ArtifactManifest(**raw)

    def _read_shards(self, manifest: ArtifactManifest) -> list[ErasureShard]:
        directory = self._dir(manifest.artifact_id)
        result = []
        for index in range(manifest.k + manifest.m):
            path = directory / f"shard-{index:04d}.bin"
            if path.exists():
                result.append(
                    ErasureShard(
                        index, path.read_bytes(), manifest.k + manifest.m, manifest.k, manifest.m
                    )
                )
        return result

    def verify_and_heal_artifact(self, artifact_id: str) -> ArtifactReceipt:
        manifest = self.load_manifest(artifact_id)
        directory = self._dir(artifact_id)
        shards = self._read_shards(manifest)
        valid = [
            s
            for s in shards
            if hashlib.sha256(s.data).hexdigest() == manifest.shard_hashes[s.index]
        ]
        if len(valid) < manifest.k:
            raise ValueError("artifact cannot be healed: fewer than k valid shards")
        healed: list[int] = []
        recovered = reconstruct(valid, manifest.original_size)
        expected = encode(recovered, manifest.k, manifest.m)
        for shard in expected:
            if (
                not (directory / f"shard-{shard.index:04d}.bin").exists()
                or hashlib.sha256(
                    (directory / f"shard-{shard.index:04d}.bin").read_bytes()
                ).hexdigest()
                != manifest.shard_hashes[shard.index]
            ):
                self._atomic_write(directory / f"shard-{shard.index:04d}.bin", shard.data)
                healed.append(shard.index)
        return ArtifactReceipt(
            artifact_id,
            hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest(),
            manifest.merkle_root,
            True,
            tuple(healed),
            time.time(),
        )

    verify_and_heal = verify_and_heal_artifact

    def read_artifact(self, artifact_id: str) -> bytes:
        manifest = self.load_manifest(artifact_id)
        shards = self._read_shards(manifest)
        valid = [
            s
            for s in shards
            if hashlib.sha256(s.data).hexdigest() == manifest.shard_hashes[s.index]
        ]
        if len(valid) < manifest.k:
            raise ValueError("artifact is unavailable")
        return reconstruct(valid, manifest.original_size)
