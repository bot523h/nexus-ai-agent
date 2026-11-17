"""F-012: dump verification must be bounded-memory.

Arena finding F-012: ``_verify_postgres_dump`` did ``path.read_bytes()`` and
``_detect_engine`` did ``read_bytes()[:100]`` — both load the *entire* artifact
into memory. A multi-gigabyte NEXUS dump would therefore need as much RAM as it
has bytes just to check a footer at the end, which can OOM the backup host.

These tests prove the check now reads a bounded window (never the whole file),
while still rejecting a truncated artifact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.maintenance import backup as backup_module
from nexus_ai_agent.maintenance.backup import (
    _PG_DUMP_FOOTER,
    _last_meaningful_line_of_file,
    _verify_postgres_dump,
)


def _write_large_dump(path: Path, filler_mb: int) -> None:
    """Write a sparse-ish dump: a large head, then the real footer at the end."""
    with path.open("wb") as handle:
        handle.write(b"--\n-- PostgreSQL database dump\n--\n")
        chunk = b"-- filler line to inflate the artifact\n" * 4096
        for _ in range(filler_mb * 4):
            handle.write(chunk)
        handle.write(f"{_PG_DUMP_FOOTER}\n".encode())
        # PG17 trailer AFTER the footer.
        handle.write(b"--\n\\unrestrict DEADBEEF\n")


def test_footer_check_reads_only_a_bounded_tail(tmp_path: Path, monkeypatch) -> None:
    """The whole file must never be read into memory (F-012)."""
    dump = tmp_path / "big.sql"
    _write_large_dump(dump, filler_mb=4)
    assert dump.stat().st_size > backup_module._TAIL_WINDOW_BYTES, "test artifact too small"

    # If any code path calls read_bytes(), fail loudly.
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("read_bytes() loaded the whole dump into memory")

    monkeypatch.setattr(Path, "read_bytes", _forbidden)

    result = _verify_postgres_dump(dump)
    assert result == {"engine": "postgres", "footer": "present"}


def test_streaming_tail_reader_matches_the_in_memory_helper(tmp_path: Path) -> None:
    dump = tmp_path / "small.sql"
    _write_large_dump(dump, filler_mb=0)
    assert _last_meaningful_line_of_file(dump) == _PG_DUMP_FOOTER.encode()


def test_truncated_dump_is_still_rejected(tmp_path: Path) -> None:
    """Bounded reading must not weaken the truncation check."""
    dump = tmp_path / "truncated.sql"
    dump.write_bytes(b"--\n-- PostgreSQL database dump\n--\nCREATE TABLE t(id int);\n")
    with pytest.raises(RuntimeError, match="truncated"):
        _verify_postgres_dump(dump)


def test_empty_dump_is_rejected(tmp_path: Path) -> None:
    dump = tmp_path / "empty.sql"
    dump.write_bytes(b"")
    with pytest.raises(RuntimeError, match="empty"):
        _verify_postgres_dump(dump)


def test_detect_engine_reads_only_the_head(tmp_path: Path, monkeypatch) -> None:
    """``_detect_engine`` sniffs 100 bytes; it must not read the whole file."""
    from nexus_ai_agent.maintenance import restore as restore_module

    dump = tmp_path / "artifact"  # no extension → sniff path
    _write_large_dump(dump, filler_mb=4)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("read_bytes() loaded the whole dump into memory")

    monkeypatch.setattr(Path, "read_bytes", _forbidden)
    assert restore_module._detect_engine(dump, "artifact") == "postgres"
