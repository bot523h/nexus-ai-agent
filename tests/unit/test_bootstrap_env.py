"""Unit tests for scripts/bootstrap_env.py (the bootstrap contract).

These prove the *environment contract* without creating a venv or hitting the
network: pin parsing from pyproject (with and without extras markers), venv
path resolution (env override vs repo default), and the in-process pin check
against a stubbed ``importlib.metadata``.
"""

from __future__ import annotations

import importlib.metadata as md
import sys
from pathlib import Path

import pytest

_CALLER_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_CALLER_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CALLER_SCRIPTS))

from bootstrap_env import Pin, read_pins, resolve_venv_dir  # noqa: E402


@pytest.fixture()
def fake_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = """\
[project]
dependencies = [
  "alembic==1.20.0",
  "asyncpg==0.31.0",
  "psycopg[binary,pool]==3.3.5",
  "fastapi>=0.110",
]
"""
    path = tmp_path / "pyproject.toml"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr("bootstrap_env.REPO_ROOT", tmp_path)


def test_read_pins_extracts_exact_pins(fake_pyproject: None) -> None:
    assert read_pins() == [
        Pin("alembic", "1.20.0"),
        Pin("asyncpg", "0.31.0"),
        Pin("psycopg", "3.3.5"),
    ]


def test_read_pins_strips_extras_marker(fake_pyproject: None) -> None:
    by_name = {pin.name: pin for pin in read_pins()}
    # ``psycopg[binary,pool]==3.3.5`` must normalize to the distribution name.
    assert "psycopg" in by_name
    assert by_name["psycopg"].version == "3.3.5"


def test_read_pins_ignores_range_specifiers(fake_pyproject: None) -> None:
    names = {pin.name for pin in read_pins()}
    assert "fastapi" not in names  # >= is a range specifier, not a pin


def test_resolve_venv_dir_defaults_inside_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_DEV_VENV", raising=False)
    resolved = resolve_venv_dir()
    assert resolved.name == ".venv"  # lives inside the repo tree (persisted path)


def test_resolve_venv_dir_honours_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_DEV_VENV", "/tmp/custom-venv")
    assert resolve_venv_dir() == Path("/tmp/custom-venv")


class _StubMetadata:
    """Stubs importlib.metadata.version for Pin.check()."""

    def __init__(self, versions: dict[str, str]) -> None:
        self._versions = versions

    def version(self, name: str) -> str:
        if name not in self._versions:
            raise md.PackageNotFoundError(name)
        return self._versions[name]


def test_pin_check_reports_missing_then_mismatch_then_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bootstrap_env

    stub = _StubMetadata({})
    monkeypatch.setattr(bootstrap_env.importlib.metadata, "version", stub.version)
    monkeypatch.setattr("bootstrap_env.read_pins", lambda: [Pin("alembic", "1.20.0")])

    # missing
    problem = Pin("alembic", "1.20.0").check()
    assert problem is not None and "not installed" in problem

    # mismatch
    stub._versions["alembic"] = "1.19.0"
    problem = Pin("alembic", "1.20.0").check()
    assert problem is not None and "got 1.19.0" in problem

    # exact
    stub._versions["alembic"] = "1.20.0"
    assert Pin("alembic", "1.20.0").check() is None
