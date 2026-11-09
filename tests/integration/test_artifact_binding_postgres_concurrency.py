"""The artifact-binding ownership fence on the real backend, across processes.

The fencing token the queue mints is **per-job**, so a predicate that compares
``artifact_id`` alone is not sufficient: two independent jobs can publish
identical bytes and must not be able to seize (or retract) each other's
binding.  ``tests/integration/test_durable_project_revision_pinning.py`` proves
the single-process semantics on SQLite; this module proves the *same* contract
on PostgreSQL with **real, separate OS processes**, which is the only way to
exercise two independent connections racing on one row:

* a cross-job bind at a HIGHER token never takes ownership of the canonical
  row (idempotency is defined on ``(project_id, artifact_id)``, not the job);
* neither job's retraction can delete a row the other job wrote;
* the owner's exact ``(job_id, attempt)`` retraction — and only that — deletes;
* the token advance is a single conditional ``UPDATE``, so two processes
  racing to advance it can never leave the LOWER token behind (the atomic
  statement that replaced a read-modify-write, F1).

Runs only where ``NEXUS_DATABASE_URL`` is set **and** the explicit
``NEXUS_PG_TEST_DESTRUCTIVE=1`` opt-in is present (D-0028): the URL alone may
point at an operator's real database.  Every row is scoped to a per-run nonce
and removed afterwards, with a survival assertion over the rows that existed
before the module ran.
"""

from __future__ import annotations

import os
import selectors
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

from nexus_ai_agent.creative.project_state.store import (
    SqlArtifactBindingStore,
    SqlProjectRevisionStore,
)
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    Timeline,
    compute_state_hash,
    new_project,
)
from nexus_ai_agent.storage.db import sync_engine

DESTRUCTIVE_OPT_IN = "NEXUS_PG_TEST_DESTRUCTIVE"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.skipif(not os.getenv("NEXUS_DATABASE_URL"), reason="requires PostgreSQL"),
    # Strict equality, never `bool(os.getenv(...))`: a non-empty false-like value
    # such as "0", "false" or "no" is truthy as a string and would silently arm
    # this module against whatever NEXUS_DATABASE_URL names (D-0028).
    pytest.mark.skipif(os.getenv(DESTRUCTIVE_OPT_IN) != "1", reason="requires destructive opt-in"),
    pytest.mark.asyncio(loop_scope="module"),
]

# The opt-in must never turn into a silent skip: armed without a database, every
# test below would collect as "skipped" and the build would report green while
# the fence proof — the reason this module exists — went unverified.
if os.getenv(DESTRUCTIVE_OPT_IN) == "1" and not os.getenv("NEXUS_DATABASE_URL"):
    pytest.fail(
        f"{DESTRUCTIVE_OPT_IN}=1 is set but NEXUS_DATABASE_URL is not: this module "
        "would collect as skipped, so the ownership-fence proof it exists to "
        "verify would go unverified while the build stayed green. Set both "
        "variables, or neither.",
        pytrace=False,
    )

_SOURCE_DURATION_US = 2_000_000


def _project(project_id: str, name: str = "pinned"):
    project = new_project(
        project_id, name, Timeline(timeline_id="tl", duration_us=_SOURCE_DURATION_US)
    ).model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="src",
                    media_kind="video",
                    content_sha256="sha256:" + "0" * 64,
                    duration_us=_SOURCE_DURATION_US,
                )
            ]
        }
    )
    project.state_hash = compute_state_hash(project)
    return project


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    return env


def _run_child(script: str, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _spawn_child(script: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _reap(proc: subprocess.Popen[str]) -> str:
    """Kill and join a child so a barrier-stalled process is never orphaned.

    Returns the child's stderr text (empty when already drained), so a caller can
    surface *why* a child failed after it has been reaped.
    """
    if proc.poll() is None:
        proc.kill()
    try:
        _, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        proc.wait(timeout=30)
        err = ""
    return err or ""


def _wait_line(proc: subprocess.Popen[str], expected: str, timeout: float = 60.0) -> None:
    """Wait for *expected* on stdout, bounded by a real deadline.

    ``proc.stdout.readline()`` blocks on a pipe with no timeout, so a child that
    stays alive without ever printing the line would hang ``_wait_line`` past its
    deadline and stall the whole CI job.  A selector-driven read wakes at the
    deadline; on timeout the child is terminated and reaped (no orphan) and a
    useful error is raised.
    """
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    buffered = ""
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not selector.select(remaining):
                break  # deadline expired with no output
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break  # EOF: the child closed stdout (exited or crashed)
            buffered += chunk.decode(errors="replace")
            while "\n" in buffered:
                line, buffered = buffered.split("\n", 1)
                if line.strip() == expected:
                    return
    finally:
        selector.close()
    stderr = _reap(proc)
    raise AssertionError(f"child never printed {expected!r} within {timeout}s: {stderr}")


_BIND_CHILD = """
import asyncio, sys
from nexus_ai_agent.creative.project_state.store import SqlArtifactBindingStore

async def main():
    store = SqlArtifactBindingStore()
    b = await store.bind(
        project_id={project_id!r},
        revision_id={revision_id!r},
        artifact_id={artifact_id!r},
        job_id={job_id!r},
        size_bytes=1,
        attempt={attempt},
    )
    print("BOUND", b.job_id, b.attempt, flush=True)

asyncio.run(main())
"""

_RETRACT_CHILD = """
import asyncio, sys
from nexus_ai_agent.creative.project_state.store import SqlArtifactBindingStore

async def main():
    removed = await SqlArtifactBindingStore().retract(
        artifact_id={artifact_id!r}, job_id={job_id!r}, attempt={attempt}
    )
    print("RETRACTED", removed, flush=True)

asyncio.run(main())
"""

_RACE_CHILD = """
import asyncio, os, sys
from nexus_ai_agent.creative.project_state.store import SqlArtifactBindingStore
from nexus_ai_agent.storage.db import get_session

async def main():
    go = {go_file!r}
    # Warm the schema BEFORE the barrier.  Two processes booting at once would
    # otherwise contend on the local migration fcntl lock (a bootstrap artifact,
    # not the fence); the parent waits for each READY in turn, so warm-up is
    # serialised while the bind itself still races.
    async with get_session():
        pass
    print("READY", flush=True)
    while not os.path.exists(go):
        await asyncio.sleep(0.01)
    b = await SqlArtifactBindingStore().bind(
        project_id={project_id!r},
        revision_id={revision_id!r},
        artifact_id={artifact_id!r},
        job_id="job-race",
        size_bytes=1,
        attempt={attempt},
    )
    print("BOUND", b.attempt, flush=True)

asyncio.run(main())
"""


async def _bind(
    project_id: str,
    revision_id: str,
    artifact_id: str,
    job_id: str,
    attempt: int,
):
    return await SqlArtifactBindingStore().bind(
        project_id=project_id,
        revision_id=revision_id,
        artifact_id=artifact_id,
        job_id=job_id,
        size_bytes=1,
        attempt=attempt,
    )


def _binding_owner(artifact_id: str) -> tuple[str, int] | None:
    """Read a binding's ``(job_id, attempt)`` directly from PostgreSQL.

    Querying the database out-of-band — not the child's printed ``RETRACTED``
    value — is what proves the DELETE *committed*: if ``retract()`` executed the
    DELETE but omitted its ``commit()``, the child would roll it back on exit and
    print ``True`` while the row survived.
    """
    engine = sync_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT job_id, attempt FROM projectartifactbinding WHERE artifact_id = :aid"),
            {"aid": artifact_id},
        ).fetchone()
    return (str(row[0]), int(row[1])) if row is not None else None


@pytest.fixture(scope="module")
def nonce() -> str:
    return "pgfence-" + uuid.uuid4().hex[:12]


@pytest.fixture(scope="module", autouse=True)
def _survival_baseline() -> Iterator[None]:
    """Snapshot/verify bindings that existed before the module (D-0028)."""
    engine = sync_engine()
    with engine.connect() as conn:
        before = set(conn.execute(text("SELECT artifact_id FROM projectartifactbinding")).scalars())
    yield
    with engine.connect() as conn:
        after = set(conn.execute(text("SELECT artifact_id FROM projectartifactbinding")).scalars())
    assert before <= after, "the module must never delete a binding it did not create"


@pytest.fixture(scope="module")
def project_id(nonce: str) -> str:
    return nonce


@pytest.fixture(scope="module")
def cleanup(nonce: str) -> Iterator[None]:
    yield
    engine = sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM projectartifactbinding WHERE project_id = :pid"), {"pid": nonce}
        )
        conn.execute(
            text("DELETE FROM projectrevisionhead WHERE project_id = :pid"), {"pid": nonce}
        )
        conn.execute(text("DELETE FROM projectrevision WHERE project_id = :pid"), {"pid": nonce})
        conn.execute(text("DELETE FROM appproject WHERE project_id = :pid"), {"pid": nonce})


async def _seed_revision(project_id: str) -> str:
    store = SqlProjectRevisionStore()
    record = await store.commit(
        _project(project_id), authorized_project_id=project_id, expected_parent_revision_id=None
    )
    return record.revision_id


# ── cross-job ownership across real processes ────────────────────────────────


async def test_cross_job_bind_never_seizes_and_only_the_owner_retracts(
    project_id: str, cleanup: None
) -> None:
    revision_id = await _seed_revision(project_id)
    artifact_id = "sha256:" + "1" * 64

    # Job A owns the row at token 2 — a real, separate process.
    owner = _run_child(
        _BIND_CHILD.format(
            project_id=project_id,
            revision_id=revision_id,
            artifact_id=artifact_id,
            job_id="job-A",
            attempt=2,
        )
    )
    assert owner.returncode == 0, owner.stderr
    assert "BOUND job-A 2" in owner.stdout

    # Job B, an independent process with the same bytes and a HIGHER token,
    # must be a cross-job idempotent no-op, not a takeover.
    second = _run_child(
        _BIND_CHILD.format(
            project_id=project_id,
            revision_id=revision_id,
            artifact_id=artifact_id,
            job_id="job-B",
            attempt=7,
        )
    )
    assert second.returncode == 0, second.stderr
    assert "BOUND job-A 2" in second.stdout, "a different job must never seize the binding"

    resolved = await SqlArtifactBindingStore().resolve(project_id, artifact_id)
    assert (resolved.job_id, resolved.attempt) == ("job-A", 2)

    # Job B's retraction (its own job_id AND attempt) removes nothing.
    b_retract = _run_child(
        _RETRACT_CHILD.format(artifact_id=artifact_id, job_id="job-B", attempt=7)
    )
    assert b_retract.returncode == 0, b_retract.stderr
    assert "RETRACTED False" in b_retract.stdout
    assert _binding_owner(artifact_id) == ("job-A", 2), (
        "a cross-job retraction must leave the owner's committed row intact"
    )

    # Only the owner's exact (job_id, attempt) retracts — in its own process.
    a_retract = _run_child(
        _RETRACT_CHILD.format(artifact_id=artifact_id, job_id="job-A", attempt=2)
    )
    assert a_retract.returncode == 0, a_retract.stderr
    assert "RETRACTED True" in a_retract.stdout
    assert _binding_owner(artifact_id) is None, (
        "the owner's retraction must COMMIT the DELETE: the row is gone from PostgreSQL"
    )


# ── same-job fencing across real processes ───────────────────────────────────


async def test_same_job_stale_attempt_cannot_retract_across_processes(
    project_id: str, cleanup: None
) -> None:
    revision_id = await _seed_revision(project_id)
    artifact_id = "sha256:" + "2" * 64

    for attempt in (1, 5):
        proc = _run_child(
            _BIND_CHILD.format(
                project_id=project_id,
                revision_id=revision_id,
                artifact_id=artifact_id,
                job_id="job-X",
                attempt=attempt,
            )
        )
        assert proc.returncode == 0, proc.stderr

    resolved = await SqlArtifactBindingStore().resolve(project_id, artifact_id)
    assert resolved.attempt == 5, "the newer attempt must own the token"

    stale = _run_child(_RETRACT_CHILD.format(artifact_id=artifact_id, job_id="job-X", attempt=1))
    assert "RETRACTED False" in stale.stdout
    assert _binding_owner(artifact_id) == ("job-X", 5), (
        "a stale attempt's retraction must not remove the live row"
    )
    live = _run_child(_RETRACT_CHILD.format(artifact_id=artifact_id, job_id="job-X", attempt=5))
    assert "RETRACTED True" in live.stdout
    assert _binding_owner(artifact_id) is None, "the live attempt's DELETE must commit"


# ── the token advance is atomic under a real race ────────────────────────────


async def test_racing_advances_never_leave_the_lower_token(project_id: str, cleanup: None) -> None:
    revision_id = await _seed_revision(project_id)
    artifact_id = "sha256:" + "3" * 64
    seeded = await _bind(project_id, revision_id, artifact_id, "job-race", 1)
    assert seeded.attempt == 1

    go_file = REPO_ROOT / f".pgfence-go-{uuid.uuid4().hex}"
    low = _spawn_child(
        _RACE_CHILD.format(
            go_file=str(go_file),
            project_id=project_id,
            revision_id=revision_id,
            artifact_id=artifact_id,
            attempt=3,
        )
    )
    high: subprocess.Popen[str] | None = None
    try:
        # Boot the children one at a time so their schema warm-up (a local
        # migration fcntl lock) never contends; each is parked at the file
        # barrier before the next starts, so the binds still race.
        _wait_line(low, "READY")
        high = _spawn_child(
            _RACE_CHILD.format(
                go_file=str(go_file),
                project_id=project_id,
                revision_id=revision_id,
                artifact_id=artifact_id,
                attempt=5,
            )
        )
        _wait_line(high, "READY")
        # Release both together so the conditional UPDATEs contend on one row.
        go_file.write_text("go")
        low_out, low_err = low.communicate(timeout=120)
        high_out, high_err = high.communicate(timeout=120)
        assert low.returncode == 0, low_err
        assert high.returncode == 0, high_err
        # The higher attempt always observes the row at 5 (it advances it, or
        # finds it already there).  The lower attempt observes 3 only if it won
        # the write first; otherwise its authoritative re-read sees the
        # successor's 5 — never a stale in-memory value.  Either way the row
        # never regresses below the higher token.
        assert "BOUND 5" in high_out
        assert "BOUND 3" in low_out or "BOUND 5" in low_out, low_out
    finally:
        _reap(low)
        if high is not None:
            _reap(high)
        go_file.unlink(missing_ok=True)

    resolved = await SqlArtifactBindingStore().resolve(project_id, artifact_id)
    assert resolved.attempt == 5, "a lower racing advance must never win the row"

    stale = _run_child(_RETRACT_CHILD.format(artifact_id=artifact_id, job_id="job-race", attempt=3))
    assert "RETRACTED False" in stale.stdout
    assert _binding_owner(artifact_id) == ("job-race", 5), (
        "the losing token's retraction must not remove the winner's committed row"
    )
    live = _run_child(_RETRACT_CHILD.format(artifact_id=artifact_id, job_id="job-race", attempt=5))
    assert "RETRACTED True" in live.stdout
    assert _binding_owner(artifact_id) is None, "the winner's DELETE must commit"
