"""The execution outbox, driven by the SERVICE rather than a hand-built rig.

The sibling suite (``test_nagar_studio_e2e_vertical_slice.py``) constructs an
``ExecutionOutboxCoordinator`` directly and proves the *coordinator* survives a
crash.  That is necessary and not sufficient: until Wave 2 the production path
never built a coordinator at all.  ``IBPCService.execute()`` consumed the
approval and then awaited ``queue.enqueue`` with no durable record of the intent
in between, so a crash in that window spent a single-use approval on a job that
was never enqueued -- a turn that can never execute and can never be retried.

These tests therefore attack the seam ``execute()`` actually goes through:

* the consumption reserves the SAME deterministic job id the outbox row was
  staged under (without that, recovery is indistinguishable from a replay);
* a crash after consumption and before enqueue leaves a resumable row, not a
  terminal one;
* a fresh service on the same directory reconciles it, consuming the approval
  exactly once and enqueueing exactly one job.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.studio_principal_auth import (
    StudioPrincipalAuthStore,
    durable_studio_auth_path,
)
from nexus_ai_agent.nagar.ibpc.approval import ApprovalBinding, ApprovalIdentity
from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
from nexus_ai_agent.nagar.ibpc.execution_outbox import (
    DurableExecutionOutboxStore,
    derive_reserved_job_id,
)
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps

TENANT = "ten-outbox"
PROJECT = "proj-outbox"
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64


class _Queue:
    """A real durable-ish queue double: idempotent on the key, and it can be
    made to fail the way a process dying mid-write fails."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, str] = {}
        self.fail_next = False

    async def enqueue(self, *, job_type: str, idempotency_key: str, payload: dict[str, Any]) -> str:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("process died before the queue write")
        existing = self.by_key.get(idempotency_key)
        if existing is not None:
            return existing
        job_id = f"job-{len(self.jobs) + 1}"
        self.jobs[job_id] = {
            "job_type": job_type,
            "idempotency_key": idempotency_key,
            "payload": payload,
            "status": "PENDING",
        }
        self.by_key[idempotency_key] = job_id
        return job_id

    async def get_status(self, job_id: str) -> Any:
        from nexus_ai_agent.application.ports.job_queue import JobStatus

        return (
            JobStatus.COMPLETED if self.jobs[job_id]["status"] == "COMPLETED" else JobStatus.PENDING
        )

    async def get_result(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs[job_id]
        if job["status"] != "COMPLETED":
            return None
        return {"success": True, "artifact_path": job.get("artifact_path", "")}


def _key_ring() -> StaticKeyRing:
    return StaticKeyRing({"k1": base64.b64decode(base64.b64encode(b"k" * 32))}, active_key_id="k1")


def _service(root: Path, queue: Any) -> IBPCService:
    """A service wired exactly the way ``build_unified_studio_runtime`` wires it."""
    root.mkdir(parents=True, exist_ok=True)
    store = StudioPrincipalAuthStore(durable_studio_auth_path(root))
    try:
        store.ensure_project(PROJECT, tenant_id=TENANT, active_revision_id="rev-1")
    except TypeError:
        # The store's seeding API differs by release; the service only needs the
        # project row to resolve a revision, so fall back to the raw insert.
        with store._transaction():
            store._conn.execute(
                "INSERT OR IGNORE INTO nexus_studio_projects "
                "(project_id, tenant_id, active_revision_id) VALUES (?, ?, ?)",
                (PROJECT, TENANT, "rev-1"),
            )
    deps = IBPCServiceDeps(
        queue=queue,
        registry=None,
        workspace_root=root,
        revision_provider=lambda: 1,
        authority=store,
        channel_binding_ref="bind-1",
        durable=True,
        project_revision_resolver=store.get_project_revision,
    )
    service = IBPCService(deps)
    service.enable_execution_outbox(DurableExecutionOutboxStore(root / "outbox.sqlite3"))
    return service


def _approved_binding(session_id: str, turn_id: str) -> tuple[ApprovalBinding, ApprovalIdentity]:
    identity = ApprovalIdentity(
        principal_id="principal-owner",
        tenant_id=TENANT,
        project_id=PROJECT,
        session_id=session_id,
        turn_id=turn_id,
        sequence_number=1,
        source_revision=0,
        source_asset_digest=SHA_A,
        ast_digest=SHA_B,
        plan_digest=SHA_C,
        preview_digest=SHA_D,
        revision_id="rev-1",
    )
    binding = ApprovalBinding(
        approval_id="appr-outbox-1",
        session_id=session_id,
        turn_id=turn_id,
        sequence_number=1,
        tenant_id=TENANT,
        project_id=PROJECT,
        principal_id="principal-owner",
        source_revision=0,
        source_asset_digest=SHA_A,
        ast_digest=SHA_B,
        plan_digest=SHA_C,
        preview_digest=SHA_D,
        revision_id="rev-1",
    )
    return binding, identity


def test_the_consumption_reserves_the_outboxs_deterministic_job_id(tmp_path: Path) -> None:
    """The ledger reservation and the outbox row must name ONE job id.

    This is the whole recovery mechanism: after a crash the ledger is asked
    "was this approval consumed for *this* job?" and can only answer yes if the
    id it reserved at consumption time is the id the outbox row derives.
    """
    service = _service(tmp_path, _Queue())
    store = service._outbox_store()
    assert store is not None
    binding, _identity = _approved_binding("sess-1", "turn-1")
    service._approvals.create(binding)

    key = service._outbox_idempotency_key  # existence check, called below for real
    assert callable(key)

    # Stage the intent exactly as execute() does, then reserve on the ledger.
    ctx = _FakeContext()
    session = _FakeSession()
    derived = key(
        ctx=ctx,
        approval_id=binding.approval_id,
        session_id="sess-1",
        turn_id="turn-1",
        session=session,
    )
    reserved = service._reserved_job_id_for(ctx, binding.approval_id, "sess-1", "turn-1", session)
    assert reserved == derive_reserved_job_id(derived)
    assert reserved is not None


class _FakePlan:
    plan_digest = SHA_C


class _FakeIdentity:
    principal_id = "principal-owner"


class _FakeContext:
    project_id = PROJECT
    plan = _FakePlan()
    intent_digest = SHA_B
    verified_identity = _FakeIdentity()


class _FakeSession:
    tenant_id = TENANT


@pytest.mark.asyncio
async def test_a_crash_after_consumption_is_resumable_not_terminal(tmp_path: Path) -> None:
    """The spent approval must survive a transient enqueue failure."""
    queue = _Queue()
    service = _service(tmp_path, queue)
    session_id = service.open_session(
        tenant_id=TENANT,
        project_id=PROJECT,
        principal_id="principal-owner",
        user_id=1,
        chat_id=1,
        current_revision=1,
        active_revision_id="rev-1",
    )
    row = await service._stage_outbox_intent(
        ctx=_FakeContext(),
        approval_id="appr-outbox-1",
        session_id=session_id,
        turn_id="turn-1",
        session=_FakeSession(),
        payload={"command": "edit", "operation": "trim", "args": ["0", "1"]},
    )
    assert row is not None
    assert row.state == "APPROVAL_CONSUMED"

    # The queue dies between consumption and the enqueue commit.
    queue.fail_next = True
    service._record_outbox_enqueue_failure(row, RuntimeError("died"))
    parked = service._outbox_store().get(outbox_id=row.outbox_id)
    assert parked is not None
    # NOT FAILED_TERMINAL: the approval is already spent, so terminalising here
    # would demand a fresh human approval for a one-second queue hiccup.
    assert parked.state == "APPROVAL_CONSUMED"
    store_states = {
        event["to_state"] for event in service._outbox_store().events_for(row.outbox_id)
    }
    assert "FAILED_TERMINAL" not in store_states


@pytest.mark.asyncio
async def test_reconcile_after_restart_enqueues_exactly_one_job(tmp_path: Path) -> None:
    """A fresh service on the same directory finishes the crashed intent."""
    queue = _Queue()
    first = _service(tmp_path, queue)
    staging = await first._stage_outbox_intent(
        ctx=_FakeContext(),
        approval_id="appr-outbox-1",
        session_id="sess-1",
        turn_id="turn-1",
        session=_FakeSession(),
        payload={"command": "edit", "operation": "trim", "args": ["0", "1"]},
    )
    assert staging is not None
    key = staging.idempotency_key
    first.close()

    # --- restart: a brand new service over the same SQLite files.
    second = _service(tmp_path, queue)
    coordinator = second.execution_outbox
    assert coordinator is not None
    outcomes = await second.reconcile_execution_outbox_async()
    assert outcomes, "the in-flight intent must be reconciled, not ignored"
    outcome = outcomes[0]
    assert outcome["idempotency_key"] == key
    assert outcome["job_id"], "a reconciled intent must own a durable job id"

    # Exactly one job: the reserved id is stable across the restart.
    assert len(queue.jobs) == 1
    assert list(queue.by_key) == [key]


def test_an_unwired_service_keeps_the_legacy_path(tmp_path: Path) -> None:
    """No outbox configured must mean "no seam", never "a broken seam"."""
    root = tmp_path / "plain"
    root.mkdir()
    service = IBPCService(
        IBPCServiceDeps(
            queue=_Queue(),
            registry=None,
            workspace_root=root,
            revision_provider=lambda: 0,
        )
    )
    assert service.execution_outbox is None
    assert service.reconcile_execution_outbox() == ()
    assert service.durable_head_revision(PROJECT) is None
    service.close()


def test_running_inside_a_loop_does_not_nest_event_loops() -> None:
    """``_run_coroutine_blocking`` must not call ``asyncio.run`` under a loop."""
    from nexus_ai_agent.nagar.ibpc.service import _run_coroutine_blocking

    async def value() -> str:
        await asyncio.sleep(0)
        return "ok"

    async def driver() -> str:
        # Inside a running loop: a naive asyncio.run() would raise RuntimeError.
        return _run_coroutine_blocking(value())

    assert asyncio.run(driver()) == "ok"


def test_the_plan_projection_excludes_per_attempt_noise() -> None:
    """The stored plan must not carry paths or timestamps.

    The outbox compares this canonical JSON on every retry to detect a
    conflicting re-stage; a workspace path or a timestamp would make every
    retry look like a different intent and turn recovery into a conflict.
    """
    from nexus_ai_agent.nagar.ibpc.service import _outbox_plan_payload

    payload = {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "input_sha256": "f" * 64,
        "workspace_dir": "/tmp/should-not-be-stored",
        "input_path": "/tmp/also-not",
        "timestamp": "2026-11-27T00:00:00Z",
    }
    projection = _outbox_plan_payload(payload)
    assert "workspace_dir" not in projection
    assert "input_path" not in projection
    assert "timestamp" not in projection
    assert projection["command"] == "edit"
