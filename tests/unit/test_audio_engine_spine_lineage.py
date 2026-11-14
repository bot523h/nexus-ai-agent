"""Staged audio publication and its RFC 6962 Merkle lineage proof.

The proof is the only part of a published artifact that lets a later reader
check *which* segments produced it, so the tests here are adversarial: they
build trees of every awkward size, then try to pass off a proof that belongs
somewhere else.

Pack evidence for ``creative.packs.audio``: the Merkle root and the per-segment
hashes are the lineage the pack publishes, so this suite measures the pack.

Sizes 1-33 and 64 matter specifically. A tree of a power-of-two size is the
case every naive Merkle implementation gets right; the odd sizes are where the
RFC 6962 split rule diverges, and where three separate defects were found and
fixed while writing this module.
"""

from __future__ import annotations

import json

import pytest

from nexus_ai_agent.creative.audio_engine.spine_lineage import (
    ArtifactPassportReceipt,
    CommandBusAuthorizer,
    FenceToken,
    LineageError,
    LineageLeaf,
    LineageQuarantineError,
    MerkleProof,
    PublicationFence,
    StagedAudioPublication,
    authorizer_from_env,
    merkle_inclusion_proof,
    merkle_leaf_hash,
    merkle_node_hash,
    merkle_root,
    verify_merkle_inclusion,
)

# --------------------------------------------------------------------------- #
# The domain separation is the security property
# --------------------------------------------------------------------------- #


def test_leaves_and_nodes_are_domain_separated() -> None:
    """RFC 6962 prefixes leaves with 0x00 and nodes with 0x01.

    Without it, a 32-byte leaf could be mistaken for an interior node and a
    second-preimage attack on the tree becomes possible.
    """
    payload = b"segment"
    import hashlib

    assert merkle_leaf_hash(payload) == hashlib.sha256(b"\x00" + payload).digest()
    left, right = b"\x11" * 32, b"\x22" * 32
    assert merkle_node_hash(left, right) == hashlib.sha256(b"\x01" + left + right).digest()
    # A leaf hash and a node hash over the same bytes must differ.
    assert merkle_leaf_hash(payload) != merkle_node_hash(payload, b"")


def test_the_empty_tree_has_the_empty_string_hash() -> None:
    import hashlib

    assert merkle_root([]) == hashlib.sha256(b"").digest()


def test_a_single_leaf_tree_is_its_own_root() -> None:
    leaf = merkle_leaf_hash(b"only")
    assert merkle_root([leaf]) == leaf


# --------------------------------------------------------------------------- #
# The property that matters: every leaf proves, at every tree size
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("total", list(range(1, 34)) + [64, 100])
def test_every_leaf_of_every_tree_size_proves_inclusion(total: int) -> None:
    """Including the odd sizes, and including both edges of the tree.

    Index ``total - 1`` is the rightmost leaf, whose path is shorter than the
    rest on every non-power-of-two tree. A proof-length check keyed only on the
    tree size rejects exactly those leaves.
    """
    leaves = [merkle_leaf_hash(bytes([index])) for index in range(total)]
    root = merkle_root(leaves)
    for index in range(total):
        proof = merkle_inclusion_proof(leaves, index)
        assert verify_merkle_inclusion(
            leaf=bytes([index]), index=index, total_leaves=total, proof=proof, root=root
        ), f"leaf {index} of {total} failed to prove"


@pytest.mark.parametrize("total", [2, 3, 5, 7, 8, 9, 16, 17, 33])
def test_a_proof_does_not_verify_at_a_different_index(total: int) -> None:
    """The index is part of the claim; a proof is not position-independent."""
    leaves = [merkle_leaf_hash(bytes([index])) for index in range(total)]
    root = merkle_root(leaves)
    for index in range(total):
        proof = merkle_inclusion_proof(leaves, index)
        other = (index + 1) % total
        if other == index:
            continue
        assert not verify_merkle_inclusion(
            leaf=bytes([index]), index=other, total_leaves=total, proof=proof, root=root
        )


@pytest.mark.parametrize("total", [2, 3, 5, 9, 17])
def test_a_tampered_sibling_breaks_the_proof(total: int) -> None:
    leaves = [merkle_leaf_hash(bytes([index])) for index in range(total)]
    root = merkle_root(leaves)
    proof = list(merkle_inclusion_proof(leaves, total // 2))
    proof[0] = bytes([0xAB]) * 32
    assert not verify_merkle_inclusion(
        leaf=bytes([total // 2]), index=total // 2, total_leaves=total, proof=proof, root=root
    )


def test_a_substituted_leaf_breaks_the_proof() -> None:
    leaves = [merkle_leaf_hash(bytes([index])) for index in range(5)]
    root = merkle_root(leaves)
    proof = merkle_inclusion_proof(leaves, 2)
    assert not verify_merkle_inclusion(
        leaf=b"a different segment", index=2, total_leaves=5, proof=proof, root=root
    )


def test_a_proof_from_another_tree_breaks_against_this_root() -> None:
    mine = merkle_root([merkle_leaf_hash(b"a"), merkle_leaf_hash(b"b")])
    theirs = [merkle_leaf_hash(b"c"), merkle_leaf_hash(b"d")]
    assert not verify_merkle_inclusion(
        leaf=b"a", index=0, total_leaves=2, proof=merkle_inclusion_proof(theirs, 0), root=mine
    )


@pytest.mark.parametrize("bad_total", [0, -1])
def test_a_nonsensical_tree_size_cannot_verify(bad_total: int) -> None:
    assert not verify_merkle_inclusion(
        leaf=b"x", index=0, total_leaves=bad_total, proof=(), root=b"\x00" * 32
    )


def test_an_out_of_range_index_cannot_verify() -> None:
    root = merkle_root([merkle_leaf_hash(b"a")])
    assert not verify_merkle_inclusion(leaf=b"a", index=1, total_leaves=1, proof=(), root=root)


def test_a_truncated_proof_cannot_verify() -> None:
    leaves = [merkle_leaf_hash(bytes([index])) for index in range(5)]
    root = merkle_root(leaves)
    proof = merkle_inclusion_proof(leaves, 1)
    assert not verify_merkle_inclusion(
        leaf=bytes([1]), index=1, total_leaves=5, proof=proof[:-1], root=root
    )


def test_an_extended_proof_cannot_verify() -> None:
    """Padding a proof must not be a way to change what it means."""
    leaves = [merkle_leaf_hash(bytes([index])) for index in range(4)]
    root = merkle_root(leaves)
    proof = merkle_inclusion_proof(leaves, 0) + (b"\x00" * 32,)
    assert not verify_merkle_inclusion(
        leaf=bytes([0]), index=0, total_leaves=4, proof=proof, root=root
    )


def test_proving_into_an_empty_tree_is_refused() -> None:
    with pytest.raises(LineageError, match="empty tree"):
        merkle_inclusion_proof([], 0)


def test_proving_an_index_outside_the_tree_is_refused() -> None:
    with pytest.raises(LineageError, match="outside a tree"):
        merkle_inclusion_proof([merkle_leaf_hash(b"a")], 1)


# --------------------------------------------------------------------------- #
# Leaves commit to two hashes
# --------------------------------------------------------------------------- #


def _leaf(name: str = "seg.wav", **overrides) -> LineageLeaf:
    payload = {
        "name": name,
        "pcm_sha256": "a" * 64,
        "wav_sha256": "b" * 64,
        "duration_us": 1_000_000,
    }
    payload.update(overrides)
    return LineageLeaf(**payload)


def test_a_leaf_commits_to_both_hashes() -> None:
    """The canonical bytes carry both, so a re-encode and a resample differ."""
    base = json.loads(_leaf().canonical_bytes())
    assert base["pcm_sha256"] == "a" * 64
    assert base["wav_sha256"] == "b" * 64
    # Changing only the PCM hash changes the committed bytes.
    assert _leaf().canonical_bytes() != _leaf(pcm_sha256="c" * 64).canonical_bytes()
    # So does changing only the file hash.
    assert _leaf().canonical_bytes() != _leaf(wav_sha256="d" * 64).canonical_bytes()


def test_the_canonical_bytes_are_key_order_independent() -> None:
    """JCS: two leaves with the same values commit to the same bytes."""
    payload = json.loads(_leaf().canonical_bytes())
    reordered = LineageLeaf(
        name=payload["name"],
        wav_sha256=payload["wav_sha256"],
        pcm_sha256=payload["pcm_sha256"],
        duration_us=payload["duration_us"],
    )
    assert reordered.canonical_bytes() == _leaf().canonical_bytes()


@pytest.mark.parametrize(
    "bad",
    [
        "a" * 63,  # too short
        "a" * 65,  # too long
        "A" * 64,  # uppercase hex
        "g" * 64,  # not hex
        "",
    ],
)
def test_a_malformed_digest_is_refused(bad: str) -> None:
    with pytest.raises(LineageError, match="64-character hex digest"):
        _leaf(pcm_sha256=bad)


def test_a_negative_duration_is_refused() -> None:
    with pytest.raises(LineageError, match="cannot be negative"):
        _leaf(duration_us=-1)


def test_a_leaf_from_a_missing_file_is_refused(tmp_path) -> None:
    with pytest.raises(LineageError, match="missing file"):
        LineageLeaf.from_wav(tmp_path / "nope.wav")


def test_a_leaf_from_a_real_file_hashes_its_bytes(tmp_path) -> None:
    import hashlib

    path = tmp_path / "seg.wav"
    path.write_bytes(b"RIFF....WAVE")
    leaf = LineageLeaf.from_wav(path, duration_us=500)
    expected = hashlib.sha256(b"RIFF....WAVE").hexdigest()
    assert leaf.pcm_sha256 == expected
    assert leaf.wav_sha256 == expected
    assert leaf.name == "seg.wav"
    assert leaf.duration_us == 500


# --------------------------------------------------------------------------- #
# MerkleProof value object
# --------------------------------------------------------------------------- #


def test_a_merkle_proof_round_trips_through_hex() -> None:
    leaves = [_leaf(f"seg{index}.wav").canonical_bytes() for index in range(6)]
    # Build with the size binding, matching what `MerkleProof.verify` requires.
    hashed = [merkle_leaf_hash(payload, total_leaves=6) for payload in leaves]
    root = merkle_root(hashed)
    index = 4
    proof = MerkleProof(
        index=index,
        total_leaves=len(leaves),
        root_hex=root.hex(),
        audit_path=tuple(node.hex() for node in merkle_inclusion_proof(hashed, index)),
    )
    assert proof.verify(_leaf(f"seg{index}.wav"))
    assert not proof.verify(_leaf("seg0.wav"))


# --------------------------------------------------------------------------- #
# The authorizer
# --------------------------------------------------------------------------- #


def test_production_without_an_authorizer_fails_closed() -> None:
    """An unconfigured authorizer in production is a deployment defect.

    The safe read is refusal: publishing without authorisation would be the
    silent version of that defect.
    """
    allowed, reason = CommandBusAuthorizer(production=True).admit("audio.publish", "x")
    assert not allowed
    assert "no authorizer configured" in reason


def test_development_without_an_authorizer_is_allowed() -> None:
    allowed, _reason = CommandBusAuthorizer(production=False).admit("audio.publish", "x")
    assert allowed


def test_a_rejected_action_reports_the_reason() -> None:
    def deny(action: str, subject: str) -> tuple[bool, str]:
        return False, "the bus rejected this"

    allowed, reason = CommandBusAuthorizer.from_callable(deny).admit("audio.publish", "x")
    assert not allowed
    assert reason == "the bus rejected this"


def test_the_production_decision_comes_from_the_environment() -> None:
    assert authorizer_from_env({}).production is False
    assert authorizer_from_env({"NEXUS_ENV": "production"}).production is True
    assert authorizer_from_env({"NEXUS_ENV": "PRODUCTION "}).production is True
    assert authorizer_from_env({"NEXUS_ENV": "staging"}).production is False


# --------------------------------------------------------------------------- #
# The fence
# --------------------------------------------------------------------------- #


def test_a_fence_epoch_increases_and_names_its_holder(tmp_path) -> None:
    fence = PublicationFence(tmp_path / "fence.json")
    first = fence.claim("render-a")
    second = fence.claim("render-b")
    assert second.epoch == first.epoch + 1
    assert second.holder == "render-b"
    assert fence.is_current(second)
    assert not fence.is_current(first)


def test_a_stale_epoch_is_not_current(tmp_path) -> None:
    fence = PublicationFence(tmp_path / "fence.json")
    token = fence.claim("render-a")
    fence.claim("render-b")
    assert not fence.is_current(token)


def test_an_unreadable_fence_refuses_rather_than_reporting_none(tmp_path) -> None:
    """Unreadable is not the same as absent: refusal is the safe read."""
    path = tmp_path / "fence.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(LineageError, match="unreadable"):
        PublicationFence(path).current()


def test_a_negative_epoch_is_refused() -> None:
    with pytest.raises(LineageError, match="cannot be negative"):
        FenceToken(epoch=-1, holder="x")


def test_a_fence_token_requires_a_holder() -> None:
    with pytest.raises(LineageError, match="requires a holder"):
        FenceToken(epoch=0, holder="")


def test_supersedes_compares_epochs() -> None:
    assert FenceToken(2, "b").supersedes(FenceToken(1, "a"))
    assert not FenceToken(1, "a").supersedes(FenceToken(2, "b"))
    assert not FenceToken(1, "a").supersedes(FenceToken(1, "b"))


# --------------------------------------------------------------------------- #
# Publication, end to end
# --------------------------------------------------------------------------- #


def _stage(directory, count: int):
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        path = directory / f"seg{index}.wav"
        path.write_bytes(b"RIFF" + bytes([index]) * 64)
        paths.append(path)
    return paths


def test_publication_returns_a_verifiable_receipt(tmp_path) -> None:
    staging = tmp_path / "staging"
    segments = _stage(staging, 5)
    fence = PublicationFence(tmp_path / "fence.json")
    token = fence.claim("test")
    publication = StagedAudioPublication(
        staging,
        tmp_path / "out" / "artifact",
        fence=fence,
        authorizer=CommandBusAuthorizer(production=False),
    )
    receipt = publication.publish(segments, token=token)

    assert receipt.destination.is_dir()
    assert receipt.passport.total_leaves == 5
    assert len(receipt.passport.leaves) == 5
    assert receipt.passport.fence_epoch == token.epoch
    # Every proof verifies against the published root.
    for leaf in receipt.passport.leaves:
        assert receipt.proof_for(leaf.name).verify(leaf)
    # The manifest on disk carries the same canonical bytes.
    manifest = json.loads((receipt.destination / "lineage.json").read_bytes())
    assert manifest["root_hex"] == receipt.passport.root_hex
    assert manifest["total_leaves"] == 5


def test_a_stale_fence_quarantines_and_retains_the_artifact(tmp_path) -> None:
    """A superseded render must not overwrite a newer one, and must not vanish.

    Deleting the loser would make "two renders raced" indistinguishable from
    "one render happened", so the bytes are retained under ``.quarantine/``.
    """
    staging = tmp_path / "staging"
    segments = _stage(staging, 2)
    fence = PublicationFence(tmp_path / "fence.json")
    stale = fence.claim("render-a")
    fence.claim("render-b")
    destination = tmp_path / "out" / "artifact"
    publication = StagedAudioPublication(
        staging,
        destination,
        fence=fence,
        authorizer=CommandBusAuthorizer(production=False),
    )
    with pytest.raises(LineageQuarantineError) as caught:
        publication.publish(segments, token=stale)
    quarantine_path = caught.value.quarantine_path
    assert quarantine_path.is_dir()
    assert quarantine_path.name.endswith(".stale-fence")
    # The staged bytes survived the refusal.
    assert (quarantine_path / "seg0.wav").is_file()
    # Nothing was published.
    assert not destination.exists()


def test_an_unauthorised_publication_leaves_the_destination_untouched(tmp_path) -> None:
    staging = tmp_path / "staging"
    segments = _stage(staging, 1)
    fence = PublicationFence(tmp_path / "fence.json")
    token = fence.claim("test")
    destination = tmp_path / "out" / "artifact"
    publication = StagedAudioPublication(
        staging,
        destination,
        fence=fence,
        authorizer=CommandBusAuthorizer(production=True),  # no admitter => refuse
    )
    with pytest.raises(LineageError, match="not authorised"):
        publication.publish(segments, token=token)
    assert not destination.exists()
    assert staging.is_dir()


def test_publishing_no_segments_is_refused(tmp_path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    fence = PublicationFence(tmp_path / "fence.json")
    token = fence.claim("test")
    publication = StagedAudioPublication(
        staging,
        tmp_path / "out",
        fence=fence,
        authorizer=CommandBusAuthorizer(production=False),
    )
    with pytest.raises(LineageError, match="no segments"):
        publication.publish([], token=token)


def test_asking_for_an_absent_proof_names_the_leaf(tmp_path) -> None:
    staging = tmp_path / "staging"
    segments = _stage(staging, 2)
    fence = PublicationFence(tmp_path / "fence.json")
    token = fence.claim("test")
    receipt = StagedAudioPublication(
        staging,
        tmp_path / "out" / "artifact",
        fence=fence,
        authorizer=CommandBusAuthorizer(production=False),
    ).publish(segments, token=token)
    with pytest.raises(LineageError, match="no lineage leaf named"):
        receipt.proof_for("absent.wav")


def test_the_passport_is_a_stable_value_object() -> None:
    leaves = (_leaf("a.wav"), _leaf("b.wav"))
    hashed = [merkle_leaf_hash(leaf.canonical_bytes()) for leaf in leaves]
    passport = ArtifactPassportReceipt(
        artifact_name="final",
        root_hex=merkle_root(hashed).hex(),
        total_leaves=2,
        leaves=leaves,
        fence_epoch=3,
        fence_holder="render",
    )
    # Rebuilding from the same inputs is byte-identical.
    rebuilt = ArtifactPassportReceipt(
        artifact_name="final",
        root_hex=merkle_root(hashed).hex(),
        total_leaves=2,
        leaves=leaves,
        fence_epoch=3,
        fence_holder="render",
    )
    assert passport.canonical_bytes() == rebuilt.canonical_bytes()
    payload = json.loads(passport.canonical_bytes())
    assert payload["fence_epoch"] == 3
    assert len(payload["leaves"]) == 2


def test_a_production_publication_needs_a_real_admission(tmp_path) -> None:
    """The end-to-end path through a configured production authorizer."""
    calls: list[tuple[str, str]] = []

    def admit(action: str, subject: str) -> tuple[bool, str]:
        calls.append((action, subject))
        return True, "admitted"

    staging = tmp_path / "staging"
    segments = _stage(staging, 3)
    fence = PublicationFence(tmp_path / "fence.json")
    token = fence.claim("test")
    receipt = StagedAudioPublication(
        staging,
        tmp_path / "out" / "artifact",
        fence=fence,
        authorizer=CommandBusAuthorizer.from_callable(admit, production=True),
    ).publish(segments, token=token)
    assert receipt.passport.total_leaves == 3
    assert calls and calls[0][0] == "audio.publish"


def test_quarantine_replaces_a_previous_quarantine_of_the_same_reason(tmp_path) -> None:
    """Two failed attempts at the same destination do not interleave evidence."""
    fence = PublicationFence(tmp_path / "fence.json")
    token = fence.claim("a")
    fence.claim("b")
    destination = tmp_path / "out" / "artifact"
    authorizer = CommandBusAuthorizer(production=False)
    for attempt in range(2):
        staging = tmp_path / f"staging{attempt}"
        _stage(staging, 1)
        with pytest.raises(LineageQuarantineError) as caught:
            StagedAudioPublication(
                staging, destination, fence=fence, authorizer=authorizer
            ).publish([staging / "seg0.wav"], token=token)
        assert caught.value.quarantine_path.is_dir()


# --------------------------------------------------------------------------- #
# The last few structural branches
# --------------------------------------------------------------------------- #


def test_splitting_a_tree_of_one_leaf_is_a_programming_error() -> None:
    """``_split`` is only called for width > 1; reaching it at 1 means the
    caller's recursion base case was lost, and it must not silently return 0
    (a zero-sized split would make the tree verify against nothing)."""
    from nexus_ai_agent.creative.audio_engine.spine_lineage import _split

    with pytest.raises(LineageError, match="cannot split a tree"):
        _split(1)
    with pytest.raises(LineageError, match="cannot split a tree"):
        _split(0)


def test_the_folding_loop_refuses_an_over_long_proof() -> None:
    """The length check normally catches this; the folding loop must also be
    safe if it is ever reached with a proof longer than the tree depth."""
    leaves = [merkle_leaf_hash(b"only")]
    root = merkle_root(leaves)
    # Two siblings for a one-leaf tree: the loop runs out of tree before the
    # proof runs out of nodes.
    assert not verify_merkle_inclusion(
        leaf=b"only", index=0, total_leaves=1, proof=(b"\x00" * 32,) * 2, root=root
    )


def test_a_fence_reports_its_cached_token_when_the_file_is_removed(tmp_path) -> None:
    """A claimed token stays valid in-process even if the file is unlinked."""
    path = tmp_path / "fence.json"
    fence = PublicationFence(path)
    token = fence.claim("render")
    path.unlink()
    assert fence.current() == token


def test_an_absent_fence_has_no_current_token(tmp_path) -> None:
    assert PublicationFence(tmp_path / "never-written.json").current() is None


def test_quarantine_handles_a_staging_directory_that_is_gone(tmp_path) -> None:
    """Quarantine must still produce a path when there is nothing to move.

    A concurrent process may have already removed the staging directory; that
    must not turn a stale-fence refusal into a crash.
    """
    fence = PublicationFence(tmp_path / "fence.json")
    stale = fence.claim("a")
    fence.claim("b")
    staging = tmp_path / "gone"
    staging.mkdir()
    publication = StagedAudioPublication(
        staging,
        tmp_path / "out",
        fence=fence,
        authorizer=CommandBusAuthorizer(production=False),
    )
    staging.rmdir()  # vanished before publication
    with pytest.raises(LineageQuarantineError) as caught:
        publication.publish([], token=stale)
    assert caught.value.quarantine_path.is_dir()


def test_publication_replaces_an_existing_destination_directory(tmp_path) -> None:
    staging = tmp_path / "staging"
    segments = _stage(staging, 2)
    fence = PublicationFence(tmp_path / "fence.json")
    token = fence.claim("test")
    destination = tmp_path / "out" / "artifact"
    destination.mkdir(parents=True)
    (destination / "stale.txt").write_text("previous render", encoding="utf-8")
    StagedAudioPublication(
        staging,
        destination,
        fence=fence,
        authorizer=CommandBusAuthorizer(production=False),
    ).publish(segments, token=token)
    # The old contents are gone: publication replaces, it does not merge.
    assert not (destination / "stale.txt").exists()
    assert (destination / "seg0.wav").is_file()


def test_a_passport_tampered_after_the_fact_no_longer_verifies(tmp_path) -> None:
    """The receipt is checkable *later*: that is the point of publishing a root."""
    staging = tmp_path / "staging"
    segments = _stage(staging, 4)
    fence = PublicationFence(tmp_path / "fence.json")
    token = fence.claim("test")
    receipt = StagedAudioPublication(
        staging,
        tmp_path / "out" / "artifact",
        fence=fence,
        authorizer=CommandBusAuthorizer(production=False),
    ).publish(segments, token=token)

    # A verifier holding only the passport can re-derive the root from the
    # leaves and prove each one; nothing needs the original ordering.
    total = receipt.passport.total_leaves
    hashed = [
        merkle_leaf_hash(leaf.canonical_bytes(), total_leaves=total)
        for leaf in receipt.passport.leaves
    ]
    assert merkle_root(hashed).hex() == receipt.passport.root_hex

    # Replace one leaf's file hash: the root no longer matches its own leaves.
    tampered = [
        LineageLeaf(
            name=leaf.name,
            pcm_sha256=leaf.pcm_sha256,
            wav_sha256="f" * 64,
            duration_us=leaf.duration_us,
        )
        for leaf in receipt.passport.leaves
    ]
    tampered_root = merkle_root(
        [merkle_leaf_hash(leaf.canonical_bytes(), total_leaves=total) for leaf in tampered]
    )
    assert tampered_root.hex() != receipt.passport.root_hex


# --------------------------------------------------------------------------- #
# Size binding: the malleability case
# --------------------------------------------------------------------------- #


def test_a_proof_cannot_be_replayed_at_a_smaller_tree_size() -> None:
    """An audit path of length k is valid for *every* size whose path is also k.

    This was a live defect in this module. For an 8-leaf tree, leaf 0's path has
    3 nodes — and so does leaf 0's path in 5-, 6- and 7-leaf trees. Folding the
    8-leaf path reproduces the same root at all four sizes, so the claim "this
    leaf is index 0 of an N-leaf tree" had N unpinned: a shorter history could
    be passed off as a longer one.

    The fix is to commit the tree size into the leaf domain on both the build
    and the verify side.
    """
    total = 8
    leaves = [merkle_leaf_hash(bytes([index]), total_leaves=total) for index in range(total)]
    root = merkle_root(leaves)
    proof = merkle_inclusion_proof(leaves, 0)
    assert len(proof) == 3

    assert verify_merkle_inclusion(
        leaf=bytes([0]),
        index=0,
        total_leaves=total,
        proof=proof,
        root=root,
        bind_tree_size=True,
    )
    for smaller in (5, 6, 7):
        assert not verify_merkle_inclusion(
            leaf=bytes([0]),
            index=0,
            total_leaves=smaller,
            proof=proof,
            root=root,
            bind_tree_size=True,
        ), f"an {total}-leaf proof verified at total_leaves={smaller}"


def test_the_unbound_primitive_stays_plain_rfc_6962() -> None:
    """Without the flag the leaf commitment is exactly SHA-256(0x00 || leaf)."""
    import hashlib

    assert merkle_leaf_hash(b"payload") == hashlib.sha256(b"\x00payload").digest()
    bound = merkle_leaf_hash(b"payload", total_leaves=4)
    assert bound != merkle_leaf_hash(b"payload")
    assert bound == merkle_leaf_hash(b"payload", total_leaves=4)
    assert bound != merkle_leaf_hash(b"payload", total_leaves=5)


@pytest.mark.parametrize("bad", [0, -1])
def test_binding_to_a_nonsensical_size_is_refused(bad: int) -> None:
    with pytest.raises(LineageError, match="must be positive"):
        merkle_leaf_hash(b"payload", total_leaves=bad)


def test_a_published_passport_is_bound_to_its_own_size(tmp_path) -> None:
    """The end-to-end path uses the binding: a published proof is not replayable."""
    staging = tmp_path / "staging"
    segments = _stage(staging, 3)
    fence = PublicationFence(tmp_path / "fence.json")
    token = fence.claim("test")
    receipt = StagedAudioPublication(
        staging,
        tmp_path / "out" / "artifact",
        fence=fence,
        authorizer=CommandBusAuthorizer(production=False),
    ).publish(segments, token=token)
    leaf = receipt.passport.leaves[0]
    proof = receipt.proof_for(leaf.name)
    assert proof.verify(leaf)
    # Replaying the same audit path at a different declared size must fail.
    forged = MerkleProof(
        index=proof.index,
        total_leaves=2,
        root_hex=proof.root_hex,
        audit_path=proof.audit_path,
    )
    assert not forged.verify(leaf)
