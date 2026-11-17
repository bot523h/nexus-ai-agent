"""Crash-injection tests for the DB <-> disk artifact reconciler.

Proves the two failure directions from the forensic audit (Dimension 3.2) are
detected — and with ``apply_repair=True`` are recovered deterministically or
forensically quarantined, never silently dropped.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from nexus_ai_agent.creative.project_state.store import SqlProjectRevisionStore
from nexus_ai_agent.creative.studio.models import Timeline, compute_state_hash, new_project
from nexus_ai_agent.maintenance.reconciler import (
    LedgerState,
    reconcile_published_artifacts_and_db,
)
from nexus_ai_agent.storage import models as orm

JOB_ID = "job-crash-1"
PROJECT_ID = "recon-project"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_published(
    root: Path,
    job_id: str,
    name: str,
    payload: bytes,
    attempt: int | str | None,
    *,
    content_digest: str | None = "measured",
):
    """Publish *payload* with the owner sidecar the real writer leaves behind.

    The writer refreshes the sidecar with the measured ``content_digest`` right
    after the atomic publish; the reconciler refuses to repair a crash-window
    orphan whose sidecar cannot name the bytes it owns, so the fixture records it
    by default and drops it on request (``content_digest=None``).
    """
    job_dir = root / "published" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    artifact = job_dir / name
    artifact.write_bytes(payload)
    sidecar = job_dir / f".{name}.owner"
    document: dict[str, object] = {"schema": 1, "job_id": job_id, "attempt": attempt}
    if content_digest is not None:
        document["content_digest"] = (
            f"sha256:{_sha256(payload)}" if content_digest == "measured" else content_digest
        )
    sidecar.write_text(json.dumps(document) + "\n", encoding="utf-8")
    return artifact, sidecar


class _Facts:
    """One queue row, shaped like ``provenance.models.JobFacts``.

    ``payload`` is the immutable admission record: inside the crash window it is
    the *only* place the project/revision pin exists, because ``result`` is
    written together with the completion this repair is recovering from.
    """

    def __init__(
        self,
        *,
        status: str,
        attempt: int,
        result: dict[str, Any] | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.attempt = attempt
        self.result = result
        self.payload = payload or {}


class _FakeQueue:
    """Minimal get_job_facts provider (sync), mirroring InProcessJobQueue."""

    def __init__(self, facts: dict[str, _Facts]) -> None:
        self._facts = facts

    def get_job_facts(self, job_id: str):
        return self._facts.get(job_id)


@pytest.fixture()
async def session_factory(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path}/recon.sqlite"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _commit_revision(tmp_path: Path, factory) -> Any:
    project = new_project(PROJECT_ID, "recon", Timeline(timeline_id="recon-tl", duration_us=0))
    assert project.state_hash == compute_state_hash(project)
    store = SqlProjectRevisionStore(session_factory=lambda: _ctx(factory))
    return await store.commit(
        project, authorized_project_id=PROJECT_ID, expected_parent_revision_id=None
    )


class _ctx:
    """Adapt async_sessionmaker to the store's zero-arg session factory protocol.

    The session is closed on exit: leaving it to the garbage collector is what
    produced ``SAWarning: the garbage collector is trying to clean up
    non-checked-in connection`` in this file — a leak in the *test* harness that
    looked like a defect in the code under test.
    """

    def __init__(self, factory) -> None:
        self._factory = factory
        self._session = None

    async def __aenter__(self):
        self._session = self._factory()
        return self._session

    async def __aexit__(self, *exc):
        if self._session is not None:
            await self._session.close()
            self._session = None
        return False


async def test_case_a_orphan_is_repaired_and_repair_is_idempotent(
    tmp_path: Path, session_factory
) -> None:
    revision = await _commit_revision(tmp_path, session_factory)
    payload = b"orphan-bytes" * 32
    artifact, _sidecar = _write_published(tmp_path, JOB_ID, "out.mp4", payload, 3)

    queue = _FakeQueue(
        {
            JOB_ID: _Facts(
                status="completed",
                attempt=3,
                result={"project_id": PROJECT_ID, "revision_id": revision.revision_id},
            )
        }
    )
    report = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=True
    )
    assert report.repaired == 1
    assert report.violations == 0

    async with session_factory() as session:
        row = await session.get(orm.ProjectArtifactBinding, (f"sha256:{_sha256(payload)}", JOB_ID))
    assert row is not None
    assert row.attempt == 3
    assert row.revision_id == revision.revision_id

    # Second pass: no orphans remain — the repair is idempotent.
    report2 = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=True
    )
    assert report2.issues == ()
    assert report2.bound == 1


async def test_case_a_fenced_out_attempt_is_quarantined_not_bound(
    tmp_path: Path, session_factory
) -> None:
    revision = await _commit_revision(tmp_path, session_factory)
    payload = b"stale-bytes" * 32
    artifact, sidecar = _write_published(tmp_path, JOB_ID, "out.mp4", payload, 1)

    queue = _FakeQueue(
        {
            JOB_ID: _Facts(
                status="completed",
                attempt=2,  # successor attempt superseded the sidecar
                result={"project_id": PROJECT_ID, "revision_id": revision.revision_id},
            )
        }
    )
    report = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=True
    )
    assert report.violations == 1
    assert report.quarantined == 1
    assert not artifact.exists()
    receipt = tmp_path / ".quarantine" / f"{JOB_ID}_1_reconciled" / "reconciliation_receipt.json"
    assert receipt.is_file()
    document = json.loads(receipt.read_text(encoding="utf-8"))
    assert document["schema"] == "nagar.reconciliation_receipt.v1"
    assert document["content_sha256"] == f"sha256:{_sha256(payload)}"
    assert document["reason"] == "unrepairable_orphan"


async def test_case_a_missing_sidecar_is_quarantined_on_apply(
    tmp_path: Path, session_factory
) -> None:
    job_dir = tmp_path / "published" / JOB_ID
    job_dir.mkdir(parents=True)
    (job_dir / "out.bin").write_bytes(b"no-sidecar")
    report = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, apply_repair=True
    )
    assert report.quarantined == 1
    assert any(
        i.kind == "sidecar_invalid" and i.state is LedgerState.VIOLATED for i in report.issues
    )


async def test_case_b_binding_with_missing_blob_is_violated(
    tmp_path: Path, session_factory
) -> None:
    revision = await _commit_revision(tmp_path, session_factory)
    ghost = f"sha256:{_sha256(b'never-published')}"
    async with session_factory() as session:
        session.add(
            orm.ProjectArtifactBinding(
                artifact_id=ghost,
                project_id=PROJECT_ID,
                revision_id=revision.revision_id,
                job_id="ghost-job",
                attempt=7,
                artifact_kind="binary",
                size_bytes=16,
            )
        )
        await session.commit()
    report = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, apply_repair=True
    )
    kinds = {i.kind for i in report.issues}
    assert "broken_blob_binding" in kinds
    assert any(
        i.kind == "broken_blob_binding" and i.state is LedgerState.VIOLATED for i in report.issues
    )
    assert report.violations >= 1


async def test_case_b_binding_with_mismatched_bytes_is_violated(
    tmp_path: Path, session_factory
) -> None:
    revision = await _commit_revision(tmp_path, session_factory)
    payload = b"real-bytes"
    artifact, _ = _write_published(tmp_path, JOB_ID, "out.bin", payload, 1)
    wrong = f"sha256:{_sha256(b'expected-bytes')}"
    async with session_factory() as session:
        session.add(
            orm.ProjectArtifactBinding(
                artifact_id=wrong,
                project_id=PROJECT_ID,
                revision_id=revision.revision_id,
                job_id=JOB_ID,
                attempt=1,
                artifact_kind="binary",
                size_bytes=13,
            )
        )
        await session.commit()
    report = await reconcile_published_artifacts_and_db(tmp_path, session_factory)
    assert any(i.kind == "broken_blob_binding" for i in report.issues)
    assert artifact.exists()  # bytes are never fabricated or deleted by the audit


async def test_dry_run_never_mutates_anything(tmp_path: Path, session_factory) -> None:
    revision = await _commit_revision(tmp_path, session_factory)
    payload = b"orphan"
    artifact, sidecar = _write_published(tmp_path, JOB_ID, "out.bin", payload, 5)
    queue = _FakeQueue(
        {
            JOB_ID: _Facts(
                status="completed",
                attempt=5,
                result={"project_id": PROJECT_ID, "revision_id": revision.revision_id},
            )
        }
    )
    report = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=False
    )
    assert report.scanned_files == 1
    assert artifact.exists() and sidecar.exists()
    assert report.issues[0].action == "would_repair"
    async with session_factory() as session:
        row = await session.get(orm.ProjectArtifactBinding, (f"sha256:{_sha256(payload)}", JOB_ID))
    assert row is None


async def test_mismatched_sidecar_namespace_is_flagged(tmp_path: Path, session_factory) -> None:
    payload = b"foreign"
    job_dir = tmp_path / "published" / JOB_ID
    job_dir.mkdir(parents=True)
    artifact = job_dir / "out.bin"
    artifact.write_bytes(payload)
    (job_dir / ".out.bin.owner").write_text(
        json.dumps({"schema": 1, "job_id": "someone-else", "attempt": 1}),
        encoding="utf-8",
    )
    report = await reconcile_published_artifacts_and_db(tmp_path, session_factory)
    assert any(i.kind == "sidecar_invalid" for i in report.issues)


async def test_consistent_publication_reports_clean(tmp_path: Path, session_factory) -> None:
    revision = await _commit_revision(tmp_path, session_factory)
    payload = b"consistent"
    artifact_id = f"sha256:{_sha256(payload)}"
    _write_published(tmp_path, JOB_ID, "out.bin", payload, 1)
    async with session_factory() as session:
        session.add(
            orm.ProjectArtifactBinding(
                artifact_id=artifact_id,
                project_id=PROJECT_ID,
                revision_id=revision.revision_id,
                job_id=JOB_ID,
                attempt=1,
                artifact_kind="binary",
                size_bytes=len(payload),
            )
        )
        await session.commit()
    report = await reconcile_published_artifacts_and_db(tmp_path, session_factory)
    assert report.violations == 0
    assert report.issues == ()
    assert report.bound == 1


# ── Case A inside the crash window (audit finding 1a) ────────────────────────
#
# The window is: ``publish_verified`` moved the bytes into
# ``published/<job_id>/<name>`` and ``_mark_completed`` never ran. The row is
# still ``verifying`` and ``result`` is empty — the *payload* is the only
# evidence that pins the revision. Treating that state as an unrepairable
# orphan quarantined a perfectly good artifact and lost the binding.


def _crash_window_payload(revision_id: str, *, project_id: str = PROJECT_ID) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "operation": "timeline.trim",
        "project_id": project_id,
        "revision_id": revision_id,
        "revision_hash": "sha256:" + "ab" * 32,
        "idempotency_key": "idem-crash-1",
    }


async def test_crash_window_row_is_repaired_from_its_payload_pin(
    tmp_path: Path, session_factory
) -> None:
    revision = await _commit_revision(tmp_path, session_factory)
    payload = b"crash-window-bytes" * 16
    artifact, _sidecar = _write_published(tmp_path, JOB_ID, "out.mp4", payload, 3)

    queue = _FakeQueue(
        {
            JOB_ID: _Facts(
                status="verifying",  # publication landed, completion did not
                attempt=3,
                result=None,
                payload=_crash_window_payload(revision.revision_id),
            )
        }
    )
    report = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=True
    )
    assert report.repaired == 1, [i.detail for i in report.issues]
    assert report.quarantined == 0
    assert artifact.exists(), "a healthy published artifact must never be moved"

    async with session_factory() as session:
        row = await session.get(orm.ProjectArtifactBinding, (f"sha256:{_sha256(payload)}", JOB_ID))
    assert row is not None
    assert row.revision_id == revision.revision_id
    assert row.attempt == 3


async def test_crash_window_without_a_pin_is_quarantined_not_guessed(
    tmp_path: Path, session_factory
) -> None:
    """No pin anywhere in the row: the artifact cannot be attributed, so it is not."""
    payload = b"unattributable" * 16
    artifact, _sidecar = _write_published(tmp_path, JOB_ID, "out.mp4", payload, 2)
    queue = _FakeQueue(
        {
            JOB_ID: _Facts(
                status="processing",
                attempt=2,
                result=None,
                payload={"operation": "timeline.trim", "idempotency_key": "k"},
            )
        }
    )
    report = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=True
    )
    assert report.quarantined == 1
    assert not artifact.exists()
    assert any("pins no project_id/revision_id" in i.detail for i in report.issues)


async def test_crash_window_refuses_bytes_that_do_not_match_the_recorded_digest(
    tmp_path: Path, session_factory
) -> None:
    revision = await _commit_revision(tmp_path, session_factory)
    payload = b"tampered-in-the-window" * 8
    _artifact, _sidecar = _write_published(tmp_path, JOB_ID, "out.mp4", payload, 4)
    recorded = _crash_window_payload(revision.revision_id)
    recorded["artifact_id"] = "sha256:" + "cd" * 32  # not these bytes

    queue = _FakeQueue(
        {JOB_ID: _Facts(status="verifying", attempt=4, result=None, payload=recorded)}
    )
    report = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=True
    )
    assert report.quarantined == 1
    assert report.repaired == 0
    assert any("do not match the content address" in i.detail for i in report.issues)


async def test_a_failed_row_is_never_repaired_even_with_a_payload_pin(
    tmp_path: Path, session_factory
) -> None:
    """The queue refused these bytes; binding them would fabricate lineage."""
    revision = await _commit_revision(tmp_path, session_factory)
    _artifact, _sidecar = _write_published(tmp_path, JOB_ID, "out.mp4", b"rejected" * 8, 1)
    queue = _FakeQueue(
        {
            JOB_ID: _Facts(
                status="failed_terminal",
                attempt=1,
                result={"project_id": PROJECT_ID, "revision_id": revision.revision_id},
                payload=_crash_window_payload(revision.revision_id),
            )
        }
    )
    report = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=True
    )
    assert report.repaired == 0
    assert report.quarantined == 1


# ── Quarantine is evidence: a second witness never overwrites the first ─────


async def test_quarantine_never_overwrites_an_existing_witness(
    tmp_path: Path, session_factory
) -> None:
    first = b"first-witness" * 8
    second = b"second-witness" * 8
    quarantine = tmp_path / ".quarantine" / f"{JOB_ID}_1_reconciled"

    artifact, _ = _write_published(tmp_path, JOB_ID, "out.mp4", first, 1)
    report = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=_FakeQueue({}), apply_repair=True
    )
    assert report.quarantined == 1
    first_bytes = (quarantine / "out.mp4").read_bytes()
    assert first_bytes == first

    # The same (job_id, attempt) crashes again with different bytes.
    artifact2, sidecar2 = _write_published(tmp_path, JOB_ID, "out.mp4", second, 1)
    report2 = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=_FakeQueue({}), apply_repair=True
    )
    assert report2.quarantined == 1

    witnesses = sorted(
        p for p in quarantine.iterdir() if p.name.startswith("out") and not p.name.startswith(".")
    )
    assert len(witnesses) == 2, witnesses
    assert (quarantine / "out.mp4").read_bytes() == first, "the first witness was overwritten"
    assert {p.read_bytes() for p in witnesses} == {first, second}
    assert any(p.name.startswith("out.") and p.name.endswith(".mp4") for p in witnesses)
    assert not artifact.exists() and not artifact2.exists()
    assert not sidecar2.exists()
    receipts = sorted(quarantine.glob("reconciliation_receipt*.json"))
    assert len(receipts) == 2, receipts


def test_quarantine_candidates_disambiguate_by_digest_then_counter(tmp_path: Path) -> None:
    from nexus_ai_agent.maintenance.reconciler import _quarantine_candidates

    candidates = _quarantine_candidates(tmp_path, "out.mp4", "sha256:" + "11" * 32)
    names = [next(candidates).name for _ in range(3)]
    assert names == ["out.mp4", "out.111111111111.mp4", "out.111111111111-2.mp4"]


# ── The CLI closes the pools it opened (audit finding 1c) ────────────────────
#
# A one-shot CLI that exits with a live aiosqlite connection produces
# `ResourceWarning: unclosed database` and can leave the sqlite worker thread
# alive. The engine cache is process-wide state, so the only honest fix is an
# explicit `dispose()` in a `finally` block — which means the cache must be
# empty afterwards, and a *subsequent* call must still work.


async def test_dispose_all_engines_empties_the_cache_and_stays_usable(tmp_path: Path) -> None:
    from nexus_ai_agent.storage import db as db_module

    database = tmp_path / "app.sqlite"
    async with db_module.get_session(str(database)) as session:
        await session.execute(orm.ProjectArtifactBinding.__table__.select())
    assert db_module._engine is not None

    await db_module.dispose_all_engines()
    assert db_module._engine is None
    assert db_module._session_factory is None

    # Idempotent, and the module still works after the pool was closed.
    await db_module.dispose_all_engines()
    async with db_module.get_session(str(database)) as session:
        await session.execute(orm.ProjectArtifactBinding.__table__.select())
    await db_module.dispose_all_engines()


def test_the_reconciler_cli_disposes_its_engines(tmp_path: Path, capsys) -> None:
    from nexus_ai_agent.maintenance.reconciler import main
    from nexus_ai_agent.storage import db as db_module

    (tmp_path / "published").mkdir()
    code = main(
        [
            "--workspace-root",
            str(tmp_path),
            "--database-path",
            str(tmp_path / "app.sqlite"),
            "--json",
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "nagar.db_disk_reconciliation.v1"
    assert db_module._engine is None, "the CLI left an engine open past its event loop"


# ── F3: quarantine witness creation is atomic, not check-then-replace ────────
#
# `if not target.exists(): os.replace(src, target)` is a TOCTOU race: two
# reconcilers scanning the same workspace can both see the name free, and the
# second `os.replace` silently destroys the first witness.  These tests pin the
# replacement: the destination must be *taken*, atomically, or the next name
# used.


def test_place_witness_never_overwrites_and_creates_the_name_atomically(
    tmp_path: Path,
) -> None:
    from nexus_ai_agent.maintenance.reconciler import _place_witness

    source = tmp_path / "src.bin"
    source.write_bytes(b"witness")
    existing = tmp_path / "target.bin"
    existing.write_bytes(b"first")

    assert _place_witness(source, existing) is False
    assert existing.read_bytes() == b"first", "an existing witness was overwritten"
    assert source.exists(), "a refused placement must not consume the source"

    free = tmp_path / "free.bin"
    assert _place_witness(source, free) is True
    assert free.read_bytes() == b"witness"
    assert not source.exists()


def test_place_witness_uses_o_excl_when_the_filesystem_cannot_hardlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-device quarantine (EXDEV) must still take the name atomically."""
    import errno

    from nexus_ai_agent.maintenance import reconciler

    def _no_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(reconciler.os, "link", _no_link)
    source = tmp_path / "src.bin"
    source.write_bytes(b"across-devices")
    taken = tmp_path / "taken.bin"
    taken.write_bytes(b"first")
    assert reconciler._place_witness(source, taken) is False
    assert taken.read_bytes() == b"first"
    free = tmp_path / "free.bin"
    assert reconciler._place_witness(source, free) is True
    assert free.read_bytes() == b"across-devices"
    assert not source.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")], "temp file leaked"


def test_a_racing_quarantiner_cannot_steal_the_witness_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate the interleaving the old code lost: name appears after the check.

    With `exists()`-then-`replace()` the winner is written, then overwritten.  Here
    the second writer must observe the taken name and fall through to the next
    candidate, so both witnesses survive.
    """
    from nexus_ai_agent.maintenance import reconciler

    destination = tmp_path / "quarantine"
    destination.mkdir()
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    real_link = reconciler.os.link
    state = {"raced": False}

    def _link_then_race(src: object, dst: object) -> None:
        # Another reconciler wins the very name we are about to take.
        if not state["raced"]:
            state["raced"] = True
            Path(str(dst)).write_bytes(b"intruder")
        return real_link(src, dst)

    monkeypatch.setattr(reconciler.os, "link", _link_then_race)
    placed_first = reconciler._quarantine_witness(
        first, destination, "out.mp4", "sha256:" + "11" * 32
    )
    monkeypatch.setattr(reconciler.os, "link", real_link)
    placed_second = reconciler._quarantine_witness(
        second, destination, "out.mp4", "sha256:" + "11" * 32
    )
    assert placed_first != placed_second
    assert (destination / "out.mp4").read_bytes() == b"intruder"  # never overwritten
    assert {placed_first.read_bytes(), placed_second.read_bytes()} == {b"first", b"second"}


# ── F3: the crash window must prove attribution, or refuse ───────────────────
#
# `pending` is not a crash window: the queue has not granted (or has released)
# the attempt, so no publication can legitimately exist.  And inside the window
# the sidecar must name the bytes; without it the bytes are just a file.


async def test_a_pending_row_is_never_repaired(tmp_path: Path, session_factory) -> None:
    revision = await _commit_revision(tmp_path, session_factory)
    artifact, _sidecar = _write_published(tmp_path, JOB_ID, "out.mp4", b"pending" * 16, 2)
    queue = _FakeQueue(
        {
            JOB_ID: _Facts(
                status="pending",
                attempt=2,
                result=None,
                payload=_crash_window_payload(revision.revision_id),
            )
        }
    )
    report = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=True
    )
    assert report.repaired == 0
    assert report.quarantined == 1
    assert not artifact.exists()
    assert any("job status is 'pending'" in i.detail for i in report.issues), report.issues


async def test_a_crash_window_sidecar_without_a_digest_is_unrepairable(
    tmp_path: Path, session_factory
) -> None:
    """Bytes that no artifact can attribute are not "probably fine"."""
    revision = await _commit_revision(tmp_path, session_factory)
    artifact, _sidecar = _write_published(
        tmp_path, JOB_ID, "out.mp4", b"unattributed" * 8, 3, content_digest=None
    )
    queue = _FakeQueue(
        {
            JOB_ID: _Facts(
                status="verifying",
                attempt=3,
                result=None,
                payload=_crash_window_payload(revision.revision_id),
            )
        }
    )
    report = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=True
    )
    assert report.repaired == 0
    assert not artifact.exists()
    assert any("carries no content_digest" in i.detail for i in report.issues), report.issues


async def test_a_crash_window_sidecar_digest_must_match_the_published_bytes(
    tmp_path: Path, session_factory
) -> None:
    revision = await _commit_revision(tmp_path, session_factory)
    artifact, _sidecar = _write_published(
        tmp_path, JOB_ID, "out.mp4", b"replaced" * 8, 3, content_digest="sha256:" + "ab" * 32
    )
    queue = _FakeQueue(
        {
            JOB_ID: _Facts(
                status="processing",
                attempt=3,
                result=None,
                payload=_crash_window_payload(revision.revision_id),
            )
        }
    )
    report = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=True
    )
    assert report.repaired == 0
    assert not artifact.exists()
    assert any("content_digest in the owner sidecar" in i.detail for i in report.issues)


async def test_a_row_that_pins_two_different_revisions_is_ambiguity_not_authority(
    tmp_path: Path, session_factory
) -> None:
    """`result` and `payload` disagreeing means we cannot choose; so we do not."""
    revision = await _commit_revision(tmp_path, session_factory)
    artifact, _sidecar = _write_published(tmp_path, JOB_ID, "out.mp4", b"ambiguous" * 8, 3)
    payload_pin = _crash_window_payload(revision.revision_id)
    queue = _FakeQueue(
        {
            JOB_ID: _Facts(
                status="completed",
                attempt=3,
                result={"project_id": PROJECT_ID, "revision_id": "some-other-revision"},
                payload=payload_pin,
            )
        }
    )
    report = await reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=True
    )
    assert report.repaired == 0
    assert report.quarantined == 1
    assert any("conflicting revisions" in i.detail for i in report.issues), report.issues
    assert not artifact.exists()


def test_the_sidecar_writer_records_the_measured_digest() -> None:
    """The reconciler's requirement is satisfiable: the writer measures, it doesn't guess."""
    import inspect

    from nexus_ai_agent.creative import render_jobs

    source = inspect.getsource(render_jobs.creative_render_publication_callback)
    assert "content_digest=content_address_of_file(" in source
    signature = inspect.signature(render_jobs._write_sidecar)
    assert signature.parameters["content_digest"].default is None


def test_the_digest_refresh_never_fabricates_a_claim(tmp_path: Path) -> None:
    """The refresh extends an existing claim; it must not be able to create one.

    If it could, removing the claim-time write would go unnoticed — which is
    exactly what the `queue-ownership-mutations` M6 probe ("publication writes no
    ownership evidence") caught when the refresh was first written as a second
    plain `_write_sidecar` call.
    """
    from nexus_ai_agent.creative import render_jobs

    sidecar = tmp_path / ".out.mp4.owner"
    digest = "sha256:" + "ab" * 32
    with pytest.raises(RuntimeError, match="claim that was never made"):
        render_jobs._refresh_sidecar_digest(
            sidecar, job_id="job-x", attempt="2", content_digest=digest
        )
    assert not sidecar.exists()

    render_jobs._write_sidecar(sidecar, job_id="job-x", attempt="2")
    with pytest.raises(RuntimeError, match="does not belong"):
        render_jobs._refresh_sidecar_digest(
            sidecar, job_id="job-y", attempt="2", content_digest=digest
        )
    assert "content_digest" not in json.loads(sidecar.read_text())

    render_jobs._refresh_sidecar_digest(sidecar, job_id="job-x", attempt="2", content_digest=digest)
    assert json.loads(sidecar.read_text()) == {
        "schema": 1,
        "job_id": "job-x",
        "attempt": "2",
        "content_digest": digest,
    }
