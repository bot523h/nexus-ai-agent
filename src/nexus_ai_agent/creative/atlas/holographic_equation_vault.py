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
from dataclasses import dataclass
from typing import Any, Final

from nexus_ai_agent.creative.atlas.atlas_spine_governance import jcs_canonical_bytes

__all__ = [
    "VAULT_SCHEMA",
    "GF256_PRIMITIVE_POLY",
    "GF256_GENERATOR",
    "MAX_SHARDS",
    "MAX_EQUATIONS",
    "DEFAULT_PARITY",
    "VaultError",
    "gf256_add",
    "gf256_mul",
    "gf256_inv",
    "mds_generator",
    "encode_shards",
    "decode_shards",
    "EquationRecord",
    "ShardSet",
    "HolographicVaultReceipt",
    "HolographicVaultAudit",
    "HolographicEquationVault",
]

VAULT_SCHEMA: Final[str] = "atlas.holographic-equation-vault/1"
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

    def available(self, indices: tuple[int, ...]) -> dict[int, bytes]:
        return {i: self.shards[i] for i in indices}

    def to_dict(self) -> dict[str, Any]:
        return {
            "equation_id": self.equation_id,
            "k": self.k,
            "n": self.n,
            "length": self.length,
            "shard_digests": [_sha256(s) for s in self.shards],
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
        """Rebuild the equation from any ``k`` of its shards; verify byte-exact."""
        record = self._records.get(equation_id)
        if record is None:
            raise VaultError(f"no equation {equation_id!r} in the vault")
        payload = decode_shards(shards, k=self._k, n=self._n)[: len(record.payload)]
        if payload != record.payload:
            raise VaultError(f"reconstructed equation {equation_id!r} does not match its seal")
        return payload.decode("utf-8")

    def audit(self) -> HolographicVaultAudit:
        """Prove the vault's four invariants against the sealed equations."""
        mds = True
        generator = mds_generator(self._k, self._n)
        from itertools import combinations

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
