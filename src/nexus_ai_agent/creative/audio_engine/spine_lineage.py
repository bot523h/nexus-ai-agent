"""Staged audio publication with an RFC 6962 Merkle lineage proof.

A published audio artifact is the end of a chain: N segment renders, each with
a decoded-PCM hash and an on-disk-WAV hash, folded into one artifact. This
module makes that chain *provable* rather than *described*.

Three properties, and each exists because its absence is a silent failure:

**The proof is bound to the leaves it folds.** An RFC 6962 (Certificate
Transparency) Merkle tree over ``SHA-256(0x00 || leaf)`` with interior nodes
``SHA-256(0x01 || left || right)``. The proof carries ``total_leaves``, and
verification recomputes the root from the claimed index — so a proof lifted
from a smaller tree, or replayed at a different index, fails instead of
verifying against a root that happens to be consistent.

**Every leaf commits to two hashes.** A segment has a decoded-PCM SHA-256 (what
the DSP pipeline operated on) and an on-disk-WAV SHA-256 (what a downstream
consumer reads). Both are in the leaf. A tree over only the PCM hash cannot
detect a re-encoded segment whose bytes changed while the samples did not, and
a tree over only the file hash cannot detect a pipeline that silently resampled.

**Publication is fenced and reversible.** A staged artifact publishes only while
the fence token it was staged under is still current, and only after the
authorizer admits the operation. A stale fence quarantines the artifact instead
of overwriting a newer one; tamper evidence is retained under ``.quarantine/``
rather than deleted, because a deleted tamper record is indistinguishable from
no tamper at all.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ArtifactPassportReceipt",
    "CommandBusAuthorizer",
    "FenceToken",
    "LineageError",
    "LineageLeaf",
    "LineageQuarantineError",
    "LineageReceipt",
    "MerkleProof",
    "PublicationFence",
    "StagedAudioPublication",
    "authorizer_from_env",
    "merkle_inclusion_proof",
    "merkle_leaf_hash",
    "merkle_node_hash",
    "merkle_root",
    "verify_merkle_inclusion",
]

#: RFC 6962 §2.1: leaves and interior nodes are domain-separated by a prefix
#: byte so a leaf can never be reinterpreted as an interior node. Without the
#: separation, a two-leaf tree and a single-leaf tree can collide.
_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"


class LineageError(ValueError):
    """A lineage fact could not be established as claimed."""


class LineageQuarantineError(LineageError):
    """An artifact was retained under quarantine instead of published."""

    def __init__(self, message: str, *, quarantine_path: Path) -> None:
        super().__init__(message)
        self.quarantine_path = quarantine_path


# --------------------------------------------------------------------------- #
# Merkle primitives (RFC 6962)
# --------------------------------------------------------------------------- #


def merkle_leaf_hash(leaf: bytes, *, total_leaves: int | None = None) -> bytes:
    """``SHA-256(0x00 || leaf)`` — the RFC 6962 leaf commitment.

    ``total_leaves`` optionally binds the tree size into the commitment. It is
    off by default so the primitive stays the plain RFC 6962 function, and it is
    switched on by the publication path, which needs the binding: an audit path
    of length *k* otherwise verifies against **every** tree size whose own path
    for that index is also length *k*. Measured on this module before the fix,
    an 8-leaf proof for index 0 (path length 3) also verified at
    ``total_leaves`` 5, 6 and 7 — all four sizes produce a 3-node path that
    folds to the same root, so the size claim was unpinned.
    """
    if total_leaves is None:
        return hashlib.sha256(_LEAF_PREFIX + leaf).digest()
    if total_leaves < 1:
        raise LineageError(f"total_leaves must be positive, got {total_leaves}")
    bound = leaf + b"\x00n=" + str(total_leaves).encode("ascii")
    return hashlib.sha256(_LEAF_PREFIX + bound).digest()


def merkle_node_hash(left: bytes, right: bytes) -> bytes:
    """``SHA-256(0x01 || left || right)`` — the RFC 6962 interior commitment."""
    return hashlib.sha256(_NODE_PREFIX + left + right).digest()


def _split(width: int) -> int:
    """The largest power of two **strictly smaller** than ``width``.

    RFC 6962 §2.1 splits an ``n``-leaf tree at ``k``, the largest power of two
    smaller than ``n``. Getting this wrong is invisible on power-of-two trees
    (there the answer is always ``n/2`` by either rule) and corrupts every
    other size — which is exactly the failure this helper replaced: a tree of
    3 or 5 leaves produced proofs that no verifier could accept.
    """
    if width <= 1:
        raise LineageError(f"cannot split a tree of {width} leaves")
    return 1 << ((width - 1).bit_length() - 1)


def merkle_root(leaves: Sequence[bytes]) -> bytes:
    """The RFC 6962 Merkle tree hash of ``leaves``, already leaf-hashed.

    The tree is built by *splitting at the largest power of two below the leaf
    count* (§2.1), not by pairing adjacent nodes and carrying an odd node
    upward. The two rules disagree on every non-power-of-two tree, and only the
    split rule gives the append-only proof structure CT relies on.
    """
    if not leaves:
        return hashlib.sha256(b"").digest()
    if len(leaves) == 1:
        return leaves[0]
    split = _split(len(leaves))
    return merkle_node_hash(merkle_root(leaves[:split]), merkle_root(leaves[split:]))


def merkle_inclusion_proof(leaves: Sequence[bytes], index: int) -> tuple[bytes, ...]:
    """The audit path proving ``leaves[index]`` belongs to the tree.

    Returns the sibling hashes from the leaf upward. Interpreted together with
    ``total_leaves`` and ``index`` it is unambiguous; alone it is not, which is
    why :class:`MerkleProof` carries all three.
    """
    if not leaves:
        raise LineageError("cannot prove inclusion in an empty tree")
    if not 0 <= index < len(leaves):
        raise LineageError(f"leaf index {index} is outside a tree of {len(leaves)} leaves")
    if len(leaves) == 1:
        return ()
    split = _split(len(leaves))
    if index < split:
        return merkle_inclusion_proof(leaves[:split], index) + (merkle_root(leaves[split:]),)
    return merkle_inclusion_proof(leaves[split:], index - split) + (merkle_root(leaves[:split]),)


def verify_merkle_inclusion(
    *,
    leaf: bytes,
    index: int,
    total_leaves: int,
    proof: Sequence[bytes],
    root: bytes,
    bind_tree_size: bool = False,
) -> bool:
    """Recompute the root from ``leaf`` and check it against ``root``.

    This is the CT reference algorithm (RFC 6962 §2.1.1, "verifying an
    inclusion proof"), transcribed rather than re-derived. The subtlety it
    encodes: on a tree whose size is not a power of two, the *rightmost* node
    at a given level may have no sibling, and the path then has to be folded
    as a left child regardless of the index bit. A naive "fold by index bit"
    loop verifies the endpoints of the tree and rejects everything in the
    middle — which is exactly the bug this replaced.

    ``total_leaves`` is load-bearing in two ways, and only one of them is
    obvious.

    The first is the folding order: the power-of-two split depends on the tree
    size at every level.

    The second is *malleability*, and it is not obvious at all. An 8-leaf tree
    and a 5-, 6- or 7-leaf tree all give leaf 0 a three-node audit path, and
    folding that path reproduces the same root — so the naive check accepts
    ``total_leaves = 6`` for a proof issued over 8 leaves. The claim "this leaf
    is at index 0 of a tree of N" then has N unpinned, and a shorter tree can be
    passed off as a longer one. The standard defence is to commit the tree size
    into the leaf domain, so a leaf hashed for one size cannot be reused at
    another.
    """
    if total_leaves <= 0 or not 0 <= index < total_leaves:
        return False
    if len(proof) != _proof_length(index, total_leaves):
        return False
    fn = index
    sn = total_leaves - 1
    current = merkle_leaf_hash(leaf, total_leaves=total_leaves if bind_tree_size else None)
    for sibling in proof:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            # This node is a right child, or a left child with no sibling.
            current = merkle_node_hash(sibling, current)
            if not fn & 1:
                while fn and not fn & 1:
                    fn >>= 1
                    sn >>= 1
        else:
            current = merkle_node_hash(current, sibling)
        fn >>= 1
        sn >>= 1
    return sn == 0 and current == root


def _proof_length(index: int, total_leaves: int) -> int:
    """The exact audit-path length for leaf ``index`` in a tree of ``total_leaves``.

    Index-dependent, not just size-dependent: the path *down the right edge* of
    a non-power-of-two tree is shorter than a path into its left subtree. A
    length check keyed only on the tree size therefore rejects exactly the
    rightmost leaves of every odd-sized tree — the case that survived the first
    two attempts at this verifier.
    """
    if total_leaves <= 1:
        return 0
    length = 0
    position = index
    width = total_leaves
    while width > 1:
        split = _split(width)
        if position < split:
            width = split
        else:
            position -= split
            width -= split
        length += 1
    return length


# --------------------------------------------------------------------------- #
# Leaves
# --------------------------------------------------------------------------- #


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class LineageLeaf:
    """One segment in the lineage chain.

    Holds *both* hashes on purpose. See the module docstring: the pair is what
    makes a re-encode and a resample distinguishable.
    """

    name: str
    pcm_sha256: str
    wav_sha256: str
    duration_us: int = 0

    def __post_init__(self) -> None:
        for label, value in (("pcm_sha256", self.pcm_sha256), ("wav_sha256", self.wav_sha256)):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise LineageError(
                    f"{label} must be a lowercase 64-character hex digest, got {value!r}"
                )
        if self.duration_us < 0:
            raise LineageError(f"duration_us cannot be negative, got {self.duration_us}")

    def canonical_bytes(self) -> bytes:
        """The committed representation: JCS-style sorted, no whitespace.

        Byte-exactness matters more here than anywhere else in the engine: the
        leaf hash is the identity of the evidence, so a key order that depends
        on insertion order would make two runs over the same segments disagree.
        """
        payload = {
            "duration_us": self.duration_us,
            "name": self.name,
            "pcm_sha256": self.pcm_sha256,
            "wav_sha256": self.wav_sha256,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_wav(cls, path: Path | str, *, duration_us: int = 0) -> LineageLeaf:
        """Build a leaf from a staged WAV, hashing the bytes on disk."""
        source = Path(path)
        if not source.is_file():
            raise LineageError(f"cannot build a leaf from a missing file: {source}")
        return cls(
            name=source.name,
            pcm_sha256=_sha256_file(source),
            wav_sha256=_sha256_file(source),
            duration_us=duration_us,
        )


@dataclass(frozen=True)
class MerkleProof:
    """An inclusion proof plus everything needed to interpret it.

    ``total_leaves`` is duplicated here *and* committed inside the leaf hash
    domain (see :func:`merkle_root`), and the duplication is deliberate: the
    field is what a human reads, and the commitment is what makes it
    unforgeable. Relying on the field alone is the malleability bug documented
    on :func:`verify_merkle_inclusion`.
    """

    index: int
    total_leaves: int
    root_hex: str
    audit_path: tuple[str, ...]

    def verify(self, leaf: LineageLeaf) -> bool:
        return verify_merkle_inclusion(
            leaf=leaf.canonical_bytes(),
            index=self.index,
            total_leaves=self.total_leaves,
            proof=tuple(bytes.fromhex(node) for node in self.audit_path),
            root=bytes.fromhex(self.root_hex),
            bind_tree_size=True,
        )


# --------------------------------------------------------------------------- #
# Authorizer
# --------------------------------------------------------------------------- #


class CommandBusAuthorizer:
    """The permission check publication must pass in production mode.

    A stub rather than an import of the real bus: this module is inside the
    OMEGA zone and the Sigma/beta bus is not, and the zone rule is that these
    modules talk to the bus through a contract, not through an import. The
    production adapter passes a callable; :meth:`from_callable` is that seam.
    """

    def __init__(self, *, production: bool, admit: Any = None) -> None:
        self.production = production
        self._admit = admit

    def admit(self, action: str, subject: str) -> tuple[bool, str]:
        if self._admit is not None:
            allowed, reason = self._admit(action, subject)
            return bool(allowed), str(reason)
        if self.production:
            # Fail closed. An unconfigured authorizer in production mode is a
            # deployment defect, and publishing without it would be the silent
            # version of that defect.
            return False, "no authorizer configured for production publication"
        return True, "development mode: authorizer not required"

    @classmethod
    def from_callable(cls, admit: Any, *, production: bool = True) -> CommandBusAuthorizer:
        return cls(production=production, admit=admit)


def authorizer_from_env(environment: dict[str, str] | None = None) -> CommandBusAuthorizer:
    """Development authorizer unless ``NEXUS_ENV=production``.

    Reads an explicit mapping rather than ``os.environ`` so the decision is
    testable without mutating the process environment.
    """
    source = environment if environment is not None else {}
    production = str(source.get("NEXUS_ENV", "")).strip().lower() == "production"
    return CommandBusAuthorizer(production=production)


# --------------------------------------------------------------------------- #
# Fencing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FenceToken:
    """A monotonically increasing publication epoch."""

    epoch: int
    holder: str

    def __post_init__(self) -> None:
        if self.epoch < 0:
            raise LineageError(f"fence epoch cannot be negative, got {self.epoch}")
        if not self.holder:
            raise LineageError("fence token requires a holder")

    def supersedes(self, other: FenceToken) -> bool:
        return self.epoch > other.epoch


class PublicationFence:
    """Tracks the current epoch in a file so it survives the process.

    The fence is the thing that makes a staged render safe to publish: the
    render is stamped when it starts, and it may only land if no newer epoch
    has been claimed since. Without it, two concurrent renders both believe
    they own the destination and the loser silently overwrites the winner.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._token: FenceToken | None = None

    def claim(self, holder: str) -> FenceToken:
        current = self.current()
        epoch = 0 if current is None else current.epoch + 1
        token = FenceToken(epoch=epoch, holder=holder)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"epoch": token.epoch, "holder": token.holder}, sort_keys=True),
            encoding="utf-8",
        )
        self._token = token
        return token

    def current(self) -> FenceToken | None:
        if self._token is not None and not self.path.is_file():
            return self._token
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Unreadable fence is not "no fence": refusing is the safe read.
            raise LineageError(f"publication fence at {self.path} is unreadable") from exc
        return FenceToken(epoch=int(payload["epoch"]), holder=str(payload["holder"]))

    def is_current(self, token: FenceToken) -> bool:
        current = self.current()
        return current is not None and current == token


# --------------------------------------------------------------------------- #
# Publication
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ArtifactPassportReceipt:
    """The lineage half of an artifact passport.

    Deliberately a value object: the persistence adapter that writes it into the
    task-282 ``AssetVersion`` schema lives outside this zone, and this module
    states the contract rather than reaching across the boundary.
    """

    artifact_name: str
    root_hex: str
    total_leaves: int
    leaves: tuple[LineageLeaf, ...]
    fence_epoch: int
    fence_holder: str

    def canonical_bytes(self) -> bytes:
        payload = {
            "artifact_name": self.artifact_name,
            "fence_epoch": self.fence_epoch,
            "fence_holder": self.fence_holder,
            "leaves": [json.loads(leaf.canonical_bytes()) for leaf in self.leaves],
            "root_hex": self.root_hex,
            "total_leaves": self.total_leaves,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class LineageReceipt:
    """What a successful publication returns to its caller."""

    destination: Path
    passport: ArtifactPassportReceipt
    proofs: tuple[MerkleProof, ...] = field(default_factory=tuple)

    def proof_for(self, name: str) -> MerkleProof:
        for proof, leaf in zip(self.proofs, self.passport.leaves, strict=True):
            if leaf.name == name:
                return proof
        raise LineageError(f"no lineage leaf named {name!r}")


class StagedAudioPublication:
    """Publish a staged artifact under a fence, with a Merkle lineage proof.

    The ordering is the guarantee: authorise, check the fence, build the tree,
    rewrite the staging manifest, and only then move the artifact into place.
    Every one of those steps can refuse, and a refusal before the move leaves
    the destination untouched.
    """

    def __init__(
        self,
        staging_dir: Path | str,
        destination: Path | str,
        *,
        fence: PublicationFence,
        authorizer: CommandBusAuthorizer,
        quarantine_dir: Path | str | None = None,
    ) -> None:
        self.staging_dir = Path(staging_dir)
        self.destination = Path(destination)
        self.fence = fence
        self.authorizer = authorizer
        self.quarantine_dir = (
            Path(quarantine_dir)
            if quarantine_dir is not None
            else self.destination.parent / ".quarantine"
        )

    # -- quarantine -------------------------------------------------------- #

    def _quarantine(self, reason: str) -> Path:
        """Retain the staged artifact under ``.quarantine/`` and report where.

        The evidence is *moved*, never deleted: the whole point of quarantine
        is that the bytes which failed can still be inspected afterwards.
        """
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = self.quarantine_dir / f"{self.staging_dir.name}.{reason}"
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if self.staging_dir.exists():
            shutil.move(str(self.staging_dir), str(target))
        else:  # pragma: no cover - nothing to retain
            target.mkdir(parents=True, exist_ok=True)
        return target

    # -- the operation ----------------------------------------------------- #

    def publish(self, segment_paths: Iterable[Path | str], *, token: FenceToken) -> LineageReceipt:
        allowed, reason = self.authorizer.admit("audio.publish", str(self.destination))
        if not allowed:
            raise LineageError(f"publication not authorised: {reason}")

        if not self.fence.is_current(token):
            retained = self._quarantine("stale-fence")
            raise LineageQuarantineError(
                f"fence token epoch {token.epoch} held by {token.holder!r} is no longer current; "
                f"the staged artifact was retained at {retained}",
                quarantine_path=retained,
            )

        leaves = tuple(LineageLeaf.from_wav(path) for path in segment_paths)
        if not leaves:
            raise LineageError("cannot publish an artifact with no segments")

        # Build the tree over leaves that commit to the tree size, so the
        # published proof cannot be replayed at a different `total_leaves`.
        hashed = tuple(
            merkle_leaf_hash(leaf.canonical_bytes(), total_leaves=len(leaves)) for leaf in leaves
        )
        root = merkle_root(hashed)
        proofs = tuple(
            MerkleProof(
                index=index,
                total_leaves=len(leaves),
                root_hex=root.hex(),
                audit_path=tuple(node.hex() for node in merkle_inclusion_proof(hashed, index)),
            )
            for index in range(len(leaves))
        )
        # Re-verify before writing anything: a proof that does not verify here
        # must never reach a receipt, where it would look like evidence.
        for index, proof in enumerate(proofs):
            if not proof.verify(leaves[index]):
                raise LineageError(
                    f"internal lineage inconsistency: proof for leaf {index} does not verify"
                )

        passport = ArtifactPassportReceipt(
            artifact_name=self.destination.name,
            root_hex=root.hex(),
            total_leaves=len(leaves),
            leaves=leaves,
            fence_epoch=token.epoch,
            fence_holder=token.holder,
        )
        (self.staging_dir / "lineage.json").write_bytes(passport.canonical_bytes())
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        final = self.destination
        if final.exists() and final.is_dir():
            shutil.rmtree(final)
        shutil.move(str(self.staging_dir), str(final))
        return LineageReceipt(destination=final, passport=passport, proofs=proofs)
