"""inspect-v1 contract: read-only, schema key, unknown_fields, estimates.

The inspect command must never touch the data: no access timestamps, no
created files, no writes of any kind (invariant I1 + "No hidden mutation").
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from typer.testing import CliRunner

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord
from nexus_ai_agent.storage.checkpoint_lifecycle_store import SQLiteCheckpointLifecycleStore

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _make_db(path: Path, thread: str, checkpoints: int) -> None:
    conn = sqlite3.connect(path)
    SqliteSaver(conn).setup()
    for i in range(1, checkpoints + 1):
        conn.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
            "type, checkpoint, metadata) VALUES (?, '', ?, ?, 'json', '{}', '{}')",
            (thread, f"cp{i}", None if i == 1 else f"cp{i - 1}"),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("NEXUS_CHECKPOINT_PATH", str(tmp_path / "lg.sqlite"))
    # Hermetic backend: pin the SQLite path even if a PG URL is present in
    # the environment (PR3 backend branching in the CLI).
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _invoke() -> list[dict[str, object]]:
    from nexus_ai_agent.cli import app

    result = CliRunner().invoke(app, ["checkpoints", "inspect", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_inspect_v1_schema_key_and_known_thread(cli_env: Path) -> None:
    db = cli_env / "lg.sqlite"
    _make_db(db, "t1", 2)
    store = SQLiteCheckpointLifecycleStore(str(db) + ".lifecycle")
    for i in (1, 2):
        store.upsert(
            CheckpointRecord(
                "t1",
                f"cp{i}",
                NOW - timedelta(days=10),
                last_accessed_at=NOW - timedelta(hours=1),
            )
        )
    store.close()

    output = _invoke()

    assert [item["thread_id"] for item in output] == ["t1"]
    entry = output[0]
    assert entry["schema"] == "inspect-v1"
    assert entry["unknown_fields"] == []
    assert entry["missing_lifecycle"] is False
    assert entry["checkpoint_count"] == 2
    assert isinstance(entry["would_free_bytes_estimate"], int)
    assert entry["would_free_bytes_estimate"] > 0  # two checkpoints stored


def test_inspect_v1_unknown_fields_for_missing_lifecycle(cli_env: Path) -> None:
    db = cli_env / "lg.sqlite"
    _make_db(db, "t2", 1)

    output = _invoke()

    entry = output[0]
    assert entry["schema"] == "inspect-v1"
    assert entry["missing_lifecycle"] is True
    assert entry["unknown_fields"] == [
        "last_accessed_at",
        "newest_created_at",
        "oldest_created_at",
        "pinned",
    ]
    # Unknown values stay live in the output (I10: unknown => inspect live).
    assert entry["oldest_created_at"] == "unknown"


def test_inspect_never_creates_the_lifecycle_index(cli_env: Path) -> None:
    db = cli_env / "lg.sqlite"
    _make_db(db, "t3", 1)
    lifecycle_file = Path(str(db) + ".lifecycle")
    assert not lifecycle_file.exists()

    _invoke()

    # "No hidden mutation": a missing index must stay missing after inspect.
    assert not lifecycle_file.exists()


def test_inspect_does_not_touch_access_timestamps(cli_env: Path) -> None:
    db = cli_env / "lg.sqlite"
    _make_db(db, "t4", 1)
    store = SQLiteCheckpointLifecycleStore(str(db) + ".lifecycle")
    store.upsert(CheckpointRecord("t4", "cp1", NOW - timedelta(days=2)))
    store.close()

    _invoke()

    store = SQLiteCheckpointLifecycleStore(str(db) + ".lifecycle")
    try:
        records = store.records()
    finally:
        store.close()
    assert len(records) == 1
    assert records[0].last_accessed_at is None  # untouched by the admin read


def test_inspect_thread_filter(cli_env: Path) -> None:
    db = cli_env / "lg.sqlite"
    _make_db(db, "ta", 1)
    _make_db(db, "tb", 1)

    from nexus_ai_agent.cli import app

    result = CliRunner().invoke(app, ["checkpoints", "inspect", "--json", "--thread", "tb"])
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert [item["thread_id"] for item in output] == ["tb"]


# ─────────────────────────────────────────────────────────────────────────
# inspect-v1 retention verdict: thread-scoped, never row-position-scoped.
#
# A thread owns many lifecycle rows.  The destructive fields
# (``pinned`` / ``active`` / ``resumable_within_window`` / ``would_delete``)
# used to be derived from ``metadata[-1]`` -- one arbitrary row of a
# ``SELECT`` with no ``ORDER BY``.  The lifecycle index is a rowid table, so
# "the last row" is *insertion order*, i.e. a storage-engine artefact.  Two
# logically identical threads could therefore get opposite destructive
# verdicts, and a thread pinned until +7d and accessed a day ago could be
# reported as ``pinned: false, would_delete: true``.
#
# These tests pin the corrected contract: protection is existential,
# destruction is universal, and the verdict is invariant under row order.
# ─────────────────────────────────────────────────────────────────────────

RETENTION_NOW = datetime.now(timezone.utc)


def _live() -> CheckpointRecord:
    """Accessed 1 day ago, pinned until +7d: protected on both counts."""
    return CheckpointRecord(
        "t1",
        "cp_live",
        RETENTION_NOW - timedelta(days=1),
        last_accessed_at=RETENTION_NOW - timedelta(days=1),
        active_until=RETENTION_NOW + timedelta(days=7),
    )


def _stale(checkpoint_id: str = "cp_old") -> CheckpointRecord:
    """40 days old and untouched since: individually deletable."""
    return CheckpointRecord(
        "t1",
        checkpoint_id,
        RETENTION_NOW - timedelta(days=40),
        last_accessed_at=RETENTION_NOW - timedelta(days=40),
    )


_SCENARIO = 0


def _thread_with_rows(cli_env: Path, rows: list[CheckpointRecord]) -> None:
    """Build one real thread on a FRESH checkpoint DB, rows written in order.

    Each call gets its own database and re-points the CLI at it, so one test
    can compare two logically identical threads that differ only in the
    physical order their lifecycle rows were written in.
    """
    global _SCENARIO
    _SCENARIO += 1
    db = cli_env / f"scenario_{_SCENARIO}.sqlite"
    _make_db(db, "t1", 2)
    store = SQLiteCheckpointLifecycleStore(str(db) + ".lifecycle")
    try:
        for record in rows:
            store.upsert(record)
    finally:
        store.close()

    os.environ["NEXUS_CHECKPOINT_PATH"] = str(db)
    get_settings.cache_clear()


def test_inspect_reports_a_pinned_thread_as_pinned(cli_env: Path) -> None:
    """One pinned row protects the thread, even when it is not the last row."""
    _thread_with_rows(cli_env, [_live(), _stale()])

    entry = _invoke()[0]

    assert entry["pinned"] is True
    assert entry["active"] is True
    assert entry["resumable_within_window"] is True
    # The safety-critical field: a live thread is never recommended for deletion.
    assert entry["would_delete"] is False


def test_inspect_verdict_is_invariant_under_lifecycle_row_order(cli_env: Path) -> None:
    """Logically identical threads get identical verdicts.

    This is the direct regression test for the ``metadata[-1]`` defect: the
    same two rows in the opposite physical order must not flip a destructive
    recommendation.
    """
    _thread_with_rows(cli_env, [_live(), _stale()])
    live_first = _invoke()[0]

    _thread_with_rows(cli_env, [_stale(), _live()])
    stale_first = _invoke()[0]

    fields = ("pinned", "active", "resumable_within_window", "would_delete")
    assert {key: live_first[key] for key in fields} == {key: stale_first[key] for key in fields}, (
        f"retention verdict depends on lifecycle row order: {live_first} != {stale_first}"
    )
    assert live_first["would_delete"] is False


def test_inspect_would_delete_requires_every_row_to_be_deletable(cli_env: Path) -> None:
    """``would_delete`` is universal over the thread's rows, not one of them."""
    _thread_with_rows(cli_env, [_stale("cp_a"), _stale("cp_b")])
    assert _invoke()[0]["would_delete"] is True

    # A single recently-accessed sibling vetoes the whole thread.
    _thread_with_rows(cli_env, [_stale("cp_a"), _live()])
    assert _invoke()[0]["would_delete"] is False


def test_inspect_never_accessed_row_blocks_deletion(cli_env: Path) -> None:
    """Unknown age is retained: a row with no ``last_accessed_at`` vetoes."""
    never_accessed = CheckpointRecord("t1", "cp_unknown", RETENTION_NOW - timedelta(days=40))
    _thread_with_rows(cli_env, [_stale(), never_accessed])

    entry = _invoke()[0]

    assert entry["would_delete"] is False
    assert entry["resumable_within_window"] is True


# ─────────────────────────────────────────────────────────────────────────
# inspect-v1 `protection_reason`: a destructive verdict must be actionable.
#
# `would_delete: false` alone does not tell an operator whether the thread is
# protected by an explicit pin, by ordinary recency, or merely retained because
# its age is unknown.  Those are three different situations and they used to
# collapse into one boolean (`resumable_within_window`).  The reason field names
# the evidence without changing the type of any existing field.
# ─────────────────────────────────────────────────────────────────────────


def test_inspect_reports_why_a_thread_is_retained(cli_env: Path) -> None:
    """An explicit pin is reported as a pin, not as an anonymous 'not deletable'."""
    _thread_with_rows(cli_env, [_live(), _stale()])

    entry = _invoke()[0]

    assert entry["would_delete"] is False
    assert entry["protection_reason"] == "pinned"


def test_inspect_distinguishes_recency_from_an_explicit_pin(cli_env: Path) -> None:
    """Recent access protects too, and says so — a different reason, same verdict."""
    recent = CheckpointRecord(
        "t1",
        "cp_recent",
        RETENTION_NOW - timedelta(days=2),
        last_accessed_at=RETENTION_NOW - timedelta(days=1),
    )
    _thread_with_rows(cli_env, [recent])

    entry = _invoke()[0]

    assert entry["would_delete"] is False
    assert entry["pinned"] is False
    assert entry["protection_reason"] == "recent_access"


def test_inspect_reports_absence_of_evidence_as_no_evidence(cli_env: Path) -> None:
    """`no_evidence` is a retention reason, and it is never a claim of safety."""
    never_accessed = CheckpointRecord("t1", "cp_unknown", RETENTION_NOW - timedelta(days=40))
    _thread_with_rows(cli_env, [never_accessed])

    entry = _invoke()[0]

    assert entry["would_delete"] is False
    assert entry["protection_reason"] == "no_evidence"


def test_inspect_reports_no_protection_for_a_deletable_thread(cli_env: Path) -> None:
    """`none` is the only reason that co-occurs with `would_delete: true`."""
    _thread_with_rows(cli_env, [_stale("cp_a"), _stale("cp_b")])

    entry = _invoke()[0]

    assert entry["would_delete"] is True
    assert entry["protection_reason"] == "none"


def test_inspect_reason_is_never_the_unknown_sentinel(cli_env: Path) -> None:
    """The reason field must not enter `unknown_fields` — it always has a value.

    Adding a key whose value can be the string "unknown" would silently change
    the inspect-v1 `unknown_fields` contract; `no_evidence` is the explicit
    spelling for "we do not know", so the list stays stable.
    """
    db = cli_env / "lg.sqlite"
    _make_db(db, "t1", 1)  # checkpoints exist, but no lifecycle index at all

    entry = _invoke()[0]

    assert entry["missing_lifecycle"] is True
    assert entry["protection_reason"] == "no_evidence"
    assert "protection_reason" not in entry["unknown_fields"]
    assert entry["unknown_fields"] == [
        "last_accessed_at",
        "newest_created_at",
        "oldest_created_at",
        "pinned",
    ]


def test_inspect_does_not_claim_resumability_it_has_no_evidence_for(cli_env: Path) -> None:
    """`resumable_within_window` is a truth claim, not the negation of a verdict.

    A thread with checkpoints but no lifecycle index at all is *retained*
    (``would_delete: false``, fail-closed) — but nothing is known about it, so
    it must not be reported as resumable.  ``known and not deletable`` keeps
    those two facts apart; plain ``not deletable`` would conflate them.
    """
    db = cli_env / "lg.sqlite"
    _make_db(db, "t1", 1)

    entry = _invoke()[0]

    assert entry["missing_lifecycle"] is True
    assert entry["would_delete"] is False  # retained, fail-closed
    assert entry["resumable_within_window"] is False  # but NOT claimed resumable
    assert entry["pinned"] == "unknown"


def test_inspect_restores_access_context_when_backend_open_fails(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed setup must not leak the admin access context (task-250).

    ``inspect_checkpoints`` raises the access context to ``"admin"`` *before*
    it opens the checkpoint backend.  When that open fails (a missing driver,
    an unreachable database), the reset used to be skipped entirely because the
    only ``finally`` started after the setup — so ``"admin"`` stayed installed
    in the caller's context.  That silently disables every lifecycle access
    touch (``LifecycleRecordingSaver`` only records while the context is
    ``"user"``) and poisons whatever runs next in the same context.
    """
    from nexus_ai_agent import cli as cli_module
    from nexus_ai_agent.adapters.langgraph.lifecycle_recording import nexus_access_context

    assert nexus_access_context.get() == "system"

    def _unavailable_backend() -> object:
        raise ModuleNotFoundError("No module named 'psycopg'")

    monkeypatch.setattr(cli_module, "_open_checkpoint_backend", _unavailable_backend)
    with pytest.raises(ModuleNotFoundError):
        cli_module.inspect_checkpoints(json_output=False, thread=None)

    assert nexus_access_context.get() == "system"


def test_inspect_restores_access_context_when_cleanup_fails(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit path G: a raising ``close()`` must not skip the context reset (task-250).

    The setup guard closes paths A/B; this test pins the *cleanup* half of the
    same invariant.  ``adapter.close()`` used to run before the reset inside the
    outer ``finally``, so a failing close skipped the reset and leaked
    ``"admin"`` into the caller's context — the same silent access-touch
    disablement the task-250 leak caused.  The backend is stubbed so no file or
    connection is involved.
    """
    from nexus_ai_agent import cli as cli_module
    from nexus_ai_agent.adapters.langgraph.lifecycle_recording import nexus_access_context

    class _Adapter:
        def list_threads(self) -> list[str]:
            return []

        def list_checkpoints(self, thread_id: str) -> list[object]:
            return []

        def estimate_thread_bytes(self, thread_id: str) -> int:
            return 0

        def close(self) -> None:
            raise RuntimeError("close failed")

    monkeypatch.setattr(cli_module, "_open_checkpoint_backend", lambda: (_Adapter(), None))
    monkeypatch.setattr(cli_module, "_open_lifecycle_store", lambda *args, **kwargs: None)

    assert nexus_access_context.get() == "system"
    with pytest.raises(RuntimeError, match="close failed"):
        cli_module.inspect_checkpoints(json_output=False, thread=None)

    assert nexus_access_context.get() == "system"


def test_access_context_is_independent_of_a_previous_failed_inspect() -> None:
    """Order-sensitivity half of the regression (task-250).

    The next thing to run in the same context must observe the untouched
    default, not a leftover ``"admin"`` from the failed command above.  This
    test is red whenever :func:`test_inspect_restores_access_context_when_backend_open_fails`
    leaks, so the leak cannot come back silently.
    """
    from nexus_ai_agent.adapters.langgraph.lifecycle_recording import nexus_access_context

    assert nexus_access_context.get() == "system"


class _CountingAdapter:
    def __init__(self, *, body_error: bool = False, close_error: bool = False) -> None:
        self.body_error = body_error
        self.close_error = close_error
        self.closes = 0

    def list_threads(self) -> list[str]:
        if self.body_error:
            raise LookupError("body failed")
        return []

    def list_checkpoints(self, thread_id: str) -> list[object]:
        return []

    def estimate_thread_bytes(self, thread_id: str) -> int:
        return 0

    def close(self) -> None:
        self.closes += 1
        if self.close_error:
            raise RuntimeError("close failed")


class _ContextSpy:
    def __init__(self, real: object) -> None:
        self._real = real
        self.tokens: list[object] = []
        self.resets: list[object] = []

    def set(self, value: str) -> object:
        token = self._real.set(value)  # type: ignore[attr-defined]
        self.tokens.append(token)
        return token

    def reset(self, token: object) -> None:
        self.resets.append(token)
        self._real.reset(token)  # type: ignore[attr-defined]

    def get(self) -> str:
        return self._real.get()  # type: ignore[attr-defined,no-any-return]


def test_inspect_closes_partial_setup_and_resets_token_once(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lifecycle-open failure after adapter acquisition must close both."""
    from nexus_ai_agent import cli as cli_module
    from nexus_ai_agent.adapters.langgraph import lifecycle_recording as recording

    spy = _ContextSpy(recording.nexus_access_context)
    monkeypatch.setattr(recording, "nexus_access_context", spy)
    adapter = _CountingAdapter()
    monkeypatch.setattr(cli_module, "_open_checkpoint_backend", lambda: (adapter, None))

    def _lifecycle_unavailable(*args: object, **kwargs: object) -> None:
        raise OSError("lifecycle unavailable")

    monkeypatch.setattr(cli_module, "_open_lifecycle_store", _lifecycle_unavailable)
    with pytest.raises(OSError, match="lifecycle unavailable"):
        cli_module.inspect_checkpoints(json_output=False, thread=None)
    assert adapter.closes == 1, "partially acquired adapter was not closed"
    assert spy.get() == "system"
    assert len(spy.resets) == 1 and spy.resets[0] is spy.tokens[0]


def test_inspect_preserves_original_body_error_when_cleanup_fails(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup exception must not replace the original inspect failure."""
    from nexus_ai_agent import cli as cli_module
    from nexus_ai_agent.adapters.langgraph import lifecycle_recording as recording

    spy = _ContextSpy(recording.nexus_access_context)
    monkeypatch.setattr(recording, "nexus_access_context", spy)
    adapter = _CountingAdapter(body_error=True, close_error=True)
    monkeypatch.setattr(cli_module, "_open_checkpoint_backend", lambda: (adapter, None))
    monkeypatch.setattr(cli_module, "_open_lifecycle_store", lambda *args, **kwargs: None)

    with pytest.raises(LookupError, match="body failed"):
        cli_module.inspect_checkpoints(json_output=False, thread=None)
    assert adapter.closes == 1
    assert spy.get() == "system"
    assert len(spy.resets) == 1


def test_repeated_inspects_use_fresh_tokens_and_do_not_poison_next_call(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prior failed call cannot change the next call's effective context."""
    from nexus_ai_agent import cli as cli_module
    from nexus_ai_agent.adapters.langgraph import lifecycle_recording as recording

    spy = _ContextSpy(recording.nexus_access_context)
    monkeypatch.setattr(recording, "nexus_access_context", spy)

    def _backend_unavailable() -> object:
        raise ModuleNotFoundError("first call failed")

    monkeypatch.setattr(cli_module, "_open_checkpoint_backend", _backend_unavailable)
    with pytest.raises(ModuleNotFoundError, match="first call failed"):
        cli_module.inspect_checkpoints(json_output=False, thread=None)
    assert spy.get() == "system"

    adapter = _CountingAdapter()
    monkeypatch.setattr(cli_module, "_open_checkpoint_backend", lambda: (adapter, None))
    monkeypatch.setattr(cli_module, "_open_lifecycle_store", lambda *args, **kwargs: None)
    cli_module.inspect_checkpoints(json_output=False, thread=None)

    assert spy.get() == "system"
    assert adapter.closes == 1
    assert len(spy.tokens) == len(spy.resets) == 2
    assert spy.resets == spy.tokens
