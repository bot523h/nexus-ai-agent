"""Regression tests for :mod:`nexus_ai_agent.features.gamification` daily rewards.

The engine is pure CRUD over SQLite, so these tests need no Telegram, no
network and no LLM — only the ``settings_override`` fixture (temp DB path)
plus ``SQLModel.metadata.create_all``.

What they lock down
-------------------
* ``claim_daily`` completes a user's **first** claim inside a single
  connection: the pre-fix version held an uncommitted INSERT and then called
  ``update_streak()``, which opened a second engine and deadlocked SQLite
  (``database is locked``).
* Legacy **naive** ``last_daily`` values (SQLite strips tzinfo) no longer raise
  ``TypeError`` when subtracted from an aware ``datetime``.
* Streak arithmetic: grows inside 48 h, resets after a missed day, and the
  payload carries enough fields for the Telegram surface to render the reward
  without a second query.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.features import gamification as gamification_module
from nexus_ai_agent.features.gamification import GamificationEngine
from nexus_ai_agent.storage import models as _models  # noqa: F401  (registers tables)
from nexus_ai_agent.storage.models import UserXP

#: A first claim must never wait longer than this for a lock; the deadlock it
#: guards against surfaced as SQLAlchemy's default 5 s timeout before raising.
LOCK_TIMEOUT_SECONDS = 0.2

XP_DAILY_BONUS = gamification_module.XP_DAILY_BONUS
XP_STREAK_BONUS = gamification_module.XP_STREAK_BONUS


@pytest.fixture()
def db(settings_override: Any) -> Any:
    """Create every table in the temp database and hand back the settings."""
    settings = get_settings()
    engine = create_engine(f"sqlite:///{settings.db_path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    return settings


@pytest.fixture()
def fail_fast_engines(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    """Make ``_sync_engine`` return a *fresh*, short-timeout engine per call.

    Production behaviour is one new engine per call, so a second connection
    opened while a transaction is pending blocks — with the default 5 s
    timeout that is a slow test, with 0.2 s it is a fast and deterministic one.
    """

    def _factory() -> Any:
        return create_engine(
            f"sqlite:///{db.db_path}", connect_args={"timeout": LOCK_TIMEOUT_SECONDS}
        )

    monkeypatch.setattr(gamification_module, "_sync_engine", _factory)


@pytest.fixture()
def update_streak_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Structural guard: ``claim_daily`` must not delegate to ``update_streak``."""

    def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover - tripwire
        raise AssertionError(
            "claim_daily opened a second code path (update_streak); "
            "the reward must be computed in one session"
        )

    monkeypatch.setattr(GamificationEngine, "update_streak", staticmethod(_boom))


def _row(user_id: int, chat_id: int) -> UserXP:
    settings = get_settings()
    engine = create_engine(f"sqlite:///{settings.db_path}")
    with Session(engine) as session:
        row = session.exec(
            select(UserXP).where(UserXP.user_id == user_id, UserXP.chat_id == chat_id)
        ).first()
        assert row is not None
        return UserXP.model_validate(row.model_dump())


def _set_last_daily(user_id: int, chat_id: int, when: datetime) -> None:
    settings = get_settings()
    engine = create_engine(f"sqlite:///{settings.db_path}")
    with Session(engine) as session:
        row = session.exec(
            select(UserXP).where(UserXP.user_id == user_id, UserXP.chat_id == chat_id)
        ).first()
        assert row is not None
        row.last_daily = when
        session.add(row)
        session.commit()


# ── the deadlock ────────────────────────────────────────────────────────


def test_first_claim_completes_without_a_second_connection(
    db: Any, fail_fast_engines: None, update_streak_is_forbidden: None
) -> None:
    """A brand-new user's first claim must not deadlock on the pending INSERT."""
    result = GamificationEngine.claim_daily(1, 10)

    assert result["claimed"] is True
    assert result["streak"] == 1
    assert result["total_reward"] == XP_DAILY_BONUS + XP_STREAK_BONUS
    assert result["xp"] == XP_DAILY_BONUS + XP_STREAK_BONUS
    assert result["streak_broken"] is False
    assert result["leveled_up"] is False

    row = _row(1, 10)
    assert row.xp == XP_DAILY_BONUS + XP_STREAK_BONUS
    assert row.streak == 1
    assert row.last_daily is not None


def test_first_claim_never_reaches_the_streak_helper(
    db: Any, fail_fast_engines: None, update_streak_is_forbidden: None
) -> None:
    """Same tripwire, asserted directly: the helper is simply never invoked."""
    assert GamificationEngine.claim_daily(7, 70)["claimed"] is True


def test_first_claim_on_a_pre_existing_row_persists_the_streak(db: Any) -> None:
    """Lost-update guard.

    A row created by ``/profile`` or ``add_xp`` has ``last_daily = NULL``. The
    pre-fix code let ``update_streak()`` commit ``streak = 1`` on a *second*
    connection and then committed its own stale in-memory object back over it
    (SQLAlchemy writes every column), so the streak stayed ``0`` and the user
    never earned the streak bonus.
    """
    GamificationEngine.add_xp(11, 110, 10)  # row exists, last_daily = NULL

    result = GamificationEngine.claim_daily(11, 110)

    assert result["claimed"] is True
    assert result["streak"] == 1
    assert result["streak_bonus"] == XP_STREAK_BONUS
    assert _row(11, 110).streak == 1


# ── 24 h window ─────────────────────────────────────────────────────────


def test_second_claim_the_same_day_is_rejected(db: Any) -> None:
    assert GamificationEngine.claim_daily(2, 20)["claimed"] is True

    second = GamificationEngine.claim_daily(2, 20)

    assert second["claimed"] is False
    assert second["reason"] == "already_claimed"
    assert second["remaining_hours"] >= 0
    assert second["remaining_minutes"] >= 1
    assert second["streak"] == 1


def test_claim_after_a_day_grows_the_streak(db: Any) -> None:
    GamificationEngine.claim_daily(3, 30)
    _set_last_daily(3, 30, datetime.now(timezone.utc) - timedelta(hours=25))

    result = GamificationEngine.claim_daily(3, 30)

    assert result["claimed"] is True
    assert result["streak"] == 2
    assert result["streak_broken"] is False
    assert result["streak_bonus"] == 2 * XP_STREAK_BONUS


def test_claim_after_two_missed_days_resets_the_streak(db: Any) -> None:
    GamificationEngine.claim_daily(4, 40)
    _set_last_daily(4, 40, datetime.now(timezone.utc) - timedelta(days=3))

    result = GamificationEngine.claim_daily(4, 40)

    assert result["claimed"] is True
    assert result["streak"] == 1
    assert result["streak_broken"] is True


# ── legacy naive timestamps ─────────────────────────────────────────────


def test_naive_last_daily_row_is_treated_as_utc(db: Any) -> None:
    """SQLite hands back naive datetimes; subtracting them must not explode."""
    GamificationEngine.claim_daily(5, 50)
    naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=3)
    _set_last_daily(5, 50, naive)

    result = GamificationEngine.claim_daily(5, 50)

    assert result["claimed"] is True
    assert result["streak"] == 1
    assert result["streak_broken"] is True


def test_naive_last_daily_within_the_window_is_rejected(db: Any) -> None:
    GamificationEngine.claim_daily(6, 60)
    naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    _set_last_daily(6, 60, naive)

    result = GamificationEngine.claim_daily(6, 60)

    assert result["claimed"] is False
    assert result["reason"] == "already_claimed"


def test_update_streak_also_tolerates_naive_rows(db: Any) -> None:
    GamificationEngine.add_xp(8, 80, 10)
    naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=25)
    _set_last_daily(8, 80, naive)

    assert GamificationEngine.update_streak(8, 80)["streak"] >= 1


# ── levelling payload ───────────────────────────────────────────────────


def test_claim_reports_a_level_up(db: Any) -> None:
    GamificationEngine.add_xp(9, 90, 45)  # level 0 → needs 50 XP for level 1

    result = GamificationEngine.claim_daily(9, 90)

    assert result["claimed"] is True
    assert result["leveled_up"] is True
    assert result["new_level"] == 1
    assert result["title"] == GamificationEngine.get_level_title(1)
