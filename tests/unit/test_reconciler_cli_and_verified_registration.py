"""Reconciler operationalisation: the CLI contract and the verified repair path.

Two things are proven here (Wave 3B):

* the module is a real operational entry point — ``python -m
  nexus_ai_agent.maintenance.reconciler`` is a **dry run** unless
  ``--apply-repair`` is passed, and ``--fail-on-violation`` turns any remaining
  divergence into a non-zero exit (which is what the nightly maintenance
  workflow consumes);
* a repair writes *verified lineage*: when the queue row still carries its
  artifact passport, the published bytes are staged into
  :class:`LocalContentAddressedBlobStore` (``blobs/<prefix>/<sha256>``) and an
  ``AssetVersion`` row is registered whose ``blob_key`` is that relative
  content-addressed key — never an absolute filesystem path — so
  ``AssetLibraryService.read_content()`` can serve it like a live row.
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
from nexus_ai_agent.maintenance import reconciler
from nexus_ai_agent.storage import models as orm

JOB_ID = "job-cli-1"
PROJECT_ID = "recon-cli-project"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Ctx:
    """Adapt ``async_sessionmaker`` to the store's zero-arg session factory."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory

    async def __aenter__(self) -> Any:
        return self._factory()

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.fixture()
async def session_factory(tmp_path: Path) -> Any:
    url = f"sqlite+aiosqlite:///{tmp_path}/recon-cli.sqlite"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _commit_revision(factory: Any) -> Any:
    project = new_project(
        PROJECT_ID, "recon-cli", Timeline(timeline_id="recon-cli-tl", duration_us=0)
    )
    assert project.state_hash == compute_state_hash(project)
    store = SqlProjectRevisionStore(session_factory=lambda: _Ctx(factory))
    return await store.commit(
        project, authorized_project_id=PROJECT_ID, expected_parent_revision_id=None
    )


def _write_published(root: Path, name: str, payload: bytes, attempt: int) -> Path:
    job_dir = root / "published" / JOB_ID
    job_dir.mkdir(parents=True, exist_ok=True)
    artifact = job_dir / name
    artifact.write_bytes(payload)
    (job_dir / f".{name}.owner").write_text(
        json.dumps({"schema": 1, "job_id": JOB_ID, "attempt": attempt}) + "\n",
        encoding="utf-8",
    )
    return artifact


class _Facts:
    def __init__(
        self,
        *,
        status: str,
        attempt: int,
        result: dict[str, Any] | None,
        artifact_passport: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.attempt = attempt
        self.result = result
        self.artifact_passport = artifact_passport


class _FakeQueue:
    def __init__(self, facts: dict[str, _Facts]) -> None:
        self._facts = facts

    def get_job_facts(self, job_id: str) -> _Facts | None:
        return self._facts.get(job_id)


def _passport() -> dict[str, Any]:
    return {
        "passport_digest": "sha256:" + "c" * 64,
        "predicate": {"verification": {"verification_digest": "sha256:" + "d" * 64}},
    }


# --------------------------------------------------------------------------- #
# CLI contract
# --------------------------------------------------------------------------- #


async def test_cli_is_a_dry_run_by_default(tmp_path: Path, session_factory: Any, capsys) -> None:
    revision = await _commit_revision(session_factory)
    artifact = _write_published(tmp_path, "out.mp4", b"orphan-bytes" * 8, 1)
    # The CLI opens its own queue; this row exists so the scan has real facts
    # when the operator later runs the same command against a live queue.
    _FakeQueue(
        {
            JOB_ID: _Facts(
                status="completed",
                attempt=1,
                result={"project_id": PROJECT_ID, "revision_id": revision.revision_id},
            )
        }
    )
    code = await reconciler._run(
        reconciler.argparse.Namespace(
            workspace_root=str(tmp_path),
            apply_repair=False,
            queue_db=None,
            database_path=None,
            json=True,
            fail_on_violation=False,
        )
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["apply_repair"] is False
    assert artifact.is_file(), "a dry run must not move or delete published bytes"
    actions = {issue["action"] for issue in report["issues"]}
    assert actions <= {"would_repair", "would_quarantine"}
    assert not actions & {"repaired", "quarantined"}


async def test_fail_on_violation_makes_a_divergence_red(
    tmp_path: Path, session_factory: Any
) -> None:
    revision = await _commit_revision(session_factory)
    _write_published(tmp_path, "out.mp4", b"orphan-bytes" * 8, 1)
    queue = _FakeQueue(
        {
            JOB_ID: _Facts(
                status="completed",
                attempt=1,
                result={"project_id": PROJECT_ID, "revision_id": revision.revision_id},
            )
        }
    )
    report = await reconciler.reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=False
    )
    assert report.violations == 1
    # The workflow consumes exactly this predicate.
    assert report.to_dict()["violations"] == 1


def test_main_parses_the_documented_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_run(args: Any) -> int:
        captured.update(vars(args))
        return 7

    monkeypatch.setattr(reconciler, "_run", fake_run)
    code = reconciler.main(
        ["--workspace-root", "/tmp/x", "--apply-repair", "--json", "--fail-on-violation"]
    )
    assert code == 7
    assert captured["workspace_root"] == "/tmp/x"
    assert captured["apply_repair"] is True
    assert captured["json"] is True
    assert captured["fail_on_violation"] is True


def test_queue_for_returns_none_without_a_queue_database(tmp_path: Path) -> None:
    assert reconciler._queue_for(tmp_path, None) is None


# --------------------------------------------------------------------------- #
# Verified registration on repair
# --------------------------------------------------------------------------- #


async def test_repair_registers_a_verified_asset_version_in_the_blob_store(
    tmp_path: Path, session_factory: Any
) -> None:
    revision = await _commit_revision(session_factory)
    # A real MP4 header: the registration re-checks the bytes against the
    # claimed media type, so a payload that is not an MP4 must be refused.
    payload = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"verified-repair-bytes" * 16
    _write_published(tmp_path, "out.mp4", payload, 2)
    digest = _sha256(payload)
    queue = _FakeQueue(
        {
            JOB_ID: _Facts(
                status="completed",
                attempt=2,
                result={"project_id": PROJECT_ID, "revision_id": revision.revision_id},
                artifact_passport=_passport(),
            )
        }
    )
    report = await reconciler.reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=True
    )
    assert report.repaired == 1, report.to_dict()
    issue = next(i for i in report.issues if i.action == "repaired")
    assert "verified AssetVersion" in issue.detail

    async with session_factory() as session:
        rows = (await session.execute(orm.AssetVersion.__table__.select())).all()
        assert len(rows) == 1, "the repair must write exactly one content-addressed AssetVersion"
        version = rows[0]
        assert version.version_id.startswith("render_version_")
        assert version.blob_key == f"blobs/{digest[:2]}/{digest}"
        assert not version.blob_key.startswith("/")
        assert version.content_sha256 == digest
        binding = await session.get(orm.ProjectArtifactBinding, (f"sha256:{digest}", JOB_ID))
        assert binding is not None
        assert binding.attempt == 2
        assert binding.artifact_version_id == version.version_id

    staged = tmp_path / "asset-library" / "blobs" / digest[:2] / digest
    assert staged.is_file(), "the published bytes must land in the content-addressed blob store"
    assert staged.read_bytes() == payload


async def test_repair_without_passport_evidence_falls_back_and_says_so(
    tmp_path: Path, session_factory: Any
) -> None:
    revision = await _commit_revision(session_factory)
    _write_published(tmp_path, "out.mp4", b"legacy-bytes" * 8, 1)
    queue = _FakeQueue(
        {
            JOB_ID: _Facts(
                status="completed",
                attempt=1,
                result={"project_id": PROJECT_ID, "revision_id": revision.revision_id},
                artifact_passport=None,
            )
        }
    )
    report = await reconciler.reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=True
    )
    assert report.repaired == 1
    issue = next(i for i in report.issues if i.action == "repaired")
    assert "verified registration unavailable" in issue.detail
    async with session_factory() as session:
        count = len((await session.execute(orm.AssetVersion.__table__.select())).all())
    assert count == 0, "a metadata-poor row must not be presented as verified lineage"


async def test_repair_refuses_an_unreviewed_media_type(
    tmp_path: Path, session_factory: Any
) -> None:
    revision = await _commit_revision(session_factory)
    _write_published(tmp_path, "out.weird", b"unknown-kind" * 8, 1)
    queue = _FakeQueue(
        {
            JOB_ID: _Facts(
                status="completed",
                attempt=1,
                result={"project_id": PROJECT_ID, "revision_id": revision.revision_id},
                artifact_passport=_passport(),
            )
        }
    )
    report = await reconciler.reconcile_published_artifacts_and_db(
        tmp_path, session_factory, queue=queue, apply_repair=True
    )
    assert report.repaired == 1
    issue = next(i for i in report.issues if i.action == "repaired")
    assert "no reviewed media type" in issue.detail
