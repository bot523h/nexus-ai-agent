"""Unit tests for the PostgreSQL job queue (task-163, ADR 0005).

A fake connection emulates the claim protocol against an in-memory table:
steal (stale ``processing`` rows) and claim (``FOR UPDATE SKIP LOCKED``)
are the two statements that carry the concurrency contract, so the fake
dispatches on their exact SQL markers and serializes the claim step the
way ``SKIP LOCKED`` serializes it.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest

from nexus_ai_agent.stateful import job_queue_pg as jqp

STEAL_MARKER = "coalesce(locked_at, started_at, created_at)"
CLAIM_MARKER = "FOR UPDATE SKIP LOCKED"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None) -> None:
        self._rows = rows
        self.description = [("x",)] if rows is not None else None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows or []


class _FakeConn:
    """In-memory stand-in for the PG queue table."""

    def __init__(
        self, fail_once_with: Exception | None = None, made: list[_FakeConn] | None = None
    ) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.by_idempotency: dict[str, str] = {}
        self.lock = threading.Lock()
        self.executed: list[str] = []
        self.fail_once_with = fail_once_with
        if made is not None:
            made.append(self)

    def execute(self, sql: str, params: tuple[Any, ...]) -> _Cursor:
        self.executed.append(sql)
        if self.fail_once_with is not None:
            exc, self.fail_once_with = self.fail_once_with, None
            raise exc
        if sql.startswith("INSERT INTO"):
            job_id, job_type, key, payload, created_at = params
            with self.lock:
                if key in self.by_idempotency:
                    raise psycopg.errors.UniqueViolation("duplicate key value")
                self.rows[job_id] = {
                    "id": job_id,
                    "job_type": job_type,
                    "idempotency_key": key,
                    "payload": payload,
                    "status": "pending",
                    "result": None,
                    "error": None,
                    "owner_id": None,
                    "created_at": created_at,
                    "locked_at": None,
                    "started_at": None,
                    "finished_at": None,
                }
                self.by_idempotency[key] = job_id
            return _Cursor(None)
        # Steal branch FIRST: the claim subquery never contains the
        # coalesce marker, so dispatch order is unambiguous.
        if STEAL_MARKER in sql:
            timeout = float(params[0])
            now = datetime.now(timezone.utc)
            with self.lock:
                for row in self.rows.values():
                    if row["status"] != "processing":
                        continue
                    ref = row["locked_at"] or row["started_at"] or row["created_at"]
                    if now - _parse(ref) >= timedelta(seconds=timeout):
                        row["status"] = "pending"
                        row["owner_id"] = None
                        row["locked_at"] = None
            return _Cursor(None)
        if CLAIM_MARKER in sql:
            owner_id, locked_at, started_at = params
            with self.lock:
                pending = [r for r in self.rows.values() if r["status"] == "pending"]
                if not pending:
                    return _Cursor(None)  # SKIP LOCKED: nothing left to take
                row = min(pending, key=lambda r: r["created_at"])
                row["status"] = "processing"
                row["owner_id"] = owner_id
                row["locked_at"] = locked_at
                row["started_at"] = started_at
                return _Cursor([(row["id"], row["job_type"], row["payload"])])
        if sql.startswith("SELECT id FROM"):
            key = params[0]
            job_id = self.by_idempotency.get(key)
            return _Cursor([(job_id,)] if job_id else None)
        if sql.startswith("SELECT id, job_type, status"):
            row = self.rows.get(params[0])
            if row is None:
                return _Cursor(None)
            return _Cursor(
                [(row["id"], row["job_type"], row["status"], row["result"], row["payload"])]
            )
        if "SET status = 'completed'" in sql:
            result, finished_at, job_id = params
            self.rows[job_id].update(
                {"status": "completed", "result": result, "error": None, "finished_at": finished_at}
            )
            return _Cursor(None)
        if "SET status = 'failed'" in sql:
            error, finished_at, job_id = params
            self.rows[job_id].update(
                {"status": "failed", "result": None, "error": error, "finished_at": finished_at}
            )
            return _Cursor(None)
        raise AssertionError(f"unexpected SQL: {sql}")


def _queue(conn: _FakeConn, **kwargs: Any) -> jqp.PgJobQueue:
    return jqp.PgJobQueue("postgresql://x", connection=conn, **kwargs)


def test_enqueue_inserts_a_pending_row() -> None:
    conn = _FakeConn()
    q = _queue(conn)

    job_id = asyncio.run(q.enqueue(job_type="t", idempotency_key="k1", payload={"a": 1}))

    assert job_id
    assert conn.rows[job_id]["status"] == "pending"
    assert conn.rows[job_id]["payload"] == {"a": 1}


def test_idempotency_key_returns_the_original_id() -> None:
    conn = _FakeConn()
    q = _queue(conn)

    first = asyncio.run(q.enqueue(job_type="t", idempotency_key="k1", payload={"a": 1}))
    second = asyncio.run(q.enqueue(job_type="t", idempotency_key="k1", payload={"a": 2}))

    assert first == second
    assert len(conn.rows) == 1  # no second effect


def test_claim_runs_the_handler_and_persists_the_result() -> None:
    conn = _FakeConn()
    q = _queue(conn)
    q.register_handler("t", lambda payload: {"echo": payload["a"]})
    asyncio.run(q.enqueue(job_type="t", idempotency_key="k1", payload={"a": 1}))

    completion = q._claim_next()

    assert completion is not None
    assert completion.status is jqp.JobStatus.COMPLETED
    assert completion.result == {"echo": 1}
    row = conn.rows[completion.job_id]
    assert row["status"] == "completed"
    assert row["result"] == {"echo": 1}
    assert row["owner_id"] is not None


def test_claim_without_a_handler_fails_the_row() -> None:
    conn = _FakeConn()
    q = _queue(conn)
    asyncio.run(q.enqueue(job_type="unknown", idempotency_key="k1", payload={}))

    completion = q._claim_next()

    assert completion.status is jqp.JobStatus.FAILED
    assert "no handler" in (completion.error or "")
    assert conn.rows[completion.job_id]["status"] == "failed"


def test_claim_records_a_handler_exception() -> None:
    conn = _FakeConn()
    q = _queue(conn)

    def boom(payload: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("kaboom")

    q.register_handler("t", boom)
    asyncio.run(q.enqueue(job_type="t", idempotency_key="k1", payload={}))

    completion = q._claim_next()
    assert completion.status is jqp.JobStatus.FAILED
    assert "kaboom" in (completion.error or "")


def test_empty_queue_returns_none() -> None:
    q = _queue(_FakeConn())
    assert q._claim_next() is None


def test_stale_processing_row_is_stolen_and_rerun() -> None:
    conn = _FakeConn()
    q = _queue(conn, processing_timeout_seconds=2.0)
    q.register_handler("t", lambda payload: {"ok": True})

    # A row claimed 5s ago by a container that then died (SIGKILL).
    crashed_at = _iso(datetime.now(timezone.utc) - timedelta(seconds=5))
    conn.rows["crashed"] = {
        "id": "crashed",
        "job_type": "t",
        "idempotency_key": "kc",
        "payload": {},
        "status": "processing",
        "result": None,
        "error": None,
        "owner_id": "pgq-deadbeef00",
        "created_at": crashed_at,
        "locked_at": crashed_at,
        "started_at": crashed_at,
        "finished_at": None,
    }

    completion = q._claim_next()  # steal first, then claim the freed row

    assert completion is not None
    assert completion.job_id == "crashed"
    assert completion.status is jqp.JobStatus.COMPLETED
    row = conn.rows["crashed"]
    assert row["owner_id"] != "pgq-deadbeef00"  # re-owned by the live container
    assert row["status"] == "completed"


def test_live_owner_row_is_never_touched() -> None:
    conn = _FakeConn()
    q = _queue(conn, processing_timeout_seconds=900.0)

    now_iso = _iso(datetime.now(timezone.utc))
    conn.rows["live"] = {
        "id": "live",
        "job_type": "t",
        "idempotency_key": "kl",
        "payload": {},
        "status": "processing",
        "result": None,
        "error": None,
        "owner_id": "pgq-otherowner",
        "created_at": now_iso,
        "locked_at": now_iso,
        "started_at": now_iso,
        "finished_at": None,
    }

    assert q._claim_next() is None  # nothing pending; the live row is NOT stolen
    assert conn.rows["live"]["status"] == "processing"
    assert conn.rows["live"]["owner_id"] == "pgq-otherowner"


def test_concurrent_claims_have_a_single_winner() -> None:
    # Two containers (threads) race for the only pending row: SKIP LOCKED
    # semantics mean exactly one wins, the other gets nothing.
    conn = _FakeConn()
    q = _queue(conn)
    q.register_handler("t", lambda payload: {"done": True})
    asyncio.run(q.enqueue(job_type="t", idempotency_key="k1", payload={}))

    async def _race() -> list:
        return await asyncio.gather(
            asyncio.to_thread(q._claim_next),
            asyncio.to_thread(q._claim_next),
        )

    results = asyncio.run(_race())
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].status is jqp.JobStatus.COMPLETED
    assert len(conn.rows) == 1


def test_get_status_and_get_result_async_port() -> None:
    conn = _FakeConn()
    q = _queue(conn)
    q.register_handler("t", lambda payload: {"r": 1})
    job_id = asyncio.run(q.enqueue(job_type="t", idempotency_key="k1", payload={}))

    assert asyncio.run(q.get_status(job_id)) is jqp.JobStatus.PENDING
    assert asyncio.run(q.get_result(job_id)) is None

    q._claim_next()

    assert asyncio.run(q.get_status(job_id)) is jqp.JobStatus.COMPLETED
    assert asyncio.run(q.get_result(job_id)) == {"r": 1}


def test_get_status_unknown_job_raises_keyerror() -> None:
    q = _queue(_FakeConn())
    with pytest.raises(KeyError):
        asyncio.run(q.get_status("nope"))


def test_transient_failure_reconnects_once() -> None:
    made: list[_FakeConn] = []
    first = _FakeConn(fail_once_with=psycopg.OperationalError("terminating connection"))
    q = _queue(first, connector=lambda url: _FakeConn(made=made))

    job_id = q._enqueue_sync("t", "k1", {"a": 1})  # dies once, reconnects, succeeds
    assert job_id in made[-1].rows
    assert len(made) == 1  # exactly one reconnect


def test_payload_round_trips_through_json() -> None:
    conn = _FakeConn()
    q = _queue(conn)
    q.register_handler("t", lambda payload: {"n": payload["n"]})
    asyncio.run(q.enqueue(job_type="t", idempotency_key="k1", payload={"n": json.loads("42")}))
    completion = q._claim_next()
    assert completion is not None and completion.result == {"n": 42}
