"""Referral tables are defined once, and codes are random (audit A-09 / A-12).

Before 2026-10-15 ``features/referral.py`` declared a second ``Referral`` /
``ReferralCode`` table over the same metadata with ``extend_existing=True``:
whichever module imported last decided whether ``referee_id`` was unique. And
``generate_code`` derived the code from the Telegram id
(``NEXUS-<id>-<sha256(id)[:8]>``), which both leaked the id and let anyone
forge any user's code.
"""

from __future__ import annotations

import re

from sqlmodel import SQLModel

from nexus_ai_agent.features import referral as referral_module
from nexus_ai_agent.features.referral import ReferralEngine
from nexus_ai_agent.storage import models


def test_the_feature_module_uses_the_storage_models() -> None:
    assert referral_module.Referral is models.Referral
    assert referral_module.ReferralCode is models.ReferralCode
    for table in ("referral", "referralcode"):
        assert "extend_existing" not in str(SQLModel.metadata.tables[table].kwargs)


def test_the_single_definition_carries_the_migrated_constraints() -> None:
    referral = SQLModel.metadata.tables["referral"]
    referee = referral.c.referee_id
    assert referee.unique is True and referee.index is True
    code_table = SQLModel.metadata.tables["referralcode"]
    assert code_table.c.user_id.unique is True
    assert code_table.c.code.unique is True


def test_referral_codes_are_random_and_do_not_embed_the_user_id(migrated_sqlite: str) -> None:
    engine = ReferralEngine(db_path=migrated_sqlite)
    first = engine.get_or_create_code(user_id=123456789)
    assert first == engine.get_or_create_code(user_id=123456789), "stable per user"
    assert "123456789" not in first
    assert re.fullmatch(r"NEXUS-[A-HJ-NP-Z2-9]{8}", first), first
    assert ReferralEngine.generate_code(123456789) != ReferralEngine.generate_code(123456789)
    second = engine.get_or_create_code(user_id=987654321)
    assert second != first
    # The old derivable scheme must be gone for good.
    assert not re.fullmatch(r"NEXUS-\d+-[0-9A-F]{8}", first)
