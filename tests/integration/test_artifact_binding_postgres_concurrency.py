"""The artifact-binding ownership fence on the real backend, across processes.

Binding identity is **per job**: two independent jobs can publish identical
bytes (the same ``artifact_id``) and each owns a *separate* row keyed by
``(artifact_id, job_id)``.  ``tests/integration/test_durable_project_revision_pinning.py``
proves the single-process semantics on SQLite; this module proves the *same*
contract on PostgreSQL with **real, separate OS processes**, which is the only
way to exercise two independent connections racing on one row:

* a cross-job bind never seizes another job's row — each job gets its own, so a
  job that reaches COMPLETED keeps its provenance even after a peer loses its
  terminal CAS and retracts (F5/R4);
* neither job's retraction can delete a row the other job wrote;
* a job's exact ``(job_id, attempt)`` retraction — and only that — deletes its
  own row;
* the token advance is a single conditional ``UPDATE`` scoped to the owning job,
  so two processes racing to advance one job's row can never leave the LOWER
  token behind (the atomic statement that replaced a read-modify-write, F1).

Runs only where ``NEXUS_DATABASE_URL`` is set **and** the explicit
``NEXUS_PG_TEST_DESTRUCTIVE=1`` opt-in is present (D-0028): the URL alone may
point at an operator's real database.  Every row is scoped to a per-run nonce
and removed afterwards, with a survival assertion over the rows that existed
before the module ran.
"""

from __future__ import annotations

import hashlib
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


def _address(nonce: str, tag: str) -> str:
    """A content address unique to this run.

    Content addresses are **global**: ``artifactcontentowner.artifact_id`` is a
    primary key across every project (that is exactly the invariant under test).
    A fixed literal would therefore collide with a previous run's ownership row,
    so each address is derived from the run nonce to keep runs self-isolating.
    """
    return "sha256:" + hashlib.sha256(f"{nonce}:{tag}".encode()).hexdigest()


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

_CROSS_PROJECT_CHILD = """
import asyncio, os, sys
from nexus_ai_agent.creative.project_state.store import SqlArtifactBindingStore
from nexus_ai_agent.storage.db import get_session

async def main():
    go = {go_file!r}
    async with get_session():
        pass
    print("READY", flush=True)
    while not os.path.exists(go):
        await asyncio.sleep(0.01)
    try:
        b = await SqlArtifactBindingStore().bind(
            project_id={project_id!r},
            revision_id={revision_id!r},
            artifact_id={artifact_id!r},
            job_id={job_id!r},
            size_bytes=1,
            attempt=1,
        )
        print("BOUND", b.project_id, flush=True)
    except Exception as exc:  # the losing racer must be refused, not crash
        print("REFUSED", type(exc).__name__, flush=True)

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


def _binding_owner(artifact_id: str, job_id: str) -> tuple[str, int] | None:
    """Read a job's binding ``(job_id, attempt)`` directly from PostgreSQL.

    Querying the database out-of-band — not the child's printed ``RETRACTED``
    value — is what proves the DELETE *committed*: if ``retract()`` executed the
    DELETE but omitted its ``commit()``, the child would roll it back on exit and
    print ``True`` while the row survived.  The row is scoped by ``job_id``
    because identity is per job, so several jobs may share one ``artifact_id``.
    """
    engine = sync_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT job_id, attempt FROM projectartifactbinding "
                "WHERE artifact_id = :aid AND job_id = :jid"
            ),
            {"aid": artifact_id, "jid": job_id},
        ).fetchone()
    return (str(row[0]), int(row[1])) if row is not None else None


def _bound_rows(artifact_id: str) -> set[tuple[str, int]]:
    """Every ``(job_id, attempt)`` row currently bound to one artifact."""
    engine = sync_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT job_id, attempt FROM projectartifactbinding WHERE artifact_id = :aid"),
            {"aid": artifact_id},
        ).fetchall()
    return {(str(row[0]), int(row[1])) for row in rows}


def _bound_project_ids(artifact_id: str) -> set[str]:
    """Every project that owns a committed binding row for one content address.

    Read out-of-band from PostgreSQL, not from either child's stdout: the claim
    that an address belongs to exactly one project must hold in the database,
    not merely in the process that printed ``BOUND``.
    """
    engine = sync_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT project_id FROM projectartifactbinding WHERE artifact_id = :aid"),
            {"aid": artifact_id},
        ).fetchall()
    return {str(row[0]) for row in rows}


@pytest.fixture(scope="module")
def nonce() -> str:
    return "pgfence-" + uuid.uuid4().hex[:12]


@pytest.fixture(scope="module", autouse=True)
def _survival_baseline() -> Iterator[None]:
    """Snapshot/verify bindings that existed before the module (D-0028)."""
    engine = sync_engine()
    with engine.connect() as conn:
        before = {
            (str(r[0]), str(r[1]))
            for r in conn.execute(
                text("SELECT artifact_id, job_id FROM projectartifactbinding")
            ).fetchall()
        }
    yield
    with engine.connect() as conn:
        after = {
            (str(r[0]), str(r[1]))
            for r in conn.execute(
                text("SELECT artifact_id, job_id FROM projectartifactbinding")
            ).fetchall()
        }
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
        # Remove the content-address ownership row BEFORE its project: the FK
        # ``artifactcontentowner.project_id → appproject.project_id`` would
        # otherwise refuse the project DELETE (the claim the fence introduced).
        conn.execute(
            text("DELETE FROM artifactcontentowner WHERE project_id = :pid"), {"pid": nonce}
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


async def test_cross_job_bind_owns_a_separate_row_and_only_that_job_retracts_it(
    project_id: str, cleanup: None
) -> None:
    revision_id = await _seed_revision(project_id)
    artifact_id = _address(project_id, "cross-job")

    # Job A owns its own row at token 2 — a real, separate process.
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

    # Job B, an independent process with the SAME bytes, gets its OWN row: it
    # neither seizes job A's binding nor is blocked by it.  Each job's provenance
    # is independent, which is what F5/R4 requires.
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
    assert "BOUND job-B 7" in second.stdout, (
        "a second job must own its own binding, not seize job A's"
    )

    # Each job resolves ONLY its own provenance.
    store = SqlArtifactBindingStore()
    a = await store.resolve(project_id, artifact_id, job_id="job-A")
    assert (a.job_id, a.attempt) == ("job-A", 2)
    b = await store.resolve(project_id, artifact_id, job_id="job-B")
    assert (b.job_id, b.attempt) == ("job-B", 7)
    assert _bound_rows(artifact_id) == {("job-A", 2), ("job-B", 7)}

    # Job B's retraction (its own job_id AND attempt) removes ONLY job B's row.
    b_retract = _run_child(
        _RETRACT_CHILD.format(artifact_id=artifact_id, job_id="job-B", attempt=7)
    )
    assert b_retract.returncode == 0, b_retract.stderr
    assert "RETRACTED True" in b_retract.stdout
    assert _binding_owner(artifact_id, "job-A") == ("job-A", 2), (
        "retracting one job's binding must leave the other job's committed row intact"
    )
    assert _binding_owner(artifact_id, "job-B") is None, (
        "the retracting job's DELETE must COMMIT: its row is gone from PostgreSQL"
    )

    # Job A's own retraction still works and commits its DELETE.
    a_retract = _run_child(
        _RETRACT_CHILD.format(artifact_id=artifact_id, job_id="job-A", attempt=2)
    )
    assert a_retract.returncode == 0, a_retract.stderr
    assert "RETRACTED True" in a_retract.stdout
    assert _binding_owner(artifact_id, "job-A") is None, (
        "the owner's retraction must COMMIT the DELETE: the row is gone from PostgreSQL"
    )


async def test_owner_retract_after_successor_completed_keeps_survivors_row(
    project_id: str, cleanup: None
) -> None:
    """F5/R4 across real processes: the survivor's provenance outlives the loser.

    The canonical mission scenario, on PostgreSQL with separate connections: a
    job that loses its terminal ``COMPLETED`` CAS retracts its binding; the job
    that DID reach COMPLETED, sharing the same bytes, must keep a resolvable,
    provable row.
    """
    revision_id = await _seed_revision(project_id)
    artifact_id = _address(project_id, "f5r4")

    # The job that will LOSE its terminal CAS binds first, at token 1.
    loser = _run_child(
        _BIND_CHILD.format(
            project_id=project_id,
            revision_id=revision_id,
            artifact_id=artifact_id,
            job_id="job-loser",
            attempt=1,
        )
    )
    assert loser.returncode == 0, loser.stderr
    assert "BOUND job-loser 1" in loser.stdout

    # The successor — the job that reaches COMPLETED — adopts the same bytes in
    # its own process.  It gets its own row; it never touches the loser's.
    winner = _run_child(
        _BIND_CHILD.format(
            project_id=project_id,
            revision_id=revision_id,
            artifact_id=artifact_id,
            job_id="job-winner",
            attempt=1,
        )
    )
    assert winner.returncode == 0, winner.stderr
    assert "BOUND job-winner 1" in winner.stdout

    # The loser loses the terminal CAS and retracts its OWN binding.
    lose = _run_child(_RETRACT_CHILD.format(artifact_id=artifact_id, job_id="job-loser", attempt=1))
    assert lose.returncode == 0, lose.stderr
    assert "RETRACTED True" in lose.stdout

    # The completed successor's binding MUST survive — in PostgreSQL, not just in
    # the child that printed RETRACTED.
    assert _binding_owner(artifact_id, "job-loser") is None, "the loser's row is gone"
    assert _binding_owner(artifact_id, "job-winner") == ("job-winner", 1), (
        "the completed job's provenance must survive the loser's retraction (F5/R4)"
    )
    survivor = await SqlArtifactBindingStore().resolve(project_id, artifact_id, job_id="job-winner")
    assert (survivor.job_id, survivor.attempt) == ("job-winner", 1)
    assert survivor.revision_id == revision_id


# ── cross-project ownership across real processes ────────────────────────────


async def test_cross_project_race_attributes_one_content_address_to_one_project(
    nonce: str,
) -> None:
    """One content address → exactly one project, decided by the database.

    The pre-check this replaces was a Python read-then-insert: two processes
    each observed "no owner" for the same address and both committed, silently
    attributing one address to two projects (the cross-project TOCTOU).  Here two
    independent OS processes, each with its own connection, race to first
    attribute one ``artifact_id`` to two different projects.  The database — via
    the ``artifactcontentowner`` primary key — must admit exactly one owner; the
    loser is refused with ``CrossProjectAccess`` and writes no binding row.
    """
    project_a = f"{nonce}-a"
    project_b = f"{nonce}-b"
    artifact_id = _address(nonce, "cross-project")
    revision_a = await _seed_revision(project_a)
    revision_b = await _seed_revision(project_b)

    go_file = REPO_ROOT / f".pgfence-go-{uuid.uuid4().hex}"
    first = _spawn_child(
        _CROSS_PROJECT_CHILD.format(
            go_file=str(go_file),
            project_id=project_a,
            revision_id=revision_a,
            artifact_id=artifact_id,
            job_id="job-a",
        )
    )
    second: subprocess.Popen[str] | None = None
    try:
        # Serialise schema warm-up (a local migration fcntl lock, a bootstrap
        # artifact) while leaving the claim itself to race: each child parks at
        # the file barrier before the next boots.
        _wait_line(first, "READY")
        second = _spawn_child(
            _CROSS_PROJECT_CHILD.format(
                go_file=str(go_file),
                project_id=project_b,
                revision_id=revision_b,
                artifact_id=artifact_id,
                job_id="job-b",
            )
        )
        _wait_line(second, "READY")
        go_file.write_text("go")
        first_out, first_err = first.communicate(timeout=120)
        second_out, second_err = second.communicate(timeout=120)
        assert first.returncode == 0, first_err
        assert second.returncode == 0, second_err
    finally:
        _reap(first)
        if second is not None:
            _reap(second)
        go_file.unlink(missing_ok=True)

    outcomes = {first_out.strip(), second_out.strip()}
    assert any(o.startswith("BOUND") for o in outcomes), f"no winner bound: {outcomes}"
    refused = next((o for o in outcomes if o.startswith("REFUSED")), None)
    assert refused is not None, f"the losing project must be refused, not admitted: {outcomes}"
    assert "CrossProjectAccess" in refused, refused

    owners = _bound_project_ids(artifact_id)
    winner_project = next(o for o in outcomes if o.startswith("BOUND")).split(" ", 1)[1].strip()
    assert owners == {winner_project}, (
        f"one content address must belong to exactly one project, got {owners}"
    )

    engine = sync_engine()
    with engine.begin() as conn:
        # Each project is deleted with its OWN bound ``project_id`` (never an
        # ``IN (:a, :b)`` list, which the data-safety guard — correctly — refuses
        # because a list can silently widen past this run's rows).  Order
        # respects the FKs: the per-job binding rows, then the content-address
        # ownership row (also scoped by the same run's ``project_id``, its
        # ``artifact_id`` being derived from this run's nonce), then the revision
        # head/revision, then the project itself.
        for pid in (project_a, project_b):
            conn.execute(
                text("DELETE FROM projectartifactbinding WHERE project_id = :pid"),
                {"pid": pid},
            )
            conn.execute(
                text("DELETE FROM artifactcontentowner WHERE project_id = :pid"),
                {"pid": pid},
            )
            conn.execute(
                text("DELETE FROM projectrevisionhead WHERE project_id = :pid"),
                {"pid": pid},
            )
            conn.execute(text("DELETE FROM projectrevision WHERE project_id = :pid"), {"pid": pid})
            conn.execute(text("DELETE FROM appproject WHERE project_id = :pid"), {"pid": pid})


# ── same-job fencing across real processes ───────────────────────────────────


async def test_same_job_stale_attempt_cannot_retract_across_processes(
    project_id: str, cleanup: None
) -> None:
    revision_id = await _seed_revision(project_id)
    artifact_id = _address(project_id, "stale")

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

    resolved = await SqlArtifactBindingStore().resolve(project_id, artifact_id, job_id="job-X")
    assert resolved.attempt == 5, "the newer attempt must own the token"

    stale = _run_child(_RETRACT_CHILD.format(artifact_id=artifact_id, job_id="job-X", attempt=1))
    assert "RETRACTED False" in stale.stdout
    assert _binding_owner(artifact_id, "job-X") == ("job-X", 5), (
        "a stale attempt's retraction must not remove the live row"
    )
    live = _run_child(_RETRACT_CHILD.format(artifact_id=artifact_id, job_id="job-X", attempt=5))
    assert "RETRACTED True" in live.stdout
    assert _binding_owner(artifact_id, "job-X") is None, "the live attempt's DELETE must commit"


# ── the token advance is atomic under a real race ────────────────────────────


async def test_racing_advances_never_leave_the_lower_token(project_id: str, cleanup: None) -> None:
    revision_id = await _seed_revision(project_id)
    artifact_id = _address(project_id, "race")
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

    resolved = await SqlArtifactBindingStore().resolve(project_id, artifact_id, job_id="job-race")
    assert resolved.attempt == 5, "a lower racing advance must never win the row"

    stale = _run_child(_RETRACT_CHILD.format(artifact_id=artifact_id, job_id="job-race", attempt=3))
    assert "RETRACTED False" in stale.stdout
    assert _binding_owner(artifact_id, "job-race") == ("job-race", 5), (
        "the losing token's retraction must not remove the winner's committed row"
    )
    live = _run_child(_RETRACT_CHILD.format(artifact_id=artifact_id, job_id="job-race", attempt=5))
    assert "RETRACTED True" in live.stdout
    assert _binding_owner(artifact_id, "job-race") is None, "the winner's DELETE must commit"
