"""Revision pinning: a job executes the EXACT revision it was admitted against.

Task-282 Slice 1.  Two layers are proven here:

* the worker's revision resolver (``_resolve_pinned_project``) loads the pinned
  revision -- never the current head -- and refuses a missing, tampered,
  cross-project or hash-mismatched pin (no fallback);
* the handoff path pins the durable head into the *durable* queue payload, and
  the existing worker then executes that revision, refusing when it disappears
  even though the project head still exists.

The database is the real one (SQLite via ``NEXUS_DB_PATH``); only the external
model seam is a declared double, exactly as the composition root injects it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.adapters.sql_application_directory import (
    DEFAULT_PROJECT_PERMISSIONS,
    SQLApplicationDirectory,
    channel_binding_id,
)
from nexus_ai_agent.application.creative_intent import CanonicalCreativeIntent
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.application.principal import ChannelBinding
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.project_state.store import SqlProjectRevisionStore
from nexus_ai_agent.creative.render_jobs import (
    CreativeRenderPayload,
    _resolve_pinned_project,
    build_job_bus,
    creative_render_job,
)
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
    sha256_file,
)
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    AssetRecord,
    Timeline,
    new_project,
)
from nexus_ai_agent.nagar.cognition import build_cognition_gateway
from nexus_ai_agent.nagar.creative import CanonicalHandoffFacts, run_free_text_intent_durable
from nexus_ai_agent.storage.db import create_all_tables
from nexus_ai_agent.worker import default_job_handlers

PROJECT_ID = "pinning-project"
SOURCE_DURATION_US = 2_000_000


class _ScriptedProducer:
    """Declared double for the external model seam only."""

    consults_model = True

    async def complete(self, prompt: str, *, idempotency_key: str | None = None) -> str:
        return json.dumps(
            {
                "schema_id": "nagar.gateway.proposal.v1",
                "schema_version": 1,
                "operation": "timeline.trim",
                "input": {"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1_000_000},
                "rationale": "scripted",
                "confidence": 1.0,
            }
        )


@pytest.fixture()
def pin_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path / "creative"))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "creative").mkdir()
    settings_module.get_settings.cache_clear()
    asyncio.run(create_all_tables(str(tmp_path / "app.sqlite")))
    yield tmp_path
    settings_module.get_settings.cache_clear()


def _project(name: str = "pinned") -> Any:
    from nexus_ai_agent.creative.studio.models import compute_state_hash

    project = new_project(
        PROJECT_ID, name, Timeline(timeline_id="tl", duration_us=SOURCE_DURATION_US)
    ).model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="src",
                    media_kind="video",
                    content_sha256="sha256:" + "0" * 64,
                    duration_us=SOURCE_DURATION_US,
                )
            ]
        }
    )
    # ``model_copy`` bypasses the ``state_hash`` validator; recompute so the
    # stored hash matches the stored content (the store's read path re-checks it).
    project.state_hash = compute_state_hash(project)
    return project


def _pinned_payload(
    workspace: Path,
    *,
    revision_id: str,
    revision_hash: str,
    principal_id: str | None = None,
    channel_binding_id: str | None = None,
) -> dict[str, Any]:
    staged = workspace / "input.mp4"
    if not staged.exists():
        staged.write_bytes(b"placeholder-bytes")
    intent = CanonicalCreativeIntent.create(
        project_id=PROJECT_ID,
        proposal_id="proposal_" + hashlib.sha256(b"pinned").hexdigest(),
        source_text_sha256="sha256:" + hashlib.sha256(b"trim").hexdigest(),
        operation="timeline.trim",
        input_data={"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1_000_000},
    )
    return {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "input_sha256": sha256_file(workspace / "input.mp4"),
        "media_duration_us": SOURCE_DURATION_US,
        "user_id": 0,
        "chat_id": 0,
        "lang": "en",
        "idempotency_key": "pinned:trim:1",
        "schema_version": 2,
        "principal_id": principal_id or "prn_" + "0" * 32,
        "project_id": PROJECT_ID,
        "channel_binding_id": channel_binding_id or "cbn_" + "a" * 64,
        "intent": intent.model_dump(mode="json"),
        "revision_id": revision_id,
        "revision_hash": revision_hash,
    }


def _clip(path: Path) -> None:
    try:
        binary = resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:
        pytest.skip(f"FFmpeg unavailable: {exc}")
    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=120,
    )


async def _commit(store: SqlProjectRevisionStore, project: Any, *, parent: Any) -> Any:
    return await store.commit(
        project, authorized_project_id=PROJECT_ID, expected_parent_revision_id=parent
    )


# ---------------------------------------------------------------------------
# Resolver unit contract (real DB, no FFmpeg)
# ---------------------------------------------------------------------------


def test_resolver_returns_none_when_unpinned(pin_env: Path) -> None:
    payload = CreativeRenderPayload.model_validate(
        {
            "command": "edit",
            "operation": "trim",
            "args": [],
            "workspace_dir": "/tmp/x",
            "user_id": 0,
            "chat_id": 0,
            "idempotency_key": "k",
        }
    )
    assert asyncio.run(_resolve_pinned_project(payload)) is None


def test_resolver_loads_pinned_revision_not_the_moved_head(pin_env: Path) -> None:
    store = SqlProjectRevisionStore()
    workspace = pin_env / "creative" / "creative_pin_resolver"
    workspace.mkdir(parents=True)
    r1 = asyncio.run(_commit(store, _project("pinned-name"), parent=None))
    # The head moves AFTER the job was admitted against r1.
    asyncio.run(_commit(store, _project("moved-head"), parent=r1.revision_id))

    payload = CreativeRenderPayload.model_validate(
        _pinned_payload(workspace, revision_id=r1.revision_id, revision_hash=r1.state_hash)
    )
    resolved = asyncio.run(_resolve_pinned_project(payload))
    assert resolved is not None
    # The pinned revision is executed, NOT the newer head.
    assert resolved.name == "pinned-name"


def test_resolver_refuses_a_missing_pinned_revision(pin_env: Path) -> None:
    store = SqlProjectRevisionStore()
    workspace = pin_env / "creative" / "creative_pin_missing"
    workspace.mkdir(parents=True)
    r1 = asyncio.run(_commit(store, _project(), parent=None))
    r2 = asyncio.run(_commit(store, _project("head"), parent=r1.revision_id))

    # Delete the pinned revision's row out of band; the head still exists.
    db = pin_env / "app.sqlite"

    def _delete() -> None:
        con = sqlite3.connect(str(db))
        try:
            con.execute("DELETE FROM projectrevision WHERE revision_id=?", (r1.revision_id,))
            con.commit()
        finally:
            con.close()

    _delete()
    payload = CreativeRenderPayload.model_validate(
        _pinned_payload(workspace, revision_id=r1.revision_id, revision_hash=r1.state_hash)
    )
    from nexus_ai_agent.creative.render_jobs import CreativeRenderError

    with pytest.raises(CreativeRenderError) as excinfo:
        asyncio.run(_resolve_pinned_project(payload))
    assert excinfo.value.code == "revision_unavailable"
    # The head still exists -- the refusal is about the PIN, not the project.
    assert asyncio.run(store.load_head(PROJECT_ID)) is not None
    assert r2.revision_id  # head revision untouched


def test_resolver_refuses_a_tampered_pinned_revision(pin_env: Path) -> None:
    store = SqlProjectRevisionStore()
    workspace = pin_env / "creative" / "creative_pin_tamper"
    workspace.mkdir(parents=True)
    r1 = asyncio.run(_commit(store, _project("Alpha"), parent=None))
    db = pin_env / "app.sqlite"
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "UPDATE projectrevision SET state_json=? WHERE revision_id=?",
            (r1.state_json.replace('"Alpha"', '"EVIL"'), r1.revision_id),
        )
        con.commit()
    finally:
        con.close()
    payload = CreativeRenderPayload.model_validate(
        _pinned_payload(workspace, revision_id=r1.revision_id, revision_hash=r1.state_hash)
    )
    from nexus_ai_agent.creative.render_jobs import CreativeRenderError

    with pytest.raises(CreativeRenderError) as excinfo:
        asyncio.run(_resolve_pinned_project(payload))
    assert excinfo.value.code == "revision_unavailable"


def test_resolver_refuses_a_hash_mismatched_pin(pin_env: Path) -> None:
    store = SqlProjectRevisionStore()
    workspace = pin_env / "creative" / "creative_pin_hash"
    workspace.mkdir(parents=True)
    r1 = asyncio.run(_commit(store, _project(), parent=None))
    payload = CreativeRenderPayload.model_validate(
        _pinned_payload(workspace, revision_id=r1.revision_id, revision_hash="sha256:" + "b" * 64)
    )
    from nexus_ai_agent.creative.render_jobs import CreativeRenderError

    with pytest.raises(CreativeRenderError) as excinfo:
        asyncio.run(_resolve_pinned_project(payload))
    assert excinfo.value.code == "revision_unavailable"


def test_payload_requires_a_complete_pin() -> None:
    # revision_id without project_id/revision_hash is not a usable pin.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CreativeRenderPayload.model_validate(
            {
                "command": "edit",
                "operation": "trim",
                "args": [],
                "workspace_dir": "/tmp/x",
                "user_id": 0,
                "chat_id": 0,
                "idempotency_key": "k",
                "revision_id": "sha256:" + "a" * 64,
            }
        )


# ---------------------------------------------------------------------------
# Handoff -> durable queue -> worker pinning (real FFmpeg lane)
# ---------------------------------------------------------------------------


def _wire_directory() -> tuple[Any, str]:
    directory = SQLApplicationDirectory()
    owner_binding = ChannelBinding("cli", "owner")
    owner = directory.resolve_or_create(owner_binding)
    directory.ensure_project(PROJECT_ID, owner, permissions=DEFAULT_PROJECT_PERMISSIONS)
    return owner, channel_binding_id(owner_binding)


@pytest.mark.asyncio
async def test_handoff_payload_pins_the_revision_before_any_media_work(
    pin_env: Path,
) -> None:
    """The durable payload carries the pin -- proven without reaching FFmpeg."""
    owner, binding_id = _wire_directory()
    store = SqlProjectRevisionStore()
    project = _project("admitted")
    revision = await _commit(store, project, parent=None)

    workspace = pin_env / "creative" / "creative_pin_payload"
    workspace.mkdir()
    (workspace / "real_source.mp4").write_bytes(b"staged-bytes")

    actor = ActorIdentity(kind="user", actor_id=str(owner))
    access = ProjectAccess(
        actor=actor, project_id=PROJECT_ID, permissions=DEFAULT_PROJECT_PERMISSIONS
    )
    bus = build_job_bus(project, authorizer=access)
    gateway = build_cognition_gateway(
        bus=bus, actor=actor, project_id=PROJECT_ID, enabled=True, completion=_ScriptedProducer()
    )
    canonical = CanonicalHandoffFacts(
        principal_id=str(owner), channel_binding_id=binding_id, project_id=PROJECT_ID
    )
    queue = InProcessJobQueue(pin_env / "jobs.sqlite3")
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)

    outcome = await run_free_text_intent_durable(
        "trim the first second",
        gateway=gateway,
        queue=queue,
        project=project,
        source_path=str(workspace / "real_source.mp4"),
        workspace_dir=str(workspace),
        user_id=0,
        chat_id=0,
        canonical=canonical,
        revision_store=store,
        admitted_revision=revision,
    )
    assert outcome.status == "enqueued", outcome
    stored = json.loads(str(queue._fetch_row_full(outcome.job_id)["payload_json"]))
    assert stored["revision_id"] == revision.revision_id
    assert stored["revision_hash"] == revision.state_hash
    # A pinned payload round-trips through the worker's own schema.
    typed = CreativeRenderPayload.model_validate(stored)
    assert typed.revision_id == revision.revision_id


@pytest.mark.asyncio
async def test_handoff_refuses_when_no_admitted_revision_is_supplied(
    pin_env: Path,
) -> None:
    """A wired store with no admitted revision refuses BEFORE enqueue -- never 'latest state'."""
    owner, binding_id = _wire_directory()
    store = SqlProjectRevisionStore()  # empty: no head for PROJECT_ID
    project = _project("uncommitted")
    workspace = pin_env / "creative" / "creative_pin_none"
    workspace.mkdir()
    (workspace / "real_source.mp4").write_bytes(b"staged-bytes")

    actor = ActorIdentity(kind="user", actor_id=str(owner))
    access = ProjectAccess(
        actor=actor, project_id=PROJECT_ID, permissions=DEFAULT_PROJECT_PERMISSIONS
    )
    bus = build_job_bus(project, authorizer=access)
    gateway = build_cognition_gateway(
        bus=bus, actor=actor, project_id=PROJECT_ID, enabled=True, completion=_ScriptedProducer()
    )
    canonical = CanonicalHandoffFacts(
        principal_id=str(owner), channel_binding_id=binding_id, project_id=PROJECT_ID
    )
    queue = InProcessJobQueue(pin_env / "jobs.sqlite3")

    outcome = await run_free_text_intent_durable(
        "trim",
        gateway=gateway,
        queue=queue,
        project=project,
        source_path=str(workspace / "real_source.mp4"),
        workspace_dir=str(workspace),
        user_id=0,
        chat_id=0,
        canonical=canonical,
        revision_store=store,
    )
    assert outcome.status == "refused"
    assert outcome.refusal_reason == "revision_unavailable"
    assert queue.job_ids() == []  # nothing was enqueued


@pytest.mark.asyncio
async def test_handoff_pins_the_durable_revision_and_the_worker_executes_it(
    pin_env: Path,
) -> None:
    owner, binding_id = _wire_directory()
    store = SqlProjectRevisionStore()
    project = _project("admitted")
    revision = await _commit(store, project, parent=None)

    workspace = pin_env / "creative" / "creative_pin_e2e"
    workspace.mkdir()
    _clip(workspace / "real_source.mp4")

    actor = ActorIdentity(kind="user", actor_id=str(owner))
    access = ProjectAccess(
        actor=actor, project_id=PROJECT_ID, permissions=DEFAULT_PROJECT_PERMISSIONS
    )
    bus = build_job_bus(project, authorizer=access)
    gateway = build_cognition_gateway(
        bus=bus,
        actor=actor,
        project_id=PROJECT_ID,
        enabled=True,
        completion=_ScriptedProducer(),
    )
    canonical = CanonicalHandoffFacts(
        principal_id=str(owner), channel_binding_id=binding_id, project_id=PROJECT_ID
    )

    queue = InProcessJobQueue(pin_env / "jobs.sqlite3")
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)

    outcome = await run_free_text_intent_durable(
        "trim the first second",
        gateway=gateway,
        queue=queue,
        project=project,
        source_path=str(workspace / "real_source.mp4"),
        workspace_dir=str(workspace),
        user_id=0,
        chat_id=0,
        canonical=canonical,
        revision_store=store,
        admitted_revision=revision,
    )
    assert outcome.status == "enqueued", outcome
    assert outcome.job_id

    # The pin is durable in the queue row, not just in memory.
    row = queue._fetch_row_full(outcome.job_id)
    stored = json.loads(str(row["payload_json"]))
    assert stored["revision_id"] == revision.revision_id
    assert stored["revision_hash"] == revision.state_hash

    # The head moves AFTER enqueue; the job must still run the pinned revision.
    await _commit(store, _project("moved-after-enqueue"), parent=revision.revision_id)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 120
    terminal = {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
    status = await queue.get_status(outcome.job_id)
    while loop.time() < deadline and status not in terminal:
        await asyncio.sleep(0.05)
        status = await queue.get_status(outcome.job_id)
    assert status is JobStatus.COMPLETED
    result = await queue.get_result(outcome.job_id)
    assert result is not None and result["success"] is True


@pytest.mark.asyncio
async def test_handoff_pins_the_admitted_revision_even_after_a_concurrent_head_move(
    pin_env: Path,
) -> None:
    """The admitted revision is pinned deterministically -- never the moved head.

    task-282: the handoff must pin the revision the composition root committed,
    not re-read the live head. The ordering is fully deterministic (no sleeps):
    commit R1 (the admitted revision), then commit R2 so the durable head is R2
    *before* the handoff runs. A handoff that re-read the head would pin R2; the
    contract requires R1.
    """
    owner, binding_id = _wire_directory()
    store = SqlProjectRevisionStore()
    project = _project("admitted")
    admitted = await _commit(store, project, parent=None)
    # A concurrent writer wins the head BEFORE the handoff is called.
    newer = await _commit(store, _project("concurrent-winner"), parent=admitted.revision_id)
    assert newer.revision_id != admitted.revision_id
    head = await store.load_head(PROJECT_ID)
    assert head is not None and head.revision_id == newer.revision_id  # the head really moved

    workspace = pin_env / "creative" / "creative_pin_race"
    workspace.mkdir()
    _clip(workspace / "real_source.mp4")

    actor = ActorIdentity(kind="user", actor_id=str(owner))
    access = ProjectAccess(
        actor=actor, project_id=PROJECT_ID, permissions=DEFAULT_PROJECT_PERMISSIONS
    )
    bus = build_job_bus(project, authorizer=access)
    gateway = build_cognition_gateway(
        bus=bus, actor=actor, project_id=PROJECT_ID, enabled=True, completion=_ScriptedProducer()
    )
    canonical = CanonicalHandoffFacts(
        principal_id=str(owner), channel_binding_id=binding_id, project_id=PROJECT_ID
    )
    queue = InProcessJobQueue(pin_env / "jobs.sqlite3")

    outcome = await run_free_text_intent_durable(
        "trim the first second",
        gateway=gateway,
        queue=queue,
        project=project,
        source_path=str(workspace / "real_source.mp4"),
        workspace_dir=str(workspace),
        user_id=0,
        chat_id=0,
        canonical=canonical,
        revision_store=store,
        admitted_revision=admitted,
    )
    assert outcome.status == "enqueued", outcome
    stored = json.loads(str(queue._fetch_row_full(outcome.job_id)["payload_json"]))
    # The pin is the ADMITTED revision R1 -- not the newer head R2.
    assert stored["revision_id"] == admitted.revision_id
    assert stored["revision_hash"] == admitted.state_hash
    assert stored["revision_id"] != newer.revision_id


def test_worker_refuses_when_the_pinned_revision_is_gone_though_head_exists(
    pin_env: Path,
) -> None:
    owner, binding_id = _wire_directory()
    store = SqlProjectRevisionStore()
    project = _project("admitted")
    revision = asyncio.run(_commit(store, project, parent=None))

    workspace = pin_env / "creative" / "creative_pin_gone"
    workspace.mkdir()
    _clip(workspace / "input.mp4")

    payload = _pinned_payload(
        workspace, revision_id=revision.revision_id, revision_hash=revision.state_hash
    )
    # Admit a second revision so the project head is healthy...
    asyncio.run(_commit(store, _project("new-head"), parent=revision.revision_id))
    # ...then remove the pinned revision out of band.
    db = pin_env / "app.sqlite"
    con = sqlite3.connect(str(db))
    try:
        con.execute("DELETE FROM projectrevision WHERE revision_id=?", (revision.revision_id,))
        con.commit()
    finally:
        con.close()

    result = asyncio.run(creative_render_job(payload))
    assert result["success"] is False
    assert result["error_code"] == "revision_unavailable"


# ---------------------------------------------------------------------------
# Slice 2 — durable artifact → revision binding (fail-closed re-verification)
# ---------------------------------------------------------------------------


def _binding_store() -> Any:
    from nexus_ai_agent.creative.project_state.store import SqlArtifactBindingStore

    return SqlArtifactBindingStore()


def test_bind_records_artifact_against_the_exact_revision(pin_env: Path) -> None:
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    artifact = pin_env / "art.bin"
    artifact.write_bytes(b"published-bytes")
    from nexus_ai_agent.creative.project_state.store import content_address_of_file

    artifact_id = content_address_of_file(artifact)
    bindings = _binding_store()
    bound = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-1",
            artifact_kind="video",
            size_bytes=artifact.stat().st_size,
        )
    )
    assert bound.revision_id == r1.revision_id
    assert bound.artifact_id == artifact_id
    # Re-verification reads the SAME bytes and re-validates the revision.
    verified = asyncio.run(
        bindings.verify_artifact(
            project_id=PROJECT_ID, artifact_id=artifact_id, artifact_path=artifact
        )
    )
    assert verified.revision_id == r1.revision_id
    assert asyncio.run(bindings.list_for_revision(PROJECT_ID, r1.revision_id))[0].artifact_id == (
        artifact_id
    )


def test_bind_is_idempotent_for_the_same_revision(pin_env: Path) -> None:
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    artifact = pin_env / "art.bin"
    artifact.write_bytes(b"same")
    from nexus_ai_agent.creative.project_state.store import content_address_of_file

    artifact_id = content_address_of_file(artifact)
    bindings = _binding_store()
    for _ in range(2):
        bound = asyncio.run(
            bindings.bind(
                project_id=PROJECT_ID,
                revision_id=r1.revision_id,
                artifact_id=artifact_id,
                size_bytes=3,
            )
        )
        assert bound.revision_id == r1.revision_id


def test_bind_refuses_repointing_an_artifact_to_another_revision(pin_env: Path) -> None:
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    r2 = asyncio.run(_commit(store, _project("rev2"), parent=r1.revision_id))
    artifact = pin_env / "art.bin"
    artifact.write_bytes(b"bytes")
    from nexus_ai_agent.creative.project_state.models import ArtifactBindingConflict
    from nexus_ai_agent.creative.project_state.store import content_address_of_file

    artifact_id = content_address_of_file(artifact)
    bindings = _binding_store()
    asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID, revision_id=r1.revision_id, artifact_id=artifact_id, size_bytes=5
        )
    )
    with pytest.raises(ArtifactBindingConflict):
        asyncio.run(
            bindings.bind(
                project_id=PROJECT_ID,
                revision_id=r2.revision_id,
                artifact_id=artifact_id,
                size_bytes=5,
            )
        )


def test_bind_refuses_an_unknown_revision(pin_env: Path) -> None:
    store = SqlProjectRevisionStore()
    asyncio.run(_commit(store, _project("rev1"), parent=None))
    artifact = pin_env / "art.bin"
    artifact.write_bytes(b"bytes")
    from nexus_ai_agent.creative.project_state.models import RevisionNotFound
    from nexus_ai_agent.creative.project_state.store import content_address_of_file

    artifact_id = content_address_of_file(artifact)
    bindings = _binding_store()
    with pytest.raises(RevisionNotFound):
        asyncio.run(
            bindings.bind(
                project_id=PROJECT_ID,
                revision_id="sha256:" + "c" * 64,
                artifact_id=artifact_id,
                size_bytes=5,
            )
        )


def test_verify_refuses_when_the_artifact_bytes_changed(pin_env: Path) -> None:
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    artifact = pin_env / "art.bin"
    artifact.write_bytes(b"original")
    from nexus_ai_agent.creative.project_state.models import ArtifactRevisionMismatch
    from nexus_ai_agent.creative.project_state.store import content_address_of_file

    artifact_id = content_address_of_file(artifact)
    bindings = _binding_store()
    asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID, revision_id=r1.revision_id, artifact_id=artifact_id, size_bytes=8
        )
    )
    # The artifact is replaced out of band: re-verification must fail closed.
    artifact.write_bytes(b"tampered")
    with pytest.raises(ArtifactRevisionMismatch):
        asyncio.run(
            bindings.verify_artifact(
                project_id=PROJECT_ID, artifact_id=artifact_id, artifact_path=artifact
            )
        )


def test_verify_refuses_when_the_bound_revision_was_tampered(pin_env: Path) -> None:
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("Alpha"), parent=None))
    artifact = pin_env / "art.bin"
    artifact.write_bytes(b"bytes")
    from nexus_ai_agent.creative.project_state.models import RevisionIntegrityError
    from nexus_ai_agent.creative.project_state.store import content_address_of_file

    artifact_id = content_address_of_file(artifact)
    bindings = _binding_store()
    asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID, revision_id=r1.revision_id, artifact_id=artifact_id, size_bytes=5
        )
    )
    db = pin_env / "app.sqlite"
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "UPDATE projectrevision SET state_json=? WHERE revision_id=?",
            (r1.state_json.replace('"Alpha"', '"EVIL"'), r1.revision_id),
        )
        con.commit()
    finally:
        con.close()
    with pytest.raises(RevisionIntegrityError):
        asyncio.run(
            bindings.verify_artifact(
                project_id=PROJECT_ID, artifact_id=artifact_id, artifact_path=artifact
            )
        )


def test_bind_refuses_cross_project_artifact_reuse(pin_env: Path) -> None:
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    other = "other-project"
    other_project = _project("other").model_copy(update={"project_id": other})
    from nexus_ai_agent.creative.studio.models import compute_state_hash

    other_project.state_hash = compute_state_hash(other_project)
    r_other = asyncio.run(
        store.commit(other_project, authorized_project_id=other, expected_parent_revision_id=None)
    )
    artifact = pin_env / "art.bin"
    artifact.write_bytes(b"bytes")
    from nexus_ai_agent.creative.project_state.models import CrossProjectAccess
    from nexus_ai_agent.creative.project_state.store import content_address_of_file

    artifact_id = content_address_of_file(artifact)
    bindings = _binding_store()
    asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID, revision_id=r1.revision_id, artifact_id=artifact_id, size_bytes=5
        )
    )
    # The same artifact cannot be re-bound through another project.
    with pytest.raises(CrossProjectAccess):
        asyncio.run(
            bindings.bind(
                project_id=other,
                revision_id=r_other.revision_id,
                artifact_id=artifact_id,
                size_bytes=5,
            )
        )
    # Nor resolved through another project.
    with pytest.raises(CrossProjectAccess):
        asyncio.run(bindings.resolve(other, artifact_id))


@pytest.mark.asyncio
async def test_pinned_job_records_intent_then_confirm_commits_the_binding(
    pin_env: Path,
) -> None:
    """The worker records the binding intent; the queue confirm hook commits it.

    P0 fix (task-282 Slice 2 residual 1): the worker must NOT commit the binding
    itself (it runs before the queue's independent verification + publication).
    It records a *measured* intent; only the post-publication confirm hook
    commits the durable row.
    """
    from nexus_ai_agent.creative.render_jobs import creative_render_binding_confirm

    owner, binding_id = _wire_directory()
    store = SqlProjectRevisionStore()
    project = _project("admitted")
    revision = await _commit(store, project, parent=None)
    workspace = pin_env / "creative" / "creative_bind_e2e"
    workspace.mkdir()
    _clip(workspace / "input.mp4")
    payload = _pinned_payload(
        workspace,
        revision_id=revision.revision_id,
        revision_hash=revision.state_hash,
        principal_id=str(owner),
        channel_binding_id=binding_id,
    )
    result = await creative_render_job(payload)
    assert result["success"] is True, result
    intent = result["artifact_binding"]
    assert intent["revision_id"] == revision.revision_id
    assert intent["artifact_id"] == result["sha256"]
    # The worker recorded ONLY an intent — nothing durable yet.
    bindings = _binding_store()
    from nexus_ai_agent.creative.project_state.models import ArtifactNotFound

    with pytest.raises(ArtifactNotFound):
        await bindings.resolve(PROJECT_ID, result["sha256"])
    # The queue's post-publication confirm hook commits the durable binding.
    await creative_render_binding_confirm(payload, {**result, "_queue_job_id": "job-42"})
    verified = await bindings.verify_artifact(
        project_id=PROJECT_ID,
        artifact_id=result["sha256"],
        artifact_path=Path(result["artifact_path"]),
    )
    assert verified.revision_id == revision.revision_id
    assert verified.job_id == "job-42"


def test_unpinned_job_publishes_no_binding(pin_env: Path) -> None:
    """A legacy (unpinned) job has no revision to bind to — a clean no-op."""
    workspace = pin_env / "creative" / "creative_bind_unpinned"
    workspace.mkdir()
    _clip(workspace / "input.mp4")
    payload = {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "input_sha256": sha256_file(workspace / "input.mp4"),
        "media_duration_us": SOURCE_DURATION_US,
        "user_id": 0,
        "chat_id": 0,
        "lang": "en",
        "idempotency_key": "unpinned:trim:1",
    }
    result = asyncio.run(creative_render_job(payload))
    assert result["success"] is True, result
    assert "artifact_binding" not in result


@pytest.mark.asyncio
async def test_binding_confirm_refuses_a_conflict_fail_closed(
    pin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binding conflict in the confirm hook is a typed, fail-closed refusal."""
    from nexus_ai_agent.creative.project_state.models import ArtifactBindingConflict
    from nexus_ai_agent.creative.project_state.store import SqlArtifactBindingStore
    from nexus_ai_agent.creative.render_jobs import (
        CreativeRenderError,
        creative_render_binding_confirm,
    )

    owner, binding_id = _wire_directory()
    store = SqlProjectRevisionStore()
    project = _project("admitted")
    revision = await _commit(store, project, parent=None)
    workspace = pin_env / "creative" / "creative_bind_failclosed"
    workspace.mkdir()
    _clip(workspace / "input.mp4")
    payload = _pinned_payload(
        workspace,
        revision_id=revision.revision_id,
        revision_hash=revision.state_hash,
        principal_id=str(owner),
        channel_binding_id=binding_id,
    )
    result = await creative_render_job(payload)
    assert result["success"] is True, result

    async def _refuse(self: Any, **kwargs: Any) -> Any:
        raise ArtifactBindingConflict("forced for test")

    monkeypatch.setattr(SqlArtifactBindingStore, "bind", _refuse)
    with pytest.raises(CreativeRenderError) as exc:
        await creative_render_binding_confirm(payload, {**result, "_queue_job_id": "j"})
    assert exc.value.code == "artifact_binding_failed"


# ---------------------------------------------------------------------------
# P0 (D-0038 residual 1): the binding is committed by the queue's confirm hook,
# AFTER independent verification + publication, and only for a job that will
# become COMPLETED. A refused confirmation fails the job and leaves no binding.
# ---------------------------------------------------------------------------


def _queue_with_binding_publication(tmp_path: Path, revision: Any) -> InProcessJobQueue:
    """A real queue wired with the creative_render publication (incl. confirm)."""
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication
    from nexus_ai_agent.jobs.verification import VerificationOutcome

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    queue = InProcessJobQueue(str(tmp_path / "binding_queue.sqlite3"))

    async def _handler(payload: dict) -> dict:
        workspace = Path(str(payload["workspace_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        artifact = workspace / "render.mp4"
        artifact.write_bytes(b"queue-confirm-bytes")
        digest = sha256_file(artifact)
        return {
            "success": True,
            "artifact_path": str(artifact),
            "workspace_dir": str(workspace),
            "sha256": digest,
            "size_bytes": artifact.stat().st_size,
            "artifact_kind": "binary",
            "artifact_binding": {
                "artifact_id": digest,
                "project_id": PROJECT_ID,
                "revision_id": revision.revision_id,
                "artifact_kind": "binary",
                "size_bytes": artifact.stat().st_size,
                "artifact_version_id": None,
            },
        }

    queue.register_handler("creative_render", _handler)
    queue.register_artifact_verifier(
        "creative_render", lambda p, r: VerificationOutcome(ok=True, reason_code=None, summary={})
    )
    queue.register_artifact_publication(
        "creative_render", build_creative_render_publication(artifact_root)
    )
    return queue


async def _await_terminal(
    queue: InProcessJobQueue, job_id: str, timeout: float = 30.0
) -> JobStatus:
    terminal = {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    status = await queue.get_status(job_id)
    while loop.time() < deadline and status not in terminal:
        await asyncio.sleep(0.05)
        status = await queue.get_status(job_id)
    return status


@pytest.mark.asyncio
async def test_queue_confirm_commits_binding_after_success(pin_env: Path) -> None:
    """A verified + published pinned job commits its binding, then COMPLETES."""
    store = SqlProjectRevisionStore()
    revision = await _commit(store, _project("admitted"), parent=None)
    queue = _queue_with_binding_publication(pin_env, revision)

    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key="confirm-ok-1",
        payload={"workspace_dir": str(pin_env / "ws_confirm_ok")},
    )
    assert await _await_terminal(queue, job_id) is JobStatus.COMPLETED
    result = await queue.get_result(job_id)
    assert result is not None and result["success"] is True
    artifact_id = result["artifact_binding"]["artifact_id"]
    binding = await _binding_store().resolve(PROJECT_ID, artifact_id)
    assert binding.revision_id == revision.revision_id
    assert binding.job_id == job_id


@pytest.mark.asyncio
async def test_queue_confirm_refusal_fails_the_job_without_a_binding(
    pin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused confirmation fails the job closed and leaves NO durable binding."""
    from nexus_ai_agent.creative.project_state.models import ArtifactBindingConflict
    from nexus_ai_agent.creative.project_state.store import (
        ArtifactNotFound,
        SqlArtifactBindingStore,
    )

    store = SqlProjectRevisionStore()
    revision = await _commit(store, _project("admitted"), parent=None)
    queue = _queue_with_binding_publication(pin_env, revision)

    async def _refuse(self: Any, **kwargs: Any) -> Any:
        raise ArtifactBindingConflict("forced for test")

    monkeypatch.setattr(SqlArtifactBindingStore, "bind", _refuse)
    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key="confirm-refuse-1",
        payload={"workspace_dir": str(pin_env / "ws_confirm_refuse")},
    )
    status = await _await_terminal(queue, job_id)
    assert status in (JobStatus.FAILED_TERMINAL, JobStatus.FAILED_RETRYABLE)
    # No binding row exists for the artifact this attempt measured.
    from nexus_ai_agent.creative.project_state.store import content_address_of_bytes

    artifact_id = content_address_of_bytes(b"queue-confirm-bytes")
    with pytest.raises(ArtifactNotFound):
        await _binding_store().resolve(PROJECT_ID, artifact_id)
    # And the refused attempt's published bytes were retracted, not left behind.
    published = pin_env / "artifacts" / "published" / job_id / "render.mp4"
    assert not published.exists()


@pytest.mark.asyncio
async def test_queue_confirm_refuses_a_superseded_attempt_at_the_commit_boundary(
    pin_env: Path,
) -> None:
    """A superseded attempt commits no binding: the fence is re-read before confirm.

    Deterministic fault injection: the verifier bumps the row's fencing token
    (simulating a takeover) during the *re-probe* window, i.e. after the
    pre-publication fence read and before the confirmation commit.  The queue
    must re-read ownership at the commit boundary and refuse to write any
    durable binding, so a stale execution can never commit provenance.
    """
    import sqlite3 as _sqlite3

    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
    from nexus_ai_agent.creative.project_state.store import (
        ArtifactNotFound,
        content_address_of_bytes,
    )
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication
    from nexus_ai_agent.jobs.verification import VerificationOutcome

    store = SqlProjectRevisionStore()
    revision = await _commit(store, _project("admitted"), parent=None)

    artifact_root = pin_env / "artifacts"
    artifact_root.mkdir()
    queue_path = pin_env / "fence_queue.sqlite3"
    queue = InProcessJobQueue(str(queue_path))

    async def _handler(payload: dict) -> dict:
        workspace = Path(str(payload["workspace_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        artifact = workspace / "render.mp4"
        artifact.write_bytes(b"queue-confirm-bytes")
        digest = sha256_file(artifact)
        return {
            "success": True,
            "artifact_path": str(artifact),
            "workspace_dir": str(workspace),
            "sha256": digest,
            "size_bytes": artifact.stat().st_size,
            "artifact_kind": "binary",
            "artifact_binding": {
                "artifact_id": digest,
                "project_id": PROJECT_ID,
                "revision_id": revision.revision_id,
                "artifact_kind": "binary",
                "size_bytes": artifact.stat().st_size,
                "artifact_version_id": None,
            },
        }

    def _supersede() -> None:
        connection = _sqlite3.connect(str(queue_path), timeout=30)
        try:
            connection.execute(
                "UPDATE nexus_job_queue SET attempt = attempt + 1 "
                "WHERE job_type = 'creative_render'"
            )
            connection.commit()
        finally:
            connection.close()

    calls = {"n": 0}

    def _verifier(payload: dict, result: dict) -> VerificationOutcome:
        calls["n"] += 1
        if calls["n"] == 2:  # the re-probe, after publication, before confirm
            _supersede()
        return VerificationOutcome(ok=True, reason_code=None, summary={})

    queue.register_handler("creative_render", _handler)
    queue.register_artifact_verifier("creative_render", _verifier)
    queue.register_artifact_publication(
        "creative_render", build_creative_render_publication(artifact_root)
    )

    # The fence re-check must refuse *before* the confirmation runs, so the
    # confirm hook must never write a binding at all — a distinct observable
    # from "the row was written and then retracted by the lost-CAS path".  Count
    # the store writes to pin that distinction.
    from nexus_ai_agent.creative.project_state.store import SqlArtifactBindingStore

    binds = {"n": 0}
    real_bind = SqlArtifactBindingStore.bind

    async def _counting_bind(self: Any, **kwargs: Any) -> Any:
        binds["n"] += 1
        return await real_bind(self, **kwargs)

    SqlArtifactBindingStore.bind = _counting_bind  # type: ignore[method-assign]
    try:
        job_id = await queue.enqueue(
            job_type="creative_render",
            idempotency_key="fence-supersede-1",
            payload={"workspace_dir": str(pin_env / "ws_fence")},
        )

        # Publication is the step immediately before the confirmation boundary.
        published = artifact_root / "published" / job_id / "render.mp4"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20.0
        while loop.time() < deadline and not published.exists():
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)  # let the (refused) confirm boundary settle
    finally:
        SqlArtifactBindingStore.bind = real_bind  # type: ignore[method-assign]

    # The fence was re-read and refused the superseded attempt before the
    # confirmation: no binding write was ever attempted.
    assert binds["n"] == 0, "the boundary fence must refuse before the confirm hook runs"
    # The superseded attempt owns nothing: it reached no COMPLETED and wrote no
    # durable binding.  (The bytes it published are the successor's to reconcile.)
    assert await queue.get_status(job_id) is not JobStatus.COMPLETED
    artifact_id = content_address_of_bytes(b"queue-confirm-bytes")
    with pytest.raises(ArtifactNotFound):
        await _binding_store().resolve(PROJECT_ID, artifact_id)


@pytest.mark.asyncio
async def test_crash_after_confirm_before_completed_recovers_honestly(pin_env: Path) -> None:
    """Crash after the binding commit but before COMPLETED: the documented model.

    Deterministic fault injection: ``_mark_completed`` is made to crash (a
    non-``Exception`` base so it is a genuine process death, not a handled
    failure) exactly once, after the confirmation committed the binding.  The
    row stays ``verifying`` with a queue-owned heartbeat, so startup recovery
    (``resume_pending``) takes it over and re-runs the job.  The consistency
    model is honest:

    * the binding is a durable record of bytes that WERE published — it is
      append-only history and is not deleted by the later failure;
    * the job is NOT COMPLETED (no false success);
    * recovery re-runs the attempt; here the re-run fails, and the final state
      is a failure — the surviving binding never claims a completed job.
    """

    class _SimulatedCrash(BaseException):
        """A process death: not an ``Exception``, so no handler swallows it."""

    from nexus_ai_agent.creative.project_state.store import content_address_of_bytes

    store = SqlProjectRevisionStore()
    revision = await _commit(store, _project("admitted"), parent=None)
    queue = _queue_with_binding_publication(pin_env, revision)

    orig_mark_completed = queue._mark_completed
    crashed = {"done": False}

    def _crash_once(*args: Any, **kwargs: Any) -> bool:
        if not crashed["done"]:
            crashed["done"] = True
            raise _SimulatedCrash("simulated crash after confirm, before COMPLETED")
        return orig_mark_completed(*args, **kwargs)

    # Second run fails the job (the retry after the crash).
    calls = {"n": 0}
    base_handler = queue._handlers["creative_render"]

    async def _flaky(payload: dict) -> dict:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("retry after crash fails")
        return await base_handler(payload)

    queue.register_handler("creative_render", _flaky)
    queue._mark_completed = _crash_once  # type: ignore[method-assign]

    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key="crash-after-confirm-1",
        payload={"workspace_dir": str(pin_env / "ws_crash")},
    )
    task = queue._tasks.get(job_id)
    assert task is not None
    try:
        await asyncio.wait_for(task, timeout=30)
    except _SimulatedCrash:
        pass
    assert crashed["done"] is True

    # The confirmation committed the binding before the crash; the job is not
    # COMPLETED and the published bytes remain.
    artifact_id = content_address_of_bytes(b"queue-confirm-bytes")
    binding = await _binding_store().resolve(PROJECT_ID, artifact_id)
    assert binding.revision_id == revision.revision_id
    assert await queue.get_status(job_id) is not JobStatus.COMPLETED
    published = pin_env / "artifacts" / "published" / job_id / "render.mp4"
    assert published.exists()

    # Startup recovery takes the unfinished row over and re-runs it.
    reclaimed = await queue.resume_pending()
    assert job_id in reclaimed
    assert await _await_terminal(queue, job_id) in (
        JobStatus.FAILED_TERMINAL,
        JobStatus.FAILED_RETRYABLE,
    )
    # The binding survives as honest history (append-only); it never claims a
    # completed job, because the job is not completed.
    still = await _binding_store().resolve(PROJECT_ID, artifact_id)
    assert still.revision_id == revision.revision_id


# ---------------------------------------------------------------------------
# F1 ownership fence: a takeover landing *inside* the confirm commit window
# (after the boundary fence re-check, at/around the binding write) must not
# leave a durable binding from a superseded attempt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_takeover_inside_the_confirm_window_retracts_the_binding(
    pin_env: Path,
) -> None:
    """A takeover *inside* the confirm commit window leaves no binding behind.

    This is the exact interleaving the earlier boundary re-check cannot cover:
    the attempt passes the pre-confirm fence re-check, the confirm hook commits
    the binding, and *then* a takeover bumps the fencing token so the terminal
    ``COMPLETED`` CAS fails.  The queue must retract exactly the row this
    attempt wrote (scoped by the queue-minted ``(job_id, attempt)`` token), so
    no superseded execution leaves durable provenance and no job reaches
    ``COMPLETED`` without a binding.
    """
    import sqlite3 as _sqlite3

    from nexus_ai_agent.creative.project_state.store import (
        ArtifactNotFound,
        SqlArtifactBindingStore,
        content_address_of_bytes,
    )
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication
    from nexus_ai_agent.jobs.verification import VerificationOutcome

    store = SqlProjectRevisionStore()
    revision = await _commit(store, _project("admitted"), parent=None)

    artifact_root = pin_env / "artifacts"
    artifact_root.mkdir()
    queue_path = pin_env / "inside_window_queue.sqlite3"
    queue = InProcessJobQueue(str(queue_path))

    async def _handler(payload: dict) -> dict:
        workspace = Path(str(payload["workspace_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        artifact = workspace / "render.mp4"
        artifact.write_bytes(b"queue-confirm-bytes")
        digest = sha256_file(artifact)
        return {
            "success": True,
            "artifact_path": str(artifact),
            "workspace_dir": str(workspace),
            "sha256": digest,
            "size_bytes": artifact.stat().st_size,
            "artifact_kind": "binary",
            "artifact_binding": {
                "artifact_id": digest,
                "project_id": PROJECT_ID,
                "revision_id": revision.revision_id,
                "artifact_kind": "binary",
                "size_bytes": artifact.stat().st_size,
                "artifact_version_id": None,
            },
        }

    def _takeover_inside_window() -> None:
        connection = _sqlite3.connect(str(queue_path), timeout=30)
        try:
            connection.execute(
                "UPDATE nexus_job_queue SET attempt = attempt + 1 "
                "WHERE job_type = 'creative_render'"
            )
            connection.commit()
        finally:
            connection.close()

    real_bind = SqlArtifactBindingStore.bind
    fired = {"n": 0}

    async def _bind_then_takeover(self: Any, **kwargs: Any) -> Any:
        # Fire the takeover exactly once, right before the real binding commit —
        # i.e. after the queue's boundary fence re-check and inside the confirm
        # commit window (the precise window Sourcery flagged).
        if fired["n"] == 0:
            fired["n"] = 1
            _takeover_inside_window()
        return await real_bind(self, **kwargs)

    queue.register_handler("creative_render", _handler)
    queue.register_artifact_verifier(
        "creative_render",
        lambda p, r: VerificationOutcome(ok=True, reason_code=None, summary={}),
    )
    queue.register_artifact_publication(
        "creative_render", build_creative_render_publication(artifact_root)
    )

    SqlArtifactBindingStore.bind = _bind_then_takeover  # type: ignore[method-assign]
    try:
        job_id = await queue.enqueue(
            job_type="creative_render",
            idempotency_key="fence-inside-window-1",
            payload={"workspace_dir": str(pin_env / "ws_inside_window")},
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20.0
        status = await queue.get_status(job_id)
        terminal = {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
        while loop.time() < deadline and status not in terminal:
            await asyncio.sleep(0.05)
            status = await queue.get_status(job_id)
        await asyncio.sleep(0.5)
    finally:
        SqlArtifactBindingStore.bind = real_bind  # type: ignore[method-assign]

    assert fired["n"] == 1, "the takeover must have fired inside the confirm window"
    # The superseded attempt never reached COMPLETED and left NO binding: the
    # confirmation it committed was retracted under its own fencing token.
    assert status is not JobStatus.COMPLETED
    artifact_id = content_address_of_bytes(b"queue-confirm-bytes")
    with pytest.raises(ArtifactNotFound):
        await _binding_store().resolve(PROJECT_ID, artifact_id)


def test_retract_is_token_scoped_and_never_deletes_a_successor_binding(
    pin_env: Path,
) -> None:
    """``retract`` deletes only the row an exact ``(job_id, attempt)`` wrote."""
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    bindings = _binding_store()
    artifact_id = "sha256:" + "1" * 64

    # The successor (attempt 7) owns the binding.
    asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-x",
            size_bytes=1,
            attempt=7,
        )
    )
    # A superseded attempt (6) retracting cannot remove the successor's row.
    removed = asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-x", attempt=6))
    assert removed is False
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id)).attempt == 7
    # A different job cannot remove it either.
    removed = asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-y", attempt=7))
    assert removed is False
    # The exact owner removes exactly its own row.
    removed = asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-x", attempt=7))
    assert removed is True
    from nexus_ai_agent.creative.project_state.store import ArtifactNotFound

    with pytest.raises(ArtifactNotFound):
        asyncio.run(bindings.resolve(PROJECT_ID, artifact_id))
    # Idempotent: a second retract removes nothing.
    assert (
        asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-x", attempt=7)) is False
    )


def test_retract_requires_a_real_fencing_token(pin_env: Path) -> None:
    """A token-less retraction is refused: it could remove a pre-fence row."""
    from nexus_ai_agent.creative.project_state.models import ArtifactBindingError

    bindings = _binding_store()
    with pytest.raises(ArtifactBindingError):
        asyncio.run(bindings.retract(artifact_id="sha256:" + "2" * 64, job_id="j", attempt=0))


def test_bind_advances_the_fencing_token_monotonically(pin_env: Path) -> None:
    """A re-bind by a newer attempt advances the token; provenance is unchanged."""
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    bindings = _binding_store()
    artifact_id = "sha256:" + "3" * 64

    first = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-z",
            size_bytes=1,
            attempt=4,
        )
    )
    assert first.attempt == 4
    # Same attempt: no-op, token unchanged.
    again = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-z",
            size_bytes=1,
            attempt=4,
        )
    )
    assert again.attempt == 4
    # Newer attempt: token advances so a stale retraction cannot match it.
    newer = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-z",
            size_bytes=1,
            attempt=9,
        )
    )
    assert newer.attempt == 9
    assert newer.revision_id == r1.revision_id  # provenance never rewritten
    # A stale (older) re-bind must not regress the token a newer attempt owns.
    stale = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-z",
            size_bytes=1,
            attempt=5,
        )
    )
    assert stale.attempt == 9  # monotonic: the higher token is retained
    # A stale (older) retraction cannot remove the row the newer attempt owns.
    assert (
        asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-z", attempt=4)) is False
    )


def test_two_independent_jobs_sharing_bytes_are_fenced_apart(pin_env: Path) -> None:
    """Adversarial: two independent jobs, one artifact_id, one revision.

    The fencing token in the current queue is PER-JOB, so a comparison on
    ``artifact_id`` alone is not sufficient.  Job A binds the artifact (owning
    the canonical row).  Job B — an independent execution that produced the
    *same bytes* — must not be able to seize ownership by presenting a higher
    token, and must never be able to retract job A's binding.  The second bind
    is an explicit, cross-job idempotent no-op: the row keeps job A's identity.
    """
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    bindings = _binding_store()
    artifact_id = "sha256:" + "5" * 64

    # Job A owns the row at token 2.
    owned = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-A",
            size_bytes=1,
            attempt=2,
        )
    )
    assert (owned.job_id, owned.attempt) == ("job-A", 2)

    # Job B, an independent job with the same bytes and a HIGHER token, must be a
    # cross-job no-op — not a takeover.
    second = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-B",
            size_bytes=1,
            attempt=7,
        )
    )
    assert (second.job_id, second.attempt) == ("job-A", 2), (
        "a different job must never seize ownership of another job's binding"
    )

    # The canonical row still belongs to job A.
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id)).job_id == "job-A"

    # Job B's retraction (its own job_id AND attempt) is invalid — it must not
    # delete the binding job A wrote.
    assert (
        asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-B", attempt=7)) is False
    )
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id)).job_id == "job-A"

    # Job B's retry at its own token is likewise a no-op (never its row).
    assert (
        asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-B", attempt=2)) is False
    )
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id)).job_id == "job-A"

    # Only the owner's exact (job_id, attempt) retracts.
    assert asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-A", attempt=2)) is True


def test_bind_returns_the_authoritative_stored_row(pin_env: Path) -> None:
    """The returned binding reflects the committed row, not stale ORM state.

    A cross-job re-bind is a no-op and must report the *stored* owner/attempt;
    the authoritative re-read makes the result exactly what a fresh reader sees.
    """
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    bindings = _binding_store()
    artifact_id = "sha256:" + "6" * 64
    asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="owner",
            artifact_kind="video",
            size_bytes=100,
            attempt=1,
        )
    )
    replayed = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="other",
            artifact_kind="audio",
            size_bytes=999,
            attempt=50,
        )
    )
    resolved = asyncio.run(bindings.resolve(PROJECT_ID, artifact_id))
    assert (replayed.job_id, replayed.attempt) == (resolved.job_id, resolved.attempt)
    assert replayed.job_id == "owner"
    assert replayed.attempt == 1


def test_concurrent_rebinds_never_regress_the_fencing_token(pin_env: Path) -> None:
    """Two racing re-binds must leave the *higher* token, never the lower.

    The token advance is a single conditional UPDATE, not a read-modify-write:
    a superseded attempt (lower token) racing a successor (higher token) must
    never let the lower token commit last, which would reopen the F1 hole (a
    later retract scoped to that lower token could delete a successor's row).
    """
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    bindings = _binding_store()
    artifact_id = "sha256:" + "4" * 64

    async def _race() -> None:
        await asyncio.gather(
            bindings.bind(
                project_id=PROJECT_ID,
                revision_id=r1.revision_id,
                artifact_id=artifact_id,
                job_id="job-race",
                size_bytes=1,
                attempt=5,
            ),
            bindings.bind(
                project_id=PROJECT_ID,
                revision_id=r1.revision_id,
                artifact_id=artifact_id,
                job_id="job-race",
                size_bytes=1,
                attempt=3,
            ),
        )

    asyncio.run(_race())
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id)).attempt == 5
    # The winner's retraction still works; the loser's cannot.
    assert (
        asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-race", attempt=3))
        is False
    )
    assert (
        asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-race", attempt=5)) is True
    )


# ---------------------------------------------------------------------------
# Slice 2 hardening (PR #319 review threads): identity, re-measure, metadata
# ---------------------------------------------------------------------------


def test_bind_refuses_a_non_hex_content_address(pin_env: Path) -> None:
    """A same-length non-hex digest can never match a file: refuse it up front."""
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    from nexus_ai_agent.creative.project_state.models import ArtifactBindingError

    bindings = _binding_store()
    with pytest.raises(ArtifactBindingError):
        asyncio.run(
            bindings.bind(
                project_id=PROJECT_ID,
                revision_id=r1.revision_id,
                artifact_id="sha256:" + "z" * 64,  # right length, not hex
                size_bytes=1,
            )
        )


def test_verify_artifact_missing_file_is_a_typed_refusal(pin_env: Path) -> None:
    """A vanished artifact is a typed mismatch, never a raw OSError."""
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    artifact = pin_env / "art.bin"
    artifact.write_bytes(b"bytes")
    from nexus_ai_agent.creative.project_state.models import ArtifactRevisionMismatch
    from nexus_ai_agent.creative.project_state.store import content_address_of_file

    artifact_id = content_address_of_file(artifact)
    bindings = _binding_store()
    asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID, revision_id=r1.revision_id, artifact_id=artifact_id, size_bytes=5
        )
    )
    artifact.unlink()
    with pytest.raises(ArtifactRevisionMismatch):
        asyncio.run(
            bindings.verify_artifact(
                project_id=PROJECT_ID, artifact_id=artifact_id, artifact_path=artifact
            )
        )


def test_bind_records_and_returns_the_asset_version_link(pin_env: Path) -> None:
    """The binding carries the immutable Asset Library version when supplied."""
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    artifact = pin_env / "art.bin"
    artifact.write_bytes(b"bytes")
    from nexus_ai_agent.creative.project_state.store import content_address_of_file

    artifact_id = content_address_of_file(artifact)
    bindings = _binding_store()
    bound = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            size_bytes=5,
            artifact_version_id="ver-123",
        )
    )
    assert bound.artifact_version_id == "ver-123"
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id)).artifact_version_id == "ver-123"


def test_bind_replay_does_not_rewrite_stored_metadata(pin_env: Path) -> None:
    """A same-revision replay is a read-only no-op; the original row is kept."""
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    artifact = pin_env / "art.bin"
    artifact.write_bytes(b"bytes")
    from nexus_ai_agent.creative.project_state.store import content_address_of_file

    artifact_id = content_address_of_file(artifact)
    bindings = _binding_store()
    first = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="first",
            artifact_kind="video",
            size_bytes=100,
        )
    )
    # Replay with DIFFERENT metadata: the stored row must be unchanged.
    replay = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="second",
            artifact_kind="audio",
            size_bytes=999,
        )
    )
    assert replay.job_id == "first"
    assert replay.artifact_kind == "video"
    assert replay.size_bytes == 100
    assert replay.created_at == first.created_at


@pytest.mark.asyncio
async def test_pinned_job_binds_to_the_admitted_revision_not_the_moved_head(
    pin_env: Path,
) -> None:
    """The binding follows the pinned revision even when the head has moved on."""
    owner, binding_id = _wire_directory()
    store = SqlProjectRevisionStore()
    admitted = await _commit(store, _project("admitted"), parent=None)
    # The head moves to a newer revision BEFORE the pinned job publishes.
    await _commit(store, _project("moved-head"), parent=admitted.revision_id)
    workspace = pin_env / "creative" / "creative_bind_moved_head"
    workspace.mkdir()
    _clip(workspace / "input.mp4")
    payload = _pinned_payload(
        workspace,
        revision_id=admitted.revision_id,
        revision_hash=admitted.state_hash,
        principal_id=str(owner),
        channel_binding_id=binding_id,
    )
    result = await creative_render_job(payload)
    assert result["success"] is True, result
    assert result["artifact_binding"]["revision_id"] == admitted.revision_id


def test_bind_published_artifact_refuses_when_the_reported_digest_is_stale(
    pin_env: Path,
) -> None:
    """The digest is re-measured at bind time; a stale report is refused.

    The pinned revision is real and valid, so the ONLY reason for refusal is the
    digest re-measurement — removing that guard would bind successfully.
    """
    from nexus_ai_agent.creative.render_jobs import (
        CreativeRenderError,
        _bind_published_artifact,
    )

    store = SqlProjectRevisionStore()
    revision = asyncio.run(_commit(store, _project("stale-digest"), parent=None))
    workspace = pin_env / "creative" / "stale_digest"
    workspace.mkdir()
    artifact = workspace / "out.bin"
    artifact.write_bytes(b"the-actual-published-bytes")
    data = CreativeRenderPayload.model_construct(
        command="edit",
        operation="trim",
        args=[],
        workspace_dir=str(workspace),
        idempotency_key="stale:1",
        schema_version=2,
        project_id=PROJECT_ID,
        revision_id=revision.revision_id,
        revision_hash=revision.state_hash,
    )
    result = {
        "artifact_path": str(artifact),
        "sha256": "sha256:" + "c" * 64,  # does NOT match the file's real bytes
        "size_bytes": 26,
    }
    with pytest.raises(CreativeRenderError) as exc:
        asyncio.run(_bind_published_artifact(data, result))
    assert exc.value.code == "artifact_binding_failed"


def test_bind_published_artifact_refuses_missing_artifact_metadata(pin_env: Path) -> None:
    """A pinned success without measurable artifact metadata is refused."""
    from nexus_ai_agent.creative.render_jobs import (
        CreativeRenderError,
        _bind_published_artifact,
    )

    data = CreativeRenderPayload.model_construct(
        command="edit",
        operation="trim",
        args=[],
        workspace_dir=str(pin_env),
        idempotency_key="nometa:1",
        schema_version=2,
        project_id=PROJECT_ID,
        revision_id="sha256:" + "a" * 64,
        revision_hash="sha256:" + "b" * 64,
    )
    with pytest.raises(CreativeRenderError) as exc:
        asyncio.run(_bind_published_artifact(data, {"success": True}))
    assert exc.value.code == "artifact_binding_failed"
