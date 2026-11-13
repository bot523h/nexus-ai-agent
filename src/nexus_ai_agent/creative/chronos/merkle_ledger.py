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


def _expected_path_directions(tree_size: int, leaf_index: int) -> tuple[bool, ...]:
    """Return audit-node sides for the canonical RFC 6962 largest-power split."""
    if tree_size <= 1:
        return ()
    split = 1 << ((tree_size - 1).bit_length() - 1)
    if leaf_index < split:
        return _expected_path_directions(split, leaf_index) + (False,)
    return _expected_path_directions(tree_size - split, leaf_index - split) + (True,)


def verify_inclusion_proof(
    receipt: Mapping[str, Any], proof: InclusionProof, expected_root: bytes
) -> bool:
    audit_path = getattr(proof, "audit_path", None)
    if not isinstance(audit_path, tuple):
        return False
    if (
        not isinstance(proof.tree_size, int)
        or isinstance(proof.tree_size, bool)
        or not isinstance(proof.leaf_index, int)
        or isinstance(proof.leaf_index, bool)
        or proof.tree_size < 1
        or not 0 <= proof.leaf_index < proof.tree_size
        or not isinstance(expected_root, bytes)
        or len(expected_root) != 32
    ):
        return False
    directions = _expected_path_directions(proof.tree_size, proof.leaf_index)
    if len(audit_path) != len(directions):
        return False
    try:
        node = leaf_hash(receipt)
        for (sibling_is_left, sibling), expected_left in zip(audit_path, directions, strict=True):
            if (
                not isinstance(sibling_is_left, bool)
                or sibling_is_left is not expected_left
                or not isinstance(sibling, bytes)
                or len(sibling) != 32
            ):
                return False
            node = node_hash(sibling, node) if sibling_is_left else node_hash(node, sibling)
        return node == expected_root
    except (TypeError, ValueError, OverflowError):
        return False


def to_rfc6962_bundle(
    receipt: Mapping[str, Any], proof: InclusionProof, expected_root: bytes
) -> dict[str, Any]:
    """Serialize a JSON-safe RFC 6962/SHA-256 inclusion bundle.

    The bundle deliberately carries canonical receipt data, domain-prefix
    declarations, a lowercase root, and directional sibling digests. A vault
    or another track can verify the same leaf/node byte law without importing
    Chronos-specific dataclasses.
    """
    if len(expected_root) != 32 or not verify_inclusion_proof(receipt, proof, expected_root):
        raise ValueError("cannot bundle an invalid RFC 6962 inclusion proof")
    return {
        "format": "RFC6962-SHA256-v1",
        "leaf_prefix_hex": "00",
        "node_prefix_hex": "01",
        "receipt": dict(receipt),
        "leaf_sha256": leaf_hash(receipt).hex(),
        "root_sha256": expected_root.hex(),
        "proof": {
            "leaf_index": proof.leaf_index,
            "tree_size": proof.tree_size,
            "audit_path": [
                {"sibling_is_left": sibling_is_left, "sha256": sibling.hex()}
                for sibling_is_left, sibling in proof.audit_path
            ],
        },
    }


def verify_rfc6962_bundle(bundle: Mapping[str, Any]) -> bool:
    """Validate a portable JSON RFC 6962 proof bundle with strict framing."""
    if (
        bundle.get("format") != "RFC6962-SHA256-v1"
        or bundle.get("leaf_prefix_hex") != "00"
        or bundle.get("node_prefix_hex") != "01"
    ):
        return False
    receipt = bundle.get("receipt")
    proof_payload = bundle.get("proof")
    root_hex = bundle.get("root_sha256")
    leaf_hex = bundle.get("leaf_sha256")
    if not isinstance(receipt, Mapping) or not isinstance(proof_payload, Mapping):
        return False
    if not isinstance(root_hex, str) or not isinstance(leaf_hex, str):
        return False
    if len(root_hex) != 64 or len(leaf_hex) != 64:
        return False
    if any(character not in "0123456789abcdef" for character in root_hex + leaf_hex):
        return False
    raw_index, raw_size = proof_payload.get("leaf_index"), proof_payload.get("tree_size")
    if (
        not isinstance(raw_index, int)
        or isinstance(raw_index, bool)
        or not isinstance(raw_size, int)
        or isinstance(raw_size, bool)
    ):
        return False
    raw_path = proof_payload.get("audit_path")
    if not isinstance(raw_path, list):
        return False
    audit_path: list[tuple[bool, bytes]] = []
    for node in raw_path:
        if not isinstance(node, Mapping):
            return False
        direction, digest = node.get("sibling_is_left"), node.get("sha256")
        if not isinstance(direction, bool) or not isinstance(digest, str) or len(digest) != 64:
            return False
        if any(character not in "0123456789abcdef" for character in digest):
            return False
        audit_path.append((direction, bytes.fromhex(digest)))
    try:
        root = bytes.fromhex(root_hex)
        proof = InclusionProof(raw_index, raw_size, tuple(audit_path))
        return leaf_hash(receipt).hex() == leaf_hex and verify_inclusion_proof(receipt, proof, root)
    except (TypeError, ValueError, OverflowError):
        return False
