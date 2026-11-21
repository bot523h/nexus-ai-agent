"""Coverage closure for the studio CLI's init and doctor probes.

The doctor reports; it never raises. Each case pins one probe failure: a symlinked
workspace, a file where a directory belongs, a missing or hung binary, and a
storage or compiler fault. Every failure must surface as a false check, not a crash.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.creative import studio_cli as cli


def test_init_refuses_a_symlinked_workspace_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    with pytest.raises(cli.StudioCliError, match="workspace root is a symlink"):
        cli.studio_init(link)


def test_init_refuses_a_symlinked_workspace_directory(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, root / cli.WORKSPACE_DIRS[0])
    with pytest.raises(cli.StudioCliError, match="is a symlink"):
        cli.studio_init(root)


def test_init_refuses_a_file_where_a_directory_belongs(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / cli.WORKSPACE_DIRS[0]).write_text("not a directory", encoding="utf-8")
    with pytest.raises(cli.StudioCliError, match="is not a directory"):
        cli.studio_init(root)


def test_a_missing_binary_fails_its_probe_without_raising() -> None:
    ok, detail = cli._binary_ok("/nonexistent/ffmpeg-binary")
    assert ok is False
    assert detail == "FileNotFoundError"


def test_a_hung_binary_is_reported_as_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _hang(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30)

    monkeypatch.setattr(cli.subprocess, "run", _hang)
    assert cli._binary_ok("ffmpeg") == (False, "TimeoutExpired")


def test_a_binary_that_exits_non_zero_reports_its_return_code(tmp_path: Path) -> None:
    script = tmp_path / "bad-ffmpeg"
    script.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    script.chmod(0o755)
    assert cli._binary_ok(str(script)) == (False, "rc=7")


def test_a_failed_atomic_write_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ws"
    cli.studio_init(root)

    def _no_replace(*_a: object, **_k: object) -> None:
        raise OSError("replace refused")

    monkeypatch.setattr(cli.os, "replace", _no_replace)
    assert cli._atomic_roundtrip(root) == (False, "OSError")


def test_a_failed_sqlite_probe_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ws"
    cli.studio_init(root)

    def _broken(*_a: object, **_k: object) -> None:
        raise sqlite3.Error("database is locked")

    monkeypatch.setattr(cli.sqlite3, "connect", _broken)
    assert cli._sqlite_roundtrip(root) == (False, "Error")


def test_a_compiler_fault_fails_the_compiler_check(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("compiler exploded")

    from nexus_ai_agent.creative import storyboard_compiler as sc

    monkeypatch.setattr(sc, "compile_storyboard", _boom)
    ok, detail = cli._compiler_ok()
    assert ok is False
    assert detail == "RuntimeError"


def test_doctor_names_the_missing_layout_and_skips_the_storage_probes(tmp_path: Path) -> None:
    report = cli.studio_doctor(
        tmp_path / "not-initialised", ffmpeg="/nonexistent/ff", ffprobe="/nonexistent/fp"
    )
    by_name = {c.name: c for c in report.checks}
    assert by_name["workspace_layout"].ok is False
    assert by_name["atomic_write"].detail == "skipped: no layout"
    assert by_name["queue_sqlite"].detail == "skipped: no layout"
