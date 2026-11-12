"""RFC 6962-style domain-separated Merkle ledger for canonical receipts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


def canonical_receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    """Stable UTF-8 JSON representation; NaN and non-string keys are rejected."""
    return json.dumps(
        receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def leaf_hash(receipt: Mapping[str, Any] | bytes) -> bytes:
    data = canonical_receipt_bytes(receipt) if isinstance(receipt, Mapping) else receipt
    return hashlib.sha256(b"\x00" + data).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    if len(left) != 32 or len(right) != 32:
        raise ValueError("Merkle nodes must be SHA-256 digests")
    return hashlib.sha256(b"\x01" + left + right).digest()


def _root(hashes: Sequence[bytes]) -> bytes:
    if not hashes:
        return hashlib.sha256(b"").digest()
    if len(hashes) == 1:
        return hashes[0]
    split = 1 << ((len(hashes) - 1).bit_length() - 1)
    return node_hash(_root(hashes[:split]), _root(hashes[split:]))


def _path(hashes: Sequence[bytes], index: int) -> list[tuple[bool, bytes]]:
    size = len(hashes)
    if size <= 1:
        return []
    split = 1 << ((size - 1).bit_length() - 1)
    if index < split:
        return _path(hashes[:split], index) + [(False, _root(hashes[split:]))]
    return _path(hashes[split:], index - split) + [(True, _root(hashes[:split]))]


@dataclass(frozen=True, slots=True)
class InclusionProof:
    leaf_index: int
    tree_size: int
    audit_path: tuple[tuple[bool, bytes], ...]


def build_inclusion_proof(receipts: Sequence[Mapping[str, Any]], leaf_index: int) -> InclusionProof:
    if not receipts or not 0 <= leaf_index < len(receipts):
        raise ValueError("leaf index is outside a non-empty tree")
    leaves = [leaf_hash(receipt) for receipt in receipts]
    return InclusionProof(leaf_index, len(leaves), tuple(_path(leaves, leaf_index)))


def merkle_root(receipts: Sequence[Mapping[str, Any]]) -> bytes:
    return _root([leaf_hash(receipt) for receipt in receipts])


def verify_inclusion_proof(
    receipt: Mapping[str, Any], proof: InclusionProof, expected_root: bytes
) -> bool:
    if (
        proof.tree_size < 1
        or not 0 <= proof.leaf_index < proof.tree_size
        or len(expected_root) != 32
    ):
        return False
    # Reconstruct RFC 6962's recursively split tree, consuming audit nodes bottom-up.
    node = leaf_hash(receipt)
    path_i = 0
    for sibling_is_left, sibling in proof.audit_path:
        if len(sibling) != 32:
            return False
        node = node_hash(sibling, node) if sibling_is_left else node_hash(node, sibling)
        path_i += 1
    expected_depth = (proof.tree_size - 1).bit_length()
    return path_i <= expected_depth and node == expected_root
