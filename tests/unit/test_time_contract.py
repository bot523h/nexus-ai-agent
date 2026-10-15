"""The single time contract (audit A-12, closeout 2026-10-15).

Stored timestamps are *naive UTC* (what every ``DateTime`` column already
holds); in-flight timestamps are *aware UTC*.  ``storage.clock`` is the only
place either is produced, and ``datetime.utcnow`` — deprecated, and the way
naive/aware values got mixed — must not appear anywhere in the package, not
even as the attribute reference ``default_factory=datetime.utcnow`` that ruff's
``DTZ003`` (a call rule) cannot see.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import DateTime, create_engine
from sqlmodel import Session, SQLModel, select

from nexus_ai_agent.storage import clock
from nexus_ai_agent.storage.models import Referral, User

SRC = Path(__file__).resolve().parents[2] / "src" / "nexus_ai_agent"


def _utcnow_references() -> list[str]:
    hits: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "utcnow":
                base = node.value
                if isinstance(base, ast.Name) and base.id == "datetime":
                    hits.append(f"{path.relative_to(SRC)}:{node.lineno}")
                elif isinstance(base, ast.Attribute) and base.attr == "datetime":
                    hits.append(f"{path.relative_to(SRC)}:{node.lineno}")
    return hits


def test_datetime_utcnow_is_not_referenced_anywhere_in_the_package() -> None:
    assert _utcnow_references() == []


def test_clock_representations() -> None:
    aware = clock.utcnow()
    naive = clock.naive_utcnow()
    assert aware.tzinfo is timezone.utc
    assert naive.tzinfo is None
    # Same instant, two representations (allow for the two calls' skew).
    assert abs(clock.as_naive_utc(aware) - naive) < timedelta(seconds=1)
    assert clock.as_aware_utc(naive).tzinfo is timezone.utc


def test_conversions_are_explicit_and_zone_correct() -> None:
    tehran = timezone(timedelta(hours=3, minutes=30))
    operator_input = datetime(2026, 10, 15, 15, 30, tzinfo=tehran)
    noon_utc_stored = datetime(2026, 10, 15, 12, 0)  # noqa: DTZ001 - the stored representation
    assert clock.as_naive_utc(operator_input) == noon_utc_stored
    stored = noon_utc_stored  # what a DateTime column returns
    assert clock.as_naive_utc(stored) is stored  # naive is assumed UTC, untouched
    assert clock.as_aware_utc(stored) == operator_input  # same instant
    assert clock.as_aware_utc(operator_input) == datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def test_stored_values_compare_with_the_stored_clock_but_not_with_aware_values() -> None:
    stored = clock.naive_utcnow() - timedelta(minutes=5)
    assert stored < clock.naive_utcnow()  # the comparison every feature makes
    with pytest.raises(TypeError):
        _ = stored < clock.utcnow()  # mixing representations is an error, not a guess
    assert clock.as_aware_utc(stored) < clock.utcnow()  # the explicit way


def test_serialisation_of_stored_values_carries_no_offset() -> None:
    """``conversation_history.created_at`` and the API payloads are written
    with ``isoformat()``; the stored representation must stay offset-free so
    old and new rows sort and compare as text."""
    assert "+" not in clock.naive_utcnow().isoformat()
    assert clock.utcnow().isoformat().endswith("+00:00")


def test_every_datetime_column_is_timezone_naive() -> None:
    """Migration compatibility: no column became ``timestamp with time zone``."""
    offenders = [
        f"{table.name}.{column.name}"
        for table in SQLModel.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, DateTime) and column.type.timezone
    ]
    assert offenders == []


def test_every_model_default_is_the_stored_clock() -> None:
    """``default_factory`` of every datetime field is ``clock.naive_utcnow``."""
    offenders: list[str] = []
    inspected = 0
    for model in SQLModel.__subclasses__():
        if getattr(model, "__table__", None) is None:
            continue
        for name, field in model.model_fields.items():
            factory = field.default_factory
            if factory is None or "datetime" not in str(field.annotation):
                continue
            inspected += 1
            if factory is not clock.naive_utcnow:
                offenders.append(f"{model.__name__}.{name} -> {factory!r}")
    assert inspected >= 30, inspected  # 32 defaults were migrated in the closeout
    assert offenders == []


def test_round_trip_through_sqlite_returns_the_stored_representation(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'clock.sqlite'}")
    SQLModel.metadata.create_all(engine)
    before = clock.naive_utcnow() - timedelta(seconds=1)
    with Session(engine) as session:
        session.add(User(telegram_id=5_000_000_001))
        session.add(
            Referral(referrer_id=5_000_000_001, referee_id=5_000_000_002, referral_code="NEXUS-X")
        )
        session.commit()
    with Session(engine) as session:
        user = session.exec(select(User)).one()
        referral = session.exec(select(Referral)).one()
    after = clock.naive_utcnow() + timedelta(seconds=1)
    for value in (user.created_at, referral.created_at):
        assert value.tzinfo is None
        assert before <= value <= after
    engine.dispose()
