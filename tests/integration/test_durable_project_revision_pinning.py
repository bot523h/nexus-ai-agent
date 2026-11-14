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
import os
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
    # Publications are namespaced by both job and attempt so stale output
    # cannot clobber bytes from a newer execution.
    published = pin_env / "artifacts" / "published" / job_id / "1" / "render.mp4"
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


def test_bind_and_retract_reject_non_integer_fencing_tokens(pin_env: Path) -> None:
    """The token is an integer identity: floats, strings and bools are refused.

    ``(job_id, attempt)`` is the ownership identity, and the queue's attempt is
    an integer.  A float (``1.5``), a numeric string (``"3"``) or a bool (which
    inherits from ``int``) would persist a token an exact-token retract could
    never match — so the store refuses every non-int up front.
    """
    from nexus_ai_agent.creative.project_state.models import ArtifactBindingError

    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    bindings = _binding_store()
    artifact_id = "sha256:" + "b" * 64

    for bad in (1.5, "3", True, False, -1):
        with pytest.raises(ArtifactBindingError):
            asyncio.run(
                bindings.bind(
                    project_id=PROJECT_ID,
                    revision_id=r1.revision_id,
                    artifact_id=artifact_id,
                    job_id="job-t",
                    size_bytes=1,
                    attempt=bad,  # type: ignore[arg-type]
                )
            )
        with pytest.raises(ArtifactBindingError):
            asyncio.run(
                bindings.retract(artifact_id=artifact_id, job_id="job-t", attempt=bad)  # type: ignore[arg-type]
            )


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


def test_two_independent_jobs_sharing_bytes_own_independent_bindings(pin_env: Path) -> None:
    """F5/R4: two jobs sharing one artifact_id hold *independent* provenance rows.

    The fencing token is per-job, and identity is ``(artifact_id, job_id)``: job
    B — an independent execution that produced the *same bytes* — gets its own
    binding row rather than a shared one.  Neither job's retraction can delete
    the other's row, and each job resolves only its own provenance.
    """
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    bindings = _binding_store()
    artifact_id = "sha256:" + "5" * 64

    # Job A binds first (token 2): it owns (artifact_id, "job-A").
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

    # Job B, an independent job with the same bytes, gets its OWN row — never a
    # takeover of job A's, and never silently merged into it.
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
    assert (second.job_id, second.attempt) == ("job-B", 7), (
        "a distinct job must own a distinct provenance row"
    )

    # Both rows resolve independently; the unscoped resolve is the earliest one.
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id)).job_id == "job-A"
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id="job-B")).job_id == "job-B"

    # Job B's retraction removes only B's own row; job A's is untouched.
    assert asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-B", attempt=7)) is True
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id)).job_id == "job-A"
    from nexus_ai_agent.creative.project_state.models import ArtifactNotFound

    with pytest.raises(ArtifactNotFound):
        asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id="job-B"))
    # Idempotent: a second retract of B's row removes nothing.
    assert (
        asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-B", attempt=7)) is False
    )
    # A cross-job retraction can never touch job A's row.
    assert (
        asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-A", attempt=7)) is False
    )
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id)).job_id == "job-A"

    # Only the owner's exact (job_id, attempt) retracts its own row.
    assert asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-A", attempt=2)) is True
    with pytest.raises(ArtifactNotFound):
        asyncio.run(bindings.resolve(PROJECT_ID, artifact_id))


def test_unscoped_resolve_fails_closed_when_an_address_has_two_revisions(
    pin_env: Path,
) -> None:
    """A content address bound to two *different* revisions is ambiguous.

    Identity is per job, so two jobs may hold separate rows for the same bytes.
    While they agree on the revision the unscoped resolve stays deterministic
    (the old single-row contract).  When the same address is bound to
    *different* revisions, "the earliest" would be an arbitrary pick between two
    real provenances, so the unscoped resolve must fail closed — while each
    job's row still resolves by its explicit ``job_id``.
    """
    from nexus_ai_agent.creative.project_state.models import ArtifactBindingAmbiguous

    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev-one"), parent=None))
    r2 = asyncio.run(_commit(store, _project("rev-two"), parent=r1.revision_id))
    assert r1.revision_id != r2.revision_id
    bindings = _binding_store()
    artifact_id = "sha256:" + "6" * 64

    for job_id, revision_id in (("job-A", r1.revision_id), ("job-B", r2.revision_id)):
        asyncio.run(
            bindings.bind(
                project_id=PROJECT_ID,
                revision_id=revision_id,
                artifact_id=artifact_id,
                job_id=job_id,
                size_bytes=1,
                attempt=1,
            )
        )

    # Each job still resolves its own exact provenance.
    assert (
        asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id="job-A")).revision_id
        == r1.revision_id
    )
    assert (
        asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id="job-B")).revision_id
        == r2.revision_id
    )

    # The unscoped resolve is ambiguous: two real provenances, no safe default.
    with pytest.raises(ArtifactBindingAmbiguous):
        asyncio.run(bindings.resolve(PROJECT_ID, artifact_id))


def test_rebind_token_advance_is_scoped_to_the_owning_job(pin_env: Path) -> None:
    """F5/R4: a token advance moves only the advancing job's own row.

    Identity is ``(artifact_id, job_id)``, so two jobs sharing one artifact's
    bytes hold *two* rows for the same ``artifact_id``.  The conditional token
    advance in ``_reconcile_existing`` must therefore be scoped to the owning
    ``job_id``: a re-bind by one job must move only its own row.  If that
    ownership predicate were dropped, the UPDATE would broadcast across every
    row with the artifact id and silently advance a *different* job's fencing
    token — letting a stale retraction of that other job match a token it never
    wrote, and letting one job seize a successor's provenance.
    """
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    bindings = _binding_store()
    artifact_id = "sha256:" + "7" * 64

    # Two jobs share identical bytes; each owns its own row at its own token.
    asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-A",
            size_bytes=1,
            attempt=2,
        )
    )
    asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-B",
            size_bytes=1,
            attempt=3,
        )
    )

    # Job B's successor attempt advances B's token — and only B's.
    advanced = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-B",
            size_bytes=1,
            attempt=9,
        )
    )
    assert advanced.attempt == 9
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id="job-A")).attempt == 2
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id="job-B")).attempt == 9
    # Job A's own exact (job_id, attempt) still matches the row it wrote.
    assert asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-A", attempt=2)) is True
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id="job-B")).attempt == 9


def test_bind_returns_the_authoritative_stored_row(pin_env: Path) -> None:
    """The returned binding reflects the committed row, not stale ORM state.

    A re-bind by the SAME job at a higher token advances the row and must report
    the *stored* owner/attempt; the authoritative re-read makes the result
    exactly what a fresh reader sees.  A *different* job is a distinct identity,
    so it never observes (nor mutates) this job's row.
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
    advanced = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="owner",
            artifact_kind="audio",
            size_bytes=999,
            attempt=50,
        )
    )
    resolved = asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id="owner"))
    assert (advanced.job_id, advanced.attempt) == (resolved.job_id, resolved.attempt)
    assert advanced.job_id == "owner"
    assert advanced.attempt == 50
    # A token advance is not a metadata rewrite: the original row is preserved.
    assert advanced.artifact_kind == "video"
    assert advanced.size_bytes == 100
    # A different job is a distinct identity: its bind writes its own row and
    # never observes the owner's token.
    other = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="other",
            size_bytes=1,
            attempt=3,
        )
    )
    assert (other.job_id, other.attempt) == ("other", 3)
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id="owner")).attempt == 50


def test_bind_refuses_when_the_authoritative_reread_finds_no_row(pin_env: Path) -> None:
    """F1: a successor attempt must not confirm on a binding retracted mid-reconcile.

    ``_reconcile_existing`` re-reads the row from the database after the
    conditional token advance and returns *that* as the authority.  If a
    concurrent owner's retraction removes the row inside that window, the
    re-read finds nothing; falling back to the stale ORM row would let the
    caller reach COMPLETED with no durable binding (the F1 hole).  The window is
    reproduced deterministically by wrapping the session factory so the
    authoritative SELECT observes no row.  The successor is the SAME job at a
    higher token (identity is ``(artifact_id, job_id)``), which is what reaches
    reconciliation.
    """
    from sqlalchemy import Select

    from nexus_ai_agent.creative.project_state.models import ArtifactBindingConflict
    from nexus_ai_agent.storage.db import get_session

    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    artifact_id = "sha256:" + "9" * 64
    asyncio.run(
        _binding_store().bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="owner",
            size_bytes=1,
            attempt=1,
        )
    )

    class _VanishingResult:
        def scalar_one_or_none(self) -> None:
            return None

    class _VanishingSession:
        def __init__(self, factory: Any) -> None:
            self._factory = factory
            self._ctx: Any = None
            self._session: Any = None

        async def __aenter__(self) -> _VanishingSession:
            self._ctx = self._factory()
            self._session = await self._ctx.__aenter__()
            return self

        async def __aexit__(self, *exc: object) -> Any:
            return await self._ctx.__aexit__(*exc)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._session, name)

        async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            # Only the authoritative SELECT (a Select of the binding row) is made
            # to observe "no row"; the conditional UPDATE and everything else run
            # against the real session.
            if isinstance(statement, Select):
                return _VanishingResult()
            return await self._session.execute(statement, *args, **kwargs)

    from nexus_ai_agent.creative.project_state.store import SqlArtifactBindingStore

    racing = SqlArtifactBindingStore(session_factory=lambda: _VanishingSession(get_session))
    with pytest.raises(ArtifactBindingConflict):
        asyncio.run(
            racing.bind(
                project_id=PROJECT_ID,
                revision_id=r1.revision_id,
                artifact_id=artifact_id,
                job_id="owner",
                size_bytes=1,
                attempt=9,
            )
        )


def test_concurrent_rebinds_never_regress_the_fencing_token(pin_env: Path) -> None:
    """A superseded (lower-token) re-bind must never regress the stored token.

    The token advance is a single conditional UPDATE, not a read-modify-write.
    ``asyncio.gather`` alone does not prove this: if the two binds happen to run
    serially the dangerous schedule — the *lower* token committing *last* — is
    never exercised, and a broken read-modify-write would still leave the higher
    token and pass.  So the dangerous schedule is forced deterministically: the
    higher token advances first, then the superseded lower token is applied
    after it and must be a no-op.  A concurrent leg then proves the same under
    real contention (no deadlock, same winner).
    """
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    bindings = _binding_store()
    artifact_id = "sha256:" + "4" * 64

    async def _bind(attempt: int, aid: str):
        return await bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=aid,
            job_id="job-race",
            size_bytes=1,
            attempt=attempt,
        )

    # Deterministic dangerous schedule: successor (5) commits, THEN the
    # superseded attempt (3) commits last.  A read-modify-write advance would
    # overwrite the row with 3 here; the conditional UPDATE leaves it at 5.
    asyncio.run(_bind(5, artifact_id))
    regressed = asyncio.run(_bind(3, artifact_id))
    assert regressed.attempt == 5, "the lower token must never win when it commits last"
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id)).attempt == 5

    # Concurrent leg on a FRESH artifact (no existing row): both tokens race the
    # INSERT/PK conflict from a clean state, and real contention on one row must
    # still leave the higher token.
    race_artifact = "sha256:" + "7" * 64

    async def _race() -> None:
        await asyncio.gather(_bind(5, race_artifact), _bind(3, race_artifact))

    asyncio.run(_race())
    assert asyncio.run(bindings.resolve(PROJECT_ID, race_artifact)).attempt == 5
    # The winner's retraction still works; the loser's cannot.
    assert (
        asyncio.run(bindings.retract(artifact_id=race_artifact, job_id="job-race", attempt=3))
        is False
    )
    assert (
        asyncio.run(bindings.retract(artifact_id=race_artifact, job_id="job-race", attempt=5))
        is True
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
    """A same-job, same-revision replay is a read-only no-op; the original row is kept."""
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
    # Replay by the SAME job with DIFFERENT metadata: the stored row is unchanged.
    replay = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="first",
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


# ---------------------------------------------------------------------------
# F5/R4 — per-job artifact binding identity (PR #325 review)
#
# Root cause: the binding table was keyed on ``artifact_id`` alone, so two jobs
# that published byte-identical artifacts shared ONE provenance row.  When the
# job that happened to *own* that row lost its terminal COMPLETED CAS and
# retracted it, the OTHER (still-COMPLETED) job's provenance was destroyed.
# The fix makes identity per producing job: primary key ``(artifact_id,
# job_id)``.  Content de-duplication (the SHA-256 bytes) is unchanged; only the
# *provenance record* is per job.
# ---------------------------------------------------------------------------


def test_owner_retract_after_successor_completed_keeps_successor_binding(
    pin_env: Path,
) -> None:
    """F5/R4 canonical scenario: a successful job keeps its binding after retract.

    1. Job A binds an artifact at a fixed content address.
    2. Job B consumes the SAME artifact + SAME revision and adopts the binding.
    3. Job B reaches COMPLETED (it holds durable provenance of its own).
    4. Job A loses the terminal CAS (its attempt is superseded).
    5. Job A retracts exactly its own binding.
    6. Job B's binding must still exist, resolve by identity and re-verify.
    """
    from nexus_ai_agent.creative.project_state.store import content_address_of_file

    store = SqlProjectRevisionStore()
    revision = asyncio.run(_commit(store, _project("f5"), parent=None))
    artifact = pin_env / "f5.bin"
    artifact.write_bytes(b"identical-bytes-shared-by-two-jobs")
    artifact_id = content_address_of_file(artifact)
    bindings = _binding_store()

    # 1. Job A creates + binds the artifact.
    asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=revision.revision_id,
            artifact_id=artifact_id,
            job_id="job-A",
            size_bytes=artifact.stat().st_size,
            attempt=1,
        )
    )
    # 2/3. Job B adopts the same bytes+revision; it becomes COMPLETED with its
    # own provenance row.
    adopted = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=revision.revision_id,
            artifact_id=artifact_id,
            job_id="job-B",
            size_bytes=artifact.stat().st_size,
            attempt=1,
        )
    )
    assert adopted.job_id == "job-B"

    # 4/5. Job A loses the terminal CAS and retracts only its own row.
    assert asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-A", attempt=1)) is True

    # 6. Job B's binding survives, resolves by identity and re-verifies.
    survivor = asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id="job-B"))
    assert survivor.job_id == "job-B"
    assert survivor.revision_id == revision.revision_id
    proven = asyncio.run(
        bindings.verify_artifact(
            project_id=PROJECT_ID,
            artifact_id=artifact_id,
            artifact_path=artifact,
            job_id="job-B",
        )
    )
    assert proven.job_id == "job-B"


@pytest.mark.asyncio
async def test_cross_job_adoption_race_keeps_both_jobs(pin_env: Path) -> None:
    """Two jobs racing to adopt the same bytes both end up with their own row."""
    store = SqlProjectRevisionStore()
    revision = await _commit(store, _project("race"), parent=None)
    artifact_id = "sha256:" + "a" * 64
    bindings = _binding_store()

    async def _bind(job: str) -> Any:
        return await bindings.bind(
            project_id=PROJECT_ID,
            revision_id=revision.revision_id,
            artifact_id=artifact_id,
            job_id=job,
            size_bytes=1,
            attempt=1,
        )

    first, second = await asyncio.gather(_bind("job-A"), _bind("job-B"))
    assert {first.job_id, second.job_id} == {"job-A", "job-B"}
    assert (await bindings.resolve(PROJECT_ID, artifact_id, job_id="job-A")).job_id == "job-A"
    assert (await bindings.resolve(PROJECT_ID, artifact_id, job_id="job-B")).job_id == "job-B"


def test_exact_job_and_attempt_fence_each_row_independently(pin_env: Path) -> None:
    """Retraction matches only its exact ``(artifact_id, job_id, attempt)`` row.

    Two jobs bind the same artifact at the SAME token; the token alone is not an
    identity, so neither may retract the other's row, and a wrong token matches
    nothing.
    """
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("fence"), parent=None))
    bindings = _binding_store()
    artifact_id = "sha256:" + "b" * 64
    for job in ("job-A", "job-B"):
        asyncio.run(
            bindings.bind(
                project_id=PROJECT_ID,
                revision_id=r1.revision_id,
                artifact_id=artifact_id,
                job_id=job,
                size_bytes=1,
                attempt=1,
            )
        )
    # A job's row can only be removed by its own exact (job_id, attempt) triple.
    assert (
        asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-A", attempt=2)) is False
    )
    assert (
        asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-C", attempt=1)) is False
    )
    assert asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-A", attempt=1)) is True
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id="job-B")).job_id == "job-B"
    assert asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-B", attempt=1)) is True
    from nexus_ai_agent.creative.project_state.models import ArtifactNotFound

    with pytest.raises(ArtifactNotFound):
        asyncio.run(bindings.resolve(PROJECT_ID, artifact_id))


def test_stale_attempt_cannot_delete_the_successors_row(pin_env: Path) -> None:
    """A superseded (lower-token) attempt of one job never deletes its successor."""
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("stale"), parent=None))
    bindings = _binding_store()
    artifact_id = "sha256:" + "c" * 64
    for attempt in (3, 9):
        asyncio.run(
            bindings.bind(
                project_id=PROJECT_ID,
                revision_id=r1.revision_id,
                artifact_id=artifact_id,
                job_id="job-X",
                size_bytes=1,
                attempt=attempt,
            )
        )
    # The stale attempt's retraction does not match the successor's row.
    assert (
        asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-X", attempt=3)) is False
    )
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id="job-X")).attempt == 9
    # The successor's own retraction removes exactly its row.
    assert asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="job-X", attempt=9)) is True


def test_many_jobs_sharing_identical_bytes_each_resolve(pin_env: Path) -> None:
    """N jobs publishing identical bytes each keep independent, resolvable rows."""
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("many"), parent=None))
    bindings = _binding_store()
    artifact_id = "sha256:" + "d" * 64
    jobs = ["j1", "j2", "j3", "j4"]
    for job in jobs:
        bound = asyncio.run(
            bindings.bind(
                project_id=PROJECT_ID,
                revision_id=r1.revision_id,
                artifact_id=artifact_id,
                job_id=job,
                size_bytes=1,
                attempt=1,
            )
        )
        assert bound.job_id == job
    for job in jobs:
        assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id=job)).job_id == job
    # Removing one job's row leaves every other job's row intact.
    assert asyncio.run(bindings.retract(artifact_id=artifact_id, job_id="j2", attempt=1)) is True
    for job in ("j1", "j3", "j4"):
        assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id=job)).job_id == job


def test_same_artifact_conflicting_revisions_are_per_job(pin_env: Path) -> None:
    """A job can never re-point its own binding; another job holds its own revision.

    Identity is per job, so a re-bind of the SAME job to a different revision is
    a hard conflict, while a *different* job publishing the same bytes against a
    different admitted revision keeps independent provenance.
    """
    from nexus_ai_agent.creative.project_state.models import ArtifactBindingConflict

    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    r2 = asyncio.run(_commit(store, _project("rev2"), parent=r1.revision_id))
    bindings = _binding_store()
    artifact_id = "sha256:" + "e" * 64
    asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-A",
            size_bytes=1,
            attempt=1,
        )
    )
    # The same job may not re-point its binding to another revision.
    with pytest.raises(ArtifactBindingConflict):
        asyncio.run(
            bindings.bind(
                project_id=PROJECT_ID,
                revision_id=r2.revision_id,
                artifact_id=artifact_id,
                job_id="job-A",
                size_bytes=1,
                attempt=2,
            )
        )
    # A different job with the same bytes bound to a different revision is its
    # own provenance, not a conflict.
    other = asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r2.revision_id,
            artifact_id=artifact_id,
            job_id="job-B",
            size_bytes=1,
            attempt=1,
        )
    )
    assert other.revision_id == r2.revision_id
    assert asyncio.run(bindings.resolve(PROJECT_ID, artifact_id, job_id="job-A")).revision_id == (
        r1.revision_id
    )


def test_cross_project_isolation_holds_for_every_job(pin_env: Path) -> None:
    """A content address belongs to exactly one project, per job identity.

    A second project cannot claim the same bytes through a *different* job, and
    it cannot resolve the first project's job-scoped binding.
    """
    from nexus_ai_agent.creative.project_state.models import CrossProjectAccess
    from nexus_ai_agent.creative.studio.models import compute_state_hash

    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("rev1"), parent=None))
    other = "f5-other-project"
    other_project = _project("other").model_copy(update={"project_id": other})
    other_project.state_hash = compute_state_hash(other_project)
    r_other = asyncio.run(
        store.commit(other_project, authorized_project_id=other, expected_parent_revision_id=None)
    )
    bindings = _binding_store()
    artifact_id = "sha256:" + "f" * 64
    asyncio.run(
        bindings.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-A",
            size_bytes=1,
            attempt=1,
        )
    )
    # A different project with a DIFFERENT job cannot bind the same content.
    with pytest.raises(CrossProjectAccess):
        asyncio.run(
            bindings.bind(
                project_id=other,
                revision_id=r_other.revision_id,
                artifact_id=artifact_id,
                job_id="job-B",
                size_bytes=1,
                attempt=1,
            )
        )
    # Nor resolve the owning project's job-scoped binding.
    with pytest.raises(CrossProjectAccess):
        asyncio.run(bindings.resolve(other, artifact_id, job_id="job-A"))


def test_restart_replay_rebinds_per_job_after_recovery(pin_env: Path) -> None:
    """A fresh store (simulated restart) resolves and replays each job's row.

    Recovery re-runs a job; its replay is an idempotent no-op on its OWN row,
    while a different job adopting the same bytes keeps a distinct row.
    """
    store = SqlProjectRevisionStore()
    r1 = asyncio.run(_commit(store, _project("recovery"), parent=None))
    artifact_id = "sha256:" + "1" * 64
    first = _binding_store()
    asyncio.run(
        first.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-A",
            size_bytes=1,
            attempt=1,
        )
    )
    # A NEW store instance (process restart) sees the committed row.
    restarted = _binding_store()
    assert asyncio.run(restarted.resolve(PROJECT_ID, artifact_id, job_id="job-A")).job_id == "job-A"
    # Job A replays idempotently after recovery; Job B adopts its own row.
    asyncio.run(
        restarted.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-A",
            size_bytes=1,
            attempt=1,
        )
    )
    asyncio.run(
        restarted.bind(
            project_id=PROJECT_ID,
            revision_id=r1.revision_id,
            artifact_id=artifact_id,
            job_id="job-B",
            size_bytes=1,
            attempt=1,
        )
    )
    assert asyncio.run(restarted.resolve(PROJECT_ID, artifact_id, job_id="job-B")).job_id == "job-B"
    assert asyncio.run(restarted.resolve(PROJECT_ID, artifact_id, job_id="job-A")).job_id == "job-A"


def _bound_projects(artifact_id: str) -> set[str]:
    """The distinct ``project_id`` values currently bound to one content address.

    Read straight from SQLite (not through the store) so the assertion is about
    what the *database* committed, not about an in-session object.
    """
    import sqlite3

    path = os.environ["NEXUS_DB_PATH"]
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT project_id FROM projectartifactbinding WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchall()
    return {str(row[0]) for row in rows}


class _AsyncBarrier:
    """A minimal ``asyncio.Barrier`` equivalent that also runs on Python 3.10.

    ``asyncio.Barrier`` was added in 3.11, but this project's CI matrix still
    includes 3.10, so the concurrency proof needs its own primitive.  The
    semantics match the stdlib barrier for this test: :meth:`wait` blocks until
    ``parties`` coroutines have arrived, then releases them together; no waiter
    proceeds before the last one arrives.

    The wait is bounded by ``timeout`` so a lost participant fails loudly rather
    than hanging the suite, and both a timeout and a cancellation decrement the
    arrival count under the lock, so a departed waiter can never leave a phantom
    arrival behind (which would strand the surviving party forever).
    """

    def __init__(self, parties: int, timeout: float = 30.0) -> None:
        if parties < 1:
            raise ValueError("parties must be >= 1")
        self._parties = parties
        self._timeout = timeout
        self._count = 0
        self._generation = 0
        self._condition = asyncio.Condition()

    async def wait(self) -> None:
        async with self._condition:
            generation = self._generation
            self._count += 1
            if self._count >= self._parties:
                # The last arrival releases every waiter and opens a new epoch.
                self._count = 0
                self._generation += 1
                self._condition.notify_all()
                return
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self._generation != generation),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError as exc:
                self._count -= 1
                self._condition.notify_all()
                raise AssertionError(
                    f"barrier timed out after {self._timeout}s: only "
                    f"{self._count + 1} of {self._parties} parties arrived"
                ) from exc
            except asyncio.CancelledError:
                self._count -= 1
                self._condition.notify_all()
                raise


@pytest.mark.asyncio
async def test_concurrent_binds_cannot_attribute_one_content_address_to_two_projects(
    pin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One content address may be owned by exactly one project — a DB invariant.

    Two projects race to bind the *same* content address, each through its own
    job, so the per-job ``(artifact_id, job_id)`` primary key cannot separate
    them.  A read-then-insert ownership guard is a TOCTOU: under real
    concurrency both reads observe "no owner row" and both inserts commit,
    silently attributing one content address to two projects.

    The two binds are released together from a barrier placed immediately after
    each has validated its revision (before either opens its binding session),
    so both enter the ownership guard with no visible owner row — the exact
    interleaving the invariant must fail closed on.  Exactly one bind may win;
    the loser must be refused with ``CrossProjectAccess``, and the database must
    end up with rows for a single project.
    """
    from nexus_ai_agent.creative.project_state.models import CrossProjectAccess
    from nexus_ai_agent.creative.project_state.store import SqlArtifactBindingStore
    from nexus_ai_agent.creative.studio.models import compute_state_hash

    store = SqlProjectRevisionStore()
    project_a, project_b = "cross-project-a", "cross-project-b"
    a_project = _project("a").model_copy(update={"project_id": project_a})
    a_project.state_hash = compute_state_hash(a_project)
    b_project = _project("b").model_copy(update={"project_id": project_b})
    b_project.state_hash = compute_state_hash(b_project)
    ra = await store.commit(
        a_project, authorized_project_id=project_a, expected_parent_revision_id=None
    )
    rb = await store.commit(
        b_project, authorized_project_id=project_b, expected_parent_revision_id=None
    )

    # Release both racers at the same instant, right before the guard.
    barrier = _AsyncBarrier(2)
    original_load = SqlProjectRevisionStore.load_revision

    async def gated_load(self: SqlProjectRevisionStore, project_id: str, revision_id: str) -> Any:
        record = await original_load(self, project_id, revision_id)
        await barrier.wait()
        return record

    monkeypatch.setattr(SqlProjectRevisionStore, "load_revision", gated_load)

    bindings = SqlArtifactBindingStore()
    artifact_id = "sha256:" + "c" * 64
    results = await asyncio.gather(
        bindings.bind(
            project_id=project_a,
            revision_id=ra.revision_id,
            artifact_id=artifact_id,
            job_id="job-A",
            size_bytes=1,
            attempt=1,
        ),
        bindings.bind(
            project_id=project_b,
            revision_id=rb.revision_id,
            artifact_id=artifact_id,
            job_id="job-B",
            size_bytes=1,
            attempt=1,
        ),
        return_exceptions=True,
    )

    winners = [r for r in results if not isinstance(r, BaseException)]
    refusals = [r for r in results if isinstance(r, BaseException)]
    assert len(winners) == 1, (
        f"exactly one project may own a content address, got {len(winners)}: {results!r}"
    )
    assert len(refusals) == 1 and isinstance(refusals[0], CrossProjectAccess), (
        f"the losing project must be refused with CrossProjectAccess: {refusals!r}"
    )
    # The invariant must hold in the database, not just in the returned objects.
    owners = _bound_projects(artifact_id)
    assert owners == {winners[0].project_id}, (
        f"the database must hold bindings for exactly the winning project: {owners!r}"
    )
    assert refusals[0].args  # the refusal carries a human-readable reason


def test_verified_asset_registration_is_transactional_idempotent_and_fenced(
    pin_env: Path,
) -> None:
    from sqlalchemy import select

    from nexus_ai_agent.creative.project_state.models import (
        ArtifactBindingError,
        ArtifactRevisionMismatch,
    )
    from nexus_ai_agent.creative.project_state.store import SqlArtifactBindingStore
    from nexus_ai_agent.storage import models as orm
    from nexus_ai_agent.storage.db import get_session

    revision = asyncio.run(
        _commit(SqlProjectRevisionStore(), _project("asset-lineage"), parent=None)
    )
    published = pin_env / "published.mp4"
    published.write_bytes(
        bytes.fromhex("00000018667479706d703432000000006d70343269736f6d") + b"payload"
    )
    store = SqlArtifactBindingStore()
    registration_kwargs = {
        "project_id": PROJECT_ID,
        "revision_id": revision.revision_id,
        "job_id": "job-lineage",
        "attempt_id": "job-lineage:attempt-1",
        "fencing_token": 1,
        "content_sha256": __import__("hashlib").sha256(published.read_bytes()).hexdigest(),
        "size_bytes": published.stat().st_size,
        "media_type": "video/mp4",
        "storage_uri": str(published),
        "verifier_digest": "sha256:" + "1" * 64,
        "passport_id": "passport-lineage",
        "passport_digest": "sha256:" + "2" * 64,
        "fence_check": lambda: True,
    }
    registered = asyncio.run(store.register_verified_asset_version(**registration_kwargs))
    replay = asyncio.run(store.register_verified_asset_version(**registration_kwargs))
    assert replay.asset_version_id == registered.asset_version_id
    assert replay.binding.passport_id == registered.binding.passport_id
    assert replay.binding.attempt_id == registered.binding.attempt_id
    assert replay.binding.verifier_digest is None
    assert replay.binding.derived_receipt_ref == registered.binding.derived_receipt_ref

    async def assert_rows() -> None:
        from nexus_ai_agent.creative.project_state.store import derive_persisted_lineage_receipt

        async with get_session() as session:
            version = await session.get(orm.AssetVersion, registered.asset_version_id)
            assert version is not None
            assert version.content_sha256 == registration_kwargs["content_sha256"]
            assert version.size_bytes == registration_kwargs["size_bytes"]
            link = await session.execute(
                select(orm.ProjectAssetLink).where(
                    orm.ProjectAssetLink.project_id == PROJECT_ID,
                    orm.ProjectAssetLink.version_id == registered.asset_version_id,
                )
            )
            assert link.scalar_one_or_none() is not None
            binding = await session.get(
                orm.ProjectArtifactBinding,
                ("sha256:" + registration_kwargs["content_sha256"], "job-lineage"),
            )
            assert binding is not None
            assert binding.artifact_version_id == registered.asset_version_id
            receipt = await derive_persisted_lineage_receipt(
                session,
                artifact_id="sha256:" + registration_kwargs["content_sha256"],
                job_id="job-lineage",
            )
            assert receipt.passport_id == registered.binding.passport_id
            assert receipt.attempt_id == registered.binding.attempt_id
            assert receipt.verifier_digest is None
            assert receipt.derived_receipt_ref == registered.binding.derived_receipt_ref

    asyncio.run(assert_rows())

    async def assert_blob_readback() -> None:
        from pathlib import Path

        from nexus_ai_agent.creative.assets.authz import (
            GrantTableAuthorizer,
            StudioAuthorizationSeam,
        )
        from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
        from nexus_ai_agent.creative.assets.service import AssetLibraryService

        async with get_session() as session:
            version = await session.get(orm.AssetVersion, registered.asset_version_id)
            assert version is not None
            assert version.blob_key.startswith("blobs/")
            assert not Path(version.blob_key).is_absolute()
            blob_key = version.blob_key
        service = AssetLibraryService(
            blob_store=LocalContentAddressedBlobStore(pin_env / ".asset-library"),
            authorizer=StudioAuthorizationSeam(
                GrantTableAuthorizer({("user-1", PROJECT_ID): frozenset({"project:read"})})
            ),
        )
        content = await service.read_content(
            actor_kind="user",
            actor_id="user-1",
            project_id=PROJECT_ID,
            asset_id=registered.asset_id,
        )
        assert content == published.read_bytes()
        assert (
            __import__("hashlib").sha256(content).hexdigest()
            == registration_kwargs["content_sha256"]
        )
        assert blob_key.startswith("blobs/")

    asyncio.run(assert_blob_readback())
    identical_retry = asyncio.run(
        store.register_verified_asset_version(
            **{**registration_kwargs, "fencing_token": 2, "attempt_id": "retry-2"}
        )
    )
    assert identical_retry.asset_version_id == registered.asset_version_id
    assert identical_retry.binding.attempt == 2
    with pytest.raises(ArtifactBindingError, match="stale"):
        asyncio.run(store.register_verified_asset_version(**registration_kwargs))

    updated_path = pin_env / "updated.mp4"
    updated_path.write_bytes(bytes.fromhex("0000001866747970") + b"mp42" + b"updated-bytes")
    changed_retry = asyncio.run(
        store.register_verified_asset_version(
            **{
                **registration_kwargs,
                "attempt_id": "retry-3",
                "fencing_token": 3,
                "storage_uri": str(updated_path),
                "content_sha256": __import__("hashlib")
                .sha256(updated_path.read_bytes())
                .hexdigest(),
                "size_bytes": updated_path.stat().st_size,
            }
        )
    )
    assert changed_retry.asset_version_id != registered.asset_version_id
    assert changed_retry.binding.attempt == 3

    async def assert_predecessor_released() -> None:
        async with get_session() as session:
            assert await session.get(orm.AssetVersion, registered.asset_version_id) is None

    asyncio.run(assert_predecessor_released())
    with pytest.raises(ArtifactRevisionMismatch, match="digest or size"):
        asyncio.run(
            store.bind(
                project_id=PROJECT_ID,
                revision_id=revision.revision_id,
                artifact_id="sha256:"
                + __import__("hashlib").sha256(updated_path.read_bytes()).hexdigest(),
                job_id="job-wrong-size",
                size_bytes=updated_path.stat().st_size + 1,
                artifact_version_id=changed_retry.asset_version_id,
            )
        )
    with pytest.raises(ArtifactBindingError, match="fence"):
        asyncio.run(
            store.register_verified_asset_version(
                **{**registration_kwargs, "fencing_token": 2, "fence_check": lambda: False}
            )
        )

    async def assert_retraction() -> None:
        assert not await store.retract(
            artifact_id=changed_retry.binding.artifact_id, job_id="job-lineage", attempt=2
        )
        assert await store.retract(
            artifact_id=changed_retry.binding.artifact_id, job_id="job-lineage", attempt=3
        )
        async with get_session() as session:
            assert (
                await session.get(
                    orm.ProjectArtifactBinding,
                    (changed_retry.binding.artifact_id, "job-lineage"),
                )
                is None
            )
            assert await session.get(orm.AssetVersion, changed_retry.asset_version_id) is None
            assert await session.get(orm.Asset, changed_retry.asset_id) is None
            links = await session.execute(
                select(orm.ProjectAssetLink).where(
                    orm.ProjectAssetLink.version_id == changed_retry.asset_version_id
                )
            )
            assert links.scalar_one_or_none() is None

    asyncio.run(assert_retraction())
    published.write_bytes(b"tampered on disk")
    with pytest.raises(ArtifactRevisionMismatch, match="digest or size"):
        asyncio.run(store.register_verified_asset_version(**registration_kwargs))


def test_binding_refuses_nonexistent_asset_version_reference(pin_env: Path) -> None:
    from nexus_ai_agent.creative.project_state.models import ArtifactBindingError

    revision = asyncio.run(
        _commit(SqlProjectRevisionStore(), _project("missing-version"), parent=None)
    )
    artifact = pin_env / "missing-version.bin"
    artifact.write_bytes(b"asset bytes")
    from nexus_ai_agent.creative.project_state.store import content_address_of_file

    with pytest.raises(ArtifactBindingError, match="no AssetVersion"):
        asyncio.run(
            _binding_store().bind(
                project_id=PROJECT_ID,
                revision_id=revision.revision_id,
                artifact_id=content_address_of_file(artifact),
                job_id="job-no-version",
                size_bytes=artifact.stat().st_size,
                artifact_version_id="does-not-exist",
                require_asset_version=True,
            )
        )
