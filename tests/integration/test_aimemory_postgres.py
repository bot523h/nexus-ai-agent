"""PostgreSQL integration tests for AIMemoryEngine consent and timestamp compatibility.

Verifies:
1. AIMemory consent grant/revoke/save logic against PostgreSQL.
2. Naive/Aware timestamp compatibility and `tzinfo is None` for naive UTC timestamps on PostgreSQL.
3. Clean state setup and teardown inside try...finally.
"""

from __future__ import annotations

import random
from datetime import datetime

import pytest
from sqlmodel import select

from nexus_ai_agent.features.ai_memory import (
    CONSENT_DENIED,
    CONSENT_GRANTED,
    CONSENT_UNSET,
    AIMemoryEngine,
)
from nexus_ai_agent.storage.db import get_session, resolve_database_url
from nexus_ai_agent.storage.models import UserMemory

pg_url = resolve_database_url()
skip_no_pg = pytest.mark.skipif(not pg_url, reason="requires PostgreSQL (NEXUS_DATABASE_URL)")


@skip_no_pg
@pytest.mark.asyncio
async def test_postgres_ai_memory_consent_lifecycle() -> None:
    engine = AIMemoryEngine()
    test_user_id = random.randint(1_000_000_000, 9_999_999_999)

    try:
        # Pre-cleanup in case of remnant
        await engine.forget_user(test_user_id)

        # 1. Default consent state
        assert await engine.get_consent(test_user_id) == CONSENT_UNSET
        assert await engine.has_been_prompted(test_user_id) is False

        # 2. Grant consent
        stored_state = await engine.set_consent(test_user_id, granted=True)
        assert stored_state == CONSENT_GRANTED
        assert await engine.get_consent(test_user_id) == CONSENT_GRANTED
        assert await engine.has_been_prompted(test_user_id) is True

        # Check row timestamp in DB - must be naive UTC (tzinfo is None)
        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == test_user_id)
            row = (await session.execute(stmt)).scalar_one_or_none()
            assert row is not None
            assert row.ai_memory_consent == CONSENT_GRANTED
            assert isinstance(row.last_updated, datetime)
            assert row.last_updated.tzinfo is None, "PostgreSQL timestamp must be timezone-naive UTC"
            assert isinstance(row.ai_memory_consent_at, datetime)
            assert row.ai_memory_consent_at.tzinfo is None, "PostgreSQL timestamp must be timezone-naive UTC"

        # 3. Deny consent
        stored_state_denied = await engine.set_consent(test_user_id, granted=False)
        assert stored_state_denied == CONSENT_DENIED
        assert await engine.get_consent(test_user_id) == CONSENT_DENIED

        # 4. Forget user
        await engine.forget_user(test_user_id)
        assert await engine.get_consent(test_user_id) == CONSENT_UNSET
        assert await engine.has_been_prompted(test_user_id) is False

    finally:
        await engine.forget_user(test_user_id)
