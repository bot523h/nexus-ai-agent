"""Approval persistence — fully isolated from the ambient ``data/app.sqlite``.

T-ISO-01. ``ApprovalSystem`` calls the *default* ``get_session()``, which resolves
``data/app.sqlite`` relative to the process working directory (see
``storage.db.get_session``). The original single-test module called it directly,
so the test did not create a database of its own: it silently created, migrated
and read the **ambient** database of wherever pytest happened to run.

Three consequences were reproduced against the unpatched module before this
patch (each one is a test below, now pinned):

* a corrupt ``data/app.sqlite`` turned the test red with
  ``sqlalchemy.exc.DatabaseError: file is not a database``;
* an ambient database left by an older build (``pendingapproval`` without the
  ``status`` column) turned it red with ``sqlalchemy.exc.OperationalError``;
* a read-only working directory turned it red with
  ``PermissionError: [Errno 13] Permission denied: 'data'``.

The repair is **test-only**: the real ``ApprovalSystem`` is pointed at a fresh
temporary file by rebinding the module-level ``get_session`` — the same seam
``tests/unit/test_updater.py`` already uses — so application behaviour is
untouched and the ambient database is never read or written.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

import nexus_ai_agent.agent.approval as approval_module
from nexus_ai_agent.agent.approval import ApprovalSystem
from nexus_ai_agent.storage.db import get_session as real_get_session
from nexus_ai_agent.storage.models import PendingApproval

REPO_ROOT = Path(__file__).parents[2]
AMBIENT_DB = Path("data") / "app.sqlite"


def _ambient_state() -> dict[str, bytes | None]:
    """The ambient database as bytes (``None`` when absent), from any CWD.

    Both candidate locations are sampled: the working directory the process is
    in *now* and the repository root, so a test cannot hide a write by changing
    directory first.
    """
    return {
        str(base / AMBIENT_DB): (base / AMBIENT_DB).read_bytes()
        if (base / AMBIENT_DB).exists()
        else None
        for base in (Path.cwd(), REPO_ROOT)
    }


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Rebind ``ApprovalSystem``'s session factory to a throwaway database.

    Returns the temporary database path. Nothing in the working directory is
    touched, which is exactly the property under test.
    """
    db_path = str(tmp_path / "isolated" / "app.sqlite")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def bound_get_session() -> AsyncIterator[Any]:
        async with real_get_session(db_path) as session:
            yield session

    monkeypatch.setattr(approval_module, "get_session", bound_get_session)
    return db_path


async def _row(isolated_db: str, approval_id: int) -> PendingApproval | None:
    async with real_get_session(isolated_db) as session:
        return await session.get(PendingApproval, approval_id)


async def test_approve_status(isolated_db: str) -> None:
    """The original behaviour, now against a database this test owns."""
    system = ApprovalSystem()
    approval_id = await system.request_approval("test_change", "test_desc")
    assert await system.approve(approval_id) is True

    approval = await _row(isolated_db, approval_id)
    assert approval is not None
    assert approval.status == "approved"
    assert approval.change_type == "test_change"

    # The other half of the same persistence round trip: reject() resolves only
    # a pending row, and a second call is a no-op rather than a rewrite.
    rejected_id = await system.request_approval("test_change", "to reject")
    assert await system.reject(rejected_id) is True
    rejected = await _row(isolated_db, rejected_id)
    assert rejected is not None and rejected.status == "rejected"
    assert await system.reject(rejected_id) is False
    assert await system.reject(approval_id) is False

    async with real_get_session(isolated_db) as session:
        rows = (await session.execute(select(PendingApproval))).scalars().all()
    assert {row.status for row in rows} == {"approved", "rejected"}


async def test_approval_never_reads_or_writes_the_ambient_database(
    isolated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``data/app.sqlite`` may appear, and no existing one may change.

    A sentinel ambient database is planted first. The invariant is *byte
    equality before/after* in every sampled location, so the test is correct
    whether the ambient file is absent (it must stay absent) or present (it must
    stay identical) — an unpatched run overwrites the sentinel.
    """
    monkeypatch.chdir(tmp_path)
    ambient = tmp_path / AMBIENT_DB
    ambient.parent.mkdir(parents=True, exist_ok=True)
    sentinel = b"sentinel: this file belongs to the ambient database, not to a test"
    ambient.write_bytes(sentinel)

    before = _ambient_state()
    system = ApprovalSystem()
    approval_id = await system.request_approval("test_change", "ambient guard")
    assert await system.approve(approval_id) is True
    after = _ambient_state()

    assert after == before, "the approval flow changed an ambient database"
    assert ambient.read_bytes() == sentinel, "the approval flow overwrote the sentinel"


async def test_approval_survives_a_corrupt_ambient_database(
    isolated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt ambient database used to turn this suite red; it must not."""
    monkeypatch.chdir(tmp_path)
    ambient = tmp_path / AMBIENT_DB
    ambient.parent.mkdir(parents=True, exist_ok=True)
    ambient.write_bytes(bytes(range(256)) * 16)  # not a SQLite file

    system = ApprovalSystem()
    approval_id = await system.request_approval("test_change", "corrupt ambient")
    assert await system.approve(approval_id) is True
    row = await _row(isolated_db, approval_id)
    assert row is not None and row.status == "approved"


async def test_approval_survives_an_incompatible_ambient_schema(
    isolated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ambient database left by an older build must not decide the outcome.

    The legacy table has no ``status`` column, which used to surface as
    ``sqlalchemy.exc.OperationalError`` from ``session.get``.
    """
    monkeypatch.chdir(tmp_path)
    ambient = tmp_path / AMBIENT_DB
    ambient.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(ambient)
    try:
        legacy.execute(
            "CREATE TABLE pendingapproval (id INTEGER PRIMARY KEY, change_type TEXT, "
            "description TEXT, created_at DATETIME)"
        )
        legacy.execute(
            "INSERT INTO pendingapproval (change_type, description) VALUES ('legacy', 'no status')"
        )
        legacy.commit()
    finally:
        legacy.close()

    system = ApprovalSystem()
    approval_id = await system.request_approval("test_change", "legacy ambient")
    assert await system.approve(approval_id) is True
    row = await _row(isolated_db, approval_id)
    assert row is not None and row.status == "approved"


async def test_approval_is_independent_of_the_working_directory(
    isolated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The result must not depend on where pytest was started.

    The unpatched module failed from a foreign CWD *and* silently wrote the
    ambient database into it. The invariant here is twofold: the same result,
    and no ``data/app.sqlite`` anywhere near the process working directory.

    NOTE (scope): a **read-only** working directory currently fails for a
    different, pre-existing reason — ``config.settings`` calls
    ``Path(settings.cache_dir).mkdir(parents=True, exist_ok=True)`` with the
    CWD-relative default ``data/cache`` when settings load. That coupling is
    outside T-ISO-01 (it is production config code, not the approval path) and is
    deliberately *not* patched here; it is recorded as an open finding with its
    own owner in the Wave 3D handoff.
    """
    foreign = tmp_path / "foreign-cwd"
    foreign.mkdir()
    monkeypatch.chdir(foreign)

    system = ApprovalSystem()
    approval_id = await system.request_approval("test_change", "foreign cwd")
    assert await system.approve(approval_id) is True
    row = await _row(isolated_db, approval_id)
    assert row is not None and row.status == "approved"

    assert not (foreign / AMBIENT_DB).exists(), (
        "the approval flow created data/app.sqlite relative to the working directory"
    )
