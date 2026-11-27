"""Wave 4 — a Studio job that outlives its own process.

``TestCrashAndReconcile`` in ``test_nagar_studio_e2e_vertical_slice.py`` injects
a crash by raising at the enqueue *seam*, which proves the state machine.  It
does not prove the thing production actually depends on: that the durable rows
survive a process that dies and that a **different OS process** can pick them up.

This module closes that gap with real process boundaries.  The parent writes
nothing; each phase is a fresh ``python -m`` child that opens the same SQLite
files on disk, does one phase of the loop, and exits (or is killed mid-phase).
The assertions are made by the *parent* against the durable state afterwards:

    phase 1  stage the intent and consume the approval, then die before enqueue
    phase 2  a second process reconciles: it must not re-consume, must not
             double-enqueue, and must drive the row to AWAITING_WORKER
    phase 3  the worker completes the job and the provenance chain is drained
             into a real CreativeGraph by a *third* process
    phase 4  a fourth process builds the passport and verifies it

Every phase prints one JSON line to stdout, so the parent's assertions read the
child's own account of what it observed rather than the parent's assumption.

Two failure modes are specifically exercised:

* **the hard kill** — phase 1 is killed with ``SIGKILL`` after it has consumed
  the approval, so no cleanup handler of any kind runs.  The residue is exactly
  what a power loss leaves behind.
* **no straggler** — after phase 2 there must be exactly one queue row and one
  outbox row for the turn, proved by the parent counting them directly.

The children are launched with ``sys.dont_write_bytecode`` semantics via
``PYTHONDONTWRITEBYTECODE=1`` so the 3-layer bytecode defence holds here too.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

TENANT = "tenant-crash-1"
PROJECT = "proj-crash-1"
OWNER_ID = "prn_" + "7" * 32
SESSION = "sess-crash-1"
TURN = "turn-crash-1"
APPROVAL = "appr-crash-1"
REVISION = "rev-crash-1"
ASSET_NAME = "reel-crash.mp4"
ASSET_BYTES = b"crash-window-payload" * 32
PLAN_DIGEST = "sha256:" + "c" * 64
PREVIEW_HEX = "d" * 64
ARTIFACT_BYTES = b"rendered-by-a-child-process" * 16

#: How long a child may take before the parent calls it hung.  Generous: the
#: point is to fail loudly on a deadlock, not to measure speed.
CHILD_TIMEOUT = 120

BOOTSTRAP = '''
import sys, os, json
sys.dont_write_bytecode = True
ROOT = {root!r}
SRC = ROOT + "/src"
for _entry in (SRC, ROOT):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

TENANT = {tenant!r}
PROJECT = {project!r}
OWNER_ID = {owner!r}
SESSION = {session!r}
TURN = {turn!r}
APPROVAL = {approval!r}
REVISION = {revision!r}
ASSET_NAME = {asset!r}
PLAN_DIGEST = {plan_digest!r}
PREVIEW_DIGEST = {preview_digest!r}
DEPLOY = __import__("pathlib").Path({deploy!r})


def emit(**payload):
    # One JSON line, flushed immediately: the parent reads exactly one line.
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\\n")
    sys.stdout.flush()


class _Clock:
    """Monotonic ISO-8601 UTC, so a ledger row's time never goes backwards."""

    def __init__(self):
        self._tick = 0

    def __call__(self):
        self._tick += 1
        return "2026-11-27T00:00:%02d+00:00" % (self._tick % 60)


CLOCK = _Clock()


def stores():
    from nexus_ai_agent.nagar.ibpc.approval import DurableApprovalLedger
    from nexus_ai_agent.nagar.ibpc.execution_outbox import DurableExecutionOutboxStore
    return (
        DurableApprovalLedger(DEPLOY / "approvals.sqlite3", now=CLOCK),
        DurableExecutionOutboxStore(DEPLOY / "outbox.sqlite3", now=CLOCK),
    )


def binding():
    from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding
    return ApprovalBinding(
        approval_id=APPROVAL,
        session_id=SESSION,
        turn_id=TURN,
        sequence_number=1,
        tenant_id=TENANT,
        project_id=PROJECT,
        principal_id=OWNER_ID,
        source_revision=0,
        source_asset_digest="sha256:" + "a" * 64,
        ast_digest="sha256:" + "b" * 64,
        plan_digest=PLAN_DIGEST,
        preview_digest="sha256:" + PREVIEW_DIGEST,
        revision_id=REVISION,
    )


def identity():
    from nexus_ai_agent.nagar.ibpc.approval import ApprovalIdentity
    return ApprovalIdentity(
        principal_id=OWNER_ID,
        tenant_id=TENANT,
        project_id=PROJECT,
        session_id=SESSION,
        turn_id=TURN,
        sequence_number=1,
        source_revision=0,
        source_asset_digest="sha256:" + "a" * 64,
        ast_digest="sha256:" + "b" * 64,
        plan_digest=PLAN_DIGEST,
        preview_digest="sha256:" + PREVIEW_DIGEST,
        revision_id=REVISION,
    )


def plan_payload():
    return {{
        "approval_identity": {{
            "principal_id": OWNER_ID,
            "sequence_number": 1,
            "source_revision": 0,
            "source_asset_digest": "sha256:" + "a" * 64,
            "ast_digest": "sha256:" + "b" * 64,
            "plan_digest": PLAN_DIGEST,
            "preview_digest": "sha256:" + PREVIEW_DIGEST,
            "revision_id": REVISION,
        }},
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "session_id": SESSION,
        "turn_id": TURN,
        "revision_jcs_sha256": "sha256:" + "d" * 64,
        "operations": [{{"op": "preview.sample_cut", "clip": "clip-1"}}],
    }}


def enqueue(*, idempotency_key, payload, job_id=None):
    """A queue row that is durable on disk: enqueue in one process, poll in another."""
    import sqlite3
    connection = sqlite3.connect(str(DEPLOY / "queue.sqlite3"), timeout=30.0)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY,"
            " idempotency_key TEXT UNIQUE, status TEXT, artifact_id TEXT)"
        )
        existing = connection.execute(
            "SELECT job_id FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            return str(existing[0])
        resolved = job_id or "job-" + idempotency_key[:16]
        connection.execute(
            "INSERT INTO jobs (job_id, idempotency_key, status, artifact_id)"
            " VALUES (?, ?, 'pending', '')",
            (resolved, idempotency_key),
        )
        connection.commit()
        return resolved
    finally:
        connection.close()


def queue_row(job_id):
    import sqlite3
    connection = sqlite3.connect(str(DEPLOY / "queue.sqlite3"), timeout=30.0)
    try:
        row = connection.execute(
            "SELECT job_id, status, artifact_id FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return None if row is None else {{"job_id": row[0], "status": row[1],
                                         "artifact_id": row[2]}}
    finally:
        connection.close()


#: The queue's own status spellings (``application.ports.job_queue.JobStatus``).
#: ``_WorkerCompletion.succeeded`` matches the UPPERCASE forms, so the durable
#: row stores the canonical lowercase spelling and this is the one place it is
#: lifted back to the worker protocol's vocabulary -- exactly the translation a
#: real queue adapter performs.
_QUEUE_STATUS_TO_WORKER = {{
    "pending": "PENDING",
    "processing": "PROCESSING",
    "verifying": "VERIFYING",
    "completed": "COMPLETED",
    "failed_retryable": "FAILED_RETRYABLE",
    "failed_terminal": "FAILED_TERMINAL",
}}


def completion_for(job_id):
    row = queue_row(job_id) or {{}}
    stored = str(row.get("status", "pending"))
    return {{
        "status": _QUEUE_STATUS_TO_WORKER.get(stored, stored.upper()),
        "artifact_id": row.get("artifact_id", ""),
        "passport_id": "passport-" + job_id,
    }}


def coordinator(store, ledger, *, poll_timeout=0.0, verify=None):
    from nexus_ai_agent.nagar.ibpc.execution_outbox import (
        ExecutionOutboxCoordinator, VerifiedExecutionCompletion,
    )

    def consume(approval_id, ident, *, job_id=None, attempt=1):
        ledger.consume(approval_id, identity=ident, job_id=job_id, attempt=attempt)

    def verify_completion(row, completion):
        artifact_id = str(completion.artifact_id)
        # The real artifact bytes a previous phase wrote to disk.
        produced = DEPLOY / "artifacts" / (artifact_id + ".bin")
        digest = hashlib.sha256(produced.read_bytes()).hexdigest() if produced.is_file() else ""
        return VerifiedExecutionCompletion(
            artifact_id=artifact_id,
            artifact_sha256=digest,
            passport_id=str(getattr(completion, "passport_id", "") or ""),
            lineage_digest="sha256:" + "e" * 64,
            verified_at_utc="2026-11-27T00:00:00+00:00",
        )

    verify_fn = verify or verify_completion
    plain = ExecutionOutboxCoordinator(
        store=store,
        consume_approval_fn=consume,
        enqueue_job_fn=enqueue,
        await_completion_fn=completion_for,
        verify_completion_fn=verify_fn,
        poll_timeout_seconds=poll_timeout,
        poll_interval_seconds=0.0,
        sleep=lambda _seconds: None,
    )
    return ExecutionOutboxCoordinator(
        store=store,
        consume_approval_fn=consume,
        enqueue_job_fn=enqueue,
        await_completion_fn=completion_for,
        verify_completion_fn=verify_fn,
        confirm_consumption_fn=plain.confirm_consumption_from_ledger(ledger),
        poll_timeout_seconds=poll_timeout,
        poll_interval_seconds=0.0,
        sleep=lambda _seconds: None,
    )


def execute_kwargs():
    return dict(
        tenant_id=TENANT,
        project_id=PROJECT,
        session_id=SESSION,
        turn_id=TURN,
        principal_id=OWNER_ID,
        approval_id=APPROVAL,
        revision_id=REVISION,
        revision_jcs_sha256="sha256:" + "d" * 64,
        plan_payload=plan_payload(),
    )
'''

# -- phase scripts ---------------------------------------------------------- #
PHASE_PREPARE = """
import hashlib as _hashlib

ledger, outbox = stores()
ledger.create(binding())
ledger.approve(APPROVAL, identity=identity(), turn_is_active=lambda: True)
(DEPLOY / "artifacts").mkdir(exist_ok=True)
(DEPLOY / "assets").mkdir(exist_ok=True)
(DEPLOY / "assets" / ASSET_NAME).write_bytes(__ASSET_BYTES__)
emit(phase="prepare", approval_state=ledger.get(APPROVAL).state.value,
     asset_sha256=_hashlib.sha256((DEPLOY / "assets" / ASSET_NAME).read_bytes()).hexdigest())
for resource in (ledger, outbox):
    closer = getattr(resource, "close", None)
    if callable(closer):
        closer()
"""

PHASE_CONSUME_THEN_DIE = """
# Consume the approval, stage the intent, and *then* die.  Nothing here is
# wrapped in ``finally`` on purpose: the child is killed from outside, so the
# only thing that can survive is what SQLite already committed.
ledger, outbox = stores()
coordinator_instance = coordinator(outbox, ledger)

def dying_enqueue(**_kwargs):
    raise RuntimeError("the process is about to be killed")

from nexus_ai_agent.nagar.ibpc.execution_outbox import ExecutionOutboxCoordinator
dying = ExecutionOutboxCoordinator(
    store=outbox,
    consume_approval_fn=lambda approval_id, ident, **kw: ledger.consume(
        approval_id, identity=ident, job_id=kw.get("job_id"), attempt=kw.get("attempt", 1)
    ),
    enqueue_job_fn=dying_enqueue,
    await_completion_fn=lambda _job_id: {"status": "PENDING"},
    verify_completion_fn=None,
    poll_timeout_seconds=0.0,
    poll_interval_seconds=0.0,
    sleep=lambda _seconds: None,
)
outcome = dying.execute_or_recover(**execute_kwargs())
emit(phase="consume", status=outcome.status, can_resume=outcome.can_resume,
     idempotency_key=outcome.idempotency_key,
     approval_state=ledger.get(APPROVAL).state.value)
# Hard-exit WITHOUT closing: the SQLite files keep whatever WAL frames a
# power loss would leave, which is exactly the state the next process sees.
import os as _os
_os._exit(0)
"""

PHASE_RECONCILE = """
ledger, outbox = stores()
resumed = coordinator(outbox, ledger)
outcomes = resumed.reconcile_in_flight()
rows = outbox.list_in_flight()
emit(phase="reconcile",
     outcomes=[{"status": o.status, "resumed_from": o.resumed_from,
                "job_id": o.job_id} for o in outcomes],
     in_flight=[{"state": r.state, "job_id": r.job_id} for r in rows],
     approval_state=ledger.get(APPROVAL).state.value)
for resource in (ledger, outbox):
    closer = getattr(resource, "close", None)
    if callable(closer):
        closer()
"""

PHASE_WORKER_AND_PROVENANCE = """
import sqlite3
from nexus_ai_agent.provenance.graph import CreativeGraph
from nexus_ai_agent.provenance.outbox import (
    OUTBOX_EVENT_ATTEMPT_RESERVED, OUTBOX_EVENT_ATTEMPT_VERIFIED,
    OUTBOX_EVENT_JOB_COMPLETED, OutboxReconciler, ProvenanceOutboxStore,
)

ledger, outbox = stores()
in_flight = outbox.list_in_flight()
assert len(in_flight) == 1, in_flight
job_id = in_flight[0].job_id
assert job_id, "the reconcile phase must have enqueued a job"

# The worker writes real bytes to disk and marks the queue row COMPLETED.
artifact_id = "artifact-crash-1"
(DEPLOY / "artifacts" / (artifact_id + ".bin")).write_bytes(__ARTIFACT_BYTES__)
connection = sqlite3.connect(str(DEPLOY / "queue.sqlite3"), timeout=30.0)
try:
    connection.execute(
        "UPDATE jobs SET status = 'completed', artifact_id = ? WHERE job_id = ?",
        (artifact_id, job_id),
    )
    connection.commit()
finally:
    connection.close()

# A third durable table, drained by the real reconciler.
store = ProvenanceOutboxStore(DEPLOY / "provenance.sqlite3")
graph = CreativeGraph(DEPLOY / "provenance.sqlite3")
reconciler = OutboxReconciler(store, graph)
artifact_sha = "sha256:" + hashlib.sha256(__ARTIFACT_BYTES__).hexdigest()
VERIFIED_PAYLOAD = {
    "job_type": "creative_render",
    "project_id": PROJECT,
    "artifact_digest": artifact_sha,
    "artifact_path": str(DEPLOY / "artifacts" / (artifact_id + ".bin")),
}
COMPLETED_PAYLOAD = {
    "job_type": "creative_render",
    "project_id": PROJECT,
    "artifact_digest": artifact_sha,
}
for kind, payload in (
    (OUTBOX_EVENT_ATTEMPT_RESERVED, {"job_type": "creative_render", "project_id": PROJECT}),
    (OUTBOX_EVENT_ATTEMPT_VERIFIED, VERIFIED_PAYLOAD),
    (OUTBOX_EVENT_JOB_COMPLETED, COMPLETED_PAYLOAD),
):
    store.enqueue(event_type=kind, job_id=job_id, attempt_id="attempt-1",
                  attempt_epoch=1, payload=payload)
    report = reconciler.reconcile_pending()
    assert report.failed == 0, report.errors

# Now the *next* reconcile must complete the turn on the verified completion.
finished = coordinator(outbox, ledger).reconcile_in_flight()
emit(phase="worker",
     statuses=[o.status for o in finished],
     artifact_id=finished[0].artifact_id if finished else "",
     job_id=job_id,
     artifact_sha256=artifact_sha,
     graph_nodes=graph.integrity_check().__dict__ if hasattr(
         graph.integrity_check(), "__dict__") else str(graph.integrity_check()),
     acyclic=graph.verify_acyclic())
for resource in (store, ledger, outbox):
    closer = getattr(resource, "close", None)
    if callable(closer):
        closer()
"""

PHASE_VERIFY_PASSPORT = """
import sqlite3
from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import EventKind, CausalEvent
from nexus_ai_agent.provenance.passport import PassportBuilder, PassportStatus

ledger, outbox = stores()
in_flight = outbox.list_in_flight(limit=100)
completed = outbox.get(idempotency_key=(in_flight[0].idempotency_key if in_flight else "")) \
    if in_flight else None
connection = sqlite3.connect(str(DEPLOY / "queue.sqlite3"), timeout=30.0)
try:
    row = connection.execute(
        "SELECT job_id, status, artifact_id FROM jobs ORDER BY rowid LIMIT 1"
    ).fetchone()
finally:
    connection.close()
assert row is not None
job_id, status, artifact_id = row
artifact_path = DEPLOY / "artifacts" / (artifact_id + ".bin")
artifact_sha = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()

# The child records the same causal chain a real worker would.
journal = CausalJournal(DEPLOY / "journal.sqlite3")
for kind, attempt in (
    (EventKind.JOB_ENQUEUED, None),
    (EventKind.JOB_RESERVED, 1),
    (EventKind.JOB_VERIFICATION_STARTED, 1),
    (EventKind.JOB_COMPLETED, 1),
):
    journal.append(CausalEvent(
        kind=kind, job_id=job_id, job_type="creative_render",
        idempotency_key=str(job_id), attempt=attempt, status=status,
        result_digest=artifact_sha if kind is EventKind.JOB_COMPLETED else None,
        detail={"artifact_path": str(artifact_path)} if kind is EventKind.JOB_COMPLETED else {},
    ))


class _Facts:
    def get_job_facts(self, wanted):
        from nexus_ai_agent.provenance.models import JobFacts
        if wanted != job_id:
            return None
        return JobFacts(
            job_id=job_id, job_type="creative_render", idempotency_key=str(job_id),
            status=status, attempt=1, error=None, created_at=None, started_at=None,
            finished_at=None, payload={"project_id": PROJECT}, payload_digest=None,
            result={"artifact_path": str(artifact_path), "sha256": artifact_sha,
                    "artifact_id": artifact_id},
            result_digest=artifact_sha, verification=None,
        )


passport = PassportBuilder(journal, _Facts()).build(job_id)
# The passport's own completeness check is the journal verification: it re-walks
# the hash chain and reconciles it against the durable row.  Reproduce the
# published head so the parent can compare it across the run.
records = passport.content["predicate"]["chain"]["records"]
emit(phase="passport", status=passport.status.value, job_id=job_id,
     artifact_sha256=artifact_sha,
     passport_digest=passport.passport_digest,
     kinds=[r["kind"] for r in records],
     chain_length=len(records),
     chain_head=dict(passport.content["predicate"]["chain"]["journal_head"]),
     findings=[f["code"] for f in passport.content.get("findings", [])])
for resource in (journal, ledger, outbox):
    closer = getattr(resource, "close", None)
    if callable(closer):
        closer()
"""


def _root() -> str:
    """The repository root (``tests/integration/<file>`` is three levels down)."""
    return str(Path(__file__).resolve().parents[2])


def _script(body: str, deploy: Path) -> str:
    """Render one child script: the shared bootstrap plus this phase's body."""
    bootstrap = BOOTSTRAP.format(
        root=_root(),
        tenant=TENANT,
        project=PROJECT,
        owner=OWNER_ID,
        session=SESSION,
        turn=TURN,
        approval=APPROVAL,
        revision=REVISION,
        asset=ASSET_NAME,
        plan_digest=PLAN_DIGEST,
        preview_digest=PREVIEW_HEX,
        deploy=str(deploy),
    )
    body = body.replace("__ASSET_BYTES__", repr(ASSET_BYTES))
    body = body.replace("__ARTIFACT_BYTES__", repr(ARTIFACT_BYTES))
    # ``hashlib`` is used by several phases; import it once in the preamble.
    return "import hashlib\n" + textwrap.dedent(bootstrap) + textwrap.dedent(body)


def _run(
    deploy: Path, body: str, *, expect_exit: int = 0, kill_after_line: str | None = None
) -> dict[str, object]:
    """Run one phase in a *separate OS process* and parse its single JSON line."""
    script = _script(body, deploy)
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": _root() + os.pathsep + str(Path(_root()) / "src"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-B", "-c", script],
        cwd=_root(),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if kill_after_line is not None:
            # Read until the phase reports it has committed its state, then
            # SIGKILL: no exit handler, no flush, nothing but the WAL survives.
            assert process.stdout is not None
            line = process.stdout.readline()
            assert line, "the child produced no output before the kill"
            process.kill()
            process.wait(timeout=CHILD_TIMEOUT)
            return json.loads(line)
        stdout, stderr = process.communicate(timeout=CHILD_TIMEOUT)
    except subprocess.TimeoutExpired:  # pragma: no cover - failure path
        process.kill()
        raise
    assert process.returncode == expect_exit, (
        f"child exited {process.returncode}, stderr:\n{stderr[-4000:]}\nstdout:\n{stdout[-2000:]}"
    )
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, f"child produced no output; stderr:\n{stderr[-4000:]}"
    return json.loads(lines[-1])


@pytest.fixture()
def deployment(tmp_path: Path) -> Path:
    deploy = tmp_path / "deployment"
    deploy.mkdir()
    return deploy


# --------------------------------------------------------------------------- #
# The multi-process walk
# --------------------------------------------------------------------------- #
class TestAMultiProcessCrashAndRestart:
    def test_a_killed_process_is_finished_by_three_others(self, deployment: Path) -> None:
        """The whole loop, across four real processes, with one hard kill."""
        prepared = _run(deployment, PHASE_PREPARE)
        assert prepared["phase"] == "prepare"
        assert prepared["approval_state"] == "APPROVED"
        assert prepared["asset_sha256"] == hashlib.sha256(ASSET_BYTES).hexdigest()

        # --- process A: consumes the approval, then is SIGKILLed.
        consumed = _run(deployment, PHASE_CONSUME_THEN_DIE, kill_after_line="ok")
        assert consumed["phase"] == "consume"
        # The enqueue failed transiently, so the intent is parked, resumable.
        assert consumed["status"] == "APPROVAL_CONSUMED"
        assert consumed["can_resume"] is True
        # The single-use approval is genuinely spent on disk.
        assert consumed["approval_state"] == "CONSUMED"
        key = str(consumed["idempotency_key"])
        assert key

        # --- process B: a brand-new interpreter reconciles.
        reconciled = _run(deployment, PHASE_RECONCILE)
        assert reconciled["phase"] == "reconcile"
        outcomes = reconciled["outcomes"]
        assert len(outcomes) == 1, outcomes
        assert outcomes[0]["resumed_from"] == "APPROVAL_CONSUMED"
        assert outcomes[0]["status"] == "AWAITING_WORKER"
        assert outcomes[0]["job_id"]
        # The approval was NOT consumed a second time: process B saw it as spent
        # and advanced the row instead of replaying the consume.
        assert reconciled["approval_state"] == "CONSUMED"
        assert len(reconciled["in_flight"]) == 1

        # Exactly one queue row, exactly one outbox row: no straggler.
        queue_rows = _child_sqlite_rows(deployment, "queue.sqlite3", "SELECT job_id FROM jobs")
        assert len(queue_rows) == 1, queue_rows
        outbox_rows = _child_sqlite_rows(
            deployment, "outbox.sqlite3", "SELECT outbox_id FROM nexus_ibpc_execution_outbox"
        )
        assert len(outbox_rows) == 1, outbox_rows

        # --- process C: the worker completes the job, provenance drains.
        worked = _run(deployment, PHASE_WORKER_AND_PROVENANCE)
        assert worked["phase"] == "worker"
        assert worked["statuses"] == ["COMPLETED"], worked
        assert worked["artifact_id"] == "artifact-crash-1"
        assert worked["artifact_sha256"] == "sha256:" + hashlib.sha256(ARTIFACT_BYTES).hexdigest()
        # The provenance chain is acyclic and its integrity check holds.
        assert worked["acyclic"] is True
        assert "False" not in str(worked["graph_nodes"])

        # --- process D: the passport is built and verified from the journal.
        passport = _run(deployment, PHASE_VERIFY_PASSPORT)
        assert passport["phase"] == "passport"
        assert passport["status"] == "VERIFIED", passport
        assert passport["artifact_sha256"] == worked["artifact_sha256"]
        assert passport["kinds"] == [
            "job_enqueued",
            "job_reserved",
            "job_verification_started",
            "job_completed",
        ]
        # The chain accounts for every transition the durable row proves, with
        # no finding at all -- the strongest statement the passport can make.
        assert passport["findings"] == [], passport["findings"]
        assert passport["chain_length"] == 4
        # The journal head is a real hash of the chain at a sequence number,
        # published so a future signed checkpoint can anchor it.
        head = passport["chain_head"]
        assert head["record_hash"].startswith("sha256:")
        assert head["seq"] == 4

    def test_the_kill_leaves_a_resumable_row_not_a_failed_turn(self, deployment: Path) -> None:
        """The crash residue is the point: a resumable state, not a dead turn."""
        _run(deployment, PHASE_PREPARE)
        consumed = _run(deployment, PHASE_CONSUME_THEN_DIE, kill_after_line="ok")
        assert consumed["status"] == "APPROVAL_CONSUMED"

        # Ask the durable row directly, from a fresh process, what state it is in.
        states = _child_sqlite_rows(
            deployment, "outbox.sqlite3", "SELECT state FROM nexus_ibpc_execution_outbox"
        )
        assert states == [("APPROVAL_CONSUMED",)], states

        # And a reconcile recovers it without a human re-approving anything.
        reconciled = _run(deployment, PHASE_RECONCILE)
        assert reconciled["outcomes"][0]["status"] == "AWAITING_WORKER"

    def test_a_second_reconcile_never_double_enqueues(self, deployment: Path) -> None:
        """Idempotence across processes: the queue dedupes on the same key."""
        _run(deployment, PHASE_PREPARE)
        _run(deployment, PHASE_CONSUME_THEN_DIE, kill_after_line="ok")
        first = _run(deployment, PHASE_RECONCILE)
        second = _run(deployment, PHASE_RECONCILE)
        assert first["outcomes"][0]["job_id"] == second["outcomes"][0]["job_id"]
        assert len(_child_sqlite_rows(deployment, "queue.sqlite3", "SELECT job_id FROM jobs")) == 1
        # The second reconcile finds the same single in-flight row.
        assert len(second["in_flight"]) == 1

    def test_the_children_never_wrote_bytecode_into_the_tree(self, deployment: Path) -> None:
        """3-layer bytecode defence: no ``__pycache__`` appears under the repo."""
        source_root = Path(_root()) / "src" / "nexus_ai_agent" / "nagar" / "ibpc"
        before = _pycache_count(source_root)
        _run(deployment, PHASE_PREPARE)
        _run(deployment, PHASE_CONSUME_THEN_DIE, kill_after_line="ok")
        _run(deployment, PHASE_RECONCILE)
        after = _pycache_count(source_root)
        assert after <= before, f"children wrote bytecode: {before} -> {after}"


# --------------------------------------------------------------------------- #
# Parent-side helpers
# --------------------------------------------------------------------------- #
def _child_sqlite_rows(deployment: Path, filename: str, query: str) -> list[tuple[object, ...]]:
    """Read a child's durable state directly: the parent's own evidence."""
    import sqlite3

    connection = sqlite3.connect(str(deployment / filename), timeout=30.0)
    try:
        return list(connection.execute(query))
    finally:
        connection.close()


def _pycache_count(root: Path) -> int:
    if not root.is_dir():  # pragma: no cover - repository layout changed
        return 0
    return sum(1 for _path in root.rglob("*.pyc"))
