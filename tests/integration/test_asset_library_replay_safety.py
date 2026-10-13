"""Replay of an interrupted ingest must never destroy the recoverable original.

When an ingest crashes mid-flight, its staged bytes are the *only* copy of the
caller's upload.  A retry with the same idempotency key presents a second stream.
That stream is untrusted: it may be empty, truncated, different, or oversized.
The contract pinned here:

* the original staged bytes are never overwritten or removed by a replay;
* a replay whose measured identity differs from the original is a typed
  ``IdempotencyConflict`` and the original stays recoverable;
* a replay that matches the original completes the ingest exactly once;
* an empty replay is a conflict, never a way to wipe the original;
* every exit path leaves no stray replay file behind.

Crashes are injected at exact boundaries via the service fault hook.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.creative.assets.authz import GrantTableAuthorizer, StudioAuthorizationSeam
from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.assets.errors import IdempotencyConflict
from nexus_ai_agent.creative.assets.models import IngestRequest, IngestState
from nexus_ai_agent.creative.assets.service import AssetLibraryService
from nexus_ai_agent.storage import models as orm
from nexus_ai_agent.storage.db import get_session

ORIGINAL = b"\x89PNG\r\n\x1a\n" + bytes(range(96))
DIFFERENT = b"\x89PNG\r\n\x1a\n" + bytes(range(96, 192))
PROJECT = "project-alpha"
ACTOR = "user-1"
KEY = "replay-key"


def _factory(db_path: Path):
    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with get_session(str(db_path)) as session:
            yield session

    return factory


def _service(tmp_path: Path, *, fault: str | None = None, max_bytes: int = 1 << 20):
    def hook(point: str) -> None:
        if fault is not None and point == fault:
            raise RuntimeError(f"injected crash at {point}")

    return AssetLibraryService(
        blob_store=LocalContentAddressedBlobStore(tmp_path / "blobs"),
        authorizer=StudioAuthorizationSeam(
            GrantTableAuthorizer({(ACTOR, PROJECT): frozenset({"project:read", "project:write"})})
        ),
        session_factory=_factory(tmp_path / "library.sqlite"),
        max_bytes=max_bytes,
        fault_hook=hook,
    )


def _ingest(service: AssetLibraryService, data: bytes, key: str = KEY):
    return service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT,
        request=IngestRequest(idempotency_key=key, original_filename="pic.png"),
        stream=io.BytesIO(data),
    )


async def _crash_at(tmp_path: Path, point: str) -> None:
    with pytest.raises(RuntimeError, match="injected crash"):
        await _ingest(_service(tmp_path, fault=point), ORIGINAL)


async def _only_ingest(tmp_path: Path) -> orm.AssetIngest:
    async with get_session(str(tmp_path / "library.sqlite")) as session:
        return (await session.execute(select(orm.AssetIngest))).scalars().one()


async def _counts(tmp_path: Path) -> tuple[int, int, int]:
    async with get_session(str(tmp_path / "library.sqlite")) as session:
        out = []
        for model in (orm.Asset, orm.AssetVersion, orm.ProjectAssetLink):
            out.append(
                (await session.execute(select(func.count()).select_from(model))).scalar_one()
            )
        return (out[0], out[1], out[2])


def _staging_files(tmp_path: Path) -> list[Path]:
    root = tmp_path / "blobs" / "staging"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _staged_bytes(tmp_path: Path, row: orm.AssetIngest) -> bytes | None:
    assert row.staging_key
    path = tmp_path / "blobs" / row.staging_key
    return path.read_bytes() if path.is_file() else None


# ---- State B: crashed after staging, before Tx2 (digest not yet recorded) ----


@pytest.mark.asyncio
async def test_empty_replay_does_not_wipe_the_only_staged_copy(tmp_path: Path) -> None:
    await _crash_at(tmp_path, "after_stage")
    row = await _only_ingest(tmp_path)
    assert row.content_sha256 is None
    assert _staged_bytes(tmp_path, row) == ORIGINAL

    with pytest.raises(IdempotencyConflict):
        await _ingest(_service(tmp_path), b"")

    assert _staged_bytes(tmp_path, row) == ORIGINAL  # the original survived
    assert await _counts(tmp_path) == (0, 0, 0)  # nothing was published or invented


@pytest.mark.asyncio
async def test_different_replay_before_tx2_is_a_conflict_not_a_silent_swap(tmp_path: Path) -> None:
    await _crash_at(tmp_path, "after_stage")
    row = await _only_ingest(tmp_path)

    with pytest.raises(IdempotencyConflict):
        await _ingest(_service(tmp_path), DIFFERENT)

    assert _staged_bytes(tmp_path, row) == ORIGINAL
    assert await _counts(tmp_path) == (0, 0, 0)


@pytest.mark.asyncio
async def test_matching_replay_before_tx2_completes_the_original_exactly_once(
    tmp_path: Path,
) -> None:
    await _crash_at(tmp_path, "after_stage")
    result = await _ingest(_service(tmp_path), ORIGINAL)
    assert result.state is IngestState.READY
    assert result.reused_result is True
    assert await _counts(tmp_path) == (1, 1, 1)
    assert _staging_files(tmp_path) == []  # no replay residue


@pytest.mark.asyncio
async def test_conflicting_replay_leaves_the_original_recoverable_afterwards(
    tmp_path: Path,
) -> None:
    """After rejecting a bad replay, normal crash recovery still completes the original."""
    await _crash_at(tmp_path, "after_stage")
    row = await _only_ingest(tmp_path)
    with pytest.raises(IdempotencyConflict):
        await _ingest(_service(tmp_path), DIFFERENT)

    report = await _service(tmp_path).recover_ingest(row.ingest_id)
    assert report.state is IngestState.READY
    assert await _counts(tmp_path) == (1, 1, 1)


# ---- State C: crashed after the digest was recorded (Tx2) ----


@pytest.mark.asyncio
async def test_different_replay_after_tx2_does_not_delete_the_original(tmp_path: Path) -> None:
    await _crash_at(tmp_path, "after_publish_before_commit")
    row = await _only_ingest(tmp_path)
    assert row.content_sha256 is not None

    with pytest.raises(IdempotencyConflict):
        await _ingest(_service(tmp_path), DIFFERENT)

    # The original must still be completable: the replay must not have removed it.
    report = await _service(tmp_path).recover_ingest(row.ingest_id)
    assert report.state is IngestState.READY
    assert await _counts(tmp_path) == (1, 1, 1)


@pytest.mark.asyncio
async def test_empty_replay_after_tx2_is_a_conflict_and_original_completes(
    tmp_path: Path,
) -> None:
    await _crash_at(tmp_path, "after_publish_before_commit")
    row = await _only_ingest(tmp_path)
    with pytest.raises(IdempotencyConflict):
        await _ingest(_service(tmp_path), b"")
    report = await _service(tmp_path).recover_ingest(row.ingest_id)
    assert report.state is IngestState.READY
    assert await _counts(tmp_path) == (1, 1, 1)


@pytest.mark.asyncio
async def test_matching_replay_after_tx2_completes_exactly_once(tmp_path: Path) -> None:
    await _crash_at(tmp_path, "after_publish_before_commit")
    result = await _ingest(_service(tmp_path), ORIGINAL)
    assert result.state is IngestState.READY
    assert await _counts(tmp_path) == (1, 1, 1)
    assert _staging_files(tmp_path) == []


# ---- hostile / degenerate replay streams ----


@pytest.mark.asyncio
async def test_oversize_replay_is_a_conflict_and_leaves_no_residue(tmp_path: Path) -> None:
    await _crash_at(tmp_path, "after_stage")
    row = await _only_ingest(tmp_path)
    small = _service(tmp_path, max_bytes=len(ORIGINAL))
    with pytest.raises(IdempotencyConflict):
        await _ingest(small, ORIGINAL + b"\x00" * 64)
    assert _staged_bytes(tmp_path, row) == ORIGINAL
    assert len(_staging_files(tmp_path)) == 1  # only the original


@pytest.mark.asyncio
async def test_truncated_replay_is_a_conflict(tmp_path: Path) -> None:
    await _crash_at(tmp_path, "after_stage")
    row = await _only_ingest(tmp_path)
    with pytest.raises(IdempotencyConflict):
        await _ingest(_service(tmp_path), ORIGINAL[: len(ORIGINAL) // 2])
    assert _staged_bytes(tmp_path, row) == ORIGINAL


@pytest.mark.asyncio
async def test_replay_never_leaves_extra_staging_files(tmp_path: Path) -> None:
    await _crash_at(tmp_path, "after_stage")
    before = {p.name for p in _staging_files(tmp_path)}
    for payload in (b"", DIFFERENT, ORIGINAL[:10]):
        with pytest.raises(IdempotencyConflict):
            await _ingest(_service(tmp_path), payload)
    assert {p.name for p in _staging_files(tmp_path)} == before


# ---- the original staged file is gone (nothing left to protect or compare) ----


def _lose_the_original(tmp_path: Path, row: orm.AssetIngest) -> None:
    assert row.staging_key
    (tmp_path / "blobs" / row.staging_key).unlink()


@pytest.mark.asyncio
async def test_empty_replay_is_refused_even_when_the_original_is_lost(tmp_path: Path) -> None:
    """With no original to compare against, an empty body must not be adopted."""
    await _crash_at(tmp_path, "after_stage")
    row = await _only_ingest(tmp_path)
    _lose_the_original(tmp_path, row)

    with pytest.raises(IdempotencyConflict):
        await _ingest(_service(tmp_path), b"")

    assert await _counts(tmp_path) == (0, 0, 0)
    assert _staging_files(tmp_path) == []


@pytest.mark.asyncio
async def test_real_replay_adopts_the_lost_original_and_completes_once(tmp_path: Path) -> None:
    """If the only copy is gone, a valid replay is the new staged original."""
    await _crash_at(tmp_path, "after_stage")
    row = await _only_ingest(tmp_path)
    _lose_the_original(tmp_path, row)

    result = await _ingest(_service(tmp_path), ORIGINAL)

    assert result.state is IngestState.READY
    assert await _counts(tmp_path) == (1, 1, 1)
    assert _staging_files(tmp_path) == []  # published, no residue


# ---- the MIME allowlist is enforced on the real ingest and recovery paths ----

HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00\x00\x00\x00mif1heic" + bytes(range(64))
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00\x00\x00\x00isommp42" + bytes(range(64))


@pytest.mark.asyncio
async def test_a_heic_upload_is_refused_by_the_real_ingest_path(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.assets.errors import UnsupportedMediaType

    with pytest.raises(UnsupportedMediaType):
        await _ingest(_service(tmp_path), HEIC, key="heic")

    assert await _counts(tmp_path) == (0, 0, 0)
    assert _staging_files(tmp_path) == []
    row = await _only_ingest(tmp_path)
    assert row.state == IngestState.FAILED.value
    assert row.error_code == "unsupported_media_type"


@pytest.mark.asyncio
async def test_a_real_mp4_is_accepted_as_video_by_the_real_ingest_path(tmp_path: Path) -> None:
    result = await _ingest(_service(tmp_path), MP4, key="mp4")
    assert result.state is IngestState.READY
    assert result.media_type == "video/mp4"
    assert await _counts(tmp_path) == (1, 1, 1)


@pytest.mark.asyncio
async def test_crash_recovery_also_refuses_a_heic_that_was_staged(tmp_path: Path) -> None:
    """Recovery re-classifies staged bytes (Tx2 never ran); the allowlist applies there too."""
    with pytest.raises(RuntimeError, match="injected crash"):
        await _ingest(_service(tmp_path, fault="after_stage"), HEIC, key="heic-crash")
    row = await _only_ingest(tmp_path)
    assert row.media_type is None  # Tx2 never classified it

    report = await _service(tmp_path).recover_ingest(row.ingest_id)

    assert report.state is IngestState.FAILED
    assert await _counts(tmp_path) == (0, 0, 0)


# ---- concurrent replays of the same interrupted ingest ----


@pytest.mark.asyncio
async def test_concurrent_matching_replays_complete_the_ingest_at_most_once(
    tmp_path: Path,
) -> None:
    """Two simultaneous retries of one interrupted ingest must not duplicate the asset."""
    import asyncio

    await _crash_at(tmp_path, "after_stage")

    outcomes = await asyncio.gather(
        _ingest(_service(tmp_path), ORIGINAL),
        _ingest(_service(tmp_path), ORIGINAL),
        return_exceptions=True,
    )

    assert await _counts(tmp_path) == (1, 1, 1)  # exactly one asset, whatever the interleaving
    successes = [o for o in outcomes if not isinstance(o, BaseException)]
    assert successes, f"at least one replay must complete: {outcomes!r}"
    assert len({s.asset_id for s in successes}) == 1  # every success names the same asset
    assert _staging_files(tmp_path) == []


@pytest.mark.asyncio
async def test_concurrent_matching_and_conflicting_replays_cannot_corrupt_the_original(
    tmp_path: Path,
) -> None:
    import asyncio

    await _crash_at(tmp_path, "after_stage")

    outcomes = await asyncio.gather(
        _ingest(_service(tmp_path), ORIGINAL),
        _ingest(_service(tmp_path), DIFFERENT),
        _ingest(_service(tmp_path), b""),
        return_exceptions=True,
    )

    # Whatever order they ran in, only the original's content may ever become the asset.
    assert await _counts(tmp_path) == (1, 1, 1)
    async with get_session(str(tmp_path / "library.sqlite")) as session:
        version = (await session.execute(select(orm.AssetVersion))).scalars().one()
    import hashlib

    assert version.content_sha256 == hashlib.sha256(ORIGINAL).hexdigest()
    assert isinstance(outcomes[1], IdempotencyConflict) or isinstance(outcomes[1], BaseException)
    assert isinstance(outcomes[2], BaseException)


@pytest.mark.asyncio
@pytest.mark.parametrize("round_", range(8))
async def test_two_different_replays_race_only_the_original_content_survives(
    tmp_path: Path, round_: int
) -> None:
    """A matching replay and a differing replay race on one interrupted ingest.

    Whatever the interleaving: exactly one asset exists, it holds the ORIGINAL
    bytes, the differing replay is refused with a typed conflict (or, if it lost
    the race to completion, also a typed conflict - never a success), and no
    staging file is left owned by nobody.
    """
    import asyncio
    import hashlib

    await _crash_at(tmp_path, "after_stage")

    matching, differing = await asyncio.gather(
        _ingest(_service(tmp_path), ORIGINAL),
        _ingest(_service(tmp_path), DIFFERENT),
        return_exceptions=True,
    )

    assert not isinstance(matching, BaseException), matching
    assert isinstance(differing, IdempotencyConflict), differing
    assert await _counts(tmp_path) == (1, 1, 1)
    async with get_session(str(tmp_path / "library.sqlite")) as session:
        version = (await session.execute(select(orm.AssetVersion))).scalars().one()
        ingest = (await session.execute(select(orm.AssetIngest))).scalars().one()
    assert version.content_sha256 == hashlib.sha256(ORIGINAL).hexdigest()
    assert ingest.state == "READY" and ingest.staging_key is None
    assert _staging_files(tmp_path) == []
