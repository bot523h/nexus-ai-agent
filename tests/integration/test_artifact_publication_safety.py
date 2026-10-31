"""task-283 — adversarial artifact-publication safety.

Each test maps to one of the risks enumerated in the mission (A–G) and asserts
a *fail-closed* invariant rather than "it worked once":

A. two different jobs publish identical file names,
B. two attempts compete for one final destination,
C. concurrent publication/backup overwrite each other's ``.prev``,
D. retract sees a missing / malformed / unreadable ownership sidecar,
E. an older attempt retracts a newer attempt's artifact,
F. workspace membership by string prefix instead of resolved containment,
G. crash between backup creation, publication and durable completion.

The old design identified an artifact's owner by *attempt number alone* in a
flat ``published/`` namespace, so two different jobs that emitted
``render.mp4`` shared both the destination and its ``.prev`` backup, and a
missing or unparsable sidecar was treated as permission to delete.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_ai_agent.creative.render_jobs import (
    _creative_render_staging,
    _publication_layout,
    _resolved_within,
    build_creative_render_publication,
)


def _publication(tmp_path: Path):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    return artifact_root, build_creative_render_publication(artifact_root)


def _workspace(tmp_path: Path, name: str) -> Path:
    workspace = tmp_path / name
    workspace.mkdir()
    return workspace


def _result(workspace: Path, name: str, job_id: str, attempt: int) -> dict[str, object]:
    return {
        "artifact_path": str(workspace / name),
        "workspace_dir": str(workspace),
        "_queue_job_id": job_id,
        "_queue_attempt": attempt,
    }


# ── A. two different jobs, the same file name ───────────────────────────────


async def test_two_jobs_with_identical_filenames_do_not_collide(tmp_path: Path) -> None:
    artifact_root, publication = _publication(tmp_path)
    ws_a, ws_b = _workspace(tmp_path, "ws_a"), _workspace(tmp_path, "ws_b")
    (ws_a / "render.mp4").write_bytes(b"job A bytes")
    (ws_b / "render.mp4").write_bytes(b"job B bytes")

    patch_a = publication.publish({}, _result(ws_a, "render.mp4", "job-a", 1))
    patch_b = publication.publish({}, _result(ws_b, "render.mp4", "job-b", 1))

    published_a, published_b = Path(patch_a["artifact_path"]), Path(patch_b["artifact_path"])
    assert published_a != published_b, "one job must never overwrite another job's artifact"
    assert published_a.read_bytes() == b"job A bytes"
    assert published_b.read_bytes() == b"job B bytes"


async def test_one_jobs_publication_cannot_clobber_anothers_backup(tmp_path: Path) -> None:
    """Risk C: the ``.prev`` backup must be per-job, not per-filename."""
    _, publication = _publication(tmp_path)
    ws_a, ws_b = _workspace(tmp_path, "ws_a"), _workspace(tmp_path, "ws_b")
    artifact_a, artifact_b = ws_a / "render.mp4", ws_b / "render.mp4"

    artifact_a.write_bytes(b"A v1")
    publication.publish({}, _result(ws_a, "render.mp4", "job-a", 1))
    artifact_a.write_bytes(b"A v2")
    patch_a = publication.publish({}, _result(ws_a, "render.mp4", "job-a", 2))
    published_a = Path(patch_a["artifact_path"])

    artifact_b.write_bytes(b"B v1")
    publication.publish({}, _result(ws_b, "render.mp4", "job-b", 1))
    artifact_b.write_bytes(b"B v2")
    published_b = Path(
        publication.publish({}, _result(ws_b, "render.mp4", "job-b", 2))["artifact_path"]
    )

    backup_a = published_a.parent / ".render.mp4.prev"
    assert backup_a.read_bytes() == b"A v1", "job B's publication overwrote job A's rollback bytes"

    backup_b = published_b.parent / ".render.mp4.prev"
    assert backup_b.read_bytes() == b"B v1"
    assert backup_a != backup_b, "each job owns a distinct backup slot"
    assert backup_a.read_bytes() == b"A v1", "job B's second publication left A's backup alone"


# ── B / E. attempt competition ──────────────────────────────────────────────


async def test_older_attempt_cannot_retract_a_newer_artifact(tmp_path: Path) -> None:
    _, publication = _publication(tmp_path)
    workspace = _workspace(tmp_path, "ws")
    artifact = workspace / "render.mp4"

    artifact.write_bytes(b"attempt 1")
    publication.publish({}, _result(workspace, "render.mp4", "job-x", 1))
    artifact.write_bytes(b"attempt 2")
    patch2 = publication.publish({}, _result(workspace, "render.mp4", "job-x", 2))
    published = Path(patch2["artifact_path"])
    assert published.read_bytes() == b"attempt 2"

    # Attempt 1 tries to retract the post-publish destination.  It must report
    # the *published* path (as the queue's retract_context does), otherwise it is
    # classified pre-publication and the attempt fence is never reached.
    publication.retract(
        {},
        {
            "artifact_path": str(published),
            "workspace_dir": str(workspace),
            "_queue_job_id": "job-x",
            "_queue_attempt": 1,
        },
    )
    assert published.read_bytes() == b"attempt 2", "a stale attempt retracted a live artifact"

    # The owning attempt can still roll back.
    post = {
        "artifact_path": str(published),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-x",
        "_queue_attempt": 2,
    }
    publication.retract({}, post)
    assert published.read_bytes() == b"attempt 1", "owning attempt must restore the backup"


# ── D. fail closed on unprovable ownership ─────────────────────────────────


def _sidecar_for(artifact_root: Path, workspace: Path, job_id: str, name: str) -> Path:
    staging = _creative_render_staging(artifact_root, workspace, workspace / name, job_id=job_id)
    _, _, _, sidecar = _publication_layout(staging, name)
    return sidecar


async def test_retract_fails_closed_when_sidecar_is_missing(tmp_path: Path) -> None:
    artifact_root, publication = _publication(tmp_path)
    workspace = _workspace(tmp_path, "ws")
    (workspace / "render.mp4").write_bytes(b"v1")
    patch = publication.publish({}, _result(workspace, "render.mp4", "job-d", 1))
    published = Path(patch["artifact_path"])

    _sidecar_for(artifact_root, workspace, "job-d", "render.mp4").unlink()

    post = {
        "artifact_path": str(published),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-d",
        "_queue_attempt": 1,
    }
    publication.retract({}, post)
    assert published.exists(), "a missing sidecar is the absence of proof, not permission to delete"


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "{}",
        '{"schema": 99, "job_id": "job-d", "attempt": "1"}',
        '{"schema": 1, "attempt": "1"}',
        "[1, 2, 3]",
        "",
    ],
    ids=["garbage", "empty-object", "wrong-schema", "no-job-id", "not-an-object", "empty-file"],
)
async def test_retract_fails_closed_on_malformed_sidecar(tmp_path: Path, content: str) -> None:
    artifact_root, publication = _publication(tmp_path)
    workspace = _workspace(tmp_path, "ws")
    (workspace / "render.mp4").write_bytes(b"v1")
    patch = publication.publish({}, _result(workspace, "render.mp4", "job-d", 1))
    published = Path(patch["artifact_path"])

    _sidecar_for(artifact_root, workspace, "job-d", "render.mp4").write_text(content)

    post = {
        "artifact_path": str(published),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-d",
        "_queue_attempt": 1,
    }
    publication.retract({}, post)
    assert published.exists(), f"malformed sidecar {content!r} must fail closed"


async def test_retract_fails_closed_when_sidecar_is_unreadable(tmp_path: Path) -> None:
    artifact_root, publication = _publication(tmp_path)
    workspace = _workspace(tmp_path, "ws")
    (workspace / "render.mp4").write_bytes(b"v1")
    patch = publication.publish({}, _result(workspace, "render.mp4", "job-d", 1))
    published = Path(patch["artifact_path"])

    sidecar = _sidecar_for(artifact_root, workspace, "job-d", "render.mp4")
    sidecar.unlink()
    sidecar.mkdir()  # reading a directory raises OSError

    post = {
        "artifact_path": str(published),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-d",
        "_queue_attempt": 1,
    }
    publication.retract({}, post)
    assert published.exists(), "an unreadable sidecar must fail closed"


async def test_retract_without_attempt_identity_cannot_mutate_published_output(
    tmp_path: Path,
) -> None:
    """A job-scoped call may not retract an attempt-scoped publication."""
    _, publication = _publication(tmp_path)
    workspace = _workspace(tmp_path, "ws")
    (workspace / "render.mp4").write_bytes(b"v1")
    patch = publication.publish({}, _result(workspace, "render.mp4", "job-e", 1))
    published = Path(patch["artifact_path"])

    publication.retract(
        {},
        {
            "artifact_path": str(published),
            "workspace_dir": str(workspace),
            "_queue_job_id": "job-e",
        },
    )
    assert published.exists(), "identity without an attempt must not match attempt 1"


async def test_sidecar_records_complete_identity(tmp_path: Path) -> None:
    artifact_root, publication = _publication(tmp_path)
    workspace = _workspace(tmp_path, "ws")
    (workspace / "render.mp4").write_bytes(b"v1")
    publication.publish({}, _result(workspace, "render.mp4", "job-ident", 7))

    sidecar = _sidecar_for(artifact_root, workspace, "job-ident", "render.mp4")
    document = json.loads(sidecar.read_text())
    assert document == {"schema": 1, "job_id": "job-ident", "attempt": "7"}


async def test_retract_cannot_cross_job_boundaries(tmp_path: Path) -> None:
    """Attempt numbers are not identities: a different job must never retract.

    The old sidecar recorded only ``{"attempt": N}``, so job A attempt 1 and job
    B attempt 1 were indistinguishable and one job could delete the other's
    artifact.
    """
    _, publication = _publication(tmp_path)
    ws_a, ws_b = _workspace(tmp_path, "ws_a"), _workspace(tmp_path, "ws_b")
    (ws_a / "render.mp4").write_bytes(b"job A bytes")
    patch = publication.publish({}, _result(ws_a, "render.mp4", "job-a", 1))
    published_a = Path(patch["artifact_path"])

    (ws_b / "render.mp4").write_bytes(b"job B bytes")

    # Job B, same attempt number, pointing at job A's published artifact.
    publication.retract(
        {},
        {
            "artifact_path": str(published_a),
            "workspace_dir": str(ws_b),
            "_queue_job_id": "job-b",
            "_queue_attempt": 1,
        },
    )
    assert published_a.read_bytes() == b"job A bytes", "job B retracted job A's artifact"


# ── F. containment, not string prefix ──────────────────────────────────────


def test_resolved_containment_rejects_sibling_prefix(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    evil = tmp_path / "ws-evil"
    evil.mkdir()

    assert _resolved_within(workspace / "a.mp4", workspace) is True
    # The old ``str(child).startswith(str(parent))`` check said True here.
    assert _resolved_within(evil / "a.mp4", workspace) is False, (
        "a sibling directory sharing a string prefix is not inside the workspace"
    )
    assert str(evil / "a.mp4").startswith(str(workspace)), (
        "guard precondition: the sibling really does share a string prefix"
    )


async def test_prefix_sibling_is_treated_as_published_output(tmp_path: Path) -> None:
    """A pre/post-publication decision must not be fooled by the prefix.

    ``ws-evil`` is *not* the workspace, so an artifact there is post-publication
    output and a refusal must roll it back rather than being skipped as a
    pre-publication temp.
    """
    artifact_root, publication = _publication(tmp_path)
    workspace = _workspace(tmp_path, "ws")
    evil = _workspace(tmp_path, "ws-evil")

    (workspace / "render.mp4").write_bytes(b"v1")
    patch = publication.publish({}, _result(workspace, "render.mp4", "job-f", 1))
    published = Path(patch["artifact_path"])

    # A refusal whose reported artifact_path sits in the prefix-sharing sibling.
    impostor = evil / "render.mp4"
    impostor.write_bytes(b"impostor")
    publication.retract(
        {},
        {
            "artifact_path": str(impostor),
            "workspace_dir": str(workspace),
            "_queue_job_id": "job-f",
            "_queue_attempt": 1,
        },
    )
    # With the old prefix check this path counted as "inside the workspace", so
    # the refusal was skipped as a pre-publication temp.  Resolved containment
    # says otherwise: it is post-publication output, so the owning attempt's
    # publication is rolled back (there is no previous version, so the refused
    # bytes are removed) while the sibling path is never touched.
    assert impostor.exists(), "retract must never delete a path outside its own namespace"
    assert impostor.read_bytes() == b"impostor"
    assert not published.exists(), "the prefix-sharing sibling was misclassified as pre-publish"


# ── G. crash between backup creation and publication ───────────────────────


async def test_crash_after_backup_before_publish_is_recoverable(tmp_path: Path) -> None:
    """Step 3→4 crash: destination gone, previous bytes stranded in ``.prev``."""
    artifact_root, publication = _publication(tmp_path)
    workspace = _workspace(tmp_path, "ws")
    artifact = workspace / "render.mp4"

    artifact.write_bytes(b"good v1")
    patch = publication.publish({}, _result(workspace, "render.mp4", "job-g", 1))
    published = Path(patch["artifact_path"])
    assert published.read_bytes() == b"good v1"

    # Simulate the crash: the previous artifact was already renamed to .prev and
    # the new bytes were staged, but the publish rename never happened.
    staging = _creative_render_staging(
        artifact_root, workspace, artifact, job_id="job-g", attempt_id="2"
    )
    staging.prepare()
    artifact.write_bytes(b"bad v2")
    staging.write("render.mp4", b"bad v2")

    _, published_path, backup_path, sidecar_path = _publication_layout(staging, "render.mp4")
    published_path.rename(backup_path)
    assert not published_path.exists()
    assert backup_path.read_bytes() == b"good v1"
    # Ownership evidence was written before the destructive step, so it exists.
    sidecar_path.write_text(json.dumps({"schema": 1, "job_id": "job-g", "attempt": "2"}))

    publication.retract(
        {},
        {
            "artifact_path": str(artifact),  # still pre-publication from the caller's view
            "workspace_dir": str(workspace),
            "_queue_job_id": "job-g",
            "_queue_attempt": 2,
        },
    )

    assert published_path.exists(), "an interrupted publication must be rolled back"
    assert published_path.read_bytes() == b"good v1"
    assert not backup_path.exists(), "the backup is consumed by the rollback"


async def test_publish_is_idempotent_for_the_same_identity(tmp_path: Path) -> None:
    """A retry after an ambiguous crash re-publishes the same bytes."""
    _, publication = _publication(tmp_path)
    workspace = _workspace(tmp_path, "ws")
    artifact = workspace / "render.mp4"
    artifact.write_bytes(b"retry bytes")
    result = _result(workspace, "render.mp4", "job-retry", 1)

    patch1 = publication.publish({}, result)
    patch2 = publication.publish({}, result)
    assert patch1["artifact_path"] == patch2["artifact_path"]
    assert Path(patch2["artifact_path"]).read_bytes() == b"retry bytes"


async def test_finalize_cleans_only_its_own_staging(tmp_path: Path) -> None:
    artifact_root, publication = _publication(tmp_path)
    workspace = _workspace(tmp_path, "ws")
    artifact = workspace / "render.mp4"
    artifact.write_bytes(b"v1")

    publication.publish({}, _result(workspace, "render.mp4", "job-fin", 1))
    publication.publish({}, _result(workspace, "render.mp4", "job-fin", 2))

    publication.finalize({}, _result(workspace, "render.mp4", "job-fin", 1))
    staging_root = artifact_root / "staging" / "job-fin"
    assert not (staging_root / "1").exists(), "finalize must clean only its own attempt"
    assert (staging_root / "2").exists(), "another attempt's staging must survive"


# ── D2. the attempt must be a real, present, canonical identity ─────────────
#
# task-283 finding 3: the sidecar reader validated ``schema`` and ``job_id`` but
# not ``attempt``, and the retract comparison ran on raw values.  A sidecar
# whose attempt was missing/``null`` plus a caller with no attempt therefore
# satisfied ``None == None`` — an ownership claim with no owner behind it.  The
# matrix below pins every representation that must be refused.


def _publish_v1_and_v2(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, object]:
    """Publish bytes for attempt 1, then attempt 2 (so a ``.prev`` exists).

    Returns ``(workspace, published_v2, backup, sidecar, artifact, publication)``.
    """
    artifact_root, publication = _publication(tmp_path)
    workspace = _workspace(tmp_path, "ws")
    artifact = workspace / "render.mp4"
    identity = {"_queue_job_id": "job-d2", "_queue_attempt": 1}

    artifact.write_bytes(b"v1 bytes")
    publication.publish({}, {**_result(workspace, "render.mp4", "job-d2", 1), **identity})

    artifact.write_bytes(b"v2 bytes")
    patch = publication.publish({}, {**_result(workspace, "render.mp4", "job-d2", 2), **identity})
    published = Path(patch["artifact_path"])
    backup = published.parent / ".render.mp4.prev"
    sidecar = published.parent / ".render.mp4.owner"
    assert published.read_bytes() == b"v2 bytes"
    assert backup.read_bytes() == b"v1 bytes"
    return workspace, published, backup, sidecar, artifact, publication


def _assert_untouched(published: Path, backup: Path) -> None:
    assert published.exists() and published.read_bytes() == b"v2 bytes", (
        "an unproven retract must never delete or replace published bytes"
    )
    assert backup.exists() and backup.read_bytes() == b"v1 bytes", (
        "an unproven retract must never consume the backup"
    )


@pytest.mark.parametrize(
    "recorded_attempt",
    [
        pytest.param(None, id="missing-null"),
        pytest.param(True, id="bool-true"),
        pytest.param(False, id="bool-false"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param("", id="empty-string"),
        pytest.param("abc", id="non-numeric-string"),
        pytest.param("1.5", id="float-string"),
        pytest.param(1.0, id="float"),
        pytest.param([1], id="list"),
        pytest.param({"n": 1}, id="object"),
    ],
)
async def test_retract_refuses_a_sidecar_without_a_canonical_attempt(
    tmp_path: Path, recorded_attempt: object
) -> None:
    workspace, published, backup, sidecar, artifact, publication = _publish_v1_and_v2(tmp_path)
    sidecar.write_text(
        json.dumps({"schema": 1, "job_id": "job-d2", "attempt": recorded_attempt}),
        encoding="utf-8",
    )

    publication.retract(
        {},
        {
            "artifact_path": str(published),
            "workspace_dir": str(workspace),
            "_queue_job_id": "job-d2",
            "_queue_attempt": 1,
        },
    )

    _assert_untouched(published, backup)


async def test_retract_refuses_a_sidecar_with_no_attempt_key(tmp_path: Path) -> None:
    workspace, published, backup, sidecar, artifact, publication = _publish_v1_and_v2(tmp_path)
    sidecar.write_text(json.dumps({"schema": 1, "job_id": "job-d2"}), encoding="utf-8")

    publication.retract(
        {},
        {
            "artifact_path": str(published),
            "workspace_dir": str(workspace),
            "_queue_job_id": "job-d2",
            "_queue_attempt": 1,
        },
    )

    _assert_untouched(published, backup)


async def test_retract_refuses_a_caller_without_a_queue_minted_attempt(
    tmp_path: Path,
) -> None:
    """The exact ``None == None`` bypass from the review.

    No queue identity on either side: the publication derives its job component
    from the workspace and records ``"attempt": null``; the retract carries no
    attempt.  Before the fix the raw comparison saw ``null == None`` and happily
    restored the backup — an ownership decision with no owner behind it.  A
    caller with no queue-minted attempt holds no proof, so nothing may move.
    """
    artifact_root, publication = _publication(tmp_path)
    workspace = _workspace(tmp_path, "ws")
    artifact = workspace / "render.mp4"

    artifact.write_bytes(b"v1 bytes")
    publication.publish({}, {"artifact_path": str(artifact), "workspace_dir": str(workspace)})

    artifact.write_bytes(b"v2 bytes")
    patch = publication.publish(
        {}, {"artifact_path": str(artifact), "workspace_dir": str(workspace)}
    )
    published = Path(patch["artifact_path"])
    backup = published.parent / ".render.mp4.prev"
    sidecar = published.parent / ".render.mp4.owner"
    assert published.read_bytes() == b"v2 bytes"
    assert backup.read_bytes() == b"v1 bytes"
    assert json.loads(sidecar.read_text(encoding="utf-8"))["attempt"] is None

    publication.retract({}, {"artifact_path": str(published), "workspace_dir": str(workspace)})

    _assert_untouched(published, backup)


async def test_retract_refuses_an_unreadable_or_malformed_sidecar(tmp_path: Path) -> None:
    workspace, published, backup, sidecar, artifact, publication = _publish_v1_and_v2(tmp_path)
    caller = {
        "artifact_path": str(published),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-d2",
        "_queue_attempt": 1,
    }

    for malformed in ('{"schema": 1, "job_id": "job-d2", "attempt":', "not json at all", "[]"):
        sidecar.write_text(malformed, encoding="utf-8")
        publication.retract({}, caller)
        _assert_untouched(published, backup)

    sidecar.unlink()
    publication.retract({}, caller)
    _assert_untouched(published, backup)


async def test_retract_refuses_another_jobs_sidecar(tmp_path: Path) -> None:
    workspace, published, backup, sidecar, artifact, publication = _publish_v1_and_v2(tmp_path)
    sidecar.write_text(
        json.dumps({"schema": 1, "job_id": "some-other-job", "attempt": 1}), encoding="utf-8"
    )

    publication.retract(
        {},
        {
            "artifact_path": str(published),
            "workspace_dir": str(workspace),
            "_queue_job_id": "job-d2",
            "_queue_attempt": 1,
        },
    )

    _assert_untouched(published, backup)


async def test_retract_refuses_a_different_attempt_of_the_same_job(tmp_path: Path) -> None:
    workspace, published, backup, sidecar, artifact, publication = _publish_v1_and_v2(tmp_path)
    sidecar.write_text(
        json.dumps({"schema": 1, "job_id": "job-d2", "attempt": 2}), encoding="utf-8"
    )

    # Attempt 1 tries to undo attempt 2's publication.
    publication.retract(
        {},
        {
            "artifact_path": str(published),
            "workspace_dir": str(workspace),
            "_queue_job_id": "job-d2",
            "_queue_attempt": 1,
        },
    )

    _assert_untouched(published, backup)


async def test_retract_accepts_the_owning_attempt_in_either_spelling(
    tmp_path: Path,
) -> None:
    """``"2"`` (sidecar spelling) and ``2`` (queue spelling) are one identity."""
    workspace, published, backup, sidecar, artifact, publication = _publish_v1_and_v2(tmp_path)
    sidecar.write_text(
        json.dumps({"schema": 1, "job_id": "job-d2", "attempt": "2"}), encoding="utf-8"
    )

    publication.retract(
        {},
        {
            "artifact_path": str(published),
            "workspace_dir": str(workspace),
            "_queue_job_id": "job-d2",
            "_queue_attempt": 2,
        },
    )

    assert not backup.exists(), "the owning attempt's retract consumes the backup"
    assert published.read_bytes() == b"v1 bytes", "the previous artifact is restored"
