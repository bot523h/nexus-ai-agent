from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_runtime_contract_is_explicit_and_canonical() -> None:
    text = _text("pyproject.toml")
    assert "[tool.nexus.runtime]" in text
    assert 'os = "26.04"' in text
    assert 'python = "3.14"' in text
    assert 'python_patch = "3.14.8"' in text


def test_dockerfile_uses_ubuntu_and_python_314() -> None:
    text = _text("Dockerfile")
    assert "FROM ubuntu:26.04" in text
    assert "python3.14" in text
    assert "python3.14-venv" in text
    assert "build-essential" in text
    assert "cmake" in text
    assert "3.14.8" in text
    assert "ffmpeg" in text
    assert "USER nexus" in text


def test_dockerfile_does_not_run_as_root() -> None:
    text = _text("Dockerfile")
    assert "USER nexus" in text
    assert "python -m pip install --no-cache-dir --no-compile ." in text


def test_ci_exercises_canonical_python_and_runtime_contract() -> None:
    text = _text(".github/workflows/ci.yml")
    assert 'python-version: "3.14"' in text
    assert "runtime-foundation" in text
    assert "scripts/check_runtime.py" in text
    assert "build-essential" in text
    assert "cmake" in text
    assert "python_patch" in _text("pyproject.toml")


def test_bootstrap_and_makefile_expose_one_canonical_path() -> None:
    assert "scripts/bootstrap_env.py" in _text("scripts/bootstrap_dev.sh")
    makefile = _text("Makefile")
    assert "runtime-check:" in makefile
    assert "runtime-receipt:" in makefile
