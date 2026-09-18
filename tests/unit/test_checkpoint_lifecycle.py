from datetime import datetime, timedelta, timezone

import pytest

from nexus_ai_agent.storage.checkpoint_lifecycle import (
    CheckpointRecord,
    RetentionPolicy,
    eligible_for_deletion,
)

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


def record(**kwargs: object) -> CheckpointRecord:
    values = {"thread_id": "t", "checkpoint_id": "c", "created_at": NOW - timedelta(days=31)}
    values.update(kwargs)
    return CheckpointRecord(**values)


def test_old_inactive_checkpoint_is_eligible() -> None:
    assert eligible_for_deletion(record(), now=NOW)


def test_recent_checkpoint_is_protected() -> None:
    assert not eligible_for_deletion(record(created_at=NOW - timedelta(days=29)), now=NOW)


def test_active_thread_grace_protects_old_checkpoint() -> None:
    assert not eligible_for_deletion(record(active_until=NOW - timedelta(days=2)), now=NOW)


def test_recently_accessed_old_checkpoint_is_protected() -> None:
    assert not eligible_for_deletion(record(last_accessed_at=NOW - timedelta(days=2)), now=NOW)


def test_policy_rejects_non_positive_age() -> None:
    with pytest.raises(ValueError):
        RetentionPolicy(max_age=timedelta(0))
