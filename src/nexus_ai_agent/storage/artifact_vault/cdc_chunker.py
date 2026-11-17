"""Deterministic content-defined chunking using a Gear hash."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    offset: int
    data: bytes
    sha256: str

    @property
    def length(self) -> int:
        return len(self.data)

    @property
    def hash(self) -> str:
        return self.sha256


def _gear_table() -> tuple[int, ...]:
    value = 0x9E3779B97F4A7C15
    table: list[int] = []
    for _ in range(256):
        value = (value + 0x9E3779B97F4A7C15) & ((1 << 64) - 1)
        z = value
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & ((1 << 64) - 1)
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & ((1 << 64) - 1)
        table.append((z ^ (z >> 31)) & ((1 << 64) - 1))
    return tuple(table)


GEAR_TABLE = _gear_table()


class GearChunker:
    """Split bytes deterministically; boundaries are independent of process state."""

    def __init__(
        self,
        min_size: int = 2**12,
        avg_size: int = 2**13,
        max_size: int = 2**14,
        *,
        min_chunk_bytes: int | None = None,
        avg_chunk_bytes: int | None = None,
        max_chunk_bytes: int | None = None,
    ) -> None:
        if min_chunk_bytes is not None:
            min_size = min_chunk_bytes
        if avg_chunk_bytes is not None:
            avg_size = avg_chunk_bytes
        if max_chunk_bytes is not None:
            max_size = max_chunk_bytes
        if min_size < 1 or min_size > avg_size or avg_size > max_size:
            raise ValueError("require 0 < min_size <= avg_size <= max_size")
        if avg_size & (avg_size - 1):
            raise ValueError("avg_size must be a power of two")
        self.min_size, self.avg_size, self.max_size = min_size, avg_size, max_size
        self._mask = avg_size - 1

    def iter_chunks(self, data: bytes | bytearray | memoryview) -> Iterator[Chunk]:
        raw = bytes(data)
        start = 0
        fingerprint = 0
        index = 0
        for position, byte in enumerate(raw):
            fingerprint = ((fingerprint << 1) + GEAR_TABLE[byte]) & ((1 << 64) - 1)
            size = position + 1 - start
            if size >= self.min_size and ((fingerprint & self._mask) == 0 or size >= self.max_size):
                part = raw[start : position + 1]
                yield Chunk(index, start, part, hashlib.sha256(part).hexdigest())
                index += 1
                start = position + 1
                fingerprint = 0
        if start < len(raw) or not raw:
            part = raw[start:]
            yield Chunk(index, start, part, hashlib.sha256(part).hexdigest())

    def chunks(self, data: bytes | bytearray | memoryview) -> list[Chunk]:
        return list(self.iter_chunks(data))

    chunk = chunks


def chunk_bytes(data: bytes | bytearray | memoryview, **kwargs: int) -> list[Chunk]:
    return GearChunker(**kwargs).chunks(data)


ContentDefinedChunker = GearChunker


def split_content_defined_chunks(
    data: bytes | bytearray | memoryview,
    *,
    min_chunk_bytes: int = 2**12,
    avg_chunk_bytes: int = 2**13,
    max_chunk_bytes: int = 2**14,
) -> list[Chunk]:
    """Split data using the named Hyperion CDC parameter contract."""
    return GearChunker(
        min_chunk_bytes=min_chunk_bytes,
        avg_chunk_bytes=avg_chunk_bytes,
        max_chunk_bytes=max_chunk_bytes,
    ).chunks(data)
