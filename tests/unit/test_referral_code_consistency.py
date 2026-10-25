"""The referral code a user sees is the code that is stored and shared.

Before 2026-10-17 ``ReferralEngine.get_referral_stats`` generated a *fresh,
unpersisted* random code for a user with no ``referralcode`` row, while
``get_referral_link`` — called by ``format_stats`` one line later — generated
**another** random code and stored that one. The first ``/referral`` therefore
displayed a code that was never written to the database and a link carrying a
different one, so the code users copied from the message was not the code the
referral was credited to.

Every assertion below reads the row in ``referralcode``, not only the string
the renderer produced.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, select

from nexus_ai_agent.features.referral import REFERRAL_REWARDS, ReferralEngine
from nexus_ai_agent.storage.models import ReferralCode

USER_A = 123456789
USER_B = 987654321
BOT = "nexus_probe_bot"
_CODE_RE = re.compile(r"NEXUS-[A-HJ-NP-Z2-9]{8}")


@pytest.fixture()
def engine(tmp_path: Path) -> Any:
    """A ``ReferralEngine`` on its own SQLite file, plus a handle on its table."""
    referral = ReferralEngine(db_path=str(tmp_path / "referral.sqlite"))
    return referral


def _rows(db_path: str) -> list[ReferralCode]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with Session(engine) as session:
            return list(session.exec(select(ReferralCode)).all())
    finally:
        engine.dispose()


def _code_in(text: str) -> str:
    match = _CODE_RE.search(text)
    assert match is not None, f"no referral code in {text!r}"
    return match.group(0)


def test_the_first_referral_request_allocates_exactly_one_persisted_code(engine: Any) -> None:
    assert _rows(engine._db_path) == [], "no row exists before the first /referral"

    message = engine.format_stats(USER_A, BOT)

    (row,) = _rows(engine._db_path)
    assert row.user_id == USER_A
    assert _CODE_RE.fullmatch(row.code), row.code
    assert _code_in(message) == row.code, "the displayed code must be the stored one"


def test_the_displayed_code_the_stored_code_and_the_link_are_one_identity(engine: Any) -> None:
    message = engine.format_stats(USER_A, BOT)
    (row,) = _rows(engine._db_path)

    assert f"📋 کد دعوت: `{row.code}`" in message
    assert f"https://t.me/{BOT}?start=ref_{row.code}" in message
    # The link is derived from the code that is displayed, so the two can no
    # longer drift apart.
    assert message.count(row.code) >= 2, "code appears in both the text and the link"


def test_a_second_referral_request_reuses_the_persisted_code(engine: Any) -> None:
    first = engine.format_stats(USER_A, BOT)
    (row,) = _rows(engine._db_path)

    second = engine.format_stats(USER_A, BOT)

    assert _code_in(second) == _code_in(first) == row.code
    assert len(_rows(engine._db_path)) == 1, "a repeated request must not allocate again"
    assert engine.get_referral_link(USER_A, BOT).endswith(row.code)
    assert engine.get_or_create_code(USER_A) == row.code


def test_get_referral_stats_reports_the_persisted_code_not_a_fresh_one(engine: Any) -> None:
    """The direct regression: ``stats['code']`` used to be an unpersisted throwaway."""
    stats = engine.get_referral_stats(USER_A)
    (row,) = _rows(engine._db_path)
    assert stats["code"] == row.code, "the statistics must report the stored code"
    assert stats == {
        "code": row.code,
        "total": 0,
        "successful": 0,
        "current_tier": None,
        "next_tier": REFERRAL_REWARDS[0],
    }


def test_two_users_get_two_distinct_persisted_codes(engine: Any) -> None:
    first = engine.format_stats(USER_A, BOT)
    second = engine.format_stats(USER_B, BOT)

    codes = {row.user_id: row.code for row in _rows(engine._db_path)}
    assert codes[USER_A] == _code_in(first)
    assert codes[USER_B] == _code_in(second)
    assert codes[USER_A] != codes[USER_B]

    # Five collisions in a row is treated as a broken RNG by get_or_create_code;
    # 200 allocations over a 34**8 space exercise the retry path's absence.
    for user_id in range(1, 201):
        assert _CODE_RE.fullmatch(engine.get_or_create_code(user_id))
    allocated = [row.code for row in _rows(engine._db_path)]
    assert len(allocated) == len(set(allocated)), "codes must be unique"


def test_a_referral_processed_through_the_displayed_code_credits_the_referrer(
    engine: Any,
) -> None:
    """End-to-end: the link the user shared must actually work for them."""
    message = engine.format_stats(USER_A, BOT)
    link = next(part for part in message.split() if part.startswith("https://t.me/"))
    start_param = link.split("?start=")[1]

    result = engine.process_referral(USER_B, start_param)

    assert result["success"] is True, result
    assert result["referrer_id"] == USER_A
    assert engine.get_referral_stats(USER_A)["successful"] == 1
