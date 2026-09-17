"""Tests for the Alembic metadata aggregation hook (D2)."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table
from sqlmodel import SQLModel

from nexus_ai_agent.storage import migration_metadata as mm


@pytest.fixture()
def original_targets() -> list[MetaData]:
    """Snapshot METADATA_TARGETS so each test can restore it."""
    return list(mm.METADATA_TARGETS)


def _second_metadata() -> MetaData:
    """A synthetic second metadata object, unrelated to any real V1 code."""
    metadata = MetaData()
    Table("extra_table", metadata, Column("id", Integer, primary_key=True))
    return metadata


def test_single_target_is_returned_as_is(original_targets: list[MetaData]) -> None:
    mm.METADATA_TARGETS = [SQLModel.metadata]
    try:
        assert mm.get_target_metadata() is SQLModel.metadata
    finally:
        mm.METADATA_TARGETS = original_targets


def test_multiple_targets_are_combined(original_targets: list[MetaData]) -> None:
    second = _second_metadata()
    mm.METADATA_TARGETS = [SQLModel.metadata, second]
    try:
        combined = mm.get_target_metadata()
        # A fresh, isolated metadata object (not one of the inputs).
        assert combined is not SQLModel.metadata
        assert combined is not second
        # Every table from every target is present.
        assert set(SQLModel.metadata.tables) <= set(combined.tables)
        assert set(second.tables) <= set(combined.tables)
        assert "extra_table" in combined.tables
        # The synthetic target's table is represented by the original Table object.
        assert combined.tables["extra_table"] is second.tables["extra_table"]
    finally:
        mm.METADATA_TARGETS = original_targets


def test_empty_targets_combine_to_empty_metadata(original_targets: list[MetaData]) -> None:
    mm.METADATA_TARGETS = []
    try:
        combined = mm.get_target_metadata()
        assert isinstance(combined, MetaData)
        assert len(combined.tables) == 0
    finally:
        mm.METADATA_TARGETS = original_targets


def test_default_targets_contain_only_sqlmodel_metadata() -> None:
    # The shipped default keeps exactly the one real target (the V1 hook is in
    # place, but nothing new is registered yet).
    assert mm.METADATA_TARGETS == [SQLModel.metadata]
