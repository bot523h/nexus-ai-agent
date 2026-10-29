from __future__ import annotations

import asyncio

import pytest

from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.models import ActorIdentity, AssetRecord, Project, Timeline
from nexus_ai_agent.nagar.p0.commit import P0CommitContext, commit_preview
from nexus_ai_agent.nagar.p0.runtime import preview_nel

SOURCE = """NEL 1
PROJECT project-1
INTENT "tighten_intro"
SELECT semantic_segment("intro")
TRIM duration_delta=-2.0s
REQUIRE evidence("scene-detector.v1")
PREVIEW
"""


class Queue:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def enqueue(
        self, *, job_type: str, idempotency_key: str, payload: dict[str, object]
    ) -> str:
        self.calls.append(
            {"job_type": job_type, "idempotency_key": idempotency_key, "payload": payload}
        )
        return "job-1"


class Directory:
    def __init__(self, actor: ActorIdentity, project_id: str) -> None:
        self.access = ProjectAccess(
            actor=actor,
            project_id=project_id,
            permissions=frozenset({"project:read", "project:write"}),
        )
        self.authorized = 0
        self.bound = 0

    def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
        self.authorized += 1
        return self.access.authorize(actor, project_id)

    def require_binding(self, binding_id: str, principal: object) -> object:
        self.bound += 1
        return object()


def project(revision: int = 3) -> Project:
    return Project(
        project_id="project-1",
        name="demo",
        timeline=Timeline(timeline_id="tl-1", duration_us=4_000_000),
        assets=[
            AssetRecord(
                asset_id="src",
                media_kind="video",
                content_sha256="sha256:" + "a" * 64,
                duration_us=4_000_000,
            )
        ],
        state_revision=revision,
    )


def preview(revision: int = 0):
    from nexus_ai_agent.nagar.p0.semantic_time import EvidenceKind, SemanticSegment

    segment = SemanticSegment(
        segment_id="seg-intro",
        project_id="project-1",
        tenant_id="tenant-1",
        asset_id="src",
        timeline_revision=revision,
        label="intro",
        start_pts=0,
        end_pts=120,
        timebase_num=1,
        timebase_den=30,
        evidence_kind=EvidenceKind.DETERMINISTIC_CHECK,
        confidence=0.95,
        uncertainty=0.01,
        source_model="scene-detector.v1",
    )
    return preview_nel(
        SOURCE,
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=revision,
        segments=[segment],
        source_duration_us=4_000_000,
    )


def context(*, revision: int = 0) -> tuple[P0CommitContext, Queue, Directory]:
    actor = ActorIdentity(kind="user", actor_id="prn_" + "1" * 32)
    queue = Queue()
    directory = Directory(actor, "project-1")
    ctx = P0CommitContext(
        actor=actor,
        tenant_id="tenant-1",
        project=project(revision),
        directory=directory,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        channel_binding_id="cbn_" + "a" * 64,
        workspace_dir="/tmp/creative_job",
        input_path="/tmp/creative_job/input.mp4",
        source_text=SOURCE,
        user_id=1,
        chat_id=2,
    )
    return ctx, queue, directory


def test_commit_rechecks_authority_and_enqueues_existing_creative_job() -> None:
    ctx, queue, directory = context()
    result = asyncio.run(commit_preview(preview(), ctx))
    assert result.status == "enqueued"
    assert result.job_id == "job-1"
    assert result.canonical_payload == {
        "clip_asset_id": "src",
        "in_point_us": 0,
        "out_point_us": 2_000_000,
    }
    assert directory.authorized == 1 and directory.bound == 1
    assert queue.calls[0]["job_type"] == "creative_render"


def test_commit_rejects_stale_preview_before_queue_insert() -> None:
    ctx, queue, _ = context(revision=4)
    with pytest.raises(ValueError, match="stale_preview_revision"):
        asyncio.run(commit_preview(preview(), ctx))
    assert queue.calls == []
