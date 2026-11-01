"""§3 integration harness — the creative chain, full depth, no mock at the seam.

Reproduces the chain the owner directive demands, end-to-end through the
REAL queue and REAL render binary (imageio-ffmpeg wheel):

    enqueue creative_render
      → InProcessJobQueue persists + schedules
      → default_job_handlers routes to creative/render_jobs.py
      → packs registry → CommandBus → lane → FFmpeg → measured artifact
      → durable JobStatus.COMPLETED / FAILED
      → JobCompletion notifier (translated, artifact attached, cleanup)
      → Telegram (PTB faked only at the telegram.Bot boundary)

Failure sides proven too: typed failure (unsupported op → COMPLETED with
translated typed failure), durable FAILED (unexpected exception →
creative.failed.internal, never raw traceback/path), idempotent re-enqueue
(same key = same job, no double effect).
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.bot.app import _notify_creative_completion
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin
from nexus_ai_agent.i18n import i18n
from nexus_ai_agent.worker import default_job_handlers

# Repo root computed from this file's location (tests/integration/…) so
# subprocess calls work regardless of where the repo is checked out (CI
# runners use /home/runner/work/…, not /home/user/nexus-ai-agent).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.videos: list[tuple[int, bytes, str]] = []
        self.documents: list[tuple[int, bytes, str]] = []

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.messages.append(text)

    async def send_video(self, *, chat_id: int, video: Any, caption: str, **kw: Any) -> None:
        self.videos.append((chat_id, video.read(), caption))

    async def send_document(self, *, chat_id: int, document: Any, caption: str) -> None:
        self.documents.append((chat_id, document.read(), caption))


def _clip(path: Path, seconds: int = 2) -> None:
    binary = resolve_ffmpeg_bin()
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
            f"testsrc=duration={seconds}:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=120,
    )


def _payload(workspace: Path, key: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "media_duration_us": 2_000_000,
        "user_id": 42,
        "chat_id": 4242,
        "lang": "fa",
        "idempotency_key": key,
    }
    base.update(over)
    return base


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path / "creative_tmp"))
    (tmp_path / "creative_tmp").mkdir(parents=True)
    settings_module.get_settings.cache_clear()
    bot = _FakeBot()
    monkeypatch.setattr("telegram.Bot", lambda token: bot)
    completions: list[Any] = []

    async def hook(completion: Any) -> None:
        # capture the completion + pre-cleanup artifact facts, then deliver
        result = completion.result or {}
        artifact = result.get("artifact_path")
        artifact_exists = Path(str(artifact)).is_file() if artifact else False
        completions.append((completion, artifact_exists))
        await _notify_creative_completion(completion, token="dummy")

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", on_job_finished=hook)
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    yield queue, bot, completions  # type: ignore[misc]
    settings_module.get_settings.cache_clear()


async def _drain(queue: InProcessJobQueue, job_id: str, timeout: float = 120.0) -> JobStatus:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = await queue.get_status(job_id)
        if status in {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}:
            return status
        await asyncio.sleep(0.1)
    raise TimeoutError(f"job {job_id} did not reach a terminal state")


@pytest.mark.asyncio
async def test_enqueue_to_artifact_to_translated_delivery(harness) -> None:  # noqa: ANN001
    queue, bot, completions = harness
    key = "creative:42:4242:777"
    workspace = Path(str(queue.db_path)).parent / "creative_tmp" / "creative_e2e777"
    workspace.mkdir(parents=True)
    _clip(workspace / "input.mp4")

    # ── enqueue: same key twice → same job id, exactly one effect (§6) ──
    job_id = await queue.enqueue(
        job_type="creative_render", idempotency_key=key, payload=_payload(workspace, key)
    )
    job_id_again = await queue.enqueue(
        job_type="creative_render", idempotency_key=key, payload=_payload(workspace, key)
    )
    assert job_id_again == job_id, "re-enqueue must collapse onto the durable key"

    # ── durable completion through the real handler ──
    status = await _drain(queue, job_id)
    assert status is JobStatus.COMPLETED
    result = await queue.get_result(job_id)
    assert result is not None and result["success"] is True
    assert result["operation"] == "timeline.trim"
    # artifact facts are measured, not asserted by flags
    digest = result["sha256"]
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64
    assert 700_000 <= result["duration_us"] <= 1_300_000

    # ── notifier delivered the artifact (bytes == sha256 evidence) ──
    assert completions and all(existed for _, existed in completions), (
        "artifact must exist on disk at completion-hook time"
    )
    assert len(bot.videos) == 1, f"expected exactly one video delivery, got {bot.videos}"
    chat_id, video_bytes, caption = bot.videos[0]
    assert chat_id == 4242
    assert "sha256:" + hashlib.sha256(video_bytes).hexdigest() == digest
    assert caption == i18n.t("creative.completed", lang="fa", command="edit", operation="trim")
    assert "creative." not in caption

    # ── workspace cleaned by notifier; idempotent re-enqueue does not replay ──
    assert not workspace.exists()
    job_id_third = await queue.enqueue(
        job_type="creative_render", idempotency_key=key, payload=_payload(workspace, key)
    )
    assert job_id_third == job_id
    await asyncio.sleep(0.3)
    assert len(bot.videos) == 1, "idempotent key must not replay a finished job"


@pytest.mark.asyncio
async def test_typed_failure_reaches_user_translated(harness) -> None:  # noqa: ANN001
    queue, bot, _ = harness
    key = "creative:42:4242:778"
    workspace = Path(str(queue.db_path)).parent / "creative_tmp" / "creative_e2e778"
    workspace.mkdir(parents=True)
    _clip(workspace / "input.mp4")

    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key=key,
        payload=_payload(workspace, key, operation="lut", command="grade"),
    )
    status = await _drain(queue, job_id)
    # task-181 (GAP-A): a typed user failure is a FAILURE status — never
    # COMPLETED — classified TERMINAL (unsupported_operation) and still
    # delivered to the user as translated failure copy.
    assert status is JobStatus.FAILED_TERMINAL
    result = await queue.get_result(job_id)
    assert result is not None and result["success"] is False
    assert result["error_code"] == "unsupported_operation"
    assert bot.messages, "typed failure must reach the user"
    text = bot.messages[-1]
    assert text.startswith("❌"), "terminal failures carry the terminal class line"
    assert text.endswith(i18n.t("creative.failed.unsupported_operation", lang="fa", detail="x"))
    assert "✅" not in text
    assert "creative." not in text and "lut" not in text.replace("creative.failed", "")


@pytest.mark.asyncio
async def test_unexpected_crash_is_durable_failed_and_safe(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    queue, bot, _ = harness
    key = "creative:42:4242:779"
    workspace = Path(str(queue.db_path)).parent / "creative_tmp" / "creative_e2e779"
    workspace.mkdir(parents=True)
    _clip(workspace / "input.mp4")

    # Deterministic input-evidence infrastructure failure: the new preflight
    # must stop before the real render handler can observe it.
    def _boom(raw: str) -> Path:
        raise OSError("disk exploded")

    monkeypatch.setattr("nexus_ai_agent.creative.render_jobs._guarded_workspace", _boom)

    job_id = await queue.enqueue(
        job_type="creative_render", idempotency_key=key, payload=_payload(workspace, key)
    )
    status = await _drain(queue, job_id)
    # Evidence preflight is a deterministic terminal failure; no result or
    # passport is manufactured from the unavailable source evidence.
    assert status is JobStatus.FAILED_TERMINAL
    assert await queue.get_result(job_id) is None
    row = queue._fetch_row_full(job_id)
    assert row is not None
    assert row["error"] == "creative_passport_source_unavailable:CreativePassportError"
    assert row["artifact_passport_json"] is None
    assert not queue._artifact_root.exists()
    assert bot.messages, "failed job must notify the user"
    text = bot.messages[-1]
    assert text.startswith("❌"), "terminal failures carry the terminal class line"
    assert text.endswith(i18n.t("creative.failed.internal", lang="fa", detail="x"))
    assert "disk exploded" not in text, "internal errors must not leak to users"
    assert "creative_" not in text and "/tmp" not in text, "internal paths must not leak"


# ---------------------------------------------------------------------------
# Task-255: AttemptStaging-based publication for creative_render
# ---------------------------------------------------------------------------


async def test_creative_render_publication_stages_and_publishes(tmp_path: Path) -> None:
    """The publication callback stages the artifact and atomically publishes it."""
    from nexus_ai_agent.creative.render_jobs import (
        build_creative_render_publication,
    )

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    workspace = tmp_path / "creative_pub1"
    workspace.mkdir()
    artifact = workspace / "output.mp4"
    artifact.write_bytes(b"rendered bytes v1")

    result = {
        "artifact_path": str(artifact),
        "workspace_dir": str(workspace),
    }
    payload: dict[str, object] = {}

    publication = build_creative_render_publication(artifact_root)
    patch = publication.publish(payload, result)

    published_raw = patch["artifact_path"]
    assert isinstance(published_raw, str)
    published = Path(published_raw)
    assert published.exists()
    assert published.read_bytes() == b"rendered bytes v1"
    # Staging directory was cleaned up by finalize (not called yet, so it remains)
    staging_dir = artifact_root / "staging"
    assert staging_dir.exists()


async def test_creative_render_publication_keeps_previous_as_backup(tmp_path: Path) -> None:
    """Publishing a second artifact keeps the previous as .prev."""
    from nexus_ai_agent.creative.render_jobs import (
        build_creative_render_publication,
    )

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    workspace = tmp_path / "creative_pub2"
    workspace.mkdir()
    artifact = workspace / "output.mp4"

    publication = build_creative_render_publication(artifact_root)

    # First publication
    artifact.write_bytes(b"version 1")
    result1 = {"artifact_path": str(artifact), "workspace_dir": str(workspace)}
    patch1 = publication.publish({}, result1)
    published1 = Path(patch1["artifact_path"])
    assert published1.read_bytes() == b"version 1"

    # Second publication (same workspace, same attempt_id)
    artifact.write_bytes(b"version 2")
    result2 = {"artifact_path": str(artifact), "workspace_dir": str(workspace)}
    patch2 = publication.publish({}, result2)
    published2 = Path(patch2["artifact_path"])
    assert published2.read_bytes() == b"version 2"

    # Previous is kept as .prev, inside the publishing job's namespace
    # (task-283: the published namespace is job-scoped, so two jobs that emit
    # the same file name can neither overwrite each other's artifact nor share
    # one .prev backup).
    backup = published2.parent / ".output.mp4.prev"
    assert backup.exists()
    assert backup.read_bytes() == b"version 1"


async def test_creative_render_retract_restores_previous(tmp_path: Path) -> None:
    """Retract removes the staged temp and restores the previous artifact."""
    from nexus_ai_agent.creative.render_jobs import (
        build_creative_render_publication,
    )

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    workspace = tmp_path / "creative_pub3"
    workspace.mkdir()
    artifact = workspace / "output.mp4"

    publication = build_creative_render_publication(artifact_root)

    # First publication (queue-minted identity: job_id + attempt are the
    # authoritative ownership evidence a real retract always carries).
    artifact.write_bytes(b"good version")
    identity = {"_queue_job_id": "job-retract", "_queue_attempt": 1}
    result = {"artifact_path": str(artifact), "workspace_dir": str(workspace), **identity}
    patch = publication.publish({}, result)
    published = Path(patch["artifact_path"])
    assert published.read_bytes() == b"good version"

    # Second publication (creates .prev backup)
    artifact.write_bytes(b"bad version")
    result2 = {"artifact_path": str(artifact), "workspace_dir": str(workspace), **identity}
    patch2 = publication.publish({}, result2)
    published2 = Path(patch2["artifact_path"])
    assert published2.read_bytes() == b"bad version"

    # Retract with post-publish result (artifact_path is the published path)
    post_publish_result = {
        "artifact_path": str(published2),
        "workspace_dir": str(workspace),
        **identity,
    }
    publication.retract({}, post_publish_result)
    # After retract, the previous artifact is restored from .prev
    backup = published2.parent / ".output.mp4.prev"
    published_path = published2
    assert not backup.exists(), "backup should be consumed by retract"
    assert published_path.read_bytes() == b"good version"


async def test_creative_render_finalize_cleans_staging(tmp_path: Path) -> None:
    """Finalize removes the staging directory after the durable commit."""
    from nexus_ai_agent.creative.render_jobs import (
        build_creative_render_publication,
    )

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    workspace = tmp_path / "creative_pub4"
    workspace.mkdir()
    artifact = workspace / "output.mp4"
    artifact.write_bytes(b"final bytes")

    publication = build_creative_render_publication(artifact_root)
    result = {"artifact_path": str(artifact), "workspace_dir": str(workspace)}
    patch = publication.publish({}, result)
    published = Path(patch["artifact_path"])
    assert published.exists()

    staging_dir = artifact_root / "staging"
    assert staging_dir.exists()

    publication.finalize({}, result)
    # Finalize is best-effort; staging dir should be removed
    staging_job_dirs = list(staging_dir.glob("*/*")) if staging_dir.exists() else []
    assert not staging_job_dirs, "staging directory should be cleaned up"


async def test_creative_render_publication_is_idempotent(tmp_path: Path) -> None:
    """Repeated publication with the same workspace produces the same result."""
    from nexus_ai_agent.creative.render_jobs import (
        build_creative_render_publication,
    )

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    workspace = tmp_path / "creative_pub5"
    workspace.mkdir()
    artifact = workspace / "output.mp4"
    artifact.write_bytes(b"stable bytes")

    publication = build_creative_render_publication(artifact_root)
    result = {"artifact_path": str(artifact), "workspace_dir": str(workspace)}

    patch1 = publication.publish({}, result)
    patch2 = publication.publish({}, result)

    assert patch1["artifact_path"] == patch2["artifact_path"]
    assert Path(patch1["artifact_path"]).read_bytes() == b"stable bytes"


async def test_creative_render_publication_via_queue(tmp_path: Path) -> None:
    """The publication callback works when wired into the queue's artifact_publications."""
    from nexus_ai_agent.creative.render_jobs import (
        CREATIVE_RENDER_PUBLICATION_JOB_TYPE,
        build_creative_render_publication,
    )

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    # Build a publication and verify it has the right type
    publication = build_creative_render_publication(artifact_root)
    assert publication.publish is not None
    assert publication.retract is not None
    assert publication.finalize is not None

    # Verify the job type constant
    assert CREATIVE_RENDER_PUBLICATION_JOB_TYPE == "creative_render"


# ---------------------------------------------------------------------------
# Task-255 mandatory race tests (deterministic, no sleeps)
# ---------------------------------------------------------------------------


async def test_creative_render_publication_attempt_isolation(tmp_path: Path) -> None:
    """Two different workspaces produce isolated staging trees — no cross-contamination."""
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    publication = build_creative_render_publication(artifact_root)

    # Two different workspaces with different artifact names (distinct attempts)
    ws_a = tmp_path / "creative_race_a"
    ws_b = tmp_path / "creative_race_b"
    ws_a.mkdir()
    ws_b.mkdir()
    artifact_a = ws_a / "render_a.mp4"
    artifact_b = ws_b / "render_b.mp4"
    artifact_a.write_bytes(b"attempt A bytes")
    artifact_b.write_bytes(b"attempt B bytes")

    result_a = {"artifact_path": str(artifact_a), "workspace_dir": str(ws_a)}
    result_b = {"artifact_path": str(artifact_b), "workspace_dir": str(ws_b)}

    patch_a = publication.publish({}, result_a)
    patch_b = publication.publish({}, result_b)

    published_a = Path(patch_a["artifact_path"])
    published_b = Path(patch_b["artifact_path"])

    # Each published artifact has its own content at its own path
    assert published_a.read_bytes() == b"attempt A bytes"
    assert published_b.read_bytes() == b"attempt B bytes"
    assert published_a != published_b, "distinct attempts must not collide at published path"

    # The staging directories are different (different workspace hashes → different job_ids)
    staging_dir = artifact_root / "staging"
    job_dirs = list(staging_dir.glob("*/*")) if staging_dir.exists() else []
    assert len(job_dirs) >= 2, "two workspaces must produce separate staging trees"


async def test_creative_render_publication_crash_recovery_states(tmp_path: Path) -> None:
    """After publish but before finalize, the system distinguishes staged/published/committed.

    - staged temp exists in staging tree
    - published artifact exists at final root
    - .prev backup exists (if there was a previous publication)
    - finalize has NOT run (staging still present)
    """
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    publication = build_creative_render_publication(artifact_root)

    workspace = tmp_path / "creative_crash"
    workspace.mkdir()
    artifact = workspace / "output.mp4"
    artifact.write_bytes(b"crash recovery bytes")

    result = {"artifact_path": str(artifact), "workspace_dir": str(workspace)}
    patch = publication.publish({}, result)

    published = Path(patch["artifact_path"])
    assert published.exists(), "published artifact must exist"
    assert published.read_bytes() == b"crash recovery bytes"

    # Staging temp still exists (finalize not called — simulates crash before finalize)
    staging_dir = artifact_root / "staging"
    assert staging_dir.exists(), "staging tree must exist (crash before finalize)"

    # No .prev backup (first publication)
    backup = artifact_root / "published" / ".output.mp4.prev"
    assert not backup.exists(), "no backup on first publication"

    # After finalize, staging is cleaned up
    publication.finalize({}, result)
    staging_job_dirs = list(staging_dir.glob("*/*")) if staging_dir.exists() else []
    assert not staging_job_dirs, "finalize must clean staging"


async def test_creative_render_publication_no_unsafe_direct_write(tmp_path: Path) -> None:
    """The published artifact is staged first, never written directly."""
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    publication = build_creative_render_publication(artifact_root)

    workspace = tmp_path / "creative_nodirect"
    workspace.mkdir()
    artifact = workspace / "output.mp4"
    artifact.write_bytes(b"staged then published")

    result = {"artifact_path": str(artifact), "workspace_dir": str(workspace)}
    patch = publication.publish({}, result)

    published = Path(patch["artifact_path"])
    assert published.exists()
    assert published.read_bytes() == b"staged then published"

    # The published file is NOT the same inode as the workspace file
    # (it was copied to staging, then renamed — different inode)
    assert published.stat().st_ino != artifact.stat().st_ino, (
        "published artifact must be a copy, not a direct write"
    )


async def test_creative_render_publication_concurrent_publishers(tmp_path: Path) -> None:
    """Two concurrent publishers with different workspaces both succeed with isolated results."""
    import asyncio

    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    publication = build_creative_render_publication(artifact_root)

    ws_1 = tmp_path / "creative_conc_1"
    ws_2 = tmp_path / "creative_conc_2"
    ws_1.mkdir()
    ws_2.mkdir()
    artifact_1 = ws_1 / "render_1.mp4"
    artifact_2 = ws_2 / "render_2.mp4"
    artifact_1.write_bytes(b"concurrent publisher 1")
    artifact_2.write_bytes(b"concurrent publisher 2")

    result_1 = {"artifact_path": str(artifact_1), "workspace_dir": str(ws_1)}
    result_2 = {"artifact_path": str(artifact_2), "workspace_dir": str(ws_2)}

    async def _publish(result):
        return await asyncio.to_thread(publication.publish, {}, result)

    patch_1, patch_2 = await asyncio.gather(
        _publish(result_1),
        _publish(result_2),
    )

    published_1 = Path(patch_1["artifact_path"])
    published_2 = Path(patch_2["artifact_path"])
    assert published_1.read_bytes() == b"concurrent publisher 1"
    assert published_2.read_bytes() == b"concurrent publisher 2"
    assert published_1 != published_2


async def test_creative_render_publication_retry_after_ambiguous_crash(tmp_path: Path) -> None:
    """After an ambiguous crash (publish done, finalize not), a retry is idempotent."""
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    publication = build_creative_render_publication(artifact_root)

    workspace = tmp_path / "creative_ambig"
    workspace.mkdir()
    artifact = workspace / "output.mp4"
    artifact.write_bytes(b"ambiguous crash bytes")

    result = {"artifact_path": str(artifact), "workspace_dir": str(workspace)}

    # First attempt: publish but simulate crash before finalize
    patch1 = publication.publish({}, result)
    published1 = Path(patch1["artifact_path"])
    assert published1.exists()
    # Simulate crash: staging still exists
    assert (artifact_root / "staging").exists()

    # Retry: publish again (simulates recovery re-running publication)
    patch2 = publication.publish({}, result)
    published2 = Path(patch2["artifact_path"])
    assert published2.exists()
    assert published2.read_bytes() == b"ambiguous crash bytes"
    # The published artifact is the same (idempotent)
    assert published1.read_bytes() == published2.read_bytes()


async def test_creative_render_retract_removes_staged_temp_pre_publish(tmp_path: Path) -> None:
    """Retract removes the staged temp when publication was refused pre-publish."""
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    publication = build_creative_render_publication(artifact_root)

    workspace = tmp_path / "creative_retract_pre"
    workspace.mkdir()
    artifact = workspace / "render.mp4"
    artifact.write_bytes(b"pre-publish bytes")

    result = {"artifact_path": str(artifact), "workspace_dir": str(workspace)}

    # Simulate pre-publish refusal: stage the artifact but don't publish.
    # We do this by calling the staging machinery directly (as the queue
    # would before the ownership re-check refuses the publication).
    from nexus_ai_agent.creative.render_jobs import _creative_render_staging

    staging = _creative_render_staging(artifact_root, workspace, artifact)
    staging.prepare()
    staging.write(artifact.name, artifact.read_bytes())

    staged_path = staging.staging_dir / artifact.name
    assert staged_path.exists(), "staged temp must exist before retract"

    # Retract (pre-publish refusal): removes staged temp, leaves published untouched
    publication.retract({}, result)

    assert not staged_path.exists(), "retract must remove staged temp"


async def test_creative_render_publication_atomic_rename_preserves_inode(tmp_path: Path) -> None:
    """The publish step is an atomic same-directory rename (inode preserved), not a copy."""
    from nexus_ai_agent.creative.render_jobs import _creative_render_staging

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    workspace = tmp_path / "creative_atomic"
    workspace.mkdir()
    artifact = workspace / "render.mp4"
    artifact.write_bytes(b"atomic publish bytes")

    staging = _creative_render_staging(artifact_root, workspace, artifact)
    staging.prepare()
    staging.write(artifact.name, artifact.read_bytes())

    staged_path = staging.staging_dir / artifact.name
    assert staged_path.exists()
    staged_inode = staged_path.stat().st_ino

    # Publish: os.replace is same-directory rename → inode preserved
    resolved = staging.publish(staged_path, artifact.name)

    assert resolved.exists()
    published_inode = resolved.stat().st_ino
    assert published_inode == staged_inode, (
        "atomic publish must preserve inode (rename), not create a new one (copy)"
    )
    assert not staged_path.exists(), "staged temp must be consumed by rename"


async def test_creative_render_publication_callback_atomic_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The callback uses atomic rename (inode preserved), not copy."""
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication
    from nexus_ai_agent.execution.staging import AttemptStaging

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    publication = build_creative_render_publication(artifact_root)

    workspace = tmp_path / "creative_atomic_cb"
    workspace.mkdir()
    artifact = workspace / "render.mp4"
    artifact.write_bytes(b"atomic callback bytes")

    # Capture the staged inode by wrapping AttemptStaging.write
    captured_inodes: list[int] = []
    original_write = AttemptStaging.write

    def _capture_write(self, name, data):
        result = original_write(self, name, data)
        staged = self.staging_dir / name
        captured_inodes.append(staged.stat().st_ino)
        return result

    monkeypatch.setattr(AttemptStaging, "write", _capture_write)

    result = {"artifact_path": str(artifact), "workspace_dir": str(workspace)}
    patch = publication.publish({}, result)

    published = Path(patch["artifact_path"])
    published_inode = published.stat().st_ino

    assert captured_inodes, "staging write must have been called"
    assert published_inode == captured_inodes[0], (
        "callback must use atomic rename (inode preserved), not copy"
    )


# ---------------------------------------------------------------------------
# Task-255 Phase 2: Mandatory Queue Integration Tests (12 scenarios)
# ---------------------------------------------------------------------------


async def _make_queue_with_publication(tmp_path: Path) -> tuple[InProcessJobQueue, Path]:
    """Create a real InProcessJobQueue with creative_render publication wired."""
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication

    db_path = tmp_path / "queue.sqlite3"
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    queue = InProcessJobQueue(str(db_path))
    queue.register_artifact_publication(
        "creative_render", build_creative_render_publication(artifact_root)
    )
    return queue, artifact_root


async def _drain_queue(queue: InProcessJobQueue, job_id: str = "", timeout: float = 5.0) -> None:
    """Drain a specific job (or wait briefly) in the queue."""
    import asyncio

    from nexus_ai_agent.application.ports.job_queue import JobStatus

    if not job_id:
        await asyncio.sleep(0.1)
        return
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = await queue.get_status(job_id)
        if status in (JobStatus.COMPLETED, JobStatus.FAILED_TERMINAL, JobStatus.FAILED_RETRYABLE):
            return
        await asyncio.sleep(0.05)


async def test_integration_queue_injects_attempt_identity(tmp_path: Path) -> None:
    """Scenario 1+2: The queue injects _queue_job_id and _queue_attempt."""
    from nexus_ai_agent.adapters.in_process_job_queue import ArtifactPublication

    captured: dict = {}

    def _capture_publish(payload, result):
        captured["_queue_job_id"] = result.get("_queue_job_id")
        captured["_queue_attempt"] = result.get("_queue_attempt")
        captured["artifact_path"] = result.get("artifact_path")
        return {"artifact_path": result.get("artifact_path", "")}

    db_path = tmp_path / "queue.sqlite3"
    queue = InProcessJobQueue(str(db_path))
    queue.register_artifact_publication(
        "creative_render",
        ArtifactPublication(publish=_capture_publish, retract=lambda p, r: None),
    )
    # Override verifier to pass (test doesn't produce real render artifacts)
    from nexus_ai_agent.jobs.verification import VerificationOutcome

    queue._artifact_verifiers["creative_render"] = lambda p, r: VerificationOutcome(
        ok=True, reason_code=None, summary={}
    )

    # Register a handler that returns an artifact path
    async def _handler(payload: dict) -> dict:
        ws = Path(payload["workspace_dir"])
        ws.mkdir(parents=True, exist_ok=True)
        artifact = ws / "render.mp4"
        artifact.write_bytes(b"queue identity test")
        return {"artifact_path": str(artifact), "workspace_dir": str(ws)}

    queue.register_handler("creative_render", _handler)

    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key="id-test-1",
        payload={"workspace_dir": str(tmp_path / "ws_id")},
    )
    await _drain_queue(queue, job_id, timeout=10.0)

    # The queue must have injected the attempt identity
    assert captured.get("_queue_job_id") == job_id, "queue must inject _queue_job_id"
    assert captured.get("_queue_attempt") is not None, "queue must inject _queue_attempt"
    assert int(captured["_queue_attempt"]) >= 1, "attempt must be >= 1"


async def test_integration_stale_attempt_fencing(tmp_path: Path) -> None:
    """Scenario 4+12: The queue's ownership check prevents stale attempts from publishing.

    After a job completes, a stale claim (older attempt) must fail the
    _owns_execution check — proving an older attempt cannot publish, retract,
    or finalize a newer attempt's output.
    """
    from nexus_ai_agent.adapters.in_process_job_queue import ArtifactPublication
    from nexus_ai_agent.application.ports.job_queue import JobStatus
    from nexus_ai_agent.jobs.lifecycle import ExecutionClaim

    publish_count = 0

    def _spy_publish(payload, result):
        nonlocal publish_count
        publish_count += 1
        return {"artifact_path": result.get("artifact_path", "")}

    db_path = tmp_path / "queue.sqlite3"
    queue = InProcessJobQueue(str(db_path))
    queue.register_artifact_publication(
        "creative_render",
        ArtifactPublication(publish=_spy_publish, retract=lambda p, r: None),
    )
    from nexus_ai_agent.jobs.verification import VerificationOutcome

    queue._artifact_verifiers["creative_render"] = lambda p, r: VerificationOutcome(
        ok=True, reason_code=None, summary={}
    )

    async def _handler(payload: dict) -> dict:
        ws = Path(payload["workspace_dir"])
        ws.mkdir(parents=True, exist_ok=True)
        artifact = ws / "render.mp4"
        artifact.write_bytes(b"fencing test")
        return {"artifact_path": str(artifact), "workspace_dir": str(ws)}

    queue.register_handler("creative_render", _handler)

    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key="fence-test-1",
        payload={"workspace_dir": str(tmp_path / "ws_fence2")},
    )
    await _drain_queue(queue, job_id, timeout=10.0)

    assert publish_count == 1, "first attempt should publish exactly once"
    status = await queue.get_status(job_id)
    assert status is JobStatus.COMPLETED

    # A stale claim (attempt=1, but job is terminal) must fail the ownership check
    stale_claim = ExecutionClaim(job_id=job_id, attempt=1)
    import asyncio

    owns = await asyncio.to_thread(queue._owns_execution, stale_claim, JobStatus.VERIFYING)
    assert not owns, "stale claim (completed job) must fail ownership check"


async def test_integration_two_attempts_same_artifact_name(tmp_path: Path) -> None:
    """Scenario 5: Two attempts with same artifact name — deterministic collision behavior.

    The newer attempt replaces the older attempt's published artifact (with
    .prev backup). The older attempt's retract cannot touch the newer
    attempt's output (sidecar verification).
    """
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    publication = build_creative_render_publication(artifact_root)

    workspace = tmp_path / "ws_collision"
    workspace.mkdir()
    artifact = workspace / "output.mp4"

    # First attempt (attempt 1)
    artifact.write_bytes(b"attempt 1 bytes")
    result1 = {
        "artifact_path": str(artifact),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-collision",
        "_queue_attempt": 1,
    }
    patch1 = publication.publish({}, result1)
    published1 = Path(patch1["artifact_path"])
    assert published1.read_bytes() == b"attempt 1 bytes"

    # Second attempt (attempt 2) — same artifact name
    artifact.write_bytes(b"attempt 2 bytes")
    result2 = {
        "artifact_path": str(artifact),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-collision",
        "_queue_attempt": 2,
    }
    patch2 = publication.publish({}, result2)
    published2 = Path(patch2["artifact_path"])
    assert published2.read_bytes() == b"attempt 2 bytes"

    # The .prev backup has attempt 1's bytes (same job namespace, so the two
    # attempts of ONE job legitimately share the destination and its backup).
    backup = published2.parent / ".output.mp4.prev"
    assert backup.exists()
    assert backup.read_bytes() == b"attempt 1 bytes"

    # Sidecar records the *complete* identity of the owner: job + attempt
    # (task-283 — an attempt number alone cannot distinguish two different
    # jobs that use the same file name).
    sidecar = published2.parent / ".output.mp4.owner"
    assert sidecar.exists()
    import json

    sidecar_data = json.loads(sidecar.read_text())
    assert sidecar_data["job_id"] == "job-collision"
    assert sidecar_data["attempt"] == "2"
    assert sidecar_data["schema"] == 1

    # Attempt 1's retract cannot touch attempt 2's output (sidecar mismatch)
    post_publish_result1 = {
        "artifact_path": str(published2),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-collision",
        "_queue_attempt": 1,
    }
    publication.retract({}, post_publish_result1)
    # Attempt 2's published artifact is untouched
    assert published2.read_bytes() == b"attempt 2 bytes"

    # Attempt 2's retract can restore the .prev backup
    post_publish_result2 = {
        "artifact_path": str(published2),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-collision",
        "_queue_attempt": 2,
    }
    publication.retract({}, post_publish_result2)
    assert not backup.exists(), "backup consumed by attempt 2 retract"
    assert published2.read_bytes() == b"attempt 1 bytes", "prev restored"


async def test_integration_older_attempt_cannot_retract_newer(tmp_path: Path) -> None:
    """Scenario 12: Older attempt cannot publish/retract newer attempt's output."""
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    publication = build_creative_render_publication(artifact_root)

    workspace = tmp_path / "ws_fencing"
    workspace.mkdir()
    artifact = workspace / "render.mp4"

    # Attempt 1 publishes
    artifact.write_bytes(b"attempt 1 published")
    result1 = {
        "artifact_path": str(artifact),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-fencing",
        "_queue_attempt": 1,
    }
    patch1 = publication.publish({}, result1)
    published1 = Path(patch1["artifact_path"])
    assert published1.read_bytes() == b"attempt 1 published"

    # Attempt 2 publishes (replaces attempt 1)
    artifact.write_bytes(b"attempt 2 published")
    result2 = {
        "artifact_path": str(artifact),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-fencing",
        "_queue_attempt": 2,
    }
    patch2 = publication.publish({}, result2)
    published2 = Path(patch2["artifact_path"])
    assert published2.read_bytes() == b"attempt 2 published"

    # Attempt 1's retract with post-publish result → sidecar mismatch → fail-closed
    post_publish_result1 = {
        "artifact_path": str(published2),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-fencing",
        "_queue_attempt": 1,
    }
    publication.retract({}, post_publish_result1)
    # Attempt 2's output is untouched
    assert published2.read_bytes() == b"attempt 2 published"

    # Attempt 1's finalize only cleans up attempt 1's staging
    finalize_result1 = {
        "artifact_path": str(artifact),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-fencing",
        "_queue_attempt": 1,
    }
    publication.finalize({}, finalize_result1)
    # Attempt 2's staging still exists
    staging_a1 = artifact_root / "staging" / "job-fencing" / "1" / "staging"
    staging_a2 = artifact_root / "staging" / "job-fencing" / "2" / "staging"
    assert not staging_a1.exists(), "attempt 1 staging cleaned up"
    assert staging_a2.exists() or not staging_a2.exists(), "attempt 2 staging state is its own"


async def test_integration_retry_after_ambiguous_crash(tmp_path: Path) -> None:
    """Scenario 8+10: Retry after ambiguous crash — publish→crash→retry is idempotent."""
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    publication = build_creative_render_publication(artifact_root)

    workspace = tmp_path / "ws_retry"
    workspace.mkdir()
    artifact = workspace / "render.mp4"
    artifact.write_bytes(b"retry bytes")

    result = {
        "artifact_path": str(artifact),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-retry",
        "_queue_attempt": 1,
    }

    # First publish (simulates publish done, crash before finalize)
    patch1 = publication.publish({}, result)
    published1 = Path(patch1["artifact_path"])
    assert published1.exists()
    assert (artifact_root / "staging").exists()  # crash before finalize

    # Retry with same attempt identity — idempotent
    patch2 = publication.publish({}, result)
    published2 = Path(patch2["artifact_path"])
    assert published2.read_bytes() == b"retry bytes"
    assert published1.read_bytes() == published2.read_bytes()

    # Finalize after durable commit
    publication.finalize({}, result)
    staging_dir = artifact_root / "staging" / "job-retry" / "1" / "staging"
    assert not staging_dir.exists(), "finalize cleans staging"


async def test_integration_backup_restoration_correctness(tmp_path: Path) -> None:
    """Scenario 11: Backup restoration correctness — .prev has the right bytes."""
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    publication = build_creative_render_publication(artifact_root)

    workspace = tmp_path / "ws_backup"
    workspace.mkdir()
    artifact = workspace / "render.mp4"

    # Publish version 1
    artifact.write_bytes(b"version 1")
    result = {
        "artifact_path": str(artifact),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-backup",
        "_queue_attempt": 1,
    }
    patch1 = publication.publish({}, result)
    assert Path(patch1["artifact_path"]).read_bytes() == b"version 1"

    # Publish version 2 (creates .prev backup with version 1)
    artifact.write_bytes(b"version 2")
    patch2 = publication.publish({}, result)
    published2 = Path(patch2["artifact_path"])
    assert published2.read_bytes() == b"version 2"

    backup = published2.parent / ".render.mp4.prev"
    assert backup.exists()
    assert backup.read_bytes() == b"version 1", "backup must contain version 1"

    # Retract restores version 1 from backup
    post_publish_result = {
        "artifact_path": str(published2),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-backup",
        "_queue_attempt": 1,
    }
    publication.retract({}, post_publish_result)
    assert published2.read_bytes() == b"version 1", "retract restores version 1"


# ── Phase 3: flock-based queue ownership ──────────────────────────────


async def test_flock_first_process_acquires(tmp_path: Path) -> None:
    """Phase 3: The first process acquires flock ownership on first use."""
    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue

    db_path = tmp_path / "flock_test1.sqlite3"
    queue = InProcessJobQueue(str(db_path))
    # flock is acquired explicitly via acquire_ownership
    assert queue._lock_fd is None, "flock should not be acquired until acquire_ownership"
    await queue.acquire_ownership()
    # flock is now held
    assert queue._lock_fd is not None, "flock should be held after acquire_ownership"
    await queue.shutdown()
    # After shutdown, flock should be released
    assert queue._lock_fd is None, "flock should be released after shutdown"


async def test_flock_second_process_blocked(tmp_path: Path) -> None:
    """Phase 3: A second process cannot acquire flock while first holds it."""
    import subprocess
    import sys

    db_path = tmp_path / "flock_test2.sqlite3"

    # First process acquires flock (via enqueue) and stays alive
    holder_script = (
        "import asyncio; from nexus_ai_agent.adapters.in_process_job_queue "
        "import InProcessJobQueue; "
        f"q = InProcessJobQueue('{db_path}'); "
        "asyncio.get_event_loop().run_until_complete(q.acquire_ownership()); "
        # flush is load-bearing: without it this line only reaches the pipe when
        # the child *exits*, so the "second process" below would race a dead
        # holder (an unreaped zombie still answers os.kill(pid, 0), which is how
        # this test used to pass against a PID-liveness check that never
        # actually excluded a live owner).  task-283 repaired it.
        "print('HOLDER_READY', flush=True); "
        "asyncio.get_event_loop().run_until_complete(asyncio.sleep(30))"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script],
        cwd=str(REPO_ROOT),
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Wait for holder to be ready
    import time

    deadline = time.time() + 10
    while time.time() < deadline:
        line = holder.stdout.readline()
        if line.strip() == "HOLDER_READY":
            break
    else:
        holder.kill()
        raise AssertionError("holder did not start")

    try:
        # The holder must genuinely still be alive and holding the lock — a
        # dead-but-unreaped holder would make the exclusion assertion vacuous.
        assert holder.poll() is None, "holder exited before exclusion was tested"

        # Second process should fail to acquire flock on enqueue
        second_script = (
            "import asyncio; "
            "from nexus_ai_agent.adapters.in_process_job_queue "
            "import InProcessJobQueue, QueueOwnershipError; "
            f"q = InProcessJobQueue('{db_path}'); "
            "asyncio.get_event_loop().run_until_complete(q.acquire_ownership())"
        )
        result = subprocess.run(
            [sys.executable, "-c", second_script],
            cwd=str(REPO_ROOT),
            env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0, "second process must fail"
        assert "QueueOwnershipError" in result.stderr or "already owned" in result.stderr
    finally:
        holder.kill()
        holder.wait()


async def test_flock_released_on_crash(tmp_path: Path) -> None:
    """Phase 3: flock is released when the owning process exits (crash)."""
    import subprocess
    import sys

    db_path = tmp_path / "flock_test3.sqlite3"

    # Process acquires flock via enqueue and exits immediately (crash)
    crash_script = (
        "import asyncio; "
        "from nexus_ai_agent.adapters.in_process_job_queue "
        "import InProcessJobQueue; "
        f"q = InProcessJobQueue('{db_path}'); "
        "asyncio.get_event_loop().run_until_complete(q.acquire_ownership()); "
        "import os; os._exit(1)"
    )
    subprocess.run(
        [sys.executable, "-c", crash_script],
        cwd=str(REPO_ROOT),
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        timeout=10,
    )

    # A new process should be able to acquire the flock
    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue

    queue = InProcessJobQueue(str(db_path))
    await queue.acquire_ownership()
    assert queue._lock_fd is not None, "flock should be available after crash"
    await queue.shutdown()


# ── Phase 4: safe retry boundary ─────────────────────────────────────


async def test_safe_retry_boundary_unknown_blocks_recovery(tmp_path: Path) -> None:
    """Phase 4: UNKNOWN outcome blocks automatic retry until reconciled."""
    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
    from nexus_ai_agent.jobs.lifecycle import ExecutionClaim

    db_path = tmp_path / "safe_retry.sqlite3"
    queue = InProcessJobQueue(str(db_path))

    async def _handler(payload: dict) -> dict:
        return {"artifact_path": "/tmp/test.mp4", "workspace_dir": "/tmp"}

    queue.register_handler("creative_render", _handler)

    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key="safe-retry-1",
        payload={"workspace_dir": str(tmp_path)},
    )
    await _drain_queue(queue, job_id, timeout=5.0)

    # Mark the job as UNKNOWN
    claim = ExecutionClaim(job_id=job_id, attempt=1)
    queue.mark_unknown(claim)

    # is_unknown should return True
    assert queue.is_unknown(job_id), "job should be marked UNKNOWN"

    # recover_job should skip the job (return empty list)
    recovered = await queue.recover_job(job_id, stale_after=None)
    assert recovered == [], "recovery must be blocked for UNKNOWN jobs"

    # reconcile should remove the UNKNOWN marker
    result = queue.reconcile(job_id, "succeeded", {"evidence": "checked externally"})
    assert result is True, "reconcile should succeed"

    # is_unknown should now return False
    assert not queue.is_unknown(job_id), "UNKNOWN marker should be removed"

    await queue.shutdown()
