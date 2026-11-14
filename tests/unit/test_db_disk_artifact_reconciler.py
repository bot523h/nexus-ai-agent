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


def _write_published(root: Path, job_id: str, name: str, payload: bytes, attempt: int | str | None):
    job_dir = root / "published" / job_id
    job_dir.mkdir(parents=True)
    artifact = job_dir / name
    artifact.write_bytes(payload)
    sidecar = job_dir / f".{name}.owner"
    sidecar.write_text(
        json.dumps({"schema": 1, "job_id": job_id, "attempt": attempt}) + "\n",
        encoding="utf-8",
    )
    return artifact, sidecar


class _Facts:
    def __init__(self, *, status: str, attempt: int, result: dict[str, Any] | None) -> None:
        self.status = status
        self.attempt = attempt
        self.result = result


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
    """Adapt async_sessionmaker to the store's zero-arg session factory protocol."""

    def __init__(self, factory) -> None:
        self._factory = factory

    async def __aenter__(self):
        return self._factory()

    async def __aexit__(self, *exc):
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
