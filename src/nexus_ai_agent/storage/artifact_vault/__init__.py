"""Hyperion Phase 1 artifact vault primitives."""

from .cdc_chunker import Chunk, GearChunker, chunk_bytes, split_content_defined_chunks
from .galois_erasure import (
    ErasureShard,
    encode,
    encode_erasure_shards,
    gf_inverse,
    gf_multiply,
    matrix_invert,
    reconstruct,
    reconstruct_from_shards,
)
from .vault_manager import (
    ArtifactManifest,
    ArtifactReceipt,
    ArtifactVault,
    merkle_proof,
    merkle_root,
    verify_merkle_proof,
)

__all__ = [
    "ArtifactManifest",
    "ArtifactReceipt",
    "ArtifactVault",
    "Chunk",
    "ErasureShard",
    "GearChunker",
    "chunk_bytes",
    "split_content_defined_chunks",
    "encode",
    "encode_erasure_shards",
    "gf_inverse",
    "gf_multiply",
    "matrix_invert",
    "merkle_proof",
    "merkle_root",
    "reconstruct",
    "reconstruct_from_shards",
    "verify_merkle_proof",
]
