"""Behavioural contract for :mod:`nexus_ai_agent.features.moderation`.

Why this file exists
--------------------
``ModerationEngine`` shipped with warning counters, mutes and a reputation
ledger and had **no importer anywhere in src/** beyond a single ``set_config``
call: the five ``/mod_*`` commands answered with constants. Nothing therefore
ever executed ``has_profanity``, ``mute_user`` or ``is_muted`` in production,
and three defects sat undetected in code that is, by its nature, punitive —
a false positive costs a real user a warning and, at ``max_warnings``, a mute.

Each test below pins one of those defects.

1. ``test_innocent_words_are_not_profanity`` — the patterns were joined into a
   bare alternation and matched with ``re.search``, so ``خر`` fired inside
   ``خرید`` ("to buy"), ``آخر`` ("last") and ``خروج`` ("exit"), and ``رید``
   fired inside ``بگیرید``. Ordinary sentences were punished.
2. ``test_is_muted_survives_a_timezone_aware_row`` — ``mute_user`` wrote an
   aware datetime while ``is_muted`` compared it against a naive
   ``utcnow()``, so the *first read after a mute* raised
   ``TypeError: can't compare offset-naive and offset-aware datetimes``. The
   mute path crashed exactly when it was used.
3. ``test_flood_tracker_is_bounded`` — the tracker was a plain unbounded dict
   keyed by user id, held for the process lifetime: a public bot leaks one
   entry per user, forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexus_ai_agent.core.timeutil import utcnow
from nexus_ai_agent.features.moderation import ModerationEngine

# ── profanity: false positives ──────────────────────────────────────────────

#: Ordinary Persian words that contain a listed term as a substring. Every one
#: of these was flagged before the whole-word anchoring.
INNOCENT = (
    "خرید",  # "purchase" — contains خر
    "می‌خرید",  # "you buy" — خر after a ZERO WIDTH NON-JOINER
    "آخر",  # "last" — ends with خر
    "خروج",  # "exit"
    "مخرب",  # "destructive"
    "بگیرید",  # "take (imperative)" — contains رید
    "گردید",  # "became"
    "مغز",  # "brain" — was listed as profanity outright
    "سگال",  # a word that merely starts with سگ
    "لطفا برای خروج از برنامه دکمه را بزنید",
    "این محصول را از فروشگاه خرید کردم",
)

#: Real insults, as standalone words — these must still be caught.
PROFANE = (
    "احمق",
    "تو یک احمق هستی",
    "ابله",
    "نادان",
    "کثیف",
    "گوساله",
    "خر",  # standalone, the insult
    "سگ",
)


@pytest.mark.parametrize("text", INNOCENT)
def test_innocent_words_are_not_profanity(text: str) -> None:
    """A substring hit inside an ordinary word must not cost a user a warning."""
    assert ModerationEngine.has_profanity(text) is False, text


@pytest.mark.parametrize("text", PROFANE)
def test_real_insults_are_still_caught(text: str) -> None:
    """Anchoring must not be so tight that the filter stops working."""
    assert ModerationEngine.has_profanity(text) is True, text


def test_profanity_check_is_punctuation_aware() -> None:
    """Terms adjacent to punctuation are still whole words."""
    assert ModerationEngine.has_profanity("!احمق") is True
    assert ModerationEngine.has_profanity("تو ابله، برو") is True


# ── links & spam ────────────────────────────────────────────────────────────


def test_link_detection_covers_the_three_shapes() -> None:
    assert ModerationEngine.has_links("go to https://example.com now") is True
    assert ModerationEngine.has_links("join t.me/somechannel") is True
    assert ModerationEngine.has_links("see www.example.com") is True
    assert ModerationEngine.has_links("no links in this message") is False


# ── mute round-trip ─────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Point the engine at a throwaway SQLite file with the tables created.

    ``_sync_engine()`` caches per ``db_path``, so a fresh path per test gets a
    fresh engine — that cache key is the reason this fixture is safe.
    """
    from nexus_ai_agent.config import settings as settings_module

    db_file = tmp_path / "moderation.db"
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("NEXUS_DB_PATH", str(db_file))

    from sqlmodel import SQLModel

    from nexus_ai_agent.features.moderation import _sync_engine

    engine = _sync_engine()
    SQLModel.metadata.create_all(engine)
    yield db_file
    settings_module.get_settings.cache_clear()


def test_mute_round_trip_is_not_a_no_op(db) -> None:  # type: ignore[no-untyped-def]
    """mute → is_muted → unmute → is_muted, the sequence /mute actually runs."""
    assert ModerationEngine.is_muted(user_id=7, chat_id=-100) is False

    ModerationEngine.mute_user(user_id=7, chat_id=-100, duration_minutes=30)
    assert ModerationEngine.is_muted(user_id=7, chat_id=-100) is True

    ModerationEngine.unmute_user(user_id=7, chat_id=-100)
    assert ModerationEngine.is_muted(user_id=7, chat_id=-100) is False


def test_is_muted_survives_a_timezone_aware_row(db) -> None:  # type: ignore[no-untyped-def]
    """The regression that crashed the mute path on its first read.

    ``mute_user`` used ``datetime.now(timezone.utc)`` while the column and ``is_muted``
    used naive UTC. Reading back the row raised ``TypeError``. Writing an aware
    value directly reproduces the old on-disk state, so this test proves
    ``is_muted`` normalises rather than assuming.
    """
    from sqlmodel import Session

    from nexus_ai_agent.features.moderation import _sync_engine
    from nexus_ai_agent.storage.models import UserReputation

    with Session(_sync_engine()) as session:
        session.add(
            UserReputation(
                user_id=9,
                chat_id=-100,
                is_muted=True,
                mute_until=datetime.now(timezone.utc) + timedelta(minutes=15),
            )
        )
        session.commit()

    assert ModerationEngine.is_muted(user_id=9, chat_id=-100) is True


def test_an_expired_mute_reads_as_unmuted(db) -> None:  # type: ignore[no-untyped-def]
    from sqlmodel import Session

    from nexus_ai_agent.features.moderation import _sync_engine
    from nexus_ai_agent.storage.models import UserReputation

    with Session(_sync_engine()) as session:
        session.add(
            UserReputation(
                user_id=11,
                chat_id=-100,
                is_muted=True,
                mute_until=utcnow() - timedelta(minutes=1),
            )
        )
        session.commit()

    assert ModerationEngine.is_muted(user_id=11, chat_id=-100) is False


def test_warnings_accumulate_and_clear(db) -> None:  # type: ignore[no-untyped-def]
    """`/warn` reported "1/3" forever because nothing was persisted."""
    assert ModerationEngine.add_warning(user_id=13, chat_id=-100, reason="spam") == 1
    assert ModerationEngine.add_warning(user_id=13, chat_id=-100, reason="spam") == 2
    assert ModerationEngine.add_warning(user_id=13, chat_id=-100, reason="spam") == 3

    reputation = ModerationEngine.get_reputation(user_id=13, chat_id=-100)
    assert reputation is not None
    assert reputation.warnings == 3

    ModerationEngine.clear_warnings(user_id=13, chat_id=-100)
    reputation = ModerationEngine.get_reputation(user_id=13, chat_id=-100)
    assert reputation is not None
    assert reputation.warnings == 0


def test_warnings_are_scoped_per_chat(db) -> None:  # type: ignore[no-untyped-def]
    """A warning in one group must not follow the user into another."""
    ModerationEngine.add_warning(user_id=17, chat_id=-100, reason="x")
    ModerationEngine.add_warning(user_id=17, chat_id=-200, reason="x")

    first = ModerationEngine.get_reputation(user_id=17, chat_id=-100)
    second = ModerationEngine.get_reputation(user_id=17, chat_id=-200)
    assert first is not None and first.warnings == 1
    assert second is not None and second.warnings == 1


# ── flood tracker ───────────────────────────────────────────────────────────


def test_flood_tracker_is_bounded() -> None:
    """One dict entry per user, kept for the process lifetime, is a leak.

    The tracker is now an LRU capped at ``FLOOD_TRACKER_MAX_USERS``. Driving
    it past the cap must evict, not grow.
    """
    ModerationEngine.reset_flood_tracking()
    cap = ModerationEngine.FLOOD_TRACKER_MAX_USERS
    try:
        for user_id in range(cap + 50):
            ModerationEngine.is_flooding(user_id)
        assert len(ModerationEngine._flood_tracker) <= cap
    finally:
        ModerationEngine.reset_flood_tracking()


def test_flooding_trips_only_past_the_threshold() -> None:
    ModerationEngine.reset_flood_tracking()
    try:
        results = [ModerationEngine.is_flooding(4242) for _ in range(12)]
        assert results[0] is False, "a single message is never flooding"
        assert any(results), "a burst of 12 messages must eventually trip"
    finally:
        ModerationEngine.reset_flood_tracking()


def test_reset_flood_tracking_is_scopeable() -> None:
    ModerationEngine.reset_flood_tracking()
    ModerationEngine.is_flooding(1)
    ModerationEngine.is_flooding(2)
    ModerationEngine.reset_flood_tracking(user_id=1)
    assert 1 not in ModerationEngine._flood_tracker
    assert 2 in ModerationEngine._flood_tracker
    ModerationEngine.reset_flood_tracking()
    assert not ModerationEngine._flood_tracker
