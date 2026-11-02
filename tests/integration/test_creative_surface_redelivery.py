"""P0 — immutable creative workspace and content identity (task-nagar-workspace-redelivery-01).

This module is the *permanent* adversarial regression suite for the redelivery
defect. Every test here runs against the **real** ``InProcessJobQueue``, a real
SQLite file, a real temporary directory and real file hashing. The only mocked
boundary is the Telegram network call itself, and that mock writes to the real
filesystem exactly like ``TelegramFile.download_to_drive`` does.

The invariant under test is one sentence:

    A repeated Telegram message must never cause the system to overwrite,
    delete, substitute or redirect the input of an already-admitted creative
    job. The accepted job identity, the persisted payload, the immutable media
    bytes and the worker-verified SHA-256 must all describe the same request.

Before the fix, three delivery shapes violated it:

* same ``user/chat/message`` identity + *different* media bytes  -> the surface
  ``rmtree``'d the deterministic workspace *before* enqueue, so the admitted
  row kept its payload while the bytes behind ``input_path`` changed (silent
  substitution through the original job id);
* a redelivery whose download failed -> the ``except`` branch ``rmtree``'d the
  same workspace, deleting the input of a job that was still ``pending``;
* a pre-planted symlink at the (publicly derivable) deterministic workspace
  path redirected the download outside ``creative_temp_dir``.

See ``docs/DECISION_LOG.md`` for the decision record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.bot.creative_surface import (
    CREATIVE_RENDER_JOB_TYPE,
    build_creative_handlers,
)
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.render_jobs import creative_render_job
from nexus_ai_agent.jobs.creative_passport import CreativeRequestConflictError

pytestmark = pytest.mark.asyncio

MEDIA_A = b"MEDIA-A-ORIGINAL-PAYLOAD-" * 16
MEDIA_B = b"MEDIA-B-DIFFERENT-PAYLOAD-" * 16
MEDIA_C = b"MEDIA-C-THIRD-PAYLOAD-" * 16

USER_ID = 1
CHAT_ID = 10
MESSAGE_ID = 777
KEY = f"creative:{USER_ID}:{CHAT_ID}:{MESSAGE_ID}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256_hex(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Telegram mocks — they write REAL bytes to a REAL path, like PTB does
# ---------------------------------------------------------------------------


class _File:
    """Plays ``telegram.File``: duck-typed via ``file_path`` / ``download_to_drive``."""

    def __init__(self, content: bytes, suffix: str = ".mp4") -> None:
        self._content = content
        self.file_path = f"videos/in{suffix}"

    async def download_to_drive(self, target: Any) -> None:
        Path(str(target)).write_bytes(self._content)


class _PartialFile(_File):
    """Writes a truncated file, then fails — a real mid-transfer interruption."""

    async def download_to_drive(self, target: Any) -> None:
        Path(str(target)).write_bytes(self._content[:9])
        raise ConnectionError("transfer interrupted (simulated)")


class _Bot:
    def __init__(self, file: Any) -> None:
        self._file = file
        self.requested: list[str] = []

    async def get_file(self, file_id: str) -> Any:
        self.requested.append(file_id)
        return self._file


class _Message:
    def __init__(self, message_id: int = MESSAGE_ID) -> None:
        self.message_id = message_id
        self.reply_to_message: Any = SimpleNamespace(
            video=SimpleNamespace(file_id="fid123", duration=5.0, file_size=1024)
        )
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


def _update(message: _Message) -> Any:
    return SimpleNamespace(
        message=message,
        edited_message=None,
        effective_user=SimpleNamespace(id=USER_ID, language_code="en"),
        effective_chat=SimpleNamespace(id=CHAT_ID),
    )


def _context(args: list[str], file: Any) -> Any:
    return SimpleNamespace(args=args, bot=_Bot(file))


async def deliver(
    handlers: dict[str, Any],
    *,
    content: bytes | None = MEDIA_A,
    message_id: int = MESSAGE_ID,
    args: list[str] | None = None,
    file: Any | None = None,
) -> _Message:
    """One complete Telegram delivery through the real surface handler."""
    message = _Message(message_id)
    media = file if file is not None else _File(content if content is not None else MEDIA_A)
    await handlers["edit"](_update(message), _context(args or ["trim", "0", "5"], media))
    return message


# ---------------------------------------------------------------------------
# Real durable queue / real filesystem fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the trusted creative root for EVERY test in this module.

    Autouse on purpose: a test that forgot to request it would silently write to
    the deployment default and prove nothing about containment.
    """
    root = tmp_path / "creative"
    root.mkdir()
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(root))
    settings_module.get_settings.cache_clear()
    yield root
    settings_module.get_settings.cache_clear()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "durable.sqlite3"


@pytest.fixture()
def queue(db_path: Path) -> InProcessJobQueue:
    return InProcessJobQueue(db_path, artifact_verifiers={})


@pytest.fixture()
def handlers(queue: InProcessJobQueue) -> dict[str, Any]:
    return build_creative_handlers(queue)


def rows(db_path: Path) -> list[sqlite3.Row]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return list(con.execute("SELECT * FROM nexus_job_queue ORDER BY created_at").fetchall())
    finally:
        con.close()


def only_row(db_path: Path) -> sqlite3.Row:
    found = rows(db_path)
    assert len(found) == 1, f"expected exactly one durable row, got {len(found)}"
    return found[0]


def payload_of(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(str(row["payload_json"]))


def assert_unchanged(path: Path, expected_bytes: bytes, label: str) -> None:
    """The single most important assertion in this file."""
    assert path.exists(), f"{label}: input disappeared at {path}"
    assert not path.is_symlink(), f"{label}: input became a symlink at {path}"
    actual = path.read_bytes()
    assert actual == expected_bytes, (
        f"{label}: bytes behind an admitted input changed\n"
        f"  path     : {path}\n"
        f"  expected : {sha256_hex(expected_bytes)}\n"
        f"  actual   : {sha256_hex(actual)}"
    )


# ===========================================================================
# A0. A missing creative root must be created, never fail every request
# ===========================================================================


async def test_missing_creative_root_is_recreated_on_a_warm_process(
    handlers: dict[str, Any], db_path: Path, temp_root: Path
) -> None:
    """A wiped temp root must not permanently refuse every creative request.

    ``get_settings()`` creates ``creative_temp_dir`` — but it is ``lru_cache``d, so
    that happens **once per process**. In production the cache is warm, so if the
    directory disappears afterwards (tmp reaper, empty volume on redeploy, an
    operator clearing scratch space) nothing re-creates it and every later media
    request would fail for the life of the process.

    The pre-change code survived this because it used
    ``workspace.mkdir(parents=True, exist_ok=True)``. This fix replaced that with a
    strict, no-traversal creation that (correctly) refuses to create parents — and
    thereby lost the resilience property without any test noticing, because this
    module's autouse fixture always pre-creates the root.

    The scenario is modelled exactly: warm the settings cache, delete the root,
    then deliver a normal request.
    """
    settings_module.get_settings()  # warm the cache exactly as a running bot would
    shutil.rmtree(temp_root)
    assert not temp_root.exists()

    message = await deliver(handlers, content=MEDIA_A)

    assert temp_root.is_dir(), "the trusted creative root must be recreated on demand"
    row = only_row(db_path)
    payload = payload_of(row)
    assert payload["input_sha256"] == sha256_hex(MEDIA_A)
    assert_unchanged(Path(payload["input_path"]), MEDIA_A, "warm-process delivery")
    assert any(str(row["id"]) in reply for reply in message.replies), (
        f"a recreated root must still admit the job, got replies: {message.replies}"
    )


# ===========================================================================
# A. Duplicate, identical content
# ===========================================================================


async def test_same_key_same_bytes_is_one_job_and_one_immutable_input(
    handlers: dict[str, Any], db_path: Path
) -> None:
    first = await deliver(handlers, content=MEDIA_A)
    first_row = only_row(db_path)
    first_payload = payload_of(first_row)
    input_path = Path(first_payload["input_path"])
    expected_digest = sha256_hex(MEDIA_A)

    second = await deliver(handlers, content=MEDIA_A)

    assert len(rows(db_path)) == 1, "an identical redelivery must not create a second row"
    second_row = only_row(db_path)
    assert str(second_row["id"]) == str(first_row["id"])
    assert payload_of(second_row) == first_payload, "the persisted payload must be stable"
    assert payload_of(second_row)["input_sha256"] == expected_digest
    assert_unchanged(input_path, MEDIA_A, "identical redelivery")
    # Both deliveries were told about the SAME logical job.
    assert any(str(first_row["id"]) in reply for reply in first.replies)
    assert any(str(first_row["id"]) in reply for reply in second.replies)


async def test_persisted_payload_carries_the_measured_digest(
    handlers: dict[str, Any], db_path: Path
) -> None:
    await deliver(handlers, content=MEDIA_A)
    payload = payload_of(only_row(db_path))
    assert payload["input_sha256"] == sha256_hex(MEDIA_A), (
        "the persisted payload must name the exact bytes it was admitted with"
    )
    assert payload["job_type"] if "job_type" in payload else True
    assert payload["idempotency_key"] == KEY


# ===========================================================================
# B. Duplicate, different content  (the core P0 defect)
# ===========================================================================


async def test_same_key_changed_bytes_conflicts_and_original_bytes_survive(
    handlers: dict[str, Any], db_path: Path
) -> None:
    await deliver(handlers, content=MEDIA_A)
    admitted = only_row(db_path)
    admitted_payload = payload_of(admitted)
    input_path = Path(admitted_payload["input_path"])

    conflicting = await deliver(handlers, content=MEDIA_B)

    # 1) no second logical job, and the admitted payload is untouched
    assert len(rows(db_path)) == 1
    assert payload_of(only_row(db_path)) == admitted_payload
    # 2) the bytes behind the admitted input are still EXACTLY the admitted bytes
    assert_unchanged(input_path, MEDIA_A, "conflicting redelivery")
    # 3) the user was told, and was NOT told "queued"
    assert conflicting.replies, "a conflicting delivery must produce a reply"
    assert not any("job" in reply and "⏳" in reply for reply in conflicting.replies), (
        f"a conflicting delivery must never report a queued job: {conflicting.replies}"
    )


async def test_conflicting_redelivery_raises_the_canonical_conflict_error(
    queue: InProcessJobQueue, db_path: Path, temp_root: Path
) -> None:
    """The queue itself must refuse the changed request once the digest is in the payload.

    This asserts the *queue contract* directly, so a surface-level workaround can
    never mask a regression in the durable conflict semantics.
    """
    handlers = build_creative_handlers(queue)
    await deliver(handlers, content=MEDIA_A)
    admitted = only_row(db_path)
    payload = payload_of(admitted)

    changed = dict(payload)
    changed["input_sha256"] = sha256_hex(MEDIA_B)
    with pytest.raises(CreativeRequestConflictError):
        await queue.enqueue(job_type=CREATIVE_RENDER_JOB_TYPE, idempotency_key=KEY, payload=changed)


# ===========================================================================
# C. Concurrency — real coroutines, real SQLite
# ===========================================================================


async def test_concurrent_identical_deliveries_produce_one_job(
    handlers: dict[str, Any], db_path: Path
) -> None:
    results = await asyncio.gather(
        *(deliver(handlers, content=MEDIA_A) for _ in range(4)), return_exceptions=True
    )
    for result in results:
        assert not isinstance(result, BaseException), f"concurrent delivery raised: {result!r}"

    assert len(rows(db_path)) == 1, "concurrent identical deliveries must collapse to one job"
    payload = payload_of(only_row(db_path))
    assert_unchanged(Path(payload["input_path"]), MEDIA_A, "concurrent identical")


async def test_concurrent_conflicting_deliveries_never_produce_torn_bytes(
    handlers: dict[str, Any], db_path: Path
) -> None:
    """Four concurrent deliveries of two different contents under one identity.

    The requirement is not "which one wins" — it is that whatever is admitted is
    intact, and that no reader can ever observe a half-written or substituted file
    behind the admitted path.
    """
    contents = [MEDIA_A, MEDIA_B, MEDIA_A, MEDIA_B]
    await asyncio.gather(
        *(deliver(handlers, content=content) for content in contents), return_exceptions=True
    )

    assert len(rows(db_path)) == 1, "conflicting concurrent deliveries must not fork the job"
    payload = payload_of(only_row(db_path))
    input_path = Path(payload["input_path"])
    assert input_path.exists()

    actual = input_path.read_bytes()
    admitted = payload["input_sha256"]
    assert sha256_hex(actual) == admitted, (
        "the bytes behind the admitted input must hash to the admitted digest; "
        "a torn or substituted file means the invariant is broken"
    )
    assert actual in (MEDIA_A, MEDIA_B), "the admitted input is neither candidate content"


# ===========================================================================
# D. Partial download
# ===========================================================================


async def test_partial_download_publishes_nothing_and_preserves_admitted_input(
    handlers: dict[str, Any], db_path: Path
) -> None:
    await deliver(handlers, content=MEDIA_A)
    admitted = only_row(db_path)
    input_path = Path(payload_of(admitted)["input_path"])

    partial = await deliver(handlers, file=_PartialFile(MEDIA_B))

    assert len(rows(db_path)) == 1
    assert_unchanged(input_path, MEDIA_A, "after a partial download")
    assert partial.replies, "a failed download must be reported"
    assert not any("⏳" in reply for reply in partial.replies)


async def test_download_exception_preserves_admitted_input(
    handlers: dict[str, Any], db_path: Path
) -> None:
    await deliver(handlers, content=MEDIA_A)
    input_path = Path(payload_of(only_row(db_path))["input_path"])

    class _Raising(_File):
        async def download_to_drive(self, target: Any) -> None:
            raise ConnectionError("downloader blew up before writing")

    failed = await deliver(handlers, file=_Raising(b""))
    assert_unchanged(input_path, MEDIA_A, "after a download exception")
    assert failed.replies


# ===========================================================================
# E. Enqueue failure
# ===========================================================================


async def test_enqueue_failure_never_deletes_an_admitted_input(
    queue: InProcessJobQueue, temp_root: Path, monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    handlers = build_creative_handlers(queue)
    await deliver(handlers, content=MEDIA_A)
    input_path = Path(payload_of(only_row(db_path))["input_path"])

    async def _boom(**kwargs: Any) -> str:
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(queue, "enqueue", _boom)
    failed = await deliver(handlers, content=MEDIA_B)

    assert_unchanged(input_path, MEDIA_A, "after an enqueue failure")
    assert failed.replies, "an enqueue failure must be reported"


async def test_enqueue_failure_leaves_no_admitted_partial_state(
    queue: InProcessJobQueue, monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    handlers = build_creative_handlers(queue)

    async def _boom(**kwargs: Any) -> str:
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(queue, "enqueue", _boom)
    await deliver(handlers, content=MEDIA_A)
    assert rows(db_path) == [], "a failed enqueue must not leave a durable row"


# ===========================================================================
# F. Restart and redelivery
# ===========================================================================


async def test_restart_then_redelivery_preserves_the_original_digest(
    temp_root: Path, db_path: Path
) -> None:
    first_queue = InProcessJobQueue(db_path, artifact_verifiers={})
    await deliver(build_creative_handlers(first_queue), content=MEDIA_A)
    admitted = only_row(db_path)
    admitted_payload = payload_of(admitted)
    input_path = Path(admitted_payload["input_path"])

    # A different process/instance reads the SAME durable database.
    second_queue = InProcessJobQueue(db_path, artifact_verifiers={})
    await deliver(build_creative_handlers(second_queue), content=MEDIA_A)

    assert len(rows(db_path)) == 1
    assert payload_of(only_row(db_path)) == admitted_payload
    assert_unchanged(input_path, MEDIA_A, "after restart + identical redelivery")

    # ... and a DIFFERENT content after restart is still refused, not substituted.
    await deliver(build_creative_handlers(second_queue), content=MEDIA_B)
    assert_unchanged(input_path, MEDIA_A, "after restart + conflicting redelivery")


# ===========================================================================
# G. Path and symlink attacks
# ===========================================================================


async def test_preplanted_workspace_symlink_cannot_redirect_the_download(
    handlers: dict[str, Any], temp_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "OUTSIDE"
    outside.mkdir()
    # The workspace name is derived from a PUBLIC formula, so an attacker can
    # predict it and pre-plant a symlink there.
    from nexus_ai_agent.bot.creative_surface import _workspace_for

    workspace = _workspace_for(KEY, str(temp_root))
    workspace.symlink_to(outside, target_is_directory=True)

    await deliver(handlers, content=MEDIA_A)

    assert list(outside.iterdir()) == [], (
        f"no bytes may be written outside the creative root: {list(outside.iterdir())}"
    )


async def test_a_symlinked_creative_temp_dir_is_refused_by_the_real_dispatch_path(
    handlers: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    """The CONFIGURED root being a symlink must be refused before any write.

    This test deliberately goes through the real handler, not
    ``_ensure_private_directory``. The helper-level test
    (``tests/unit/test_creative_surface.py::test_private_directory_still_refuses_a_symlinked_root``)
    passes while the production path is unsafe, because ``_dispatch`` calls
    ``Path(settings.creative_temp_dir).resolve(strict=False)`` FIRST: resolve()
    canonicalizes the symlink away, so ``_refuse_symlinked_directory`` only ever
    sees the real target and can never observe that the configured root was a link.

    Consequence without this test: an operator or an attacker who can replace the
    configured creative directory with a symlink silently redirects every creative
    download into an unrelated directory. Reproduced before the fix: the job was
    queued and bytes landed in the unrelated directory.
    """
    unrelated = tmp_path / "unrelated_secret_dir"
    unrelated.mkdir()
    configured = tmp_path / "creative_link"
    configured.symlink_to(unrelated, target_is_directory=True)

    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(configured))
    settings_module.get_settings.cache_clear()

    message = await deliver(handlers, content=MEDIA_A)

    assert list(unrelated.iterdir()) == [], (
        "a symlinked creative root must never redirect creative writes into "
        f"another directory; found: {[p.name for p in unrelated.iterdir()]}"
    )
    assert rows(db_path) == [], "a refused delivery must not enqueue a job"
    assert message.replies, "the user must be told the request was refused"
    assert not any("⏳" in reply for reply in message.replies), (
        f"a refused delivery must never report a queued job: {message.replies}"
    )


async def test_a_workspace_symlink_inside_the_root_is_still_refused(
    handlers: dict[str, Any], temp_root: Path, db_path: Path
) -> None:
    """Containment alone would accept this: the link target is another workspace.

    Only an explicit symlink refusal keeps one request's download out of another
    request's directory, so this is the test that pins that refusal.
    """
    from nexus_ai_agent.bot.creative_surface import _workspace_for

    other = temp_root / "creative_aaaaaaaaaaaa"
    other.mkdir()
    workspace = _workspace_for(KEY, str(temp_root))
    workspace.symlink_to(other, target_is_directory=True)

    message = await deliver(handlers, content=MEDIA_A)

    assert list(other.iterdir()) == [], (
        "a delivery must not write through a symlink into another workspace: "
        f"{list(other.iterdir())}"
    )
    # The surface refuses and leaves the planted link untouched: deleting a path an
    # attacker owns would itself be an unauthorized filesystem mutation.
    assert workspace.is_symlink(), "the planted link must not be rewritten or removed"
    assert rows(db_path) == [], "a delivery through a pre-planted symlink must not be admitted"
    assert message.replies and not any("⏳" in reply for reply in message.replies), (
        f"the delivery must be reported as failed, not queued: {message.replies}"
    )


async def test_preplanted_input_symlink_is_refused(
    handlers: dict[str, Any], tmp_path: Path
) -> None:
    outside_file = tmp_path / "outside.bin"
    outside_file.write_bytes(b"ATTACKER-CONTROLLED")

    from nexus_ai_agent.bot.creative_surface import _workspace_for

    root = Path(os.environ["CREATIVE_TEMP_DIR"])
    workspace = _workspace_for(KEY, str(root))
    workspace.mkdir(parents=True, exist_ok=True)
    for name in ("input.mp4", f"input-{sha256_hex(MEDIA_A)}.mp4"):
        (workspace / name).symlink_to(outside_file)

    await deliver(handlers, content=MEDIA_A)

    assert outside_file.read_bytes() == b"ATTACKER-CONTROLLED", (
        "the surface must never write through a pre-planted input symlink"
    )


async def test_media_less_operation_keeps_its_existing_behaviour(
    handlers: dict[str, Any], db_path: Path
) -> None:
    message = _Message(4242)
    await handlers["grade"](_update(message), _context(["otio"], _File(b"")))
    row = only_row(db_path)
    payload = payload_of(row)
    assert payload["input_path"] is None
    assert payload.get("input_sha256") is None
    assert Path(payload["workspace_dir"]).is_dir()


async def test_ovresize_actual_bytes_are_rejected(
    handlers: dict[str, Any], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limit must be enforced against the ACTUAL download, not Telegram's metadata.

    ``file_size=1024`` in the fixture claims 1 KiB; the real payload is far larger.
    """
    from nexus_ai_agent.bot import creative_surface as surface_module

    monkeypatch.setattr(surface_module, "MAX_MEDIA_BYTES", 64)
    message = await deliver(handlers, content=b"X" * 4096)
    assert rows(db_path) == [], "oversized actual bytes must not be admitted"
    assert message.replies and not any("⏳" in reply for reply in message.replies)


# ===========================================================================
# H. Worker-side independent verification (the bytes are the authority)
# ===========================================================================


async def test_worker_refuses_a_digest_that_does_not_match_the_bytes(
    handlers: dict[str, Any], db_path: Path
) -> None:
    await deliver(handlers, content=MEDIA_A)
    payload = payload_of(only_row(db_path))
    input_path = Path(payload["input_path"])

    # Someone swapped the bytes after admission, leaving the payload intact.
    input_path.write_bytes(MEDIA_B)

    result = await creative_render_job(payload)
    assert result["success"] is False
    assert result["error_code"] == "input_integrity"
    assert sha256_hex(MEDIA_A) in result["error_detail"] or "digest" in result["error_detail"]


async def test_worker_refuses_before_touching_any_engine_on_digest_mismatch(
    handlers: dict[str, Any], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.creative import render_jobs

    await deliver(handlers, content=MEDIA_A)
    payload = payload_of(only_row(db_path))
    Path(payload["input_path"]).write_bytes(MEDIA_B)

    touched: list[str] = []

    def _spy(*args: Any, **kwargs: Any) -> Any:
        touched.append("engine")
        raise AssertionError("the render engine must not be reached on a digest mismatch")

    monkeypatch.setattr(render_jobs, "_run_render_branch", _spy)
    monkeypatch.setattr(render_jobs, "_run_caption_branch", _spy)
    monkeypatch.setattr(render_jobs, "_run_otio_branch", _spy)

    result = await creative_render_job(payload)
    assert result["success"] is False
    assert result["error_code"] == "input_integrity"
    assert touched == [], "no engine may run before the digest is verified"


async def test_worker_refuses_a_legacy_media_payload_without_a_digest(
    handlers: dict[str, Any], db_path: Path
) -> None:
    """Documented legacy policy: fail closed, never silently 'verified'.

    ``nagar/creative/handoff.py`` is a second producer of media-bearing payloads
    that predates the digest contract and is outside this task's zone. Such a row
    is refused with a typed error and requires operator reconciliation; it is
    never executed as if it were verified.
    """
    await deliver(handlers, content=MEDIA_A)
    payload = payload_of(only_row(db_path))
    legacy = dict(payload)
    legacy.pop("input_sha256")

    result = await creative_render_job(legacy)
    assert result["success"] is False
    assert result["error_code"] == "input_integrity"
    assert "input_sha256" in result["error_detail"]


async def test_worker_refuses_a_symlink_that_stays_inside_the_workspace(
    handlers: dict[str, Any], db_path: Path
) -> None:
    """The hard case: the link target is INSIDE the workspace and hashes correctly.

    Containment (`relative_to(workspace)`) therefore passes and the digest matches,
    so only the ``lstat``/``O_NOFOLLOW`` file-type check can refuse this. If that
    check is removed, this test is the one that goes red.
    """
    await deliver(handlers, content=MEDIA_A)
    payload = payload_of(only_row(db_path))
    input_path = Path(payload["input_path"])
    decoy = input_path.parent / "decoy.bin"
    decoy.write_bytes(MEDIA_A)  # same bytes, INSIDE the workspace

    input_path.unlink()
    input_path.symlink_to(decoy)

    result = await creative_render_job(payload)
    assert result["success"] is False, "a symlinked input must never render"
    assert result["error_code"] == "input_integrity", (
        f"expected a typed integrity refusal, got {result}"
    )
    assert "symlink" in result["error_detail"]


async def test_worker_refuses_a_symlinked_input_pointing_outside(
    handlers: dict[str, Any], db_path: Path
) -> None:
    await deliver(handlers, content=MEDIA_A)
    payload = payload_of(only_row(db_path))
    input_path = Path(payload["input_path"])
    outside = input_path.parent.parent / "outside.bin"
    outside.write_bytes(MEDIA_A)
    input_path.unlink()
    input_path.symlink_to(outside)

    result = await creative_render_job(payload)
    assert result["success"] is False, "a symlinked input must never render"
    # Containment may fire first when the link escapes the workspace; both are typed
    # refusals and neither reaches an engine.
    assert result["error_code"] in {"input_integrity", "media_missing", "invalid_request"}


async def test_worker_refuses_a_workspace_outside_the_creative_root(
    handlers: dict[str, Any], db_path: Path, tmp_path: Path
) -> None:
    await deliver(handlers, content=MEDIA_A)
    payload = payload_of(only_row(db_path))
    escaped = dict(payload)
    escaped["workspace_dir"] = str(tmp_path / "elsewhere" / "creative_x")
    result = await creative_render_job(escaped)
    assert result["success"] is False
    assert result["error_code"] == "invalid_request"


async def test_a_corrupt_immutable_destination_is_refused_not_reused(
    handlers: dict[str, Any], db_path: Path
) -> None:
    """Reuse of an existing content-addressed input must verify its bytes.

    The only reason a destination can pre-exist is a previous delivery of the SAME
    content. If those bytes are wrong, reuse would silently re-admit corrupt media,
    so publication must fail closed instead of trusting the filename.
    """
    from nexus_ai_agent.bot import creative_surface as surface_module

    root = Path(os.environ["CREATIVE_TEMP_DIR"])
    workspace = surface_module._workspace_for(KEY, str(root))
    workspace.mkdir(parents=True, exist_ok=True)
    poisoned = workspace / f"input-{sha256_hex(MEDIA_A)}.mp4"
    poisoned.write_bytes(MEDIA_B)  # correct name, wrong bytes

    message = await deliver(handlers, content=MEDIA_A)

    assert poisoned.read_bytes() == MEDIA_B, "the surfaced must not rewrite the poisoned file"
    assert rows(db_path) == [], "a corrupt destination must not be admitted"
    assert message.replies and not any("⏳" in reply for reply in message.replies)


# ===========================================================================
# I. Staging hygiene
# ===========================================================================


async def test_no_staging_residue_survives_a_successful_delivery(
    handlers: dict[str, Any], temp_root: Path, db_path: Path
) -> None:
    await deliver(handlers, content=MEDIA_A)
    leftovers = [
        entry.name
        for entry in temp_root.iterdir()
        if entry.name != Path(payload_of(only_row(db_path))["workspace_dir"]).name
    ]
    assert leftovers == [], f"staging residue left behind: {leftovers}"


async def test_no_staging_residue_survives_a_partial_download(
    handlers: dict[str, Any], temp_root: Path, db_path: Path
) -> None:
    await deliver(handlers, file=_PartialFile(MEDIA_B))
    residue = [entry.name for entry in temp_root.iterdir() if entry.name.startswith("staging_")]
    assert residue == [], f"a failed delivery must clean up its own staging: {residue}"
    assert rows(db_path) == [], "a failed download must not admit a job"
    # The deterministic workspace may exist (empty). What must NOT exist is any file.
    files = [p for p in temp_root.rglob("*") if p.is_file()]
    assert files == [], f"a failed download must publish no file at all: {files}"


async def test_the_workspace_name_is_still_worker_compatible(
    handlers: dict[str, Any], db_path: Path
) -> None:
    await deliver(handlers, content=MEDIA_A)
    payload = payload_of(only_row(db_path))
    assert Path(payload["workspace_dir"]).name.startswith("creative_")
    assert Path(payload["input_path"]).parent == Path(payload["workspace_dir"])
