"""WAVE 30 — the Holographic Equation Vault.

Every normative equation of the ATLAS/PROMETHEUS doctrine is a short byte string.
Storing one copy is fragile: a single flipped byte loses the equation. The vault
stores each equation the way a hologram stores an image — *spread* across many
shards so that any sufficiently large subset rebuilds the whole, and no single
shard is privileged.

The mechanism is a systematic MDS Reed–Solomon code over ``GF(2^8)``:

* :func:`gf256_mul` / :func:`gf256_inv` — arithmetic in ``GF(2^8)`` with the
  primitive polynomial ``0x11D`` and generator ``0x02`` (log/exp tables);
* :func:`mds_generator` — the ``n x k`` *systematic* generator ``[I_k | P]``
  built from a Vandermonde matrix with ``n`` distinct nodes, so **every** ``k``
  rows form an invertible matrix (the MDS property: any ``k`` shards recover);
* :func:`encode_shards` / :func:`decode_shards` — split a payload into ``n``
  shards, ``k`` of which reconstruct it byte-exactly, and reconstruct from any
  ``k`` of them;
* :class:`HolographicEquationVault` — the vault itself: it seals a set of
  equations, exposes their shards, reconstructs any equation from any ``k``
  shards, and proves the holographic invariants into
  :class:`HolographicVaultReceipt` / :class:`HolographicVaultAudit`.

Everything is pure Python + stdlib, offline, bounded and deterministic; a
malformed parameter or a corrupt shard raises :class:`VaultError` rather than
returning a wrong answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from nexus_ai_agent.creative.atlas.atlas_spine_governance import jcs_canonical_bytes

__all__ = [
    "VAULT_SCHEMA",
    "SHARD_LEAF_SCHEMA",
    "SHARD_LEAF_DOMAIN",
    "MAX_EXHAUSTIVE_COMBINATIONS",
    "GF256_PRIMITIVE_POLY",
    "GF256_GENERATOR",
    "MAX_SHARDS",
    "MAX_EQUATIONS",
    "DEFAULT_PARITY",
    "VaultError",
    "VaultAuditComplexityError",
    "gf256_add",
    "gf256_mul",
    "gf256_inv",
    "mds_generator",
    "encode_shards",
    "decode_shards",
    "shard_leaf",
    "shard_merkle_root",
    "EquationRecord",
    "ShardSet",
    "HolographicVaultReceipt",
    "HolographicVaultAudit",
    "HolographicEquationVault",
]

VAULT_SCHEMA: Final[str] = "atlas.holographic-equation-vault/1"
#: The record schema written into the on-disk JCS vault manifest.
SHARD_LEAF_SCHEMA: Final[str] = "atlas.shard-leaf/1"
#: Domain separator for a shard Merkle leaf (RFC 6962 leaf prefix is separate).
SHARD_LEAF_DOMAIN: Final[bytes] = b"NAGAR_SHARD_V1\x00"
#: ``audit()`` enumerates ``comb(n, k)`` submatrices; above this it refuses
#: rather than hanging (``comb(255, 127)`` is astronomical).
MAX_EXHAUSTIVE_COMBINATIONS: Final[int] = 10_000
#: x^8 + x^4 + x^3 + x^2 + 1, the AES field polynomial (irreducible over GF(2)).
GF256_PRIMITIVE_POLY: Final[int] = 0x11D
GF256_GENERATOR: Final[int] = 0x02
#: ``n`` (total shards) is bounded so a payload never explodes into unbounded I/O.
MAX_SHARDS: Final[int] = 255
MAX_EQUATIONS: Final[int] = 4096
#: Default redundancy: two parity shards beyond the data shards.
DEFAULT_PARITY: Final[int] = 2


class VaultError(ValueError):
    """Raised for a malformed vault parameter, payload or shard set."""


class VaultAuditComplexityError(VaultError):
    """Raised when ``audit()`` would enumerate more than the bounded maximum."""


# --- GF(2^8) arithmetic ------------------------------------------------------------


def _build_tables() -> tuple[tuple[int, ...], tuple[int, ...]]:
    exp = [0] * 512
    log = [0] * 256
    value = 1
    for power in range(255):
        exp[power] = value
        log[value] = power
        value <<= 1
        if value & 0x100:
            value ^= GF256_PRIMITIVE_POLY
    for power in range(255, 512):
        exp[power] = exp[power - 255]
    return tuple(exp), tuple(log)


_EXP, _LOG = _build_tables()


def gf256_add(a: int, b: int) -> int:
    """Addition in ``GF(2^8)`` is XOR."""
    return a ^ b


def gf256_mul(a: int, b: int) -> int:
    """Multiplication in ``GF(2^8)`` via log/exp tables."""
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def gf256_inv(a: int) -> int:
    """Multiplicative inverse in ``GF(2^8)``; ``0`` has none."""
    if a == 0:
        raise VaultError("0 has no multiplicative inverse in GF(256)")
    return _EXP[255 - _LOG[a]]


def _gf256_pow(a: int, power: int) -> int:
    if power == 0:
        return 1
    if a == 0:
        return 0
    return _EXP[(_LOG[a] * power) % 255]


def _gf256_mat_inv(matrix: list[list[int]]) -> list[list[int]]:
    """Gauss–Jordan inversion of a square ``GF(2^8)`` matrix (raises on singular)."""
    size = len(matrix)
    aug = [row[:] + [1 if i == j else 0 for j in range(size)] for i, row in enumerate(matrix)]
    for col in range(size):
        pivot = next((r for r in range(col, size) if aug[r][col] != 0), None)
        if pivot is None:
            raise VaultError("singular matrix: the shard subset is not invertible")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        inv = gf256_inv(aug[col][col])
        aug[col] = [gf256_mul(v, inv) for v in aug[col]]
        for r in range(size):
            if r != col and aug[r][col] != 0:
                factor = aug[r][col]
                aug[r] = [aug[r][c] ^ gf256_mul(factor, aug[col][c]) for c in range(2 * size)]
    return [row[size:] for row in aug]


def mds_generator(k: int, n: int) -> tuple[tuple[int, ...], ...]:
    """The systematic ``n x k`` MDS generator ``[I_k | P]`` over ``GF(2^8)``.

    Built from a Vandermonde matrix with ``n`` distinct nodes; every ``k`` of its
    ``n`` rows are linearly independent, so any ``k`` shards reconstruct.
    """
    if not isinstance(k, int) or not isinstance(n, int):
        raise VaultError("k and n must be integers")
    if k < 1:
        raise VaultError("k must be >= 1")
    if n < k:
        raise VaultError("n must be >= k")
    if n > MAX_SHARDS:
        raise VaultError(f"n must be <= {MAX_SHARDS}")
    nodes = [_gf256_pow(GF256_GENERATOR, i) for i in range(n)]
    if len(set(nodes)) != n:  # pragma: no cover - generator 0x02 has order 255
        raise VaultError("Vandermonde nodes are not distinct")
    # Vandermonde n x k, then reduce the top k rows to the identity.
    vander = [[_gf256_pow(nodes[r], c) for c in range(k)] for r in range(n)]
    top_inv = _gf256_mat_inv(vander[:k])
    generator: list[tuple[int, ...]] = []
    for r in range(n):
        row = vander[r]
        out: list[int] = []
        for c in range(k):
            acc = 0
            for j in range(k):
                acc ^= gf256_mul(row[j], top_inv[j][c])
            out.append(acc)
        generator.append(tuple(out))
    return tuple(generator)


def _payload_bytes(payload: bytes | bytearray | str) -> bytes:
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    raise VaultError("payload must be bytes or str")


def encode_shards(payload: bytes | bytearray | str, *, k: int, n: int) -> tuple[bytes, ...]:
    """Split ``payload`` into ``n`` shards, any ``k`` of which reconstruct it.

    The payload is zero-padded to a multiple of ``k``; the original length is
    recovered by :func:`decode_shards`.
    """
    data = _payload_bytes(payload)
    generator = mds_generator(k, n)
    pad = (-len(data)) % k
    padded = data + b"\x00" * pad
    shards = [bytearray() for _ in range(n)]
    for offset in range(0, len(padded), k):
        block = padded[offset : offset + k]
        for r in range(n):
            acc = 0
            for c in range(k):
                acc ^= gf256_mul(generator[r][c], block[c])
            shards[r].append(acc)
    return tuple(bytes(s) for s in shards)


def decode_shards(shards: dict[int, bytes], *, k: int, n: int) -> bytes:
    """Reconstruct the payload from any ``k`` shards (keyed by shard index)."""
    if len(shards) < k:
        raise VaultError(f"need at least k={k} shards, got {len(shards)}")
    indices = sorted(shards)
    if indices[0] < 0 or indices[-1] >= n:
        raise VaultError(f"shard indices must be in [0, {n})")
    lengths = {len(shards[i]) for i in indices}
    if len(lengths) != 1:
        raise VaultError("all shards must have the same length")
    block_len = next(iter(lengths))
    generator = mds_generator(k, n)
    chosen = indices[:k]
    submatrix = [list(generator[r]) for r in chosen]
    inverse = _gf256_mat_inv(submatrix)
    payload = bytearray()
    for pos in range(block_len):
        received = [shards[r][pos] for r in chosen]
        for c in range(k):
            acc = 0
            for j in range(k):
                acc ^= gf256_mul(inverse[c][j], received[j])
            payload.append(acc)
    return bytes(payload)


# --- vault -------------------------------------------------------------------------


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class EquationRecord:
    """One sealed equation: its identity, its wave and its canonical statement."""

    equation_id: str
    wave: int
    statement: str

    @property
    def payload(self) -> bytes:
        return self.statement.encode("utf-8")

    @property
    def digest(self) -> str:
        return _sha256(self.payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "equation_id": self.equation_id,
            "wave": self.wave,
            "statement": self.statement,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class ShardSet:
    """The ``n`` shards of one equation plus the parameters that produced them."""

    equation_id: str
    k: int
    n: int
    length: int
    shards: tuple[bytes, ...]
    shard_merkle_root: str = ""

    def __post_init__(self) -> None:
        if not self.shard_merkle_root:
            object.__setattr__(self, "shard_merkle_root", shard_merkle_root(self.shards))

    def available(self, indices: tuple[int, ...]) -> dict[int, bytes]:
        return {i: self.shards[i] for i in indices}

    @property
    def shard_digests(self) -> tuple[str, ...]:
        return tuple(_sha256(s) for s in self.shards)

    def to_dict(self) -> dict[str, Any]:
        return {
            "equation_id": self.equation_id,
            "k": self.k,
            "n": self.n,
            "length": self.length,
            "shard_digests": list(self.shard_digests),
            "shard_merkle_root": self.shard_merkle_root,
        }


@dataclass(frozen=True)
class HolographicVaultReceipt:
    """Deterministic telemetry for one vault seal."""

    schema: str
    equation_count: int
    k: int
    n: int
    redundancy: float
    merkle_root: str
    vault_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "equation_count": self.equation_count,
            "k": self.k,
            "n": self.n,
            "redundancy": self.redundancy,
            "merkle_root": self.merkle_root,
            "vault_digest": self.vault_digest,
        }

    def canonical_bytes(self) -> bytes:
        return jcs_canonical_bytes(self.to_dict())


@dataclass(frozen=True)
class HolographicVaultAudit:
    """The proven invariants of the vault (all must hold for a valid seal)."""

    mds_property_verified: bool
    any_k_reconstruction_verified: bool
    fewer_than_k_is_refused: bool
    tamper_is_detected: bool

    @property
    def all_verified(self) -> bool:
        return (
            self.mds_property_verified
            and self.any_k_reconstruction_verified
            and self.fewer_than_k_is_refused
            and self.tamper_is_detected
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mds_property_verified": self.mds_property_verified,
            "any_k_reconstruction_verified": self.any_k_reconstruction_verified,
            "fewer_than_k_is_refused": self.fewer_than_k_is_refused,
            "tamper_is_detected": self.tamper_is_detected,
        }


def _merkle_root(leaves: list[bytes]) -> str:
    """RFC 6962-style Merkle root over already-hashed leaves (unbalanced safe)."""
    if not leaves:
        return _sha256(b"")
    level = [_sha256(b"\x00" + leaf) for leaf in leaves]
    while len(level) > 1:
        nxt: list[str] = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(_sha256(b"\x01" + bytes.fromhex(level[i]) + bytes.fromhex(level[i + 1])))
            else:
                nxt.append(level[i])
        level = nxt
    return level[0]


def shard_leaf(*, shard_index: int, total_shards: int, shard: bytes) -> bytes:
    """The canonical, domain-separated leaf for one encoded shard.

    The leaf commits to the shard's *position* (``shard_index`` of
    ``total_shards``) as well as its bytes, so a shard cannot be moved to another
    index and still verify against the Merkle root.
    """
    if not 0 <= shard_index < total_shards:
        raise VaultError(f"shard index {shard_index} out of range for {total_shards} shards")
    return jcs_canonical_bytes(
        {
            "schema": SHARD_LEAF_SCHEMA,
            "shard_index": shard_index,
            "total_shards": total_shards,
            "shard_sha256": _sha256(shard),
            "shard_length": len(shard),
        }
    )


def shard_merkle_root(shards: tuple[bytes, ...]) -> str:
    """RFC 6962 size-bound Merkle root over the actual encoded shards."""
    leaves = [
        SHARD_LEAF_DOMAIN + shard_leaf(shard_index=index, total_shards=len(shards), shard=shard)
        for index, shard in enumerate(shards)
    ]
    return _merkle_root(leaves)


class HolographicEquationVault:
    """A holographic store of doctrine equations with MDS redundancy.

    Seal equations with :meth:`seal`, then rebuild any of them from *any* ``k``
    of their ``n`` shards with :meth:`reconstruct`. The vault proves its own
    invariants with :meth:`audit` and emits a canonical receipt with
    :meth:`receipt`.
    """

    def __init__(self, *, k: int, parity: int = DEFAULT_PARITY) -> None:
        if not isinstance(k, int) or k < 1:
            raise VaultError("k must be an integer >= 1")
        if not isinstance(parity, int) or parity < 1:
            raise VaultError("parity must be an integer >= 1")
        if k + parity > MAX_SHARDS:
            raise VaultError(f"k + parity must be <= {MAX_SHARDS}")
        self._k = k
        self._parity = parity
        self._n = k + parity
        self._records: dict[str, EquationRecord] = {}
        self._shards: dict[str, ShardSet] = {}

    @property
    def k(self) -> int:
        return self._k

    @property
    def n(self) -> int:
        return self._n

    @property
    def equations(self) -> tuple[EquationRecord, ...]:
        return tuple(self._records.values())

    def seal(self, equation_id: str, wave: int, statement: str) -> ShardSet:
        """Seal one equation; returns its shard set (idempotent per id)."""
        if not equation_id:
            raise VaultError("equation_id must be non-empty")
        if equation_id in self._records:
            raise VaultError(f"equation {equation_id!r} is already sealed")
        if len(self._records) >= MAX_EQUATIONS:
            raise VaultError(f"the vault holds at most {MAX_EQUATIONS} equations")
        if not isinstance(wave, int) or wave < 0:
            raise VaultError("wave must be a non-negative integer")
        if not statement:
            raise VaultError("statement must be non-empty")
        record = EquationRecord(equation_id=equation_id, wave=wave, statement=statement)
        shards = encode_shards(record.payload, k=self._k, n=self._n)
        shard_set = ShardSet(
            equation_id=equation_id,
            k=self._k,
            n=self._n,
            length=len(record.payload),
            shards=shards,
        )
        self._records[equation_id] = record
        self._shards[equation_id] = shard_set
        return shard_set

    def shard_set(self, equation_id: str) -> ShardSet:
        try:
            return self._shards[equation_id]
        except KeyError:
            raise VaultError(f"no equation {equation_id!r} in the vault") from None

    def reconstruct(self, equation_id: str, shards: dict[int, bytes]) -> str:
        """Rebuild the equation from any ``k`` of its shards; verify byte-exact.

        Every supplied shard must pass its Merkle inclusion proof against the
        sealed :attr:`ShardSet.shard_merkle_root` before it is trusted, so a
        shard moved to another index or edited after the seal is refused rather
        than decoded into a wrong answer.
        """
        record = self._records.get(equation_id)
        if record is None:
            raise VaultError(f"no equation {equation_id!r} in the vault")
        self._verify_shard_inclusion(self._shards[equation_id], shards)
        payload = decode_shards(shards, k=self._k, n=self._n)[: len(record.payload)]
        if payload != record.payload:
            raise VaultError(f"reconstructed equation {equation_id!r} does not match its seal")
        return payload.decode("utf-8")

    def _verify_shard_inclusion(self, shard_set: ShardSet, shards: dict[int, bytes]) -> None:
        """Refuse any shard subset that is not the sealed shard set.

        Recomputes the RFC 6962 shard Merkle root over the sealed leaves with the
        supplied shards substituted at their indices; a mismatch means at least
        one shard fails its inclusion proof.
        """
        if shard_merkle_root(shard_set.shards) != shard_set.shard_merkle_root:
            raise VaultError("the stored shard set does not match its sealed Merkle root")
        for index in shards:
            if not 0 <= index < shard_set.n:
                raise VaultError(f"shard index {index} out of range for {shard_set.n} shards")
        leaves = [
            SHARD_LEAF_DOMAIN
            + shard_leaf(
                shard_index=index,
                total_shards=shard_set.n,
                shard=shards.get(index, shard_set.shards[index]),
            )
            for index in range(shard_set.n)
        ]
        if _merkle_root(leaves) != shard_set.shard_merkle_root:
            raise VaultError("shard Merkle inclusion proof failed: the shards are not the seal")

    def audit(self) -> HolographicVaultAudit:
        """Prove the vault's four invariants against the sealed equations.

        Refuses (:class:`VaultAuditComplexityError`) rather than hanging when the
        MDS sweep would enumerate more than :data:`MAX_EXHAUSTIVE_COMBINATIONS`
        submatrices — e.g. ``comb(255, 127)``.
        """
        from itertools import combinations
        from math import comb

        if comb(self._n, self._k) > MAX_EXHAUSTIVE_COMBINATIONS:
            raise VaultAuditComplexityError(
                f"audit would enumerate comb({self._n}, {self._k}) > "
                f"{MAX_EXHAUSTIVE_COMBINATIONS} submatrices; request a bounded "
                "algebraic verification instead"
            )
        mds = True
        generator = mds_generator(self._k, self._n)

        for combo in combinations(range(self._n), self._k):
            _gf256_mat_inv([list(generator[r]) for r in combo])

        any_k = True
        fewer = True
        tamper = True
        for record in self._records.values():
            shard_set = self._shards[record.equation_id]
            # any k shards reconstruct (head, tail and a spread subset)
            for indices in self._k_subsets():
                try:
                    rebuilt = self.reconstruct(record.equation_id, shard_set.available(indices))
                except VaultError:
                    any_k = False
                    break
                if rebuilt != record.statement:
                    any_k = False
                    break
            # fewer than k shards is refused
            if self._k > 1:
                try:
                    decode_shards({0: shard_set.shards[0]}, k=self._k, n=self._n)
                    fewer = False
                except VaultError:
                    pass
            # a corrupted data shard no longer reproduces the sealed equation
            corrupted = dict(shard_set.available(tuple(range(self._k))))
            corrupted[0] = bytes([corrupted[0][0] ^ 0xFF]) + corrupted[0][1:]
            try:
                if self.reconstruct(record.equation_id, corrupted) == record.statement:
                    tamper = False
            except VaultError:
                pass
        return HolographicVaultAudit(
            mds_property_verified=mds,
            any_k_reconstruction_verified=any_k,
            fewer_than_k_is_refused=fewer,
            tamper_is_detected=tamper,
        )

    def _k_subsets(self) -> tuple[tuple[int, ...], ...]:
        """A bounded, deterministic set of ``k``-subsets to exercise.

        ``n = k + parity`` and ``parity >= 1``, so ``n > k`` and every subset
        below has exactly ``k`` distinct members.
        """
        head = tuple(range(self._k))
        tail = tuple(range(self._n - self._k, self._n))
        subsets: set[tuple[int, ...]] = {head, tail}
        if self._k > 1:
            step = (self._n - 1) / (self._k - 1)
            spread = tuple(sorted({round(i * step) for i in range(self._k)}))
            if len(spread) == self._k:
                subsets.add(spread)
        return tuple(sorted(subsets))

    def merkle_root(self) -> str:
        leaves = [bytes.fromhex(r.digest) for r in self._records.values()]
        return _merkle_root(leaves)

    def shard_merkle_root(self, record_id: str) -> str:
        """The sealed RFC 6962 shard Merkle root of one equation's shards."""
        return self.shard_set(record_id).shard_merkle_root

    # --- disk persistence -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The canonical on-disk vault document (JCS-serialisable)."""
        return {
            "schema": VAULT_SCHEMA,
            "k": self._k,
            "n": self._n,
            "equations": [
                {
                    "equation_id": record.equation_id,
                    "wave": record.wave,
                    "statement": record.statement,
                    "digest": record.digest,
                    "length": self._shards[record.equation_id].length,
                    "shard_digests": list(self._shards[record.equation_id].shard_digests),
                    "shard_merkle_root": self._shards[record.equation_id].shard_merkle_root,
                }
                for record in self._records.values()
            ],
        }

    def save_to_disk(self, vault_dir: str | Path) -> Path:
        """Persist the vault to ``vault_dir`` atomically and return the directory.

        Writes one binary shard file per ``(equation_id, shard_index)`` plus a
        canonical JCS ``vault.json`` manifest; every file is written through a
        temp file, ``os.fsync``-ed, then ``os.replace``-d into place, so a crash
        can never leave a half-written shard or manifest.
        """
        directory = Path(vault_dir)
        directory.mkdir(parents=True, exist_ok=True)
        for record in self._records.values():
            shard_set = self._shards[record.equation_id]
            for index, shard in enumerate(shard_set.shards):
                _atomic_write(directory / _shard_filename(record.equation_id, index), shard)
        _atomic_write(directory / "vault.json", jcs_canonical_bytes(self.to_dict()))
        return directory

    @classmethod
    def load_from_disk(cls, vault_dir: str | Path) -> HolographicEquationVault:
        """Reload a vault from disk, verifying and repairing shard files.

        Every shard file is checked against its Merkle leaf; missing or corrupted
        files (up to ``n - k`` per equation) are reconstructed from the surviving
        shards and written back, so the vault restores across process restarts
        and heals disk damage without trusting a damaged byte.
        """
        directory = Path(vault_dir)
        manifest_path = directory / "vault.json"
        if not manifest_path.is_file():
            raise VaultError(f"no vault.json under {directory}")
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise VaultError(f"vault.json is not valid JSON: {exc}") from exc
        if document.get("schema") != VAULT_SCHEMA:
            raise VaultError(f"unknown vault schema {document.get('schema')!r}")
        vault = cls(k=int(document["k"]), parity=int(document["n"]) - int(document["k"]))
        for record_document in document["equations"]:
            equation_id = str(record_document["equation_id"])
            statement = str(record_document["statement"])
            record = EquationRecord(
                equation_id=equation_id,
                wave=int(record_document["wave"]),
                statement=statement,
            )
            if record.digest != str(record_document["digest"]):
                raise VaultError(f"equation {equation_id!r} on disk does not match its digest")
            shards = _read_and_repair_shards(
                directory, record, k=vault._k, n=vault._n, length=int(record_document["length"])
            )
            vault._records[equation_id] = record
            vault._shards[equation_id] = ShardSet(
                equation_id=equation_id,
                k=vault._k,
                n=vault._n,
                length=len(record.payload),
                shards=shards,
            )
        return vault

    def receipt(self) -> HolographicVaultReceipt:
        document = {
            "schema": VAULT_SCHEMA,
            "k": self._k,
            "n": self._n,
            "equations": [r.to_dict() for r in self._records.values()],
        }
        return HolographicVaultReceipt(
            schema=VAULT_SCHEMA,
            equation_count=len(self._records),
            k=self._k,
            n=self._n,
            redundancy=round(self._n / self._k, 6),
            merkle_root=self.merkle_root(),
            vault_digest=_sha256(jcs_canonical_bytes(document)),
        )


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write ``payload`` to ``path`` atomically (temp file + fsync + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _shard_filename(equation_id: str, index: int) -> str:
    """The deterministic, filesystem-safe shard file name for one shard."""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in equation_id)
    return f"{safe}.shard.{index}.bin"


def _manifest_shard_digests(directory: Path, record: EquationRecord) -> list[str]:
    """The per-shard digests the on-disk manifest sealed for ``record``."""
    try:
        document = json.loads((directory / "vault.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    for entry in document.get("equations", []):
        if str(entry.get("equation_id")) == record.equation_id:
            digests = entry.get("shard_digests")
            return [str(d) for d in digests] if isinstance(digests, list) else []
    return []


def _read_and_repair_shards(
    directory: Path, record: EquationRecord, *, k: int, n: int, length: int
) -> tuple[bytes, ...]:
    """Read the shard files, repair missing/corrupt ones from the survivors.

    A shard file is trusted only if its bytes hash to the digest the on-disk
    manifest sealed at that index; damaged or absent files (up to ``n - k`` per
    equation) are reconstructed from any ``k`` readable shards, verified against
    the record's seal, and rewritten atomically.
    """
    manifest_digests = _manifest_shard_digests(directory, record)
    good: dict[int, bytes] = {}
    for index in range(n):
        path = directory / _shard_filename(record.equation_id, index)
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if index < len(manifest_digests) and _sha256(raw) == manifest_digests[index]:
            good[index] = raw
    if len(good) < k:
        raise VaultError(
            f"equation {record.equation_id!r} has only {len(good)} readable shards; need {k}"
        )
    rebuilt = decode_shards(good, k=k, n=n)[:length]
    if rebuilt != record.payload:
        raise VaultError(f"equation {record.equation_id!r} on disk does not match its seal")
    fresh = encode_shards(rebuilt, k=k, n=n)
    for index in range(n):
        if index not in good:
            _atomic_write(directory / _shard_filename(record.equation_id, index), fresh[index])
    return fresh
