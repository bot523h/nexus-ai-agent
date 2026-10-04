"""Deterministic identities and safe storage-key handling for queue evidence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.jobs.creative_passport import (
    CreativePassportError,
    _resolve_store_path,
    attempt_id,
    is_timeline_trim_request,
    request_identity,
)


def _trim_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "workspace_dir": "/tmp/creative_trim",
        "input_path": "/tmp/creative_trim/input.mp4",
        "media_duration_us": 1_000_000,
        "user_id": 7,
        "chat_id": 8,
        "lang": "en",
        "idempotency_key": "creative:7:8:trim-test",
    }
    payload.update(overrides)
    return payload


def test_request_identity_is_canonical_and_transaction_bound_to_payload() -> None:
    first = request_identity("creative_render", "same-key", {"args": ["0", "1"], "x": 1})
    reordered = request_identity("creative_render", "same-key", {"x": 1, "args": ["0", "1"]})
    revised = request_identity("creative_render", "same-key", {"x": 1, "args": ["0", "2"]})

    assert first == reordered
    assert first.request_id == revised.request_id
    assert first.request_fingerprint != revised.request_fingerprint
    assert first.transaction_id != revised.transaction_id
    assert first.request_fingerprint.startswith("sha256:")
    assert first.transaction_id.startswith("transaction_")


def test_attempt_identity_is_stable_and_increments_by_generation() -> None:
    assert attempt_id("job-1", 2) == attempt_id("job-1", 2)
    assert attempt_id("job-1", 1) != attempt_id("job-1", 2)
    with pytest.raises(ValueError):
        attempt_id("job-1", 0)


def test_trim_request_recognition_requires_the_existing_typed_surface() -> None:
    assert is_timeline_trim_request(_trim_payload())
    assert not is_timeline_trim_request(_trim_payload(operation="speed"))
    assert not is_timeline_trim_request({"operation": "trim"})


def test_artifact_storage_keys_cannot_escape_the_queue_root(tmp_path: Path) -> None:
    root = tmp_path / "queue.artifacts"
    with pytest.raises(CreativePassportError):
        _resolve_store_path(root, "../outside.mp4")
    with pytest.raises(CreativePassportError):
        _resolve_store_path(root, "/tmp/outside.mp4")
    assert _resolve_store_path(root, "jobs/job/attempt/output.mp4").is_relative_to(root)


@pytest.mark.asyncio
async def test_legacy_queue_migration_backfills_identity_without_inventing_old_attempts(
    tmp_path: Path,
) -> None:
    db = tmp_path / "legacy.sqlite3"
    payload = {"k": "v"}
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            CREATE TABLE nexus_job_queue (
                id TEXT PRIMARY KEY,
                job_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                attempt INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO nexus_job_queue
                (id, job_type, idempotency_key, payload_json, status, result_json,
                 created_at, started_at, finished_at, attempt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-job",
                "creative_like",
                "legacy-key",
                json.dumps(payload),
                "completed",
                json.dumps({"ok": True}),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:01+00:00",
                "2026-01-01T00:00:02+00:00",
                2,
            ),
        )

    queue = InProcessJobQueue(db, artifact_verifiers={})
    identity = await queue.get_request_identity("legacy-job")
    history = await queue.get_attempt_history("legacy-job")

    assert identity["request_id"].startswith("request_")
    assert identity["transaction_id"].startswith("transaction_")
    assert len(history) == 1  # old rows did not contain per-attempt history
    assert history[0]["attempt_number"] == 2
    assert history[0]["status"] == "legacy_observed_completed"
