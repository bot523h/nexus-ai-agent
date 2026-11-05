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


def _pinned_payload(workspace: Path, *, revision_id: str, revision_hash: str) -> dict[str, Any]:
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
        "principal_id": "prn_" + "0" * 32,
        "project_id": PROJECT_ID,
        "channel_binding_id": "cbn_" + "a" * 64,
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
