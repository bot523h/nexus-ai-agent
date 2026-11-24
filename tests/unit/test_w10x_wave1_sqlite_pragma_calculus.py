"""Wave 1: deterministic SQLite pragma enforcement and Kingman timeout calculus."""

from __future__ import annotations

import dataclasses
import math
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.storage import sqlite_pragma_calculus as mod
from nexus_ai_agent.storage.sqlite_pragma_calculus import (
    ContentionCalculusReceipt,
    QueueInstabilityError,
    SQLitePragmaPolicy,
    SQLitePragmaReceipt,
    SQLitePragmaVerificationError,
    apply_and_verify_sqlite_pragmas,
    compute_optimal_busy_timeout_ms,
    policy_for_contention,
)


class _FakeCursor:
    def __init__(self, value: Any) -> None:
        self._value = value

    def fetchone(self) -> Any:
        return self._value


class _FakeConn:
    """Minimal stand-in used only to exercise defensive branches."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)

    def execute(self, _sql: str) -> _FakeCursor:
        return _FakeCursor(self._rows.pop(0))


class _LyingForeignKeysConnection(sqlite3.Connection):
    """A real connection that refuses to report foreign_keys=1."""

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:  # type: ignore[override]
        if sql == "PRAGMA foreign_keys":
            return _FakeCursor((0,))  # type: ignore[return-value]
        return super().execute(sql, *args)


class _BrokenForeignKeysConnection(sqlite3.Connection):
    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:  # type: ignore[override]
        if sql.startswith("PRAGMA foreign_keys="):
            raise sqlite3.OperationalError("not compiled with FK support")
        return super().execute(sql, *args)


class _LyingJournalConnection(sqlite3.Connection):
    def __init__(self, *args: Any, reported: str = "delete", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reported = reported

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:  # type: ignore[override]
        if sql == "PRAGMA journal_mode":
            return _FakeCursor((self._reported,))  # type: ignore[return-value]
        return super().execute(sql, *args)


def test_policy_normalises_and_freezes() -> None:
    policy = SQLitePragmaPolicy(journal_mode="wal", synchronous="full", temp_store="memory")
    assert policy.journal_mode == "WAL"
    assert policy.synchronous == "FULL"
    assert policy.temp_store == "MEMORY"
    assert policy.busy_timeout_ms == 5000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"journal_mode": "SIDEWAYS"},
        {"journal_mode": 3},
        {"synchronous": "TURBO"},
        {"synchronous": 2},
        {"temp_store": "RAM"},
        {"temp_store": object()},
        {"busy_timeout_ms": True},
        {"busy_timeout_ms": 1.5},
        {"busy_timeout_ms": 0},
        {"busy_timeout_ms": -3},
        {"foreign_keys": 1},
        {"trusted_schema": "no"},
        {"cell_size_check": 0},
        {"require_wal_for_file_backed": 1},
    ],
)
def test_policy_rejects_invalid_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        SQLitePragmaPolicy(**kwargs)


def test_policy_requires_wal_when_file_backed_flag_set() -> None:
    with pytest.raises(ValueError):
        SQLitePragmaPolicy(journal_mode="DELETE", require_wal_for_file_backed=True)
    policy = SQLitePragmaPolicy(journal_mode="DELETE", require_wal_for_file_backed=False)
    assert policy.journal_mode == "DELETE"


def test_receipt_verified_pragmas_are_read_only() -> None:
    receipt = SQLitePragmaReceipt(
        file_backed=False,
        journal_mode="memory",
        synchronous="FULL",
        foreign_keys=True,
        busy_timeout_ms=10,
        temp_store="MEMORY",
        trusted_schema=False,
        cell_size_check=True,
        verified_pragmas={"a": "b"},
    )
    assert receipt.verified_pragmas["a"] == "b"
    with pytest.raises(TypeError):
        receipt.verified_pragmas["a"] = "c"  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        receipt.journal_mode = "wal"  # type: ignore[misc]


def test_apply_and_verify_file_backed(tmp_path: Path) -> None:
    db = tmp_path / "wave1.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        policy = SQLitePragmaPolicy(
            journal_mode="WAL",
            synchronous="FULL",
            busy_timeout_ms=1234,
            foreign_keys=True,
        )
        receipt = apply_and_verify_sqlite_pragmas(conn, policy)
        assert receipt.file_backed is True
        assert receipt.journal_mode == "wal"
        assert receipt.busy_timeout_ms == 1234
        assert receipt.verified_pragmas["synchronous"] == "2"
        # Prove the values are real in SQLite, not just recorded.
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert conn.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        assert conn.execute("PRAGMA cell_size_check").fetchone()[0] == 1
    finally:
        conn.close()


def test_apply_and_verify_in_memory_and_non_wal(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        receipt = apply_and_verify_sqlite_pragmas(
            conn,
            SQLitePragmaPolicy(journal_mode="WAL", synchronous="NORMAL", foreign_keys=False),
        )
        assert receipt.file_backed is False
        assert receipt.journal_mode == "memory"
        assert receipt.synchronous == "NORMAL"
        assert receipt.foreign_keys is False
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        conn.close()

    db = tmp_path / "delete.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        policy = SQLitePragmaPolicy(journal_mode="DELETE", require_wal_for_file_backed=False)
        assert apply_and_verify_sqlite_pragmas(conn, policy).journal_mode == "delete"
    finally:
        conn.close()


def test_pragma_readback_mismatch_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "lying.sqlite"
    conn = _LyingForeignKeysConnection(str(db))
    try:
        with pytest.raises(SQLitePragmaVerificationError, match="referential integrity"):
            apply_and_verify_sqlite_pragmas(conn, SQLitePragmaPolicy())
    finally:
        conn.close()


def test_foreign_key_activation_error_fails_closed() -> None:
    conn = _BrokenForeignKeysConnection(":memory:")
    try:
        with pytest.raises(SQLitePragmaVerificationError, match="foreign-key activation"):
            apply_and_verify_sqlite_pragmas(conn, SQLitePragmaPolicy())
    finally:
        conn.close()


def test_journal_mode_mismatch_fails_closed(tmp_path: Path) -> None:
    conn = _LyingJournalConnection(str(tmp_path / "journal.sqlite"), reported="delete")
    try:
        with pytest.raises(SQLitePragmaVerificationError, match="required 'wal'"):
            apply_and_verify_sqlite_pragmas(conn, SQLitePragmaPolicy())
    finally:
        conn.close()

    memory = _LyingJournalConnection(":memory:", reported="weird")
    try:
        with pytest.raises(SQLitePragmaVerificationError, match="unexpected journal_mode"):
            apply_and_verify_sqlite_pragmas(memory, SQLitePragmaPolicy())
    finally:
        memory.close()


def test_apply_requires_real_connection() -> None:
    with pytest.raises(TypeError):
        apply_and_verify_sqlite_pragmas(object(), SQLitePragmaPolicy())  # type: ignore[arg-type]


def test_as_int_coercions() -> None:
    assert mod._as_int(True) == 1
    assert mod._as_int(7) == 7
    assert mod._as_int(3.0) == 3
    assert mod._as_int("9") == 9
    with pytest.raises(SQLitePragmaVerificationError):
        mod._as_int(None)
    with pytest.raises(SQLitePragmaVerificationError):
        mod._as_int(object())


def test_pragma_scalar_and_database_list_defensive_branches() -> None:
    with pytest.raises(SQLitePragmaVerificationError):
        mod._pragma_scalar(_FakeConn([None]), "journal_mode")  # type: ignore[arg-type]
    with pytest.raises(SQLitePragmaVerificationError):
        mod._is_file_backed(_FakeConn([None]))  # type: ignore[arg-type]
    with pytest.raises(SQLitePragmaVerificationError):
        mod._is_file_backed(_FakeConn([(0, "main")]))  # type: ignore[arg-type]
    assert mod._is_file_backed(_FakeConn([(0, "main", "")])) is False  # type: ignore[arg-type]
    assert mod._is_file_backed(_FakeConn([(0, "main", "/x")])) is True  # type: ignore[arg-type]


def test_kingman_receipt_values() -> None:
    receipt = compute_optimal_busy_timeout_ms(
        arrival_rate_hz=20.0, mean_service_ms=10.0, service_cv=1.0, target_tail_prob=1e-6
    )
    # rho = 0.2, Wq = (0.2/0.8) * 1 * 10 = 2.5 ms, T = ceil(10 + 2.5*ln(1e6))
    assert receipt.utilization == pytest.approx(0.2)
    assert receipt.mean_wait_ms == pytest.approx(2.5)
    assert receipt.busy_timeout_ms == math.ceil(10.0 + 2.5 * math.log(1e6))
    assert receipt.verified_pragmas == ()

    deterministic = compute_optimal_busy_timeout_ms(20.0, 10.0, 0.0)
    assert deterministic.mean_wait_ms == pytest.approx(1.25)


def test_kingman_rejects_unstable_queue() -> None:
    with pytest.raises(QueueInstabilityError):
        compute_optimal_busy_timeout_ms(100.0, 10.0, 1.0)
    with pytest.raises(QueueInstabilityError):
        compute_optimal_busy_timeout_ms(1000.0, 10.0, 1.0)


@pytest.mark.parametrize(
    "args",
    [
        (-1.0, 10.0, 1.0),
        (1.0, 0.0, 1.0),
        (1.0, -10.0, 1.0),
        (1.0, 10.0, -0.1),
    ],
)
def test_kingman_rejects_bad_inputs(args: tuple[float, float, float]) -> None:
    lam, mean_ms, cv = args
    with pytest.raises(ValueError):
        compute_optimal_busy_timeout_ms(lam, mean_ms, cv)


@pytest.mark.parametrize("eps", [0.0, 1.0, -0.1, 1.5])
def test_kingman_rejects_bad_tail_probability(eps: float) -> None:
    with pytest.raises(ValueError):
        compute_optimal_busy_timeout_ms(1.0, 10.0, 1.0, eps)


@pytest.mark.parametrize(
    "values",
    [
        (math.nan, 10.0, 1.0),
        (math.inf, 10.0, 1.0),
        (1.0, math.nan, 1.0),
        (1.0, 10.0, math.inf),
    ],
)
def test_kingman_rejects_non_finite(values: tuple[float, float, float]) -> None:
    with pytest.raises(ValueError):
        compute_optimal_busy_timeout_ms(*values)


def test_kingman_rejects_non_numeric() -> None:
    with pytest.raises(TypeError):
        compute_optimal_busy_timeout_ms(True, 10.0, 1.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compute_optimal_busy_timeout_ms(1.0, "10", 1.0)  # type: ignore[arg-type]


def test_contention_receipt_validation() -> None:
    good = ContentionCalculusReceipt(
        utilization=0.1, mean_wait_ms=1.0, busy_timeout_ms=5, target_tail_prob=0.5
    )
    assert good.verified_pragmas == ()
    base = {"utilization": 0.1, "mean_wait_ms": 1.0, "busy_timeout_ms": 5, "target_tail_prob": 0.5}
    bad_kwargs: list[dict[str, Any]] = [
        {**base, "utilization": math.nan},
        {**base, "utilization": 1.0},
        {**base, "utilization": -0.1},
        {**base, "mean_wait_ms": math.nan},
        {**base, "mean_wait_ms": -1.0},
        {**base, "busy_timeout_ms": True},
        {**base, "busy_timeout_ms": 1.5},
        {**base, "busy_timeout_ms": 0},
        {**base, "target_tail_prob": 0.0},
        {**base, "target_tail_prob": 1.0},
    ]
    for kwargs in bad_kwargs:
        with pytest.raises((TypeError, ValueError)):
            ContentionCalculusReceipt(**kwargs)


def test_policy_for_contention_round_trip() -> None:
    policy, receipt = policy_for_contention(20.0, 10.0, 1.0)
    assert policy.busy_timeout_ms == receipt.busy_timeout_ms
    assert policy.synchronous == "FULL"
    policy2, _ = policy_for_contention(20.0, 10.0, 1.0, synchronous="NORMAL")
    assert policy2.synchronous == "NORMAL"
